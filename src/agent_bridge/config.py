"""Where the bridge keeps its data, and who is allowed to use it.

Everything here is local. No network endpoints, no credentials, no telemetry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import UnknownAgentError, ValidationError

#: Environment variable overriding the data directory (useful for tests).
HOME_ENV = "AGENT_BRIDGE_HOME"
#: Environment variable setting this process's agent identity.
AGENT_ENV = "AGENT_BRIDGE_AGENT"
#: Environment variable extending the agent roster, comma separated.
AGENTS_ENV = "AGENT_BRIDGE_AGENTS"

DEFAULT_AGENTS: tuple[str, ...] = ("claude", "codex")

#: Agent ids are deliberately boring so they read well in tool output.
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 100_000
MAX_CONTEXT_VALUE_CHARS = 500
MAX_REASON_CHARS = 500

#: Availability states an agent can report for itself.
#:
#: These are *reported*, never inferred. In particular the bridge never decides
#: an agent is ``usage_exhausted`` because it has been quiet: only an explicit
#: report from the agent, its client, or the operator sets that.
AGENT_STATUSES: tuple[str, ...] = (
    "available",  # ready for work
    "busy",  # working on something, still alive
    "waiting",  # blocked in wait_for_event
    "usage_exhausted",  # subscription limit hit; see resume_after
    "auth_error",  # login expired or rejected
    "client_closed",  # the editor/panel went away cleanly
    "unresponsive",  # someone observed it failing to answer
    "unknown",  # never reported anything
)

#: Statuses that mean "do not expect progress from this agent right now".
#:
#: Note what is absent: nothing about how much quota has been consumed. A
#: agent at 99% of its window is still perfectly available.
UNAVAILABLE_STATUSES: frozenset[str] = frozenset(
    {"usage_exhausted", "auth_error", "client_closed", "unresponsive"}
)

# --- failure vocabulary ----------------------------------------------------
#: Claude Code's documented ``StopFailure.error_type`` values, verbatim.
#: Source: https://code.claude.com/docs/en/hooks (StopFailure -> Input).
CLAUDE_STOP_FAILURE_TYPES: tuple[str, ...] = (
    "rate_limit",
    "overloaded",
    "authentication_failed",
    "oauth_org_not_allowed",
    "billing_error",
    "invalid_request",
    "model_not_found",
    "server_error",
    "max_output_tokens",
    "unknown",
)

#: Claude Code's documented ``SessionEnd.end_reason`` values, verbatim.
CLAUDE_SESSION_END_REASONS: tuple[str, ...] = (
    "clear",
    "resume",
    "logout",
    "prompt_input_exit",
    "bypass_permissions_disabled",
    "other",
)

#: Failure kinds the bridge stores. The Claude values are kept as-is rather
#: than folded together, so "we ran out of quota", "the card was declined",
#: "the login expired" and "Anthropic had a bad minute" stay distinguishable.
FAILURE_KINDS: tuple[str, ...] = CLAUDE_STOP_FAILURE_TYPES + ("client_error",)

#: How a failure projects onto *availability*. This is a coarsening, which is
#: exactly why the raw failure kind is stored alongside it.
#:
#: Anything absent from this map records the failure without touching
#: availability: a malformed request or an output-length cap says nothing
#: about whether the agent can work on the next thing.
FAILURE_TO_STATUS: dict[str, str] = {
    "rate_limit": "usage_exhausted",
    # Distinct failure, same practical consequence: the account cannot spend.
    "billing_error": "usage_exhausted",
    "authentication_failed": "auth_error",
    "oauth_org_not_allowed": "auth_error",
    # Provider trouble is explicitly NOT usage exhaustion.
    "overloaded": "unresponsive",
    "server_error": "unresponsive",
    "client_error": "unresponsive",
}

#: Usage windows the bridge understands, per source.
USAGE_WINDOWS: tuple[str, ...] = ("five_hour", "seven_day", "primary", "secondary")

#: Where a usage sample came from, and how much to trust it.
USAGE_SOURCES: tuple[str, ...] = (
    "claude_statusline",  # official Claude Code status line JSON
    "codex_rollout",  # Codex local session files; undocumented, best effort
    "manual",  # typed in by a person
)


def require_intent(intent: str) -> str:
    normalized = intent.strip().lower()
    if normalized not in MESSAGE_INTENTS:
        raise ValidationError(
            f"Unknown intent {intent!r}. Valid intents: {', '.join(MESSAGE_INTENTS)}."
        )
    return normalized


def default_requires_response(intent: str) -> bool:
    """Whether an intent wants the peer woken, absent an explicit choice."""
    return intent not in NON_WAKING_INTENTS


def require_failure_kind(kind: str) -> str:
    normalized = kind.strip().lower()
    if normalized not in FAILURE_KINDS:
        raise ValidationError(
            f"Unknown failure kind {kind!r}. Valid kinds: {', '.join(FAILURE_KINDS)}."
        )
    return normalized

# --- wait_for_event bounds -------------------------------------------------
#: Server-side clamp. A caller cannot ask to block forever.
MAX_WAIT_SECONDS = 600
MIN_WAIT_SECONDS = 1
DEFAULT_WAIT_SECONDS = 60
#: Claude Code moves a main-conversation MCP call to a background task once it
#: passes this mark (v2.1.212+). Waits at or under it stay in the same turn.
SAME_TURN_WAIT_SECONDS = 120

#: How often a waiter re-reads SQLite. This is what makes cross-process
#: wake-ups work at all: an in-process event cannot reach the other agent's
#: server, which is a separate OS process.
POLL_INTERVAL_ENV = "AGENT_BRIDGE_POLL_INTERVAL"
DEFAULT_POLL_INTERVAL = 0.2
#: How often a blocked call emits an MCP progress notification, so clients can
#: see it is alive and their idle timers stay reset.
HEARTBEAT_SECONDS = 15.0


def poll_interval() -> float:
    """Seconds between persistent-state re-checks while waiting."""
    raw = os.environ.get(POLL_INTERVAL_ENV)
    if not raw:
        return DEFAULT_POLL_INTERVAL
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError(f"{POLL_INTERVAL_ENV} must be a number, got {raw!r}.") from exc
    if not 0.001 <= value <= 5.0:
        raise ValidationError(f"{POLL_INTERVAL_ENV} must be between 0.001 and 5 seconds.")
    return value


# --- sessions and wake-up ---------------------------------------------------
#: Client session lifecycle states. Reported by the client's own hooks.
SESSION_STATES: tuple[str, ...] = (
    "active",  # generating or running tools
    "idle",  # turn ended, sitting at the prompt, doorbell armed
    "waiting",  # blocked inside wait_for_event
    "closed",  # SessionEnd fired
    "stale",  # not seen for too long
)

#: How a session can be woken. One real adapter today, room for another.
WAKE_METHODS: tuple[str, ...] = (
    "stop_hook_rewake",  # Claude Code asyncRewake Stop hook
    "none",  # no idle wake available (Codex today)
)

#: A session not seen for this long is treated as stale and never targeted.
SESSION_STALE_SECONDS = 30 * 60

#: Message intents. Optional and backward compatible; existing rows read as
#: ``handoff``.
MESSAGE_INTENTS: tuple[str, ...] = (
    "info",
    "question",
    "proposal",
    "review_request",
    "review_result",
    "handoff",
    "blocker",
    "objection",
    "decision",
)

#: Intents that do not wake the peer by default. These are the ones that end
#: an exchange rather than continue it -- the ping-pong killers.
NON_WAKING_INTENTS: frozenset[str] = frozenset({"info", "decision", "review_result"})

#: Circuit breaker. Consecutive automatic wakes allowed for one session with
#: no human input in between. Reset by UserPromptSubmit. This is the backstop
#: that stops two agents from politely acknowledging each other forever.
MAX_CONSECUTIVE_AUTO_WAKES = 6

#: How long the Stop-hook doorbell stays armed after a turn ends. Kept just
#: inside Claude Code's 600s default command-hook timeout so the waiter
#: retires on its own terms rather than being killed mid-claim.
DOORBELL_SECONDS = 570


def require_valid_status(status: str) -> str:
    """Normalize and check an availability status."""
    normalized = status.strip().lower()
    if normalized not in AGENT_STATUSES:
        raise ValidationError(
            f"Unknown status {status!r}. Valid statuses: {', '.join(AGENT_STATUSES)}."
        )
    return normalized


def clamp_wait_seconds(seconds: float) -> float:
    """Clamp a requested wait to the server-side bounds."""
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        raise ValidationError("timeout_seconds must be a finite number.")
    return float(min(max(seconds, MIN_WAIT_SECONDS), MAX_WAIT_SECONDS))


def data_dir() -> Path:
    """Return the bridge's data directory, creating it if needed."""
    override = os.environ.get(HOME_ENV)
    root = Path(override).expanduser() if override else Path.home() / ".agent-bridge"
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return data_dir() / "agent-bridge.db"


