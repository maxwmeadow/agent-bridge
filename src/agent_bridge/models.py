"""Plain data structures returned by the store.

These are storage-shaped, not display-shaped. Rendering lives in
:mod:`agent_bridge.formatting`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    """Timestamps are ISO-8601 UTC with microseconds, so they sort as text."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    thread_id: str
    reply_to_id: str | None
    sender: str
    recipient: str
    subject: str
    body: str
    created_at: str
    read_at: str | None
    context: dict[str, str] = field(default_factory=dict)

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Message":
        raw = row["metadata_json"]
        return Message(
            id=row["id"],
            thread_id=row["thread_id"],
            reply_to_id=row["reply_to_id"],
            sender=row["sender"],
            recipient=row["recipient"],
            subject=row["subject"],
            body=row["body"],
            created_at=row["created_at"],
            read_at=row["read_at"],
            context=json.loads(raw) if raw else {},
        )


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    id: str
    subject: str
    created_at: str
    updated_at: str
    participants: tuple[str, ...]
    message_count: int
    unread_count: int
    last_sender: str
    last_message_id: str
    last_preview: str


@dataclass(frozen=True, slots=True)
class AgentStatus:
    id: str
    unread: int
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class BridgeStatus:
    version: str
    agent: str
    db_path: str
    schema_version: int
    known_agents: tuple[str, ...]
    agents: tuple[AgentStatus, ...]
    total_messages: int
    total_threads: int
