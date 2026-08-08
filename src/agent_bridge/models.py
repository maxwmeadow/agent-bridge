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
    intent: str = "handoff"
    requires_response: bool = True
    wake_notified_at: str | None = None

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    @property
    def wants_wake(self) -> bool:
        """Whether this message should ring the recipient's doorbell."""
        return self.requires_response and self.wake_notified_at is None

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
            intent=row["intent"],
            requires_response=bool(row["requires_response"]),
            wake_notified_at=row["wake_notified_at"],
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
class UsageSample:
    """How much of a quota window an agent has consumed.

    This is a *metric*. A high percentage is not unavailability: an agent at
    99% of its five-hour window can still do plenty of work. Only a reported
    failure or an explicit status makes an agent unavailable.
    """

    percent: float
    window: str
    resets_at: str | None
    source: str
    sampled_at: str

    @property
    def is_official_source(self) -> bool:
        """Whether the sample came from a documented, supported mechanism."""
        return self.source in ("claude_statusline", "manual")


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """An agent's reported availability, plus what the bridge itself observes.

    ``status`` is always something an agent, its client, or the operator
    reported. ``last_seen_at`` is observed by the bridge. The two are kept
    separate on purpose: silence is not evidence of a usage limit.
    """

    id: str
    unread: int
    last_seen_at: str | None
    # --- availability ---
    status: str = "unknown"
    status_changed_at: str | None = None
    status_reason: str | None = None
    resume_after: str | None = None
    status_source: str = "unknown"
    wait_cancel_seq: int = 0
    # --- failure (what went wrong last, in the client's own vocabulary) ---
    last_failure_kind: str | None = None
    last_failure_detail: str | None = None
    last_failure_at: str | None = None
    # --- usage (a metric, never an availability signal) ---
    usage: "UsageSample | None" = None

    @staticmethod
    def unknown(agent: str) -> "AgentStatus":
        """The record for an agent that has never connected."""
        return AgentStatus(id=agent, unread=0, last_seen_at=None)

    @property
    def is_unavailable(self) -> bool:
        from .config import UNAVAILABLE_STATUSES

        return self.status in UNAVAILABLE_STATUSES

    def seconds_since_seen(self) -> float | None:
        if self.last_seen_at is None:
            return None
        seen = datetime.fromisoformat(self.last_seen_at)
        return (datetime.now(timezone.utc) - seen).total_seconds()


@dataclass(frozen=True, slots=True)
class WaitOutcome:
    """Why a ``wait_for_event`` call returned."""

    reason: str
    waited_seconds: float
    messages: tuple[Message, ...] = ()
    peer: AgentStatus | None = None
    detail: str | None = None


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
