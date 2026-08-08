"""Blocking waits for bridge events.

The hard constraint is that the two agents run in *separate OS processes*, so
an in-process event set by Claude's server can never reach a waiter inside
Codex's server. SQLite is therefore the authoritative wake-up channel: a
waiter re-reads persistent state on a short interval and decides from what it
finds there. The in-process :class:`EventHub` is a latency optimization for
waiters that happen to share a process with the writer, nothing more.

Lost wake-ups are prevented by checking persistent state *before* registering
with the hub and again *immediately after*. Anything that happened in the gap
is caught by the second check; anything after registration either sets the
event or is found by the next poll.

Nothing here sleeps a subprocess, spawns a process, or asks a model to retry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio

from .config import (
    HEARTBEAT_SECONDS,
    UNAVAILABLE_STATUSES,
    clamp_wait_seconds,
    known_agents,
    poll_interval,
    require_known_agent,
)
from .models import AgentStatus, WaitOutcome
from .store import MessageStore

log = logging.getLogger(__name__)


class WaitReason:
    """Why a wait ended. Returned verbatim so callers can branch on it."""

    MESSAGE_RECEIVED = "message_received"
    PEER_UNAVAILABLE = "peer_unavailable"
    PEER_AVAILABLE = "peer_available"
    CANCELLED = "cancelled"
    #: Reserved for the goal system. Nothing produces it yet; it exists so the
    #: reason vocabulary does not have to change when goals land.
    GOAL_CANCELLED = "goal_cancelled"
    TIMEOUT = "timeout"
    BRIDGE_SHUTDOWN = "bridge_shutdown"


class EventHub:
    """In-process wake-ups for waiters sharing this server process.

    Purely an optimization. Correctness never depends on a notification
    arriving: every waiter also re-reads SQLite on its poll interval.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, list[anyio.Event]] = {}
        self._shutting_down = False

    def subscribe(self, agent: str) -> anyio.Event:
        """Register interest and return a one-shot event for this generation."""
        event = anyio.Event()
        self._waiters.setdefault(agent, []).append(event)
        return event

    def unsubscribe(self, agent: str, event: anyio.Event) -> None:
        waiters = self._waiters.get(agent)
        if waiters and event in waiters:
            waiters.remove(event)

    def notify(self, agent: str) -> None:
        """Wake every waiter registered for ``agent`` in this process."""
        for event in self._waiters.pop(agent, []):
            event.set()

    def shutdown(self) -> None:
        """Wake every waiter so they can return ``bridge_shutdown``."""
        self._shutting_down = True
        for agent in list(self._waiters):
            self.notify(agent)

    def is_shutting_down(self) -> bool:
        # A method, not a property: the value changes across awaits, and a
        # property would let type narrowing treat the second check as dead.
        return self._shutting_down

    @property
    def waiter_count(self) -> int:
        return sum(len(events) for events in self._waiters.values())


@dataclass(frozen=True, slots=True)
class _Baseline:
    """Persistent state as it looked before the wait started."""

    cancel_seq: int
    peers: dict[str, tuple[str, str | None]]


def _snapshot(store: MessageStore, agent: str) -> _Baseline:
    statuses = {status.id: status for status in store.all_statuses()}
    mine = statuses.get(agent)
    return _Baseline(
        cancel_seq=mine.wait_cancel_seq if mine else 0,
        peers={
            name: (status.status, status.status_changed_at)
            for name, status in statuses.items()
            if name != agent
        },
    )


def _evaluate(
    store: MessageStore,
    agent: str,
    baseline: _Baseline,
    *,
    wake_on_peer_status: bool,
    started: float,
) -> WaitOutcome | None:
    """Read persistent state and decide whether the wait is over.

    This is the only place that decides. It reads SQLite every time and never
    trusts an in-process signal, which is what keeps the two processes
    consistent.
    """
    elapsed = time.monotonic() - started

    unread = store.inbox(agent, unread_only=True, limit=20)
    if unread:
        return WaitOutcome(
            reason=WaitReason.MESSAGE_RECEIVED,
            waited_seconds=elapsed,
            messages=tuple(unread),
        )

    statuses = {status.id: status for status in store.all_statuses()}

    mine = statuses.get(agent)
    if mine is not None and mine.wait_cancel_seq > baseline.cancel_seq:
        return WaitOutcome(
            reason=WaitReason.CANCELLED,
            waited_seconds=elapsed,
            detail="A cancellation was requested for this agent's waits.",
        )

    if wake_on_peer_status:
        for name, was in baseline.peers.items():
            now_status = statuses.get(name)
            if now_status is None:
                continue
            if (now_status.status, now_status.status_changed_at) == was:
                continue
            # A peer moving to busy or waiting is noise: it is still alive and
            # still working. Only availability changes end a wait.
            if now_status.status in UNAVAILABLE_STATUSES:
                return WaitOutcome(
                    reason=WaitReason.PEER_UNAVAILABLE,
                    waited_seconds=elapsed,
                    peer=now_status,
                    detail=_peer_detail(now_status),
                )
            if now_status.status == "available" and was[0] != "available":
                return WaitOutcome(
                    reason=WaitReason.PEER_AVAILABLE,
                    waited_seconds=elapsed,
                    peer=now_status,
                    detail=f"{name} reported itself available.",
                )
    return None


