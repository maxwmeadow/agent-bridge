"""Claude Code hook entry point.

Claude Code runs hooks as local processes and pipes the event JSON to stdin.
That is what makes this useful: when a turn dies on a rate limit, the bridge
learns about it from the hook process, with no model turn and no cost. The
peer blocked in ``wait_for_event`` finds out on its next poll.

Field names and enum values here come from the documented schemas at
https://code.claude.com/docs/en/hooks:

* ``StopFailure``  -> ``error_type``, ``error_message``
* ``SessionEnd``   -> ``end_reason``

Nothing is inferred. An event carrying an ``error_type`` outside the
documented set is stored as ``unknown`` with the raw value in the detail,
rather than being guessed at.

A hook must never break the session it is attached to, so every failure path
here exits 0 after writing to the log.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .config import (
    CLAUDE_SESSION_END_REASONS,
    CLAUDE_STOP_FAILURE_TYPES,
    ServerConfig,
    database_path,
    log_path,
)
from .logging_setup import configure as configure_logging
from .store import MessageStore

log = logging.getLogger(__name__)

#: SessionEnd reasons that mean something for availability. The rest are
#: ordinary session churn (``clear``, ``resume``) and are recorded as nothing.
SESSION_END_TO_STATUS: dict[str, str] = {
    "logout": "auth_error",
    "prompt_input_exit": "client_closed",
    "bypass_permissions_disabled": "client_closed",
    "other": "client_closed",
}


def handle_stop_failure(store: MessageStore, agent: str, payload: dict[str, Any]) -> str:
    """Record a ``StopFailure`` event using its documented fields."""
    raw_type = payload.get("error_type")
    message = payload.get("error_message")

    if isinstance(raw_type, str) and raw_type in CLAUDE_STOP_FAILURE_TYPES:
        kind = raw_type
        detail = message if isinstance(message, str) else None
    else:
        # Undocumented or missing value: keep it visible instead of guessing.
        kind = "unknown"
        detail = f"undocumented error_type {raw_type!r}"
        if isinstance(message, str):
            detail += f": {message}"

    record = store.record_failure(agent, kind, detail=detail, source="self")
    return (
        f"recorded failure {kind} for {agent}; availability is now {record.status}"
    )


def handle_session_end(store: MessageStore, agent: str, payload: dict[str, Any]) -> str:
    """Record a ``SessionEnd`` event using its documented ``end_reason``."""
    reason = payload.get("end_reason")
    if not isinstance(reason, str) or reason not in CLAUDE_SESSION_END_REASONS:
        log.warning("SessionEnd with unrecognised end_reason %r; ignoring", reason)
        return f"ignored SessionEnd with end_reason {reason!r}"

    status = SESSION_END_TO_STATUS.get(reason)
    if status is None:
        # clear / resume: the session is being recycled, not going away.
        return f"SessionEnd end_reason={reason} needs no availability change"

    store.set_status(agent, status, reason=f"SessionEnd: {reason}", source="self")
    return f"recorded {status} for {agent} (end_reason={reason})"


def handle_statusline(store: MessageStore, agent: str, payload: dict[str, Any]) -> str:
    """Record quota usage from Claude Code's status line JSON.

    ``rate_limits`` is official and documented, appears only for Claude.ai
    subscribers, and only after the first API response of a session. Its
    ``resets_at`` is Unix epoch seconds and is the reliable reset timestamp
    the bridge stores as a future ``resume_after`` when a rate limit hits.

    Usage is recorded as a metric only. It never changes availability.
    """
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return "no rate_limits in status line payload"

    recorded = []
    # Record the window closest to exhaustion; that is the one whose reset
    # time matters.
    best: tuple[float, str, Any] | None = None
    for window in ("five_hour", "seven_day"):
        entry = limits.get(window)
        if not isinstance(entry, dict):
            continue
        percent = entry.get("used_percentage")
        if not isinstance(percent, (int, float)):
            continue
        if best is None or float(percent) > best[0]:
            best = (float(percent), window, entry.get("resets_at"))

    if best is None:
        return "no usable rate_limits windows in status line payload"

    percent, window, resets_at = best
    store.record_usage(
        agent,
        percent=percent,
        window=window,
        resets_at=_epoch_to_iso(resets_at),
        source="claude_statusline",
    )
    recorded.append(f"{window}={percent:.0f}%")
    return "recorded usage " + ", ".join(recorded)


def _epoch_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
        timespec="microseconds"
    )


HANDLERS = {
    "StopFailure": handle_stop_failure,
    "SessionEnd": handle_session_end,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-hook",
        description=(
            "Claude Code hook receiver. Reads the hook JSON on stdin and updates "
            "agent-bridge availability, failure, and usage state."
        ),
    )
    parser.add_argument(
        "--agent", help="Which agent this Claude Code instance is. Defaults to $AGENT_BRIDGE_AGENT."
    )
    parser.add_argument(
        "--statusline",
        action="store_true",
        help=(
            "Treat stdin as status line JSON instead of a hook event, and record "
            "rate_limits as a usage sample. Prints nothing, so chain it before "
            "your real status line command."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Always exits 0. A hook that fails must not disturb the session."""
    args = build_parser().parse_args(argv)
    try:
        raw = sys.stdin.read()
    except OSError as exc:  # pragma: no cover - stdin is always readable in practice
        print(f"agent-bridge-hook: could not read stdin: {exc}", file=sys.stderr)
        return 0

    try:
        config = ServerConfig.resolve(args.agent)
        configure_logging(config.agent, log_path())
        store = MessageStore(database_path())

        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")

        if args.statusline:
            outcome = handle_statusline(store, config.agent, payload)
        else:
            event = payload.get("hook_event_name")
            handler = HANDLERS.get(event) if isinstance(event, str) else None
            if handler is None:
                outcome = f"no handler for hook_event_name {event!r}"
                log.debug(outcome)
            else:
                outcome = handler(store, config.agent, payload)

        log.info("hook handled: %s", outcome)
    except Exception as exc:  # noqa: BLE001
        # Logged with a traceback, never swallowed silently -- but never
        # propagated either, because this process is attached to a live
        # Claude Code session.
        logging.getLogger("agent_bridge.hooks").exception("hook failed")
        print(f"agent-bridge-hook: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
