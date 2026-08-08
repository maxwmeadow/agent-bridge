"""Data-layer tests. No MCP transport involved."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_bridge import db
from agent_bridge.errors import (
    NotFoundError,
    PermissionDeniedError,
    UnknownAgentError,
    ValidationError,
)
from agent_bridge.store import MessageStore


def test_send_puts_message_in_recipient_inbox(store: MessageStore) -> None:
    sent = store.send(
        sender="claude", recipient="codex", subject="V1 bridge test", body="Hello from Claude."
    )

    inbox = store.inbox("codex")
    assert [m.id for m in inbox] == [sent.id]
    assert inbox[0].sender == "claude"
    assert inbox[0].body == "Hello from Claude."
    assert inbox[0].is_unread


def test_sender_does_not_see_own_message_in_inbox(store: MessageStore) -> None:
    store.send(sender="claude", recipient="codex", subject="s", body="b")

    assert store.inbox("claude") == []
    assert store.unread_count("claude") == 0
    assert store.unread_count("codex") == 1


def test_unrelated_agent_cannot_see_or_read_message(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_AGENTS", "claude,codex,gemini")
    sent = store.send(sender="claude", recipient="codex", subject="s", body="b")

    assert store.inbox("gemini") == []
    with pytest.raises(PermissionDeniedError):
        store.get_message(sent.id, viewer="gemini")


def test_read_state_transitions(store: MessageStore) -> None:
    sent = store.send(sender="claude", recipient="codex", subject="s", body="b")
    assert sent.read_at is None

    marked = store.mark_read("codex", sent.id)
    assert marked.read_at is not None
    assert store.inbox("codex", unread_only=True) == []
    assert len(store.inbox("codex", unread_only=False)) == 1

    # Marking twice keeps the original timestamp.
    again = store.mark_read("codex", sent.id)
    assert again.read_at == marked.read_at


def test_sender_cannot_mark_own_message_read(store: MessageStore) -> None:
    sent = store.send(sender="claude", recipient="codex", subject="s", body="b")
    with pytest.raises(PermissionDeniedError):
        store.mark_read("claude", sent.id)


def test_reply_preserves_thread_and_flips_direction(store: MessageStore) -> None:
    first = store.send(
        sender="codex", recipient="claude", subject="Perf work ready", body="Commit 8c82f8f"
    )
    answer = store.reply(sender="claude", message_id=first.id, body="Two issues found.")

    assert answer.thread_id == first.thread_id
    assert answer.reply_to_id == first.id
    assert (answer.sender, answer.recipient) == ("claude", "codex")
    assert answer.subject == "Re: Perf work ready"

    # A reply to the reply keeps one "Re:" prefix, not two.
    followup = store.reply(sender="codex", message_id=answer.id, body="Fixed in a1b2c3d")
    assert followup.subject == "Re: Perf work ready"
    assert followup.thread_id == first.thread_id


def test_thread_is_returned_in_chronological_order(store: MessageStore) -> None:
    first = store.send(sender="codex", recipient="claude", subject="Work", body="one")
    second = store.reply(sender="claude", message_id=first.id, body="two")
    third = store.reply(sender="codex", message_id=second.id, body="three")

    subject, messages = store.read_thread(first.thread_id)
    assert subject == "Work"
    assert [m.id for m in messages] == [first.id, second.id, third.id]
    assert [m.body for m in messages] == ["one", "two", "three"]


def test_read_thread_rejects_non_participant(
    store: MessageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_AGENTS", "claude,codex,gemini")
    sent = store.send(sender="claude", recipient="codex", subject="s", body="b")
    with pytest.raises(PermissionDeniedError):
        store.read_thread(sent.thread_id, viewer="gemini")


def test_list_threads_reports_participants_and_unread(store: MessageStore) -> None:
    first = store.send(sender="codex", recipient="claude", subject="Alpha", body="one")
    store.reply(sender="claude", message_id=first.id, body="two")
    store.send(sender="claude", recipient="codex", subject="Beta", body="separate")

    threads = store.list_threads("codex")
    # Most recently active first.
    assert threads[0].subject == "Beta"
    assert threads[0].unread_count == 1
    assert threads[1].subject == "Alpha"
    assert threads[1].message_count == 2
    # Codex sent "one" and received "two", so one unread from its point of view.
    assert threads[1].unread_count == 1
    assert set(threads[1].participants) == {"claude", "codex"}


def test_optional_context_round_trips(store: MessageStore) -> None:
    sent = store.send(
        sender="codex",
        recipient="claude",
        subject="s",
        body="b",
        context={"project": "axiom", "git_commit": "8c82f8f"},
    )
    fetched = store.get_message(sent.id, viewer="claude")
    assert fetched.context == {"project": "axiom", "git_commit": "8c82f8f"}


def test_context_rejects_unknown_keys(store: MessageStore) -> None:
    with pytest.raises(ValidationError, match="Unknown context keys"):
        store.send(
            sender="codex", recipient="claude", subject="s", body="b", context={"secrets": "no"}
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"subject": "", "body": "b"}, "subject must not be empty"),
        ({"subject": "s", "body": "   "}, "body must not be empty"),
        ({"subject": "s" * 500, "body": "b"}, "subject is 500 characters"),
    ],
)
def test_input_validation(store: MessageStore, kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        store.send(sender="claude", recipient="codex", **kwargs)


def test_unknown_recipient_is_rejected(store: MessageStore) -> None:
    with pytest.raises(UnknownAgentError, match="Unknown agent 'gpt'"):
        store.send(sender="claude", recipient="gpt", subject="s", body="b")


def test_agent_cannot_message_itself(store: MessageStore) -> None:
    with pytest.raises(ValidationError, match="cannot send a message to itself"):
        store.send(sender="claude", recipient="claude", subject="s", body="b")


def test_malformed_and_missing_ids(store: MessageStore) -> None:
    with pytest.raises(ValidationError, match="is not a message id"):
        store.get_message("not-an-id")
    with pytest.raises(NotFoundError):
        store.get_message("msg_01K7Q8Z4M0V3TB9YH2C5RD6EWX")
    with pytest.raises(NotFoundError):
        store.read_thread("thr_01K7Q8Z4M0V3TB9YH2C5RD6EWX")


def test_limit_is_validated_and_clamped(store: MessageStore) -> None:
    for index in range(5):
        store.send(sender="claude", recipient="codex", subject=f"s{index}", body="b")

    assert len(store.inbox("codex", limit=2)) == 2
    assert len(store.inbox("codex", limit=1000)) == 5  # clamped, not rejected
    with pytest.raises(ValidationError):
        store.inbox("codex", limit=0)


def test_messages_persist_across_reopen(db_path: Path) -> None:
    first = MessageStore(db_path)
    sent = first.send(sender="claude", recipient="codex", subject="Persisted", body="still here")

    reopened = MessageStore(db_path)
    fetched = reopened.get_message(sent.id, viewer="codex")
    assert fetched.body == "still here"
    assert fetched.subject == "Persisted"
    assert reopened.unread_count("codex") == 1


def test_schema_version_is_recorded_and_migration_is_idempotent(db_path: Path) -> None:
    MessageStore(db_path).unread_count("codex")
    with db.session(db_path) as conn:
        assert db.current_schema_version(conn) == db.SCHEMA_VERSION
        db.migrate(conn)  # no-op second time
        assert db.current_schema_version(conn) == db.SCHEMA_VERSION


def test_concurrent_inserts_all_land(db_path: Path) -> None:
    MessageStore(db_path).record_agent_seen("claude")  # create the schema up front
    count = 20

    def send(index: int) -> str:
        return MessageStore(db_path).send(
            sender="claude", recipient="codex", subject=f"msg {index}", body="body"
        ).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(send, range(count)))

    assert len(set(ids)) == count
    assert MessageStore(db_path).unread_count("codex") == count


def test_status_reports_roster_and_counts(store: MessageStore) -> None:
    store.record_agent_seen("codex")
    store.send(sender="claude", recipient="codex", subject="s", body="b")

    status = store.status("codex")
    assert status.known_agents == ("claude", "codex")
    assert status.total_messages == 1
    assert status.total_threads == 1
    by_id = {agent.id: agent for agent in status.agents}
    assert by_id["codex"].unread == 1
    assert by_id["codex"].last_seen_at is not None
    assert by_id["claude"].last_seen_at is None
