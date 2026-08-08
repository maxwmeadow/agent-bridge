"""Local file logging.

Two rules that matter here:

1. Never write to stdout. On stdio transport, stdout is the MCP wire protocol.
2. Never log message bodies. Only ids, participants and sizes, so the log can
   answer "did Codex connect, and was the message stored?" without becoming a
   copy of the mailbox.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(agent)s] %(message)s"


class _AgentFilter(logging.Filter):
    def __init__(self, agent: str) -> None:
        super().__init__()
        self.agent = agent

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "agent"):
            record.agent = self.agent
        return True


def configure(agent: str, log_file: Path, *, verbose: bool = False) -> None:
    """Attach a rotating file handler plus a stderr handler for warnings."""
    root = logging.getLogger("agent_bridge")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    agent_filter = _AgentFilter(agent)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    file_handler.addFilter(agent_filter)
    root.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(_FORMAT))
    stderr_handler.addFilter(agent_filter)
    root.addHandler(stderr_handler)
