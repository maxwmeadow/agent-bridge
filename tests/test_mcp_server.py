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
    "wait_for_mail",
    "set_status",
    "peer_status",
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


def test_wait_for_mail_blocks_then_wakes_over_mcp(db_path: Path) -> None:
    """A blocked MCP tool call resolves when the other agent's server sends."""

    async def scenario() -> None:
        claude = make_server("claude", db_path)
        codex = make_server("codex", db_path)

        async with Client(claude) as claude_client, Client(codex) as codex_client:
            results: list[str] = []

            async with anyio.create_task_group() as tg:

                async def blocked_call() -> None:
                    results.append(
                        text_of(
                            await claude_client.call_tool(
                                "wait_for_mail", {"timeout_seconds": 20}
                            )
                        )
                    )

                async def sender() -> None:
                    await anyio.sleep(0.2)
                    await codex_client.call_tool(
                        "send_message",
                        {"to": "claude", "subject": "woke you", "body": "done implementing"},
                    )

                tg.start_soon(blocked_call)
                tg.start_soon(sender)

            assert "message_received" in results[0]
            assert "woke you" in results[0]

    anyio.run(scenario)


def test_wait_for_mail_times_out_over_mcp(db_path: Path) -> None:
    async def scenario() -> None:
        async with Client(make_server("claude", db_path)) as client:
            result = text_of(await client.call_tool("wait_for_mail", {"timeout_seconds": 1}))
            assert "timeout" in result

    anyio.run(scenario)


def test_peer_status_wakes_a_blocked_mcp_call(db_path: Path) -> None:
    async def scenario() -> None:
        claude = make_server("claude", db_path)
        codex = make_server("codex", db_path)

        async with Client(claude) as claude_client, Client(codex) as codex_client:
            results: list[str] = []

            async with anyio.create_task_group() as tg:

                async def blocked_call() -> None:
                    results.append(
                        text_of(
                            await claude_client.call_tool("wait_for_mail", {"timeout_seconds": 20})
                        )
                    )

                async def peer() -> None:
                    await anyio.sleep(0.2)
                    await codex_client.call_tool(
                        "set_status",
                        {
                            "status": "usage_exhausted",
                            "reason": "weekly cap reached",
                            "resume_after": "2026-08-09T00:00:00Z",
                        },
                    )

                tg.start_soon(blocked_call)
                tg.start_soon(peer)

            assert "peer_unavailable" in results[0]
            assert "weekly cap reached" in results[0]

    anyio.run(scenario)


def test_an_agent_can_only_set_its_own_status(db_path: Path) -> None:
    async def scenario() -> None:
        async with Client(make_server("codex", db_path)) as client:
            schema = {
                tool.name: tool.input_schema
                for tool in (await client.list_tools()).tools
            }["set_status"]
            # No agent parameter exists, so there is nothing to point elsewhere.
            assert set(schema.get("properties", {})) == {"status", "reason", "resume_after"}

            await client.call_tool("set_status", {"status": "busy", "reason": "implementing"})
            assert MessageStore(db_path).get_status("codex").status == "busy"
            # Setting its own status says nothing about the other agent.
            assert MessageStore(db_path).get_status("claude").status == "unknown"

        # Leaving the context stops the server, which is a direct observation
        # that this client went away.
        assert MessageStore(db_path).get_status("codex").status == "client_closed"

    anyio.run(scenario)
