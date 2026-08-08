# Research notes — what V1 assumes and how it was checked

Checked 2026-08-08 against primary sources. Findings are tagged:

- **Known supported** — stated in official documentation.
- **Verified** — executed on this machine and observed working.
- **Assumed** — not yet checked; needs manual confirmation.

---

## Claude Code

**Sources**
- <https://code.claude.com/docs/en/mcp> (the CLI docs redirect here from `docs.claude.com`)
- <https://code.claude.com/docs/en/vs-code>

| Finding | Status |
| --- | --- |
| stdio MCP servers are added with `claude mcp add [options] <name> -- <command> [args...]`; everything after `--` is passed to the server untouched | Known supported |
| Scopes are `local` (default, per project), `project` (`.mcp.json` in the repo), and `user` (all projects). Local and user scope both live in `~/.claude.json` | Known supported |
| Environment variables go through `-e`/`--env KEY=value`, before the server name | Known supported |
| The VS Code extension shares MCP configuration with the CLI. The docs' feature table lists MCP config as "Partial (add servers via CLI; manage existing servers with `/mcp` in the chat panel)" | Known supported |
| `claude mcp add --scope user --transport stdio agent-bridge -- …agent-bridge-mcp.exe --agent claude` succeeded, and `claude mcp list` reports `agent-bridge: … ✔ Connected` | Verified (CLI) |
| The same server appears and connects inside the VS Code chat panel | Assumed — needs manual check via `/mcp` in the panel |

Note: the extension does **not** put `claude` on your PATH. A standalone CLI
install is what makes `claude mcp add` available. On this machine the CLI is
already installed at `~/.local/bin/claude` (v2.1.204).

---

## Codex

**Sources**
- <https://learn.chatgpt.com/docs/extend/mcp> (`developers.openai.com/codex/mcp` redirects here)

| Finding | Status |
| --- | --- |
| MCP servers live in `~/.codex/config.toml` under `[mcp_servers.<name>]` with `command`, `args`, optional `cwd`, and `[mcp_servers.<name>.env]` | Known supported |
| Servers can be added with `codex mcp add <name> -- <command> [args...]`, listed with `codex mcp list` | Known supported |
| stdio and streamable HTTP are both supported transports | Known supported |
| "The ChatGPT desktop app, Codex CLI, and IDE extension all reference the same configuration file" | Known supported |
| `codex mcp add agent-bridge -- …agent-bridge-mcp.exe --agent codex` succeeded; `codex mcp list` shows it `enabled` | Verified (CLI) |
| Codex in the VS Code panel lists and calls the agent-bridge tools | Assumed — needs manual check in the Codex panel |

On this machine there is no standalone `codex` on PATH. The VS Code extension
(`openai.chatgpt-26.803.41515-win32-x64`) bundles one at
`~/.vscode/extensions/openai.chatgpt-…/bin/windows-x86_64/codex.exe`, and that
binary reads and writes the same `~/.codex/config.toml`. That is the binary
used for registration.

---

## MCP SDK

| Finding | Status |
| --- | --- |
| Latest `mcp` Python package is **2.0.0** (requires Python ≥ 3.10), a rewrite that targets the 2026-07-28 MCP spec | Known supported |
| `mcp.server.fastmcp.FastMCP` is gone in 2.0. The decorator-based server is now `mcp.server.mcpserver.MCPServer`, with `@server.tool(...)` and `server.run("stdio")` | Verified (introspected the installed package) |
| `mcp.Client` accepts an `MCPServer` instance directly for in-memory testing | Verified (used by `tests/test_mcp_server.py`) |
| Tool annotations are evaluated to build input schemas, so `from __future__ import annotations` breaks descriptions built from closure variables | Verified (hit and fixed; see the comment at the top of `server.py`) |

---

## End-to-end checks performed here

| Check | Status |
| --- | --- |
| Two separate `agent-bridge-mcp` OS processes (`--agent claude`, `--agent codex`) over real stdio, sharing one SQLite file: send → check_inbox → reply → check_inbox, same thread | Verified |
| Same round trip over the SDK's in-memory transport, as an automated test | Verified |
| Registration with both clients from the command line | Verified |
| Either client's **GUI panel** actually calling the tools | Assumed — this is the manual step in the README |

---

## Deliberately not relied upon

- No Anthropic or OpenAI API key. The bridge never contacts a model provider,
  so both clients keep using their own subscription login.
- No network transport. stdio only; the bridge opens no socket.
- No wake-up mechanism. Claude Code hooks/channels and any Codex equivalent
  were read about but not used — V1 requires the user to prompt the recipient.
