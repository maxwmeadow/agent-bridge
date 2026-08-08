"""SQLite connection handling and schema migrations.

One short-lived connection per operation. That keeps the store free of
threading assumptions and lets WAL mode do the concurrency work: Claude and
Codex are separate OS processes writing the same file.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE agents (
            id            TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE threads (
            id         TEXT PRIMARY KEY,
            subject    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE messages (
            id            TEXT PRIMARY KEY,
            thread_id     TEXT NOT NULL REFERENCES threads(id),
            reply_to_id   TEXT REFERENCES messages(id),
            sender        TEXT NOT NULL,
            recipient     TEXT NOT NULL,
            subject       TEXT NOT NULL,
            body          TEXT NOT NULL,
            metadata_json TEXT,
            created_at    TEXT NOT NULL,
            read_at       TEXT
        )
        """,
        "CREATE INDEX idx_messages_inbox ON messages(recipient, read_at, created_at)",
        "CREATE INDEX idx_messages_thread ON messages(thread_id, created_at)",
        "CREATE INDEX idx_threads_updated ON threads(updated_at DESC)",
    ),
    2: (
        # Availability records. Reported by agents, never inferred from silence.
        "ALTER TABLE agents ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown'",
        "ALTER TABLE agents ADD COLUMN status_changed_at TEXT",
        "ALTER TABLE agents ADD COLUMN status_reason TEXT",
        "ALTER TABLE agents ADD COLUMN resume_after TEXT",
        # Who reported it: 'self' (the agent), 'cli' (the operator), 'peer'.
        "ALTER TABLE agents ADD COLUMN status_source TEXT NOT NULL DEFAULT 'unknown'",
        # Bumped to cancel this agent's in-flight waits. Waiters compare the
        # value they started with, so a cancel can never be missed.
        "ALTER TABLE agents ADD COLUMN wait_cancel_seq INTEGER NOT NULL DEFAULT 0",
    ),
    3: (
        # Three separate concerns, deliberately not merged into one column:
        #
        #   availability -- can this agent make progress right now
        #   failure      -- what went wrong last, in the client's own vocabulary
        #   usage        -- how much quota has been consumed, which is a metric
        #                   and NOT an availability signal
        "ALTER TABLE agents ADD COLUMN last_failure_kind TEXT",
        "ALTER TABLE agents ADD COLUMN last_failure_detail TEXT",
        "ALTER TABLE agents ADD COLUMN last_failure_at TEXT",
        "ALTER TABLE agents ADD COLUMN usage_percent REAL",
        "ALTER TABLE agents ADD COLUMN usage_window TEXT",
        "ALTER TABLE agents ADD COLUMN usage_resets_at TEXT",
        "ALTER TABLE agents ADD COLUMN usage_source TEXT",
        "ALTER TABLE agents ADD COLUMN usage_sampled_at TEXT",
    ),
    4: (
        # A live client session that can be woken. Registered by the client's
        # own lifecycle hooks, not guessed at.
        """
        CREATE TABLE client_sessions (
            id              TEXT PRIMARY KEY,
            agent           TEXT NOT NULL,
            client_type     TEXT NOT NULL,
            provider        TEXT,
            project         TEXT,
            registered_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            state           TEXT NOT NULL,
            wake_method     TEXT NOT NULL,
            wake_generation INTEGER NOT NULL DEFAULT 0,
            auto_wakes      INTEGER NOT NULL DEFAULT 0,
            metadata_json   TEXT
        )
        """,
        "CREATE INDEX idx_sessions_agent ON client_sessions(agent, last_seen_at DESC)",
        # Message semantics. Defaults keep every pre-existing row meaningful:
        # a V1 message reads as a handoff that wants an answer.
        "ALTER TABLE messages ADD COLUMN intent TEXT NOT NULL DEFAULT 'handoff'",
        "ALTER TABLE messages ADD COLUMN requires_response INTEGER NOT NULL DEFAULT 1",
        # Set once a wake for this message has actually been delivered, so a
        # restart cannot re-ring the doorbell for mail already announced.
        "ALTER TABLE messages ADD COLUMN wake_notified_at TEXT",
        # Held while an agent is blocked inside wait_for_event. Self-expiring,
        # so a crashed waiter cannot suppress wakes forever.
        "ALTER TABLE agents ADD COLUMN wait_lease_until TEXT",
    ),
    5: (
        # Claude Code delivers an asyncRewake through the same path as a typed
        # prompt, so UserPromptSubmit fires for our own injection. Without a
        # timestamp to compare against, every automatic wake would reset the
        # consecutive-wake budget and the loop brake would never engage.
        "ALTER TABLE client_sessions ADD COLUMN last_auto_wake_at TEXT",
    ),
}


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # WAL lets one writer and many readers coexist across processes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait rather than fail if the other agent happens to be writing.
    conn.execute("PRAGMA busy_timeout=5000")


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the database up to :data:`SCHEMA_VERSION`."""
    version = current_schema_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than this build supports "
            f"({SCHEMA_VERSION}). Upgrade agent-bridge."
        )
    while version < SCHEMA_VERSION:
        target = version + 1
        statements = _MIGRATIONS[target]
        with conn:
            for statement in statements:
                conn.execute(statement)
            # PRAGMA does not accept bound parameters; target is an int literal.
            conn.execute(f"PRAGMA user_version={target:d}")
        log.info("applied schema migration to version %d", target)
        version = target


def connect(db_path: Path, *, migrate_if_needed: bool = True) -> sqlite3.Connection:
    """Open a configured connection to the bridge database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    if migrate_if_needed:
        migrate(conn)
    return conn


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection for one unit of work and always close it."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
