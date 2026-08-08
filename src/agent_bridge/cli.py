"""Human-facing debug CLI.

This is for inspecting the bridge without either AI client. Unlike the MCP
server it accepts ``--from``, because a person at a terminal is already
trusted to say who they are.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio

from . import __version__
from .codex_usage import read_latest_usage
from .config import (
    AGENT_STATUSES,
    DEFAULT_AGENTS,
    DEFAULT_WAIT_SECONDS,
    FAILURE_KINDS,
    MAX_WAIT_SECONDS,
    USAGE_WINDOWS,
    database_path,
    known_agents,
    log_path,
    require_known_agent,
)
from .errors import BridgeError
from .events import EventHub, wait_for_event
from .formatting import (
    format_agent_status,
    format_inbox,
    format_message,
    format_status,
    format_thread,
    format_threads,
    format_wait_outcome,
)
from .store import MessageStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge",
        description="Inspect and exercise the local agent-bridge mailbox.",
    )
    parser.add_argument("--version", action="version", version=f"agent-bridge {__version__}")
    parser.add_argument(
        "--db", type=Path, default=None, help="Database path (default ~/.agent-bridge/agent-bridge.db)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show bridge state, or one agent's availability.")
    status.add_argument(
        "agent", nargs="?", default=None, help="Show just this agent's availability record."
    )
    status.add_argument(
        "--agent", dest="viewpoint", default=DEFAULT_AGENTS[0], help="Point of view for the report."
    )

    set_status = sub.add_parser("set-status", help="Record an agent's availability.")
    set_status.add_argument("agent")
    set_status.add_argument("status", choices=AGENT_STATUSES)
    set_status.add_argument("--reason", default=None, help="Human-readable explanation.")
    set_status.add_argument(
        "--resume-after", default=None, help="ISO-8601 time when it should be usable again."
    )

    wait = sub.add_parser("wait", help="Block until mail arrives or a peer changes state.")
    wait.add_argument("agent", help="Whose inbox to wait on.")
    wait.add_argument(
        "--timeout", type=float, default=DEFAULT_WAIT_SECONDS,
        help=f"Seconds to block (1-{MAX_WAIT_SECONDS}, clamped).",
    )
    wait.add_argument(
        "--ignore-peer-status", action="store_true", help="Only wake for mail and cancellation."
    )

    cancel = sub.add_parser("cancel-wait", help="Wake an agent's blocked waiters.")
    cancel.add_argument("agent")
    cancel.add_argument("--reason", default=None)

    failure = sub.add_parser("record-failure", help="Record a client failure for an agent.")
    failure.add_argument("agent")
    failure.add_argument("kind", choices=FAILURE_KINDS)
    failure.add_argument("--detail", default=None)
    failure.add_argument(
        "--resume-after", default=None, help="ISO-8601 time when it should recover."
    )

    usage = sub.add_parser("record-usage", help="Record a quota usage sample (a metric only).")
    usage.add_argument("agent")
    usage.add_argument("percent", type=float)
    usage.add_argument("--window", choices=USAGE_WINDOWS, default="five_hour")
    usage.add_argument("--resets-at", default=None)

    codex = sub.add_parser(
        "codex-usage",
        help=(
            "Read Codex's local session rollout files for a usage sample. "
            "Opt-in and best-effort: the format is undocumented."
        ),
    )
    codex.add_argument(
        "--apply",
        action="store_true",
        help="Store the sample against the codex agent instead of only printing it.",
    )
    codex.add_argument(
        "--sessions-dir", type=Path, default=None, help="Override ~/.codex/sessions."
    )
    codex.add_argument("--agent", default="codex", help="Which agent the sample belongs to.")

    inbox = sub.add_parser("inbox", help="List messages addressed to an agent.")
    inbox.add_argument("agent", help="Agent whose inbox to list.")
    inbox.add_argument("--all", action="store_true", help="Include messages already read.")
    inbox.add_argument("--limit", type=int, default=20)

    threads = sub.add_parser("threads", help="List recent conversation threads.")
    threads.add_argument("--agent", default=None, help="Restrict to threads involving this agent.")
    threads.add_argument("--limit", type=int, default=10)

    read = sub.add_parser("read", help="Print one message in full.")
    read.add_argument("message_id")
    read.add_argument(
        "--mark-read", metavar="AGENT", default=None, help="Also mark it read as this agent."
    )

    thread = sub.add_parser("thread", help="Print every message in a thread.")
    thread.add_argument("thread_id")
    thread.add_argument("--limit", type=int, default=50)

    send = sub.add_parser("send", help="Send a message.")
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", dest="recipient", required=True)
    send.add_argument("--subject", required=True)
    body_group = send.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body")
    body_group.add_argument("--body-file", type=Path, help="Read the body from a file ('-' for stdin).")

    reply = sub.add_parser("reply", help="Reply to a message, keeping its thread.")
    reply.add_argument("message_id")
    reply.add_argument("--from", dest="sender", required=True)
    reply.add_argument("--body", required=True)

    mark = sub.add_parser("mark-read", help="Mark a message read.")
    mark.add_argument("message_id")
    mark.add_argument("--agent", required=True, help="The recipient marking it read.")

    sub.add_parser("agents", help="List the configured agent roster.")
    sub.add_parser("paths", help="Print the database and log file paths.")

    return parser


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return str(args.body)
    path: Path = args.body_file
    if str(path) == "-":
        return sys.stdin.read()
    return path.read_text(encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    db_file = args.db if args.db is not None else database_path()
    store = MessageStore(db_file)

    if args.command == "paths":
        print(f"database: {db_file}")
        print(f"log:      {log_path()}")
        return 0

    if args.command == "agents":
        print("\n".join(known_agents()))
        return 0

    if args.command == "status":
        if args.agent:
            print(format_agent_status(store.get_status(args.agent)))
        else:
            print(format_status(store.status(args.viewpoint)))
        return 0

    if args.command == "set-status":
        record = store.set_status(
            args.agent,
            args.status,
            reason=args.reason,
            resume_after=args.resume_after,
            source="cli",
        )
        print(format_agent_status(record))
        return 0

    if args.command == "record-failure":
        record = store.record_failure(
            args.agent,
            args.kind,
            detail=args.detail,
            source="cli",
            resume_after=args.resume_after,
        )
        print(format_agent_status(record))
        return 0

    if args.command == "record-usage":
        record = store.record_usage(
            args.agent,
            percent=args.percent,
            window=args.window,
            resets_at=args.resets_at,
            source="manual",
        )
        print(format_agent_status(record))
        return 0

    if args.command == "codex-usage":
        sample = read_latest_usage(args.sessions_dir)
        if sample is None:
            print(
                "No Codex usage sample found. This reader is best-effort: Codex may "
                "not have written a rollout file yet, or its format may have changed.",
                file=sys.stderr,
            )
            return 1
        reached = "yes" if sample.limit_reached else "no"
        print(
            f"{sample.percent:.1f}% of {sample.window} window"
            + (f", resets {sample.resets_at}" if sample.resets_at else "")
            + f"\nplan: {sample.plan_type or 'unknown'}   limit reached: {reached}"
            f"\nsource: {sample.source_file} (undocumented format, best effort)"
        )
        if args.apply:
            record = store.record_usage(
                args.agent,
                percent=sample.percent,
                window=sample.window,
                resets_at=sample.resets_at,
                source="codex_rollout",
            )
            print()
            print(format_agent_status(record))
            print(
                "\nRecorded as a usage metric only. Availability is unchanged: "
                "consuming quota is not the same as running out."
            )
        return 0

    if args.command == "cancel-wait":
        seq = store.cancel_waits(args.agent, reason=args.reason)
        print(f"Cancelled {args.agent}'s waits (sequence {seq}).")
        return 0

    if args.command == "wait":
        agent = require_known_agent(args.agent)
        outcome = anyio.run(
            lambda: wait_for_event(
                store,
                EventHub(),
                agent=agent,
                timeout_seconds=args.timeout,
                wake_on_peer_status=not args.ignore_peer_status,
            )
        )
        print(format_wait_outcome(agent, outcome))
        # Distinguish "something happened" from "nothing did", for shell use.
        return 0 if outcome.reason != "timeout" else 3

    if args.command == "inbox":
        agent = require_known_agent(args.agent)
        messages = store.inbox(agent, unread_only=not args.all, limit=args.limit)
        print(format_inbox(agent, messages, unread_only=not args.all))
        return 0

    if args.command == "threads":
        summaries = store.list_threads(args.agent, limit=args.limit)
        print(format_threads(summaries, agent=args.agent))
        return 0

    if args.command == "read":
        viewer = require_known_agent(args.mark_read) if args.mark_read else None
        message = store.get_message(args.message_id, viewer=viewer)
        if viewer and message.recipient == viewer and message.is_unread:
            message = store.mark_read(viewer, message.id)
        _, thread_messages = store.read_thread(message.thread_id, limit=500)
        print(format_message(message, thread_size=len(thread_messages)))
        return 0

    if args.command == "thread":
        subject, messages = store.read_thread(args.thread_id, limit=args.limit)
        print(format_thread(args.thread_id, subject, messages))
        return 0

    if args.command == "send":
        message = store.send(
            sender=args.sender,
            recipient=args.recipient,
            subject=args.subject,
            body=_read_body(args),
        )
        print(f"Sent {message.id} to {message.recipient} in thread {message.thread_id}.")
        return 0

    if args.command == "reply":
        message = store.reply(sender=args.sender, message_id=args.message_id, body=args.body)
        print(f"Sent {message.id} to {message.recipient} in thread {message.thread_id}.")
        return 0

    if args.command == "mark-read":
        message = store.mark_read(args.agent, args.message_id)
        print(f"Marked {message.id} read for {args.agent}.")
        return 0

    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except BridgeError as exc:
        print(f"agent-bridge: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"agent-bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
