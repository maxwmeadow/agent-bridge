"""MCP-level smoke tests.

These run two servers -- one bound to ``claude``, one to ``codex`` -- against
the same database, over the SDK's in-memory transport. That is the same shape
as the real setup, minus the stdio pipes.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from mcp import Client

from agent_bridge.config import ServerConfig
from agent_bridge.errors import ValidationError
from agent_bridge.server import build_server
from agent_bridge.store import MessageStore

EXPECTED_TOOLS = {
    "send_message",
    "check_inbox",
    "read_message",
    "reply",
    "mark_read",
    "list_threads",
    "read_thread",
    "bridge_status",
}


def make_server(agent: str, db_path: Path):  # type: ignore[no-untyped-def]
    config = ServerConfig.resolve(agent)
    return build_server(config, MessageStore(db_path))


def text_of(result) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(block.text for block in result.content if block.type == "text")


def test_tool_surface(db_path: Path) -> None:
    async def scenario() -> None:
        async with Client(make_server("claude", db_path)) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
            # No tool lets a model choose its own sender.
            for tool in tools.tools:
                assert "from" not in tool.input_schema.get("properties", {})
                assert "sender" not in tool.input_schema.get("properties", {})

    anyio.run(scenario)


def test_round_trip_between_two_agent_servers(db_path: Path) -> None:
    """The V1 acceptance path: Claude sends, Codex replies, Claude sees it."""

    async def scenario() -> None:
        claude = make_server("claude", db_path)
        codex = make_server("codex", db_path)

        async with Client(claude) as claude_client:
            sent = text_of(
                await claude_client.call_tool(
                    "send_message",
                    {"to": "codex", "subject": "V1 bridge test", "body": "Hello from Claude."},
                )
            )
            assert "Sent to codex" in sent

        async with Client(codex) as codex_client:
            inbox = text_of(await codex_client.call_tool("check_inbox", {}))
            assert "V1 bridge test" in inbox
            assert "From:    claude" in inbox

            message_id = inbox.split("[")[1].split("]")[0]
            full = text_of(
                await codex_client.call_tool("read_message", {"message_id": message_id})
            )
            assert "Hello from Claude." in full

            # read_message marked it read by default.
            assert "No unread messages for codex" in text_of(
                await codex_client.call_tool("check_inbox", {})
            )

            reply = text_of(
                await codex_client.call_tool(
                    "reply", {"message_id": message_id, "body": "Hello from Codex."}
                )
            )
            assert "Sent to claude" in reply

        async with Client(claude) as claude_client:
            inbox = text_of(await claude_client.call_tool("check_inbox", {}))
            assert "Hello from Codex." in inbox
            assert "From:    codex" in inbox

            threads = text_of(await claude_client.call_tool("list_threads", {}))
            thread_id = threads.split("[thr_")[1].split("]")[0]
            thread = text_of(
                await claude_client.call_tool("read_thread", {"thread_id": "thr_" + thread_id})
            )
            # Both messages, oldest first, in one thread.
            assert thread.index("Hello from Claude.") < thread.index("Hello from Codex.")

    anyio.run(scenario)


def test_tool_errors_are_reported_not_swallowed(db_path: Path) -> None:
    async def scenario() -> None:
        async with Client(make_server("claude", db_path)) as client:
            result = await client.call_tool(
                "send_message", {"to": "nobody", "subject": "s", "body": "b"}
            )
            assert result.is_error
            assert "Unknown agent 'nobody'" in text_of(result)

            result = await client.call_tool("read_message", {"message_id": "bogus"})
            assert result.is_error
            assert "is not a message id" in text_of(result)

    anyio.run(scenario)


def test_bridge_status_reports_identity_and_database(db_path: Path) -> None:
    async def scenario() -> None:
        async with Client(make_server("codex", db_path)) as client:
            status = text_of(await client.call_tool("bridge_status", {}))
            assert "this agent:     codex" in status
            assert str(db_path) in status
            assert "known agents:   claude, codex" in status

    anyio.run(scenario)


def test_identity_must_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_BRIDGE_AGENT", raising=False)
    with pytest.raises(ValidationError, match="No agent identity configured"):
        ServerConfig.resolve(None)


def test_identity_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BRIDGE_AGENT", "codex")
    assert ServerConfig.resolve(None).agent == "codex"
    # An explicit flag wins over the environment.
    assert ServerConfig.resolve("claude").agent == "claude"
