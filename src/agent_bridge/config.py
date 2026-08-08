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
UNAVAILABLE_STATUSES: frozenset[str] = frozenset(
    {"usage_exhausted", "auth_error", "client_closed", "unresponsive"}
)

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
