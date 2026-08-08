"""ULID-style identifiers.

Ids are lexicographically sortable by creation time, collision-safe without
coordination, and readable enough for a human or an LLM to copy from tool
output: ``msg_01K7Q8Z4M0V3TB9YH2C5RD6EWX``.
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_CHARS = 10  # 48 bits of milliseconds
_RANDOM_CHARS = 16  # 80 bits of randomness

MESSAGE_PREFIX = "msg_"
THREAD_PREFIX = "thr_"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, remainder = divmod(value, 32)
        chars.append(_CROCKFORD[remainder])
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Return a new prefixed ULID."""
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    return prefix + _encode(timestamp, _TIME_CHARS) + _encode(secrets.randbits(80), _RANDOM_CHARS)


def new_message_id() -> str:
    return new_id(MESSAGE_PREFIX)


def new_thread_id() -> str:
    return new_id(THREAD_PREFIX)


def looks_like_id(value: str, prefix: str) -> bool:
    """Cheap shape check so bad ids fail with a clear message, not a silent miss."""
    if not value.startswith(prefix):
        return False
    body = value[len(prefix) :]
    return len(body) == _TIME_CHARS + _RANDOM_CHARS and all(c in _CROCKFORD for c in body)
