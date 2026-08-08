"""agent-bridge: a local, persistent message bridge between coding agents.

The bridge stores messages in SQLite and exposes them over MCP. It never talks
to any model provider and never executes message content.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
