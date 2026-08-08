"""Error types shared by the store, the MCP server, and the CLI."""


class BridgeError(Exception):
    """Base class for errors that are safe to show to a user or an agent."""


class UnknownAgentError(BridgeError):
    """An agent id was used that is not in the configured roster."""


class NotFoundError(BridgeError):
    """A message or thread id does not exist."""


class PermissionDeniedError(BridgeError):
    """An agent tried to act on a message or thread it is not part of."""


class ValidationError(BridgeError):
    """Caller-supplied input failed validation."""
