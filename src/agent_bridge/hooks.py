"""Claude Code hook entry point.

Claude Code runs hooks as local processes and pipes the event JSON to stdin.
That is what makes this useful: the bridge learns about failures, session
lifecycle, and pending peer mail without a model turn and without cost.

Field names and enum values come from the documented schemas at
https://code.claude.com/docs/en/hooks:

* ``SessionStart``      -> ``session_id``, ``cwd``
* ``UserPromptSubmit``  -> ``session_id``
* ``Stop``              -> ``session_id``
* ``StopFailure``       -> ``error_type``, ``error_message``
* ``SessionEnd``        -> ``end_reason``

Nothing is inferred. An event carrying an ``error_type`` outside the
documented set is stored as ``unknown`` with the raw value in the detail.

**The doorbell.** The ``Stop`` handler is the idle-wake mechanism. Configured
with ``asyncRewake``, Claude Code runs it in the background when a turn ends
and wakes the model if it exits 2, showing its stderr as a system reminder.
So it blocks on this database and exits 2 when actionable peer mail arrives.
That single mechanism covers both mail that landed while the agent was busy
(found immediately) and mail that lands while the agent sits idle (found
later, while still armed).

A hook must never break the session it is attached to, so every failure path
exits 0 after logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import (
    CLAUDE_SESSION_END_REASONS,
    CLAUDE_STOP_FAILURE_TYPES,
    DOORBELL_SECONDS,
    MAX_CONSECUTIVE_AUTO_WAKES,
    ServerConfig,
    database_path,
    log_path,
    poll_interval,
)
from .errors import NotFoundError
from .logging_setup import configure as configure_logging
from .sessions import SessionRegistry
from .store import MessageStore
from .wake import notification_text

log = logging.getLogger(__name__)

#: Exit code that makes an ``asyncRewake`` hook wake the model.
REWAKE_EXIT_CODE = 2

#: SessionEnd reasons that mean something for availability. The rest are
#: ordinary session churn (``clear``, ``resume``).
SESSION_END_TO_STATUS: dict[str, str] = {
    "logout": "auth_error",
    "prompt_input_exit": "client_closed",
    "bypass_permissions_disabled": "client_closed",
    "other": "client_closed",
}


@dataclass(frozen=True, slots=True)
class HookContext:
    store: MessageStore
    registry: SessionRegistry
    agent: str
    payload: dict[str, Any]

    @property
    def session_id(self) -> str | None:
        value = self.payload.get("session_id")
        return value if isinstance(value, str) and value.strip() else None

    @property
    def cwd(self) -> str | None:
        value = self.payload.get("cwd")
        return value if isinstance(value, str) and value.strip() else None


def _ensure_session(ctx: HookContext, client_type: str, provider: str | None) -> str | None:
    """Register the session if this is the first hook that mentions it.

    Hooks can be added mid-session, so the doorbell cannot assume SessionStart
    already ran for this session id.
    """
    session_id = ctx.session_id
    if session_id is None:
        return None
    try:
        ctx.registry.get(session_id)
    except NotFoundError:
        ctx.registry.register(
            session_id=session_id,
            agent=ctx.agent,
            client_type=client_type,
            provider=provider,
            project=ctx.cwd,
        )
    return session_id


# --------------------------------------------------------------- handlers


def handle_session_start(ctx: HookContext) -> int:
    session_id = ctx.session_id
    if session_id is None:
        log.warning("SessionStart with no session_id; cannot register a wake target")
        return 0
    ctx.registry.register(
        session_id=session_id,
        agent=ctx.agent,
        client_type="claude_code",
        provider="anthropic",
        project=ctx.cwd,
    )
    ctx.registry.prune()
    return 0


def handle_user_prompt_submit(ctx: HookContext) -> int:
    """A human typed something: the session is active and the budget resets.

    This is the circuit breaker's reset. Consecutive automatic wakes only
    accumulate while no person is involved.
    """
    session_id = _ensure_session(ctx, "claude_code", "anthropic")
    if session_id is not None:
        ctx.registry.touch(session_id, state="active", reset_auto_wakes=True)
    return 0


def handle_stop(ctx: HookContext) -> int:
    """The doorbell. Returns :data:`REWAKE_EXIT_CODE` to start a new turn."""
    session_id = _ensure_session(ctx, "claude_code", "anthropic")
    if session_id is None:
        log.warning("Stop with no session_id; cannot arm the doorbell")
        return 0

    ctx.registry.touch(session_id, state="idle")
    generation = ctx.registry.next_wake_generation(session_id)
    interval = max(poll_interval(), 0.5)
    deadline = time.monotonic() + DOORBELL_SECONDS
    log.info(
        "doorbell armed session=%s agent=%s generation=%d for %ds",
        session_id,
        ctx.agent,
        generation,
        DOORBELL_SECONDS,
    )

    while time.monotonic() < deadline:
        # A newer turn armed a newer doorbell: retire rather than ring twice.
        current = ctx.registry.current_wake_generation(session_id)
        if current is None or current != generation:
            log.info(
                "doorbell superseded session=%s generation=%d -> %s",
                session_id,
                generation,
                current,
            )
            return 0

        # Mode 1 wins over Mode 2. An in-turn wait_for_event will resolve on
        # its own; a second injected turn would duplicate the work.
        if ctx.store.has_active_wait(ctx.agent):
            time.sleep(interval)
            continue

        pending = ctx.store.pending_wake_messages(ctx.agent)
        if pending:
            session = ctx.registry.get(session_id)
            if session.auto_wakes >= MAX_CONSECUTIVE_AUTO_WAKES:
                log.warning(
                    "doorbell suppressed session=%s: %d consecutive auto wakes with no "
                    "human input; %d message(s) left in the inbox",
                    session_id,
                    session.auto_wakes,
                    len(pending),
                )
                return 0

            # Claim the messages first. If another doorbell got there, the
            # update touches nothing and we do not ring.
            claimed = ctx.store.mark_wake_notified([m.id for m in pending])
            if claimed == 0:
                log.info("doorbell lost the claim race session=%s", session_id)
                continue

            ctx.registry.record_auto_wake(session_id)
            log.info(
                "doorbell ringing session=%s agent=%s messages=%d ids=%s",
                session_id,
                ctx.agent,
                len(pending),
                ",".join(m.id for m in pending),
            )
            # stderr is what Claude Code shows the model as a system reminder.
            print(notification_text(ctx.agent, pending), file=sys.stderr)
            return REWAKE_EXIT_CODE

        time.sleep(interval)

    log.info("doorbell expired session=%s generation=%d", session_id, generation)
    return 0


def handle_stop_failure(ctx: HookContext) -> int:
    raw_type = ctx.payload.get("error_type")
    message = ctx.payload.get("error_message")

    if isinstance(raw_type, str) and raw_type in CLAUDE_STOP_FAILURE_TYPES:
        kind = raw_type
        detail = message if isinstance(message, str) else None
    else:
        kind = "unknown"
        detail = f"undocumented error_type {raw_type!r}"
        if isinstance(message, str):
            detail += f": {message}"

    record = ctx.store.record_failure(ctx.agent, kind, detail=detail, source="self")
    log.info("recorded failure %s; availability is now %s", kind, record.status)
    return 0


def handle_session_end(ctx: HookContext) -> int:
    reason = ctx.payload.get("end_reason")
    if not isinstance(reason, str) or reason not in CLAUDE_SESSION_END_REASONS:
        log.warning("SessionEnd with unrecognised end_reason %r; ignoring", reason)
        return 0

    session_id = ctx.session_id
    if session_id is not None:
        ctx.registry.close(session_id)

    status = SESSION_END_TO_STATUS.get(reason)
    if status is None:
        # clear / resume: the session is being recycled, not going away.
        return 0
    ctx.store.set_status(ctx.agent, status, reason=f"SessionEnd: {reason}", source="self")
    return 0


def handle_statusline(ctx: HookContext) -> int:
    """Record quota usage from Claude Code's official status line JSON.

    Usage is a metric. It never changes availability.
    """
    limits = ctx.payload.get("rate_limits")
    if not isinstance(limits, dict):
        return 0

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
        return 0

    percent, window, resets_at = best
    ctx.store.record_usage(
        ctx.agent,
        percent=percent,
        window=window,
        resets_at=_epoch_to_iso(resets_at),
        source="claude_statusline",
    )
    log.info("recorded usage %s=%.0f%%", window, percent)
    return 0


def _epoch_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
            timespec="microseconds"
        )
    except (OverflowError, OSError, ValueError):
        return None


HANDLERS = {
    "SessionStart": handle_session_start,
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "StopFailure": handle_stop_failure,
    "SessionEnd": handle_session_end,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge-hook",
        description=(
            "Claude Code hook receiver. Reads the hook JSON on stdin and updates "
            "agent-bridge session, availability, failure, and usage state. The Stop "
            "handler is the idle-wake doorbell and exits 2 when peer mail arrives."
        ),
    )
    parser.add_argument(
        "--agent", help="Which agent this client is. Defaults to $AGENT_BRIDGE_AGENT."
    )
    parser.add_argument(
        "--statusline",
        action="store_true",
        help="Treat stdin as status line JSON and record rate_limits as a usage sample.",
    )
    parser.add_argument(
        "--doorbell-seconds",
        type=int,
        default=None,
        help=f"Override how long the Stop doorbell stays armed (default {DOORBELL_SECONDS}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Exits 0, or 2 from the Stop doorbell to wake the model."""
    args = build_parser().parse_args(argv)
    try:
        raw = sys.stdin.read()
    except OSError as exc:  # pragma: no cover
        print(f"agent-bridge-hook: could not read stdin: {exc}", file=sys.stderr)
        return 0

    try:
        config = ServerConfig.resolve(args.agent)
        configure_logging(config.agent, log_path())

        if args.doorbell_seconds is not None:
            global DOORBELL_SECONDS
            DOORBELL_SECONDS = max(1, args.doorbell_seconds)

        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")

        ctx = HookContext(
            store=MessageStore(database_path()),
            registry=SessionRegistry(database_path()),
            agent=config.agent,
            payload=payload,
        )

        if args.statusline:
            return handle_statusline(ctx)

        event = payload.get("hook_event_name")
        handler = HANDLERS.get(event) if isinstance(event, str) else None
        if handler is None:
            log.debug("no handler for hook_event_name %r", event)
            return 0
        return handler(ctx)
    except Exception as exc:  # noqa: BLE001
        # Logged with a traceback, never swallowed silently -- but never
        # propagated either, because this process is attached to a live
        # Claude Code session.
        logging.getLogger("agent_bridge.hooks").exception("hook failed")
        print(f"agent-bridge-hook: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
