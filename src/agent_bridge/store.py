"""All message operations. This is the layer the MCP server and CLI share.

Nothing here knows about MCP. Nothing here executes message content: bodies
are stored and returned verbatim as data.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from . import __version__, db, ids
from .config import (
    MAX_BODY_CHARS,
    MAX_CONTEXT_VALUE_CHARS,
    MAX_SUBJECT_CHARS,
    known_agents,
    require_known_agent,
)
from .errors import NotFoundError, PermissionDeniedError, ValidationError
from .models import AgentStatus, BridgeStatus, Message, ThreadSummary, utc_now

log = logging.getLogger(__name__)

#: Optional per-message context. Callers supply these; the bridge never
#: inspects a git repository on its own to fill them in.
CONTEXT_KEYS: frozenset[str] = frozenset(
    {"project", "working_directory", "git_branch", "git_commit"}
)

PREVIEW_CHARS = 160


def _clean_text(value: str, *, field: str, limit: int, allow_empty: bool = False) -> str:
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ValidationError(f"{field} must not be empty.")
    if len(cleaned) > limit:
        raise ValidationError(f"{field} is {len(cleaned)} characters; the limit is {limit}.")
    return cleaned


def _clean_context(context: dict[str, str] | None) -> dict[str, str]:
    if not context:
        return {}
    unknown = sorted(set(context) - CONTEXT_KEYS)
    if unknown:
        raise ValidationError(
            f"Unknown context keys: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(CONTEXT_KEYS))}."
        )
    cleaned: dict[str, str] = {}
    for key, value in context.items():
        text = value.strip() if value else ""
        if not text:
            continue
        if len(text) > MAX_CONTEXT_VALUE_CHARS:
            raise ValidationError(
                f"context.{key} is {len(text)} characters; the limit is "
                f"{MAX_CONTEXT_VALUE_CHARS}."
            )
        cleaned[key] = text
    return cleaned


def preview(body: str, limit: int = PREVIEW_CHARS) -> str:
    """One-line excerpt of a body, for inbox and thread listings."""
    collapsed = " ".join(body.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


class MessageStore:
    """Message operations against one bridge database."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ---------------------------------------------------------------- agents

    def record_agent_seen(self, agent: str) -> None:
        """Note that an agent connected. Used by ``bridge_status`` diagnostics."""
        agent = require_known_agent(agent)
        now = utc_now()
        with db.session(self.db_path) as conn, conn:
            conn.execute(
                """
                INSERT INTO agents (id, first_seen_at, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at
                """,
                (agent, now, now),
            )

    # --------------------------------------------------------------- sending

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        context: dict[str, str] | None = None,
        thread_id: str | None = None,
        reply_to_id: str | None = None,
    ) -> Message:
        """Insert a message, creating a thread when this starts a new one."""
        sender = require_known_agent(sender)
        recipient = require_known_agent(recipient)
        if sender == recipient:
            raise ValidationError(
                f"{sender} cannot send a message to itself. "
                f"Known agents: {', '.join(known_agents())}."
            )
        subject = _clean_text(subject, field="subject", limit=MAX_SUBJECT_CHARS)
        body = _clean_text(body, field="body", limit=MAX_BODY_CHARS)
        metadata = _clean_context(context)

        now = utc_now()
        message_id = ids.new_message_id()

        with db.session(self.db_path) as conn, conn:
            if thread_id is None:
                thread_id = ids.new_thread_id()
                conn.execute(
                    "INSERT INTO threads (id, subject, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (thread_id, subject, now, now),
                )
            else:
                conn.execute(
                    "UPDATE threads SET updated_at = ? WHERE id = ?", (now, thread_id)
                )
            conn.execute(
                """
                INSERT INTO messages (
                    id, thread_id, reply_to_id, sender, recipient,
                    subject, body, metadata_json, created_at, read_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    message_id,
                    thread_id,
                    reply_to_id,
                    sender,
                    recipient,
                    subject,
                    body,
                    json.dumps(metadata) if metadata else None,
                    now,
                ),
            )

        log.info(
            "message sent id=%s from=%s to=%s thread=%s body_chars=%d",
            message_id,
            sender,
            recipient,
            thread_id,
            len(body),
        )
        return Message(
            id=message_id,
            thread_id=thread_id,
            reply_to_id=reply_to_id,
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            created_at=now,
            read_at=None,
            context=metadata,
        )

    def reply(
        self,
        *,
        sender: str,
        message_id: str,
        body: str,
        context: dict[str, str] | None = None,
    ) -> Message:
        """Reply to a message, keeping the thread and flipping the direction."""
        sender = require_known_agent(sender)
        original = self.get_message(message_id, viewer=sender)
        if original.sender == sender:
            recipient = original.recipient
        else:
            recipient = original.sender

        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"[:MAX_SUBJECT_CHARS]

        return self.send(
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            context=context,
            thread_id=original.thread_id,
            reply_to_id=original.id,
        )

    # --------------------------------------------------------------- reading

    def get_message(self, message_id: str, *, viewer: str | None = None) -> Message:
        """Fetch one message. ``viewer`` restricts access to its participants."""
        if not ids.looks_like_id(message_id, ids.MESSAGE_PREFIX):
            raise ValidationError(
                f"{message_id!r} is not a message id. Message ids look like "
                f"'{ids.MESSAGE_PREFIX}01K7Q8Z4M0V3TB9YH2C5RD6EWX'."
            )
        with db.session(self.db_path) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"No message with id {message_id}.")
        message = Message.from_row(row)
        if viewer is not None and viewer not in (message.sender, message.recipient):
            raise PermissionDeniedError(
                f"Message {message_id} is between {message.sender} and "
                f"{message.recipient}; {viewer} is not a participant."
            )
        return message

    def inbox(
        self, agent: str, *, unread_only: bool = True, limit: int = 20
    ) -> list[Message]:
        """Messages addressed to ``agent``, newest first."""
        agent = require_known_agent(agent)
        limit = _clean_limit(limit)
        query = "SELECT * FROM messages WHERE recipient = ?"
        params: list[object] = [agent]
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with db.session(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Message.from_row(row) for row in rows]

    def unread_count(self, agent: str) -> int:
        with db.session(self.db_path) as conn:
            return _count_unread(conn, agent)

    def mark_read(self, agent: str, message_id: str) -> Message:
        """Mark a message the agent received as read. Idempotent."""
        agent = require_known_agent(agent)
        message = self.get_message(message_id, viewer=agent)
        if message.recipient != agent:
            raise PermissionDeniedError(
                f"{agent} sent message {message_id} and cannot mark it read; "
                f"only {message.recipient} can."
            )
        if message.read_at is not None:
            return message
        now = utc_now()
        with db.session(self.db_path) as conn, conn:
            conn.execute(
                "UPDATE messages SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (now, message_id),
            )
        log.info("message marked read id=%s agent=%s", message_id, agent)
        return self.get_message(message_id, viewer=agent)

    # --------------------------------------------------------------- threads

    def list_threads(self, agent: str | None = None, *, limit: int = 10) -> list[ThreadSummary]:
        """Recent threads, most recently active first.

        ``agent`` restricts the list to threads that agent takes part in and
        counts unread from that agent's point of view.
        """
        if agent is not None:
            agent = require_known_agent(agent)
        limit = _clean_limit(limit, maximum=100)

        query = """
            SELECT t.id, t.subject, t.created_at, t.updated_at
            FROM threads t
            {where}
            ORDER BY t.updated_at DESC, t.rowid DESC
            LIMIT ?
        """
        params: list[object] = []
        if agent is None:
            where = ""
        else:
            where = """
                WHERE EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.thread_id = t.id AND (m.sender = ? OR m.recipient = ?)
                )
            """
            params.extend([agent, agent])
        params.append(limit)

        summaries: list[ThreadSummary] = []
        with db.session(self.db_path) as conn:
            thread_rows = conn.execute(query.format(where=where), params).fetchall()
            for thread in thread_rows:
                messages = conn.execute(
                    "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC, rowid ASC",
                    (thread["id"],),
                ).fetchall()
                if not messages:  # pragma: no cover - threads always have messages
                    continue
                participants: list[str] = []
                unread = 0
                for row in messages:
                    for who in (row["sender"], row["recipient"]):
                        if who not in participants:
                            participants.append(who)
                    if row["read_at"] is None and (agent is None or row["recipient"] == agent):
                        unread += 1
                last = messages[-1]
                summaries.append(
                    ThreadSummary(
                        id=thread["id"],
                        subject=thread["subject"],
                        created_at=thread["created_at"],
                        updated_at=thread["updated_at"],
                        participants=tuple(participants),
                        message_count=len(messages),
                        unread_count=unread,
                        last_sender=last["sender"],
                        last_message_id=last["id"],
                        last_preview=preview(last["body"]),
                    )
                )
        return summaries

    def read_thread(
        self, thread_id: str, *, viewer: str | None = None, limit: int = 50
    ) -> tuple[str, list[Message]]:
        """Return a thread's subject and its messages, oldest first."""
        if not ids.looks_like_id(thread_id, ids.THREAD_PREFIX):
            raise ValidationError(
                f"{thread_id!r} is not a thread id. Thread ids look like "
                f"'{ids.THREAD_PREFIX}01K7Q8Z4M0V3TB9YH2C5RD6EWX'."
            )
        limit = _clean_limit(limit, maximum=500)
        with db.session(self.db_path) as conn:
            thread = conn.execute(
                "SELECT subject FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if thread is None:
                raise NotFoundError(f"No thread with id {thread_id}.")
            rows = conn.execute(
                """
                SELECT * FROM messages WHERE thread_id = ?
                ORDER BY created_at ASC, rowid ASC LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        messages = [Message.from_row(row) for row in rows]
        if viewer is not None and not any(
            viewer in (m.sender, m.recipient) for m in messages
        ):
            raise PermissionDeniedError(f"{viewer} is not a participant in thread {thread_id}.")
        return thread["subject"], messages

    # ---------------------------------------------------------------- status

    def status(self, agent: str) -> BridgeStatus:
        agent = require_known_agent(agent)
        roster = known_agents()
        with db.session(self.db_path) as conn:
            schema_version = db.current_schema_version(conn)
            total_messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            total_threads = int(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
            seen = {
                row["id"]: row["last_seen_at"]
                for row in conn.execute("SELECT id, last_seen_at FROM agents").fetchall()
            }
            agents = tuple(
                AgentStatus(id=name, unread=_count_unread(conn, name), last_seen_at=seen.get(name))
                for name in roster
            )
        return BridgeStatus(
            version=__version__,
            agent=agent,
            db_path=str(self.db_path),
            schema_version=schema_version,
            known_agents=roster,
            agents=agents,
            total_messages=total_messages,
            total_threads=total_threads,
        )


def _count_unread(conn: sqlite3.Connection, agent: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE recipient = ? AND read_at IS NULL", (agent,)
    ).fetchone()
    return int(row[0])


def _clean_limit(limit: int, *, maximum: int = 100) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValidationError("limit must be an integer.")
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    return min(limit, maximum)
