"""Deciding whether, where, and how to wake a peer.

**The doorbell, not the message.** Everything here produces a short,
bridge-authored notification saying that mail exists. The mail itself stays in
SQLite and is read back through MCP. Peer text is never injected into a
notification, never interpreted, and never executed: a notification is
assembled from ids, counts and sender names only.

**Pull at the client, by design.** The only wake mechanism available today is
Claude Code's ``asyncRewake`` Stop hook: a background process the client
itself starts, which blocks on this database and exits 2 when mail lands.
Nothing here reaches into a running client. :class:`WakeAdapter` exists so a
future push mechanism (Channels, once it is out of research preview and
accepts custom servers) can slot in without the rest of the bridge noticing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .config import MAX_CONSECUTIVE_AUTO_WAKES
from .models import AgentStatus, Message
from .sessions import ClientSession, SessionRegistry
from .store import MessageStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WakeDecision:
    """Whether a wake can happen, and why not when it cannot."""

    agent: str
    deliverable: bool
    reason: str
    target: ClientSession | None = None
    peer: AgentStatus | None = None

    def describe(self) -> str:
        if self.deliverable and self.target is not None:
            where = f" in {self.target.project}" if self.target.project else ""
            return f"{self.agent} has a live session{where}; it will be notified automatically."
        return f"{self.agent} will not be notified automatically: {self.reason}"


@dataclass(frozen=True, slots=True)
class WakeResult:
    delivered: bool
    reason: str
    message_ids: tuple[str, ...] = ()


class WakeAdapter(Protocol):
    """One way of getting a session's attention."""

    name: str

    def can_wake(self, target: ClientSession) -> bool: ...

    def wake(self, target: ClientSession, notification: str) -> WakeResult: ...


class StopHookRewakeAdapter:
    """Claude Code ``asyncRewake`` Stop hook.

    The client arms its own doorbell when a turn ends, so there is nothing to
    push from here. :meth:`wake` reports that the armed waiter is what
    delivers -- it does not pretend to have injected anything.
    """

    name = "stop_hook_rewake"

    def can_wake(self, target: ClientSession) -> bool:
        return target.wake_method == "stop_hook_rewake" and target.can_wake

    def wake(self, target: ClientSession, notification: str) -> WakeResult:
        if not self.can_wake(target):
            return WakeResult(False, f"session {target.id} has no usable wake method")
        # Delivery is performed by the armed Stop-hook process when it sees the
        # pending message. Claiming success here would be a lie.
        return WakeResult(
            True, f"queued for session {target.id}; its armed Stop-hook doorbell will deliver"
        )


class NullAdapter:
    """For clients with no idle-wake mechanism -- Codex today."""

    name = "none"

    def can_wake(self, target: ClientSession) -> bool:
        return False

    def wake(self, target: ClientSession, notification: str) -> WakeResult:
        return WakeResult(
            False,
            f"{target.client_type} has no supported idle-wake mechanism; "
            "the peer must use wait_for_event or check its inbox",
        )


ADAPTERS: dict[str, WakeAdapter] = {
    StopHookRewakeAdapter.name: StopHookRewakeAdapter(),
    NullAdapter.name: NullAdapter(),
}


def adapter_for(target: ClientSession) -> WakeAdapter:
    return ADAPTERS.get(target.wake_method, ADAPTERS["none"])


def plan_wake(
    store: MessageStore,
    registry: SessionRegistry,
    *,
    recipient: str,
    project: str | None = None,
) -> WakeDecision:
    """Work out what would happen if we tried to wake ``recipient`` now.

    Called at send time so the sender learns the truth immediately, including
    when the peer is out of quota or has nothing listening.
    """
    peer = store.get_status(recipient)

    if peer.is_unavailable:
        detail = f"reported {peer.status}"
        if peer.resume_after:
            detail += f", resume after {peer.resume_after}"
        return WakeDecision(recipient, False, detail, peer=peer)

    target = registry.select_target(recipient, project=project)
    if target is None:
        return WakeDecision(
            recipient, False, "no live registered session to notify", peer=peer
        )

    adapter = adapter_for(target)
    if not adapter.can_wake(target):
        return WakeDecision(
            recipient,
            False,
            f"session {target.id} uses wake method {target.wake_method}",
            target=target,
            peer=peer,
        )

    if store.has_active_wait(recipient):
        # Mode 1 beats Mode 2: an already-blocked wait_for_event will resolve
        # on its own, and injecting a second turn would duplicate the work.
        return WakeDecision(
            recipient,
            True,
            "already blocked in wait_for_event; that call will resolve instead",
            target=target,
            peer=peer,
        )

    if target.auto_wakes >= MAX_CONSECUTIVE_AUTO_WAKES:
        return WakeDecision(
            recipient,
            False,
            f"automatic wake budget spent ({target.auto_wakes} consecutive wakes "
            "with no human input); the message is waiting in the inbox",
            target=target,
            peer=peer,
        )

    return WakeDecision(recipient, True, "live session with an armed doorbell", target, peer)


def notification_text(agent: str, messages: list[Message]) -> str:
    """The doorbell text. Bridge-authored, deliberately uninformative.

    It says that mail exists and who from. It does not carry bodies, does not
    summarise, and does not tell the agent what to conclude -- the peer's
    words are collaboration input to be read and judged through MCP, not
    instructions arriving with system authority.
    """
    senders = sorted({message.sender for message in messages})
    threads = sorted({message.thread_id for message in messages})
    count = len(messages)

    who = ", ".join(senders)
    lines = [
        f"agent-bridge: {count} new peer message{'s' if count != 1 else ''} "
        f"from {who} waiting for {agent}."
    ]
    if len(threads) == 1:
        lines.append(f"Thread: {threads[0]}")
    projects = sorted(
        {m.context["project"] for m in messages if m.context.get("project")}
    )
    if len(projects) == 1:
        lines.append(f"Project: {projects[0]}")
    lines.append(
        "Read your unread agent-bridge messages, decide whether action is required, "
        "and continue the collaboration if appropriate."
    )
    return "\n".join(lines)
