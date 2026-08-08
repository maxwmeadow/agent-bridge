from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from agent_bridge.config import AGENT_ENV, AGENTS_ENV, HOME_ENV
from agent_bridge.store import MessageStore


@pytest.fixture(autouse=True)
def bridge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every test at a throwaway data directory, never the real one."""
    home = tmp_path / "bridge-home"
    home.mkdir()
    monkeypatch.setenv(HOME_ENV, str(home))
    monkeypatch.delenv(AGENT_ENV, raising=False)
    monkeypatch.delenv(AGENTS_ENV, raising=False)
    yield home


@pytest.fixture
def db_path(bridge_home: Path) -> Path:
    return bridge_home / "agent-bridge.db"


@pytest.fixture
def store(db_path: Path) -> MessageStore:
    return MessageStore(db_path)
