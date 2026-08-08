"""Blocking-wait experiments over real stdio, against the real database.

Runs the same shape as the GUI experiments, but driven by an MCP client
instead of a chat panel: two separate `agent-bridge-mcp` processes with
different identities, one blocking in wait_for_mail while the other sends.

    uv run python scripts/wait_experiment.py

Measures how long each blocked call actually took and whether the wake was
prompt. Cleans up the mail it creates so the real mailbox is left as found.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

EXE = str(Path.home() / ".local" / "bin" / "agent-bridge-mcp.exe")
if not Path(EXE).exists():  # non-Windows fallback
    EXE = str(Path.home() / ".local" / "bin" / "agent-bridge-mcp")


def text_of(result: object) -> str:
    return "\n".join(b.text for b in result.content if b.type == "text")  # type: ignore[attr-defined]


class Agent:
    """One `agent-bridge-mcp --agent X` subprocess with an MCP session."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.progress_pings = 0

    async def __aenter__(self) -> "Agent":
        params = StdioServerParameters(
            command=EXE, args=["--agent", self.name], env=dict(os.environ)
        )
        self._transport = stdio_client(params)
        read, write = await self._transport.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._session.__aexit__(*exc)  # type: ignore[arg-type]
        await self._transport.__aexit__(*exc)  # type: ignore[arg-type]

    async def call(self, tool: str, args: dict[str, object] | None = None) -> str:
        async def on_progress(progress: float, total: float | None, message: str | None) -> None:
            self.progress_pings += 1

        result = await self._session.call_tool(
            tool, args or {}, progress_callback=on_progress
        )
        return text_of(result)


async def experiment(label: str, timeout: int, send_after: float | None) -> None:
    print(f"\n=== {label} ===", flush=True)
    async with Agent("claude") as claude, Agent("codex") as codex:
        await claude.call("check_inbox", {})  # ensure a clean start
        outcome: list[str] = []
        started = time.monotonic()

        async with anyio.create_task_group() as tg:

            async def blocked() -> None:
                outcome.append(await claude.call("wait_for_mail", {"timeout_seconds": timeout}))

            async def sender() -> None:
                if send_after is None:
                    return
                await anyio.sleep(send_after)
                print(f"  [{time.monotonic() - started:6.1f}s] codex sends", flush=True)
                await codex.call(
                    "send_message",
                    {
                        "to": "claude",
                        "subject": f"{label} wake",
                        "body": "Sent while the other side was blocked.",
                    },
                )

            tg.start_soon(blocked)
            tg.start_soon(sender)

        elapsed = time.monotonic() - started
        reason = outcome[0].splitlines()[0]
        print(f"  [{elapsed:6.1f}s] {reason}", flush=True)
        print(f"  progress notifications received: {claude.progress_pings}", flush=True)
        if send_after is not None:
            lag = elapsed - send_after
            print(f"  wake latency after send: {lag * 1000:.0f} ms", flush=True)
            assert "message_received" in outcome[0], outcome[0]
            assert lag < 2.0, f"wake took {lag:.2f}s"
            # Leave the mailbox as we found it.
            message_id = outcome[0].split("[")[-1].split("]")[0]
            await claude.call("mark_read", {"message_id": message_id})
        else:
            assert "timeout" in outcome[0], outcome[0]


async def main() -> None:
    print(f"executable: {EXE}")
    # 1. Short blocked call woken by the peer partway through.
    await experiment("30s blocked call, peer sends at 8s", timeout=30, send_after=8.0)
    # 2. Long blocked call woken well past the one-minute mark.
    await experiment("120s blocked call, peer sends at 75s", timeout=120, send_after=75.0)
    # 3. Nothing arrives: the bounded timeout must fire on its own.
    await experiment("20s blocked call, nothing sent", timeout=20, send_after=None)
    print("\nAll stdio wait experiments passed.")


if __name__ == "__main__":
    try:
        anyio.run(main)
    except AssertionError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