def _peer_detail(peer: AgentStatus) -> str:
    detail = f"{peer.id} reported status {peer.status}"
    if peer.status_reason:
        detail += f": {peer.status_reason}"
    if peer.resume_after:
        detail += f" (resume after {peer.resume_after})"
    return detail


async def wait_for_event(
    store: MessageStore,
    hub: EventHub,
    *,
    agent: str,
    timeout_seconds: float,
    wake_on_peer_status: bool = True,
    heartbeat: Callable[[float, float], Awaitable[None]] | None = None,
) -> WaitOutcome:
    """Block until something relevant happens, or the bounded timeout expires.

    Returns a :class:`WaitOutcome` whose ``reason`` is one of
    :class:`WaitReason`. Cancellation of the surrounding task (an MCP request
    cancellation, for instance) propagates as ``anyio.get_cancelled_exc_class``
    rather than being converted into an outcome, because the caller is gone.
    """
    agent = require_known_agent(agent)
    timeout = clamp_wait_seconds(timeout_seconds)
    interval = poll_interval()
    started = time.monotonic()
    deadline = started + timeout

    baseline = _snapshot(store, agent)

    # Check 1: before registering. Anything already pending returns at once.
    outcome = _evaluate(
        store, agent, baseline, wake_on_peer_status=wake_on_peer_status, started=started
    )
    if outcome is not None:
        log.info("wait resolved immediately agent=%s reason=%s", agent, outcome.reason)
        return outcome

    if hub.is_shutting_down():
        return WaitOutcome(
            reason=WaitReason.BRIDGE_SHUTDOWN, waited_seconds=0.0, detail="Bridge is shutting down."
        )

    log.info(
        "wait started agent=%s timeout=%.1fs peers=%s",
        agent,
        timeout,
        ",".join(name for name in known_agents() if name != agent),
    )

    next_heartbeat = started + HEARTBEAT_SECONDS
    try:
        while True:
            event = hub.subscribe(agent)

            # Check 2: after registering. Closes the lost-wakeup window --
            # anything that landed between check 1 and registration is seen
            # here, and anything after registration sets the event.
            outcome = _evaluate(
                store, agent, baseline, wake_on_peer_status=wake_on_peer_status, started=started
            )
            if outcome is not None:
                hub.unsubscribe(agent, event)
                log.info(
                    "wait resolved agent=%s reason=%s after=%.2fs",
                    agent,
                    outcome.reason,
                    outcome.waited_seconds,
                )
                return outcome

            if hub.is_shutting_down():
                hub.unsubscribe(agent, event)
                return WaitOutcome(
                    reason=WaitReason.BRIDGE_SHUTDOWN,
                    waited_seconds=time.monotonic() - started,
                    detail="Bridge is shutting down.",
                )

            now = time.monotonic()
            if now >= deadline:
                hub.unsubscribe(agent, event)
                waited = now - started
                log.info("wait timed out agent=%s after=%.1fs", agent, waited)
                return WaitOutcome(
                    reason=WaitReason.TIMEOUT,
                    waited_seconds=waited,
                    detail=f"Nothing arrived within {timeout:.0f}s.",
                )

            if heartbeat is not None and now >= next_heartbeat:
                await heartbeat(now - started, timeout)
                next_heartbeat = now + HEARTBEAT_SECONDS

            # Sleep until the in-process event fires or the next poll is due,
            # whichever comes first. The poll is what catches the other agent's
            # process; the event just makes same-process waits instant.
            budget = min(interval, deadline - now)
            if heartbeat is not None:
                budget = min(budget, max(next_heartbeat - now, 0.001))
            with anyio.move_on_after(max(budget, 0.001)):
                await event.wait()
            hub.unsubscribe(agent, event)
    except anyio.get_cancelled_exc_class():
        log.info("wait cancelled by client agent=%s after=%.2fs", agent, time.monotonic() - started)
        raise