def log_path() -> Path:
    return data_dir() / "agent-bridge.log"


def known_agents() -> tuple[str, ...]:
    """Return the agent roster.

    Defaults to ``claude`` and ``codex``. Additional agents can be added with
    ``AGENT_BRIDGE_AGENTS=claude,codex,gemini`` without touching the schema.
    """
    raw = os.environ.get(AGENTS_ENV)
    if not raw:
        return DEFAULT_AGENTS
    agents = tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))
    for agent in agents:
        if not _AGENT_RE.match(agent):
            raise ValidationError(
                f"Invalid agent id {agent!r} in {AGENTS_ENV}. "
                "Use lowercase letters, digits, '-' or '_'."
            )
    if not agents:
        return DEFAULT_AGENTS
    return agents


def require_known_agent(agent: str) -> str:
    """Normalize an agent id and check it against the roster."""
    normalized = agent.strip().lower()
    roster = known_agents()
    if normalized not in roster:
        raise UnknownAgentError(
            f"Unknown agent {agent!r}. Known agents: {', '.join(roster)}."
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Immutable per-process configuration for the MCP server."""

    agent: str
    db_path: Path
    log_file: Path

    @staticmethod
    def resolve(agent: str | None) -> "ServerConfig":
        """Resolve identity from the ``--agent`` flag or ``AGENT_BRIDGE_AGENT``.

        Identity is fixed when the process starts. Tools never accept a sender
        argument, so a model cannot impersonate the other agent.
        """
        candidate = agent or os.environ.get(AGENT_ENV)
        if not candidate:
            raise ValidationError(
                "No agent identity configured. Pass --agent claude (or codex), "
                f"or set {AGENT_ENV}."
            )
        return ServerConfig(
            agent=require_known_agent(candidate),
            db_path=database_path(),
            log_file=log_path(),
        )
