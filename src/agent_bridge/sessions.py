"""Live client sessions, and choosing which one to wake.

A session row exists because a client's own lifecycle hook said so. The bridge
never guesses that a session exists, and never invents one from message
traffic. If nothing registered, there is nothing to wake, and the mail simply
waits in SQLite as it always did.

Target selection, in order:

1. A live session for that agent whose ``project`` matches the message's
   project, if the message carries one.
2. Otherwise the most recently active live session for that agent.

"Live" means registered, not closed, and seen within
:data:`~agent_bridge.config.SESSION_STALE_SECONDS`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import (
    SESSION_STALE_SECONDS,
    SESSION_STATES,
    WAKE_METHODS,
    require_known_agent,
)
from .errors import NotFoundError, ValidationError
from .models import utc_now

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClientSession:
    """One registered client session that may be a wake target."""

    id: str
    agent: str
    client_type: str
    provider: str | None
    project: str | None
    registered_at: str
    last_seen_at: str
    state: str
    wake_method: str
    wake_generation: int
    auto_wakes: int
    metadata: dict[str, str]

    @property
    def can_wake(self) -> bool:
        """Whether this session has a usable idle-wake mechanism."""
        return self.wake_method != "none" and self.state in ("idle", "active", "waiting")

    def is_stale(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        seen = datetime.fromisoformat(self.last_seen_at)
        return (moment - seen).total_seconds() > SESSION_STALE_SECONDS

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ClientSession":
        raw = row["metadata_json"]
        return ClientSession(
            id=row["id"],
            agent=row["agent"],
            client_type=row["client_type"],
            provider=row["provider"],
            project=row["project"],
            registered_at=row["registered_at"],
            last_seen_at=row["last_seen_at"],
            state=row["state"],
            wake_method=row["wake_method"],
            wake_generation=int(row["wake_generation"]),
            auto_wakes=int(row["auto_wakes"]),
            metadata=json.loads(raw) if raw else {},
        )


def _normalize_project(project: str | None) -> str | None:
    """Projects are compared as normalized paths, case-insensitively.

    Windows hands the same directory back in several spellings, and a target
    that fails to match because of a drive-letter case is a silent bug.
    """
    if not project:
        return None
    text = project.strip()
    if not text:
        return None
    try:
        return str(Path(text).resolve()).rstrip("\\/").casefold()
    except (OSError, ValueError):
        return text.rstrip("\\/").casefold()


class SessionRegistry:
    """Registration and lookup of live client sessions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------ writing

    def register(
        self,
        *,
        session_id: str,
        agent: str,
        client_type: str,
        provider: str | None = None,
        project: str | None = None,
        wake_method: str = "stop_hook_rewake",
        metadata: dict[str, str] | None = None,
    ) -> ClientSession:
        """Record a session, or refresh one that already exists."""
        agent = require_known_agent(agent)
        session_id = session_id.strip()
        if not session_id:
            raise ValidationError("session_id must not be empty.")
        if wake_method not in WAKE_METHODS:
            raise ValidationError(
                f"Unknown wake method {wake_method!r}. Valid: {', '.join(WAKE_METHODS)}."
            )

        now = utc_now()
        with db.session(self.db_path) as conn, conn:
            conn.execute(
                """
                INSERT INTO client_sessions (
                    id, agent, client_type, provider, project,
                    registered_at, last_seen_at, state, wake_method,
                    wake_generation, auto_wakes, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'idle', ?, 0, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    agent        = excluded.agent,
                    client_type  = excluded.client_type,
                    provider     = excluded.provider,
                    project      = excluded.project,
                    last_seen_at = excluded.last_seen_at,
                    state        = 'idle',
                    wake_method  = excluded.wake_method
                """,
                (
                    session_id,
                    agent,
                    client_type,
                    provider,
                    _normalize_project(project),
                    now,
                    now,
                    wake_method,
                    json.dumps(metadata) if metadata else None,
                ),
            )
        log.info(
            "session registered id=%s agent=%s client=%s project=%s wake=%s",
            session_id,
            agent,
            client_type,
            _normalize_project(project) or "-",
            wake_method,
        )
        return self.get(session_id)

    def touch(
        self, session_id: str, *, state: str | None = None, reset_auto_wakes: bool = False
    ) -> ClientSession | None:
        """Refresh liveness, optionally moving state or clearing the wake budget."""
        if state is not None and state not in SESSION_STATES:
            raise ValidationError(
                f"Unknown session state {state!r}. Valid: {', '.join(SESSION_STATES)}."
            )
        with db.session(self.db_path) as conn, conn:
            row = conn.execute(
                "SELECT id FROM client_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                f"""
                UPDATE client_sessions
                SET last_seen_at = ?
                    {", state = ?" if state else ""}
                    {", auto_wakes = 0" if reset_auto_wakes else ""}
                WHERE id = ?
                """,
                (utc_now(), state, session_id) if state else (utc_now(), session_id),
            )
        return self.get(session_id)

    def next_wake_generation(self, session_id: str) -> int:
        """Claim the doorbell for this session.

        Each armed waiter takes a generation number. When a newer turn arms a
        newer waiter, the older one sees a higher generation and retires
        instead of ringing a second time.
        """
        with db.session(self.db_path) as conn, conn:
            conn.execute(
                "UPDATE client_sessions SET wake_generation = wake_generation + 1 WHERE id = ?",
                (session_id,),
            )
            row = conn.execute(
                "SELECT wake_generation FROM client_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"No registered session {session_id}.")
        return int(row[0])

    def current_wake_generation(self, session_id: str) -> int | None:
        with db.session(self.db_path) as conn:
            row = conn.execute(
                "SELECT wake_generation FROM client_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def record_auto_wake(self, session_id: str) -> int:
        """Count one automatic wake against this session's budget."""
        with db.session(self.db_path) as conn, conn:
            conn.execute(
                """
                UPDATE client_sessions
                SET auto_wakes = auto_wakes + 1, last_seen_at = ?, state = 'active'
                WHERE id = ?
                """,
                (utc_now(), session_id),
            )
            row = conn.execute(
                "SELECT auto_wakes FROM client_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self, session_id: str) -> None:
        with db.session(self.db_path) as conn, conn:
            conn.execute(
                "UPDATE client_sessions SET state = 'closed', last_seen_at = ? WHERE id = ?",
                (utc_now(), session_id),
            )
        log.info("session closed id=%s", session_id)

    def prune(self, *, older_than_seconds: int | None = None) -> int:
        """Delete closed sessions and ones long past staleness."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=older_than_seconds
            if older_than_seconds is not None
            else SESSION_STALE_SECONDS * 2
        )
        with db.session(self.db_path) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM client_sessions WHERE state = 'closed' OR last_seen_at < ?",
                (cutoff.isoformat(timespec="microseconds"),),
            )
            removed = cursor.rowcount
        if removed:
            log.info("pruned %d stale session(s)", removed)
        return int(removed)

    # ------------------------------------------------------------ reading

    def get(self, session_id: str) -> ClientSession:
        with db.session(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM client_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"No registered session {session_id}.")
        return ClientSession.from_row(row)

    def list_all(self, agent: str | None = None) -> list[ClientSession]:
        query = "SELECT * FROM client_sessions"
        params: list[object] = []
        if agent is not None:
            query += " WHERE agent = ?"
            params.append(require_known_agent(agent))
        query += " ORDER BY last_seen_at DESC"
        with db.session(self.db_path) as conn:
            return [ClientSession.from_row(row) for row in conn.execute(query, params)]

    def live(self, agent: str) -> list[ClientSession]:
        """Sessions for an agent that are registered, open, and not stale."""
        now = datetime.now(timezone.utc)
        return [
            session
            for session in self.list_all(agent)
            if session.state != "closed" and not session.is_stale(now)
        ]

    def select_target(self, agent: str, *, project: str | None = None) -> ClientSession | None:
        """Pick the session to wake for this agent.

        Same project wins; otherwise the most recently active live session.
        Returns ``None`` when the agent has no live wakeable session, which is
        a normal outcome, not an error -- the mail still waits in the mailbox.
        """
        candidates = [session for session in self.live(agent) if session.can_wake]
        if not candidates:
            return None

        wanted = _normalize_project(project)
        if wanted is not None:
            same_project = [s for s in candidates if s.project == wanted]
            if same_project:
                # live() is already ordered by last_seen_at DESC.
                return same_project[0]
        return candidates[0]
