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
