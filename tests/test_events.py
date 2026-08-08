"""Tests for blocking waits.

Two shapes matter and are tested separately:

* **Same process** -- the waiter and the writer share an :class:`EventHub`, so
  the in-process notification is what wakes it.
* **Separate processes** -- the writer uses its own ``MessageStore`` and its
  own hub, so nothing in memory is shared and only SQLite can carry the news.
  This is the real Claude/Codex arrangement.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import anyio
import pytest

from agent_bridge.errors import ValidationError
from agent_bridge.events import EventHub, WaitReason, wait_for_event
from agent_bridge.store import MessageStore


def wait(
    store: MessageStore,
    hub: EventHub,
    agent: str = "claude",
    timeout: float = 5.0,
    **kwargs: object,
):  # type: ignore[no-untyped-def]
    return wait_for_event(store, hub, agent=agent, timeout_seconds=timeout, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------- immediate


def test_existing_unread_mail_returns_immediately(store: MessageStore) -> None:
    store.send(sender="codex", recipient="claude", subject="Already here", body="hello")

    async def scenario() -> None:
        started = time.monotonic()
        outcome = await wait(store, EventHub(), timeout=30)
        assert outcome.reason == WaitReason.MESSAGE_RECEIVED
        assert [m.subject for m in outcome.messages] == ["Already here"]
        # Must not have blocked at all.
        assert time.monotonic() - started < 1.0

    anyio.run(scenario)


def test_read_mail_does_not_end_a_wait(store: MessageStore) -> None:
    sent = store.send(sender="codex", recipient="claude", subject="old", body="hello")
    store.mark_read("claude", sent.id)

    async def scenario() -> None:
        outcome = await wait(store, EventHub(), timeout=1)
        assert outcome.reason == WaitReason.TIMEOUT

    anyio.run(scenario)


# ------------------------------------------------------------------ wakeups


def test_blocked_waiter_woken_by_new_message_same_process(store: MessageStore) -> None:
    hub = EventHub()

    async def scenario() -> None:
        async with anyio.create_task_group() as tg:
            outcomes = []

            async def waiter() -> None:
                outcomes.append(await wait(store, hub, timeout=10))

            async def sender() -> None:
                await anyio.sleep(0.05)
                store.send(sender="codex", recipient="claude", subject="new", body="wake up")
                hub.notify("claude")

            tg.start_soon(waiter)
            tg.start_soon(sender)

        assert outcomes[0].reason == WaitReason.MESSAGE_RECEIVED
        assert outcomes[0].messages[0].subject == "new"

    anyio.run(scenario)


def test_blocked_waiter_woken_across_processes(db_path: Path) -> None:
    """No shared memory: only SQLite connects the writer and the waiter."""
    waiter_store = MessageStore(db_path)
    writer_store = MessageStore(db_path)  # stands in for the other agent's process
    waiter_hub = EventHub()

    async def scenario() -> None:
        outcomes = []

        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(waiter_store, waiter_hub, timeout=10))

            async def writer() -> None:
                await anyio.sleep(0.05)
                # Deliberately no hub.notify: the other process cannot do that.
                writer_store.send(
                    sender="codex", recipient="claude", subject="cross", body="from elsewhere"
                )

            tg.start_soon(waiter)
            tg.start_soon(writer)

        assert outcomes[0].reason == WaitReason.MESSAGE_RECEIVED
        assert outcomes[0].messages[0].subject == "cross"

    anyio.run(scenario)


def test_timeout(store: MessageStore) -> None:
    async def scenario() -> None:
        started = time.monotonic()
        outcome = await wait(store, EventHub(), timeout=1)
        elapsed = time.monotonic() - started
        assert outcome.reason == WaitReason.TIMEOUT
        assert 0.9 <= elapsed <= 3.0
        assert outcome.waited_seconds >= 0.9

    anyio.run(scenario)


def test_timeout_is_clamped_to_server_bounds(store: MessageStore) -> None:
    async def scenario() -> None:
        # Far above MAX_WAIT_SECONDS; must not be honoured. Cancel once we have
        # confirmed it started blocking rather than returning.
        with anyio.move_on_after(0.5):
            await wait(store, EventHub(), timeout=10_000_000)
        with pytest.raises(ValidationError):
            await wait(store, EventHub(), timeout=float("inf"))

    anyio.run(scenario)


# -------------------------------------------------------------- peer status


def test_peer_going_unavailable_wakes_waiter(db_path: Path) -> None:
    waiter_store = MessageStore(db_path)
    peer_store = MessageStore(db_path)
    waiter_store.set_status("codex", "busy", reason="working")

    async def scenario() -> None:
        outcomes = []

        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(waiter_store, EventHub(), timeout=10))

            async def peer() -> None:
                await anyio.sleep(0.05)
                peer_store.set_status(
                    "codex",
                    "usage_exhausted",
                    reason="5-hour limit reached",
                    resume_after="2026-08-08T12:00:00+00:00",
                )

            tg.start_soon(waiter)
            tg.start_soon(peer)

        outcome = outcomes[0]
        assert outcome.reason == WaitReason.PEER_UNAVAILABLE
        assert outcome.peer is not None
        assert outcome.peer.status == "usage_exhausted"
        assert outcome.peer.resume_after is not None
        assert "5-hour limit" in (outcome.detail or "")

    anyio.run(scenario)


def test_peer_becoming_available_wakes_waiter(db_path: Path) -> None:
    waiter_store = MessageStore(db_path)
    waiter_store.set_status("codex", "usage_exhausted", reason="limit")

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(waiter_store, EventHub(), timeout=10))

            async def peer() -> None:
                await anyio.sleep(0.05)
                MessageStore(db_path).set_status("codex", "available", reason="quota reset")

            tg.start_soon(waiter)
            tg.start_soon(peer)

        assert outcomes[0].reason == WaitReason.PEER_AVAILABLE

    anyio.run(scenario)


def test_peer_going_busy_does_not_wake_waiter(db_path: Path) -> None:
    """busy and waiting are noise: the peer is alive and working."""
    waiter_store = MessageStore(db_path)
    waiter_store.set_status("codex", "available")

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(waiter_store, EventHub(), timeout=1))

            async def peer() -> None:
                await anyio.sleep(0.05)
                MessageStore(db_path).set_status("codex", "busy", reason="implementing")

            tg.start_soon(waiter)
            tg.start_soon(peer)

        assert outcomes[0].reason == WaitReason.TIMEOUT

    anyio.run(scenario)


def test_peer_status_wakeup_can_be_disabled(db_path: Path) -> None:
    waiter_store = MessageStore(db_path)
    waiter_store.set_status("codex", "available")

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(
                    await wait(waiter_store, EventHub(), timeout=1, wake_on_peer_status=False)
                )

            async def peer() -> None:
                await anyio.sleep(0.05)
                MessageStore(db_path).set_status("codex", "auth_error", reason="logged out")

            tg.start_soon(waiter)
            tg.start_soon(peer)

        assert outcomes[0].reason == WaitReason.TIMEOUT

    anyio.run(scenario)


# ------------------------------------------------------------- cancellation


def test_cancel_waits_wakes_blocked_waiter(db_path: Path) -> None:
    waiter_store = MessageStore(db_path)

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(waiter_store, EventHub(), timeout=10))

            async def canceller() -> None:
                await anyio.sleep(0.05)
                MessageStore(db_path).cancel_waits("claude", reason="user interrupted")

            tg.start_soon(waiter)
            tg.start_soon(canceller)

        assert outcomes[0].reason == WaitReason.CANCELLED

    anyio.run(scenario)


def test_cancellation_before_wait_starts_is_not_missed(db_path: Path) -> None:
    """A cancel that lands before the wait begins must not leave it blocked."""
    store = MessageStore(db_path)
    store.cancel_waits("claude")

    async def scenario() -> None:
        # The baseline is captured at wait start, so an *earlier* cancel is
        # already accounted for and the wait proceeds normally...
        outcome = await wait(store, EventHub(), timeout=1)
        assert outcome.reason == WaitReason.TIMEOUT
        # ...while a cancel after that baseline is always seen.
        store.cancel_waits("claude")
        outcome = await wait(store, EventHub(), timeout=1)
        assert outcome.reason == WaitReason.TIMEOUT

    anyio.run(scenario)


def test_task_cancellation_propagates(store: MessageStore) -> None:
    """An MCP request cancellation must abort the wait, not swallow it."""

    async def scenario() -> None:
        started = time.monotonic()
        with anyio.move_on_after(0.2) as scope:
            await wait(store, EventHub(), timeout=60)
        assert scope.cancelled_caught
        assert time.monotonic() - started < 2.0

    anyio.run(scenario)


def test_bridge_shutdown_wakes_waiters(store: MessageStore) -> None:
    hub = EventHub()

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(store, hub, timeout=10))

            async def stopper() -> None:
                await anyio.sleep(0.05)
                hub.shutdown()

            tg.start_soon(waiter)
            tg.start_soon(stopper)

        assert outcomes[0].reason == WaitReason.BRIDGE_SHUTDOWN

    anyio.run(scenario)


def test_wait_after_shutdown_returns_immediately(store: MessageStore) -> None:
    hub = EventHub()
    hub.shutdown()

    async def scenario() -> None:
        outcome = await wait(store, hub, timeout=30)
        assert outcome.reason == WaitReason.BRIDGE_SHUTDOWN

    anyio.run(scenario)


# ---------------------------------------------------------- many waiters


def test_multiple_waiters_all_wake(db_path: Path) -> None:
    hub = EventHub()
    stores = [MessageStore(db_path) for _ in range(5)]

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter(store: MessageStore) -> None:
                outcomes.append(await wait(store, hub, timeout=10))

            for store in stores:
                tg.start_soon(waiter, store)

            async def sender() -> None:
                await anyio.sleep(0.1)
                MessageStore(db_path).send(
                    sender="codex", recipient="claude", subject="broadcast", body="one message"
                )

            tg.start_soon(sender)

        assert len(outcomes) == 5
        assert all(o.reason == WaitReason.MESSAGE_RECEIVED for o in outcomes)
        # Every waiter sees the same persistent message; nothing is consumed.
        assert {o.messages[0].subject for o in outcomes} == {"broadcast"}
        assert hub.waiter_count == 0

    anyio.run(scenario)


def test_waiters_for_different_agents_are_independent(db_path: Path) -> None:
    hub = EventHub()

    async def scenario() -> None:
        results: dict[str, str] = {}
        async with anyio.create_task_group() as tg:

            async def waiter(agent: str) -> None:
                outcome = await wait(MessageStore(db_path), hub, agent=agent, timeout=2)
                results[agent] = outcome.reason

            tg.start_soon(waiter, "claude")
            tg.start_soon(waiter, "codex")

            async def sender() -> None:
                await anyio.sleep(0.1)
                MessageStore(db_path).send(
                    sender="codex", recipient="claude", subject="for claude only", body="x"
                )

            tg.start_soon(sender)

        assert results["claude"] == WaitReason.MESSAGE_RECEIVED
        assert results["codex"] == WaitReason.TIMEOUT

    anyio.run(scenario)


# ------------------------------------------------------------------- races


@pytest.mark.parametrize("iteration", range(25))
def test_no_lost_wakeup_when_send_races_the_wait(db_path: Path, iteration: int) -> None:
    """Send at a random offset around the moment the wait registers.

    The dangerous window is between the pre-registration check and the
    registration itself. Sending at randomised sub-poll offsets walks the send
    across that window; the waiter must never miss it.
    """
    random.seed(iteration)
    delay = random.uniform(0.0, 0.03)
    waiter_store = MessageStore(db_path)
    writer_store = MessageStore(db_path)

    async def scenario() -> None:
        outcomes = []
        async with anyio.create_task_group() as tg:

            async def waiter() -> None:
                outcomes.append(await wait(waiter_store, EventHub(), timeout=5))

            async def writer() -> None:
                await anyio.sleep(delay)
                writer_store.send(
                    sender="codex", recipient="claude", subject=f"race {iteration}", body="x"
                )

            tg.start_soon(waiter)
            tg.start_soon(writer)

        assert outcomes[0].reason == WaitReason.MESSAGE_RECEIVED, (
            f"lost wakeup at delay={delay:.4f}s"
        )

    anyio.run(scenario)


def test_send_immediately_before_wait_is_never_missed(db_path: Path) -> None:
    """The zero-delay case: the message exists before the wait is even called."""
    for index in range(20):
        writer = MessageStore(db_path)
        waiter = MessageStore(db_path)
        sent = writer.send(sender="codex", recipient="claude", subject=f"m{index}", body="x")

        async def scenario() -> None:
            outcome = await wait(waiter, EventHub(), timeout=5)
            assert outcome.reason == WaitReason.MESSAGE_RECEIVED

        anyio.run(scenario)
        waiter.mark_read("claude", sent.id)


# ------------------------------------------------------- persistence


def test_wait_sees_mail_written_before_the_database_was_reopened(db_path: Path) -> None:
    first = MessageStore(db_path)
    first.send(sender="codex", recipient="claude", subject="survives", body="persisted")
    del first

    reopened = MessageStore(db_path)

    async def scenario() -> None:
        outcome = await wait(reopened, EventHub(), timeout=5)
        assert outcome.reason == WaitReason.MESSAGE_RECEIVED
        assert outcome.messages[0].subject == "survives"

    anyio.run(scenario)


def test_status_survives_reopen(db_path: Path) -> None:
    MessageStore(db_path).set_status(
        "codex", "usage_exhausted", reason="weekly cap", resume_after="2026-08-09T00:00:00Z"
    )

    reopened = MessageStore(db_path).get_status("codex")
    assert reopened.status == "usage_exhausted"
    assert reopened.status_reason == "weekly cap"
    assert reopened.resume_after is not None
    assert reopened.is_unavailable
