"""Availability records: what is reported, what is observed, and what is not."""

from __future__ import annotations

import pytest

from agent_bridge.config import AGENT_STATUSES
from agent_bridge.errors import UnknownAgentError, ValidationError
from agent_bridge.store import MessageStore


def test_unknown_until_reported(store: MessageStore) -> None:
    record = store.get_status("codex")
    assert record.status == "unknown"
    assert record.status_changed_at is None
    assert record.last_seen_at is None


@pytest.mark.parametrize("status", AGENT_STATUSES)
def test_every_documented_status_round_trips(store: MessageStore, status: str) -> None:
    record = store.set_status("codex", status, reason=f"testing {status}")
    assert record.status == status
    assert record.status_changed_at is not None
    assert record.status_reason == f"testing {status}"
    assert store.get_status("codex").status == status


def test_invalid_status_value_is_rejected(store: MessageStore) -> None:
    with pytest.raises(ValidationError, match="Unknown status 'exhausted'"):
        store.set_status("codex", "exhausted")


def test_status_for_unknown_agent_is_rejected(store: MessageStore) -> None:
    with pytest.raises(UnknownAgentError):
        store.set_status("gpt", "available")


def test_any_transition_is_allowed_by_design(store: MessageStore) -> None:
    """No transition graph is enforced; a client can die in any state.

    Rejecting a "wrong" transition would preserve a stale record over a fresh
    one, which is worse than allowing the jump.
    """
    for status in ("available", "usage_exhausted", "busy", "client_closed", "available"):
        assert store.set_status("codex", status).status == status


def test_resume_after_is_validated_and_normalized(store: MessageStore) -> None:
    record = store.set_status(
        "codex", "usage_exhausted", resume_after="2026-08-08T12:00:00+02:00"
    )
    assert record.resume_after is not None
    # Normalized to UTC.
    assert record.resume_after.startswith("2026-08-08T10:00:00")

    # A naive timestamp is treated as UTC rather than rejected.
    record = store.set_status("codex", "usage_exhausted", resume_after="2026-08-08T12:00:00")
    assert record.resume_after is not None and record.resume_after.startswith("2026-08-08T12:00:00")

    with pytest.raises(ValidationError, match="ISO-8601"):
        store.set_status("codex", "usage_exhausted", resume_after="tomorrow-ish")


def test_status_source_is_recorded(store: MessageStore) -> None:
    assert store.set_status("codex", "available", source="self").status_source == "self"
    assert store.set_status("codex", "unresponsive", source="cli").status_source == "cli"
    with pytest.raises(ValidationError, match="Unknown status source"):
        store.set_status("codex", "available", source="guesswork")


def test_seeing_an_agent_never_changes_its_reported_status(store: MessageStore) -> None:
    """Liveness and availability are separate facts."""
    store.set_status("codex", "usage_exhausted", reason="weekly cap")
    store.record_agent_seen("codex")

    record = store.get_status("codex")
    assert record.status == "usage_exhausted"  # unchanged by the connection
    assert record.last_seen_at is not None  # but liveness was updated


def test_silence_is_never_promoted_to_usage_exhausted(store: MessageStore) -> None:
    """The bridge has no code path that infers a limit from inactivity."""
    store.set_status("codex", "available")
    # Pretend a long time has passed by rewriting last_seen_at directly.
    from agent_bridge import db

    with db.session(store.db_path) as conn, conn:
        conn.execute(
            "UPDATE agents SET last_seen_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000000+00:00", "codex"),
        )

    record = store.get_status("codex")
    assert record.status == "available"
    assert not record.is_unavailable
    idle = record.seconds_since_seen()
    assert idle is not None and idle > 60 * 60 * 24  # observably stale...
    assert record.status == "available"  # ...but the reported status stands


def test_cancel_sequence_increments(store: MessageStore) -> None:
    assert store.cancel_waits("claude") == 1
    assert store.cancel_waits("claude") == 2
    assert store.get_status("claude").wait_cancel_seq == 2
    # Independent per agent.
    assert store.cancel_waits("codex") == 1
