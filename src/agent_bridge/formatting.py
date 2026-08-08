"""Rendering for MCP tool results and the debug CLI.

Tool results are plain text on purpose: an inbox listing reads the same way a
person would write it, so both Claude and Codex can act on it without parsing
a JSON blob. Ids are always shown verbatim so they can be passed straight back
into ``read_message``, ``reply``, or ``mark_read``.
"""

from __future__ import annotations

from .models import AgentStatus, BridgeStatus, Message, ThreadSummary, WaitOutcome
from .store import preview


def short_time(timestamp: str) -> str:
    """Trim an ISO-8601 UTC timestamp to whole seconds for display."""
    if not timestamp:
        return ""
    seconds = timestamp.partition(".")[0].partition("+")[0]
    return seconds.replace("T", " ") + "Z"


def _context_lines(message: Message) -> list[str]:
    if not message.context:
        return []
    parts = [f"{key}={value}" for key, value in sorted(message.context.items())]
    return [f"Context: {', '.join(parts)}"]


def format_sent(message: Message) -> str:
    lines = [
        f"Sent to {message.recipient}.",
        "",
        f"Message: {message.id}",
        f"Thread:  {message.thread_id}",
        f"Subject: {message.subject}",
    ]
    if message.reply_to_id:
        lines.append(f"In reply to: {message.reply_to_id}")
    lines.append(
        f"{message.recipient} will see it the next time it checks its agent-bridge inbox."
    )
    return "\n".join(lines)


def format_inbox(agent: str, messages: list[Message], *, unread_only: bool) -> str:
    scope = "unread" if unread_only else "recent"
    if not messages:
        if unread_only:
            return (
                f"No unread messages for {agent}.\n"
                "Pass unread_only=false to see recent messages that are already read."
            )
        return f"No messages for {agent} yet."

    count = len(messages)
    header = f"{count} {scope} message{'s' if count != 1 else ''} for {agent}:"
    blocks = [header]
    for message in messages:
        flag = "UNREAD" if message.is_unread else "read"
        block = [
            "",
            f"[{message.id}]  ({flag})",
            f"From:    {message.sender}",
            f"Subject: {message.subject}",
            f"Sent:    {short_time(message.created_at)}",
            f"Thread:  {message.thread_id}",
        ]
        block.extend(_context_lines(message))
        block.append(f"Preview: {preview(message.body)}")
        blocks.extend(block)
    blocks.append("")
    blocks.append("Use read_message(message_id) for the full text, then reply(message_id, body).")
    return "\n".join(blocks)


def format_message(message: Message, *, thread_size: int | None = None) -> str:
    lines = [
        f"[{message.id}]",
        f"From:    {message.sender}",
        f"To:      {message.recipient}",
        f"Subject: {message.subject}",
        f"Sent:    {short_time(message.created_at)}",
        f"Status:  {'unread' if message.is_unread else 'read ' + short_time(message.read_at or '')}",
        f"Thread:  {message.thread_id}"
        + (f" ({thread_size} messages)" if thread_size is not None else ""),
    ]
    if message.reply_to_id:
        lines.append(f"Replies to: {message.reply_to_id}")
    lines.extend(_context_lines(message))
    lines.extend(["", "--- body ---", message.body, "--- end body ---"])
    return "\n".join(lines)


def format_threads(summaries: list[ThreadSummary], *, agent: str | None = None) -> str:
    if not summaries:
        who = f" involving {agent}" if agent else ""
        return f"No threads{who} yet."
    scope = f" involving {agent}" if agent else ""
    lines = [f"{len(summaries)} recent thread{'s' if len(summaries) != 1 else ''}{scope}:"]
    for summary in summaries:
        lines.extend(
            [
                "",
                f"[{summary.id}]",
                f"Subject:      {summary.subject}",
                f"Participants: {', '.join(summary.participants)}",
                f"Messages:     {summary.message_count} ({summary.unread_count} unread)",
                f"Last active:  {short_time(summary.updated_at)} by {summary.last_sender}",
                f"Latest:       {summary.last_preview}",
            ]
        )
    lines.extend(["", "Use read_thread(thread_id) to read one in full."])
    return "\n".join(lines)


def format_thread(thread_id: str, subject: str, messages: list[Message]) -> str:
    lines = [
        f"Thread [{thread_id}]: {subject}",
        f"{len(messages)} message{'s' if len(messages) != 1 else ''}, oldest first.",
    ]
    for message in messages:
        flag = "" if not message.is_unread else "  (UNREAD)"
        lines.extend(
            [
                "",
                f"--- {short_time(message.created_at)}  {message.sender} -> "
                f"{message.recipient}{flag}",
                f"[{message.id}] {message.subject}",
                "",
                message.body,
            ]
        )
    return "\n".join(lines)


def format_status(status: BridgeStatus) -> str:
    lines = [
        "agent-bridge status",
        f"  version:        {status.version}",
        f"  this agent:     {status.agent}",
        f"  database:       {status.db_path}",
        f"  schema version: {status.schema_version}",
        f"  messages:       {status.total_messages} in {status.total_threads} threads",
        f"  known agents:   {', '.join(status.known_agents)}",
        "",
        "  agent      status           unread  last connected",
    ]
    for agent in status.agents:
        seen = short_time(agent.last_seen_at) if agent.last_seen_at else "never"
        lines.append(f"  {agent.id:<10} {agent.status:<16} {agent.unread:>6}  {seen}")
    lines.append("")
    lines.append("  Status is what each agent reported, not what the bridge guessed.")
    return "\n".join(lines)


def format_agent_status(status: AgentStatus) -> str:
    """Detailed availability record for one agent."""
    lines = [
        f"{status.id}: {status.status}",
        f"  reported by:   {status.status_source}",
        f"  changed at:    "
        + (short_time(status.status_changed_at) if status.status_changed_at else "never"),
        f"  last connected: "
        + (short_time(status.last_seen_at) if status.last_seen_at else "never"),
        f"  unread:        {status.unread}",
    ]
    if status.resume_after:
        lines.append(f"  resume after:  {short_time(status.resume_after)}")
    if status.status_reason:
        lines.append(f"  reason:        {status.status_reason}")

    idle = status.seconds_since_seen()
    if idle is not None and idle > 900:
        # Observed, and labelled as observed. Silence is never converted into
        # a reported status such as usage_exhausted.
        lines.append(
            f"  note:          not seen for {idle / 60:.0f} minutes "
            "(observation only; status is unchanged)"
        )
    return "\n".join(lines)


def format_wait_outcome(agent: str, outcome: WaitOutcome) -> str:
    """Render a wait result so the agent knows what to do next."""
    header = f"Wait ended after {outcome.waited_seconds:.1f}s: {outcome.reason}"

    if outcome.reason == "message_received":
        return header + "\n\n" + format_inbox(agent, list(outcome.messages), unread_only=True)

    lines = [header]
    if outcome.detail:
        lines.extend(["", outcome.detail])
    if outcome.peer is not None:
        lines.extend(["", format_agent_status(outcome.peer)])
    if outcome.reason == "timeout":
        lines.append("")
        lines.append(
            "Nothing arrived. Wait again if you are still expecting something, "
            "or tell the user what you are blocked on."
        )
    elif outcome.reason == "peer_unavailable":
        lines.append("")
        lines.append(
            "Do not keep waiting on this peer. Tell the user what happened and, "
            "if a resume time was given, when it is worth retrying."
        )
    elif outcome.reason == "bridge_shutdown":
        lines.append("")
        lines.append("The bridge server is stopping. Nothing was lost; messages persist.")
    return "\n".join(lines)
