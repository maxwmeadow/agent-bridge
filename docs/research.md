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

## Blocking MCP calls (added for the wait release)

Checked 2026-08-08 against <https://code.claude.com/docs/en/mcp>.

| Finding | Status |
| --- | --- |
| A tool call that sends no response **and no progress notification** for the idle window is aborted. For **stdio** servers the idle window defaults to **30 minutes** | Known supported |
| The wall-clock limit is the per-server `timeout`, or `MCP_TOOL_TIMEOUT`, which defaults to **about 28 hours** when unset. Progress notifications do not extend it | Known supported |
| Stdio servers have **no** per-request (first-byte) timer; that 60-second timer applies only to HTTP/SSE/connector servers | Known supported |
| On **v2.1.212+**, a main-conversation MCP call still running after **two minutes** moves to a background task. Claude gets a task id immediately, keeps working, and the result arrives later as a task notification | Known supported |
| This machine runs Claude Code **v2.1.204**, so backgrounding does not apply yet and a two-minute call stays inline in the turn | Verified |

**What this means for `wait_for_mail`.** A blocked wait is safe well past two
minutes on stdio, but the default timeout is 60 s and the tool's own
description steers callers to stay at or under 120 s
(`SAME_TURN_WAIT_SECONDS`). Past that mark, a future Claude Code upgrade will
background the call and the agent will *not* continue the same turn — it will
be notified later instead. The wait also emits a progress notification every
15 s, which is both a liveness signal and idle-timer insurance.

No equivalent documented idle/backgrounding behaviour was found for Codex;
that is one of the things the GUI experiments exist to establish.

---

## Detecting usage exhaustion and client failure

The requirement is to never infer `usage_exhausted` from silence. These are the
mechanisms that could feed a *reported* status instead.

### Claude Code

| Mechanism | What it gives you | Classification |
| --- | --- | --- |
| **`StopFailure` hook** | Fires when a turn ends from an API error. Re-verified 2026-08-08 against the documented input table, not inferred: the fields are **`error_type`** and **`error_message`**, and `error_type` is one of `rate_limit`, `overloaded`, `authentication_failed`, `oauth_org_not_allowed`, `billing_error`, `invalid_request`, `model_not_found`, `server_error`, `max_output_tokens`, `unknown` | **Official / stable.** This is the mechanism that cleanly separates usage exhaustion (`rate_limit`, `billing_error`) from ordinary server trouble (`overloaded`, `server_error`) and from auth failure (`authentication_failed`, `oauth_org_not_allowed`) |
| **Status line `rate_limits`** | `rate_limits.five_hour.used_percentage` / `.resets_at` and `rate_limits.seven_day.used_percentage` / `.resets_at`; `resets_at` is Unix epoch seconds. Present only for Claude.ai Pro/Max subscribers, and only after the first API response of a session | **Official / stable.** Gives an early warning and an exact `resume_after`, without waiting for a failure |
| **`/usage`** | Interactive dialog showing plan, session and weekly bars, and reset times | **Official but interactive.** Not machine-readable; unsuitable as a feed |
| **`SessionEnd` hook** | Documented input field is **`end_reason`**, one of `clear`, `resume`, `logout`, `prompt_input_exit`, `bypass_permissions_disabled`, `other` | **Official / stable.** A clean source for `client_closed`; `logout` maps to `auth_error`; `clear` and `resume` are session churn and change nothing |

**Implemented** in `agent_bridge/hooks.py`, registered in
`~/.claude/settings.json` as `StopFailure` and `SessionEnd`. The mapping:

| Signal | Status to report |
| --- | --- |
| `StopFailure` `error_type=rate_limit` or `billing_error` | `usage_exhausted`, `resume_after` from the status line's `resets_at` |
| `StopFailure` `error_type=authentication_failed` / `oauth_org_not_allowed` | `auth_error` |
| `StopFailure` `error_type=overloaded` / `server_error` | **not** `usage_exhausted` — a transient provider problem, at most `unresponsive` |
| `SessionEnd` | `client_closed` |
| Status line `five_hour.used_percentage` ≥ ~95 | early `usage_exhausted` warning with `resets_at` as `resume_after` |

### Codex

| Mechanism | What it gives you | Classification |
| --- | --- | --- |
| **`/status` in the Codex TUI** | Current rate-limit state for the active session | **Official but interactive.** Not machine-readable |
| **`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`** | `token_count` events carrying a `rate_limits` payload. Confirmed on this machine: `{"limit_id":"codex","primary":{"used_percent":48.0,"window_minutes":10080,"resets_at":<epoch>},"secondary":null,"credits":{...},"plan_type":"plus","rate_limit_reached_type":null}` | **Local but undocumented.** No network, no credentials, purely reading files Codex already writes. The field names are not in any published schema and can change without notice |
| **`rate_limits` in `codex exec` JSONL output** | Would be the clean machine-readable path | **Not available.** Still an open feature request upstream (openai/codex issue #14728) |
| **`chatgpt.com/backend-api/…` usage endpoints** | Used by some third-party dashboards | **Private / reverse-engineered.** Requires reusing your ChatGPT session credentials against an undocumented endpoint |
| **`xiangz19/codex-ratelimit`** and similar trackers | Verified: reads the local session JSONL only, no network calls, no credentials | **Local but undocumented**, same class as the rollout files |

**Decision: the rollout reader ships as opt-in; the private endpoint does not
ship at all.**

`agent_bridge/codex_usage.py` reads the local rollout files. It is never
called automatically — only by `agent-bridge codex-usage`, and it only writes
anything with `--apply`. Every failure mode returns `None`; messaging and
waiting have no dependency on it. Verified against real files on this machine:
48.0% of a 10080-minute (weekly) window, `plan_type: plus`,
`rate_limit_reached_type: null`.

That last field is what separates "consuming quota" from "limit hit", and the
adapter surfaces it as `limit_reached` rather than inferring exhaustion from
the percentage.

The `chatgpt.com/backend-api` route is **not implemented and will not be
without an explicit instruction** — it is a private endpoint requiring reuse
of ChatGPT session credentials.

---

## GUI experiment results (blocking waits)

Run 2026-08-08 in the Claude Code VS Code panel on v2.1.204, against the real
database, with the peer sending via the CLI.

| Experiment | Result | Status |
| --- | --- | --- |
| 30 s wait, nothing sent | Returned `timeout` at exactly 30.0 s | Verified (GUI) |
| 90 s wait, peer sends at ~20 s | Returned `message_received` at 16.3 s, sub-second after the send landed; the agent continued in the **same turn** | Verified (GUI) |
| 170 s wait, peer sends at ~140 s | Returned `message_received` at **133.1 s**, inline, **same turn** — no backgrounding on v2.1.204 | Verified (GUI) |
| Blocked call spinning the model | Did not happen; one tool call, no repeated turns | Verified (GUI) |

The >2-minute result confirms the documented boundary: backgrounding arrives
in v2.1.212, so this inline behaviour will change on upgrade. Keep production
waits at or under 120 s.

### Codex panel

Run 2026-08-08 in the Codex VS Code panel (`openai.chatgpt` extension,
codex-cli 0.147.0-alpha.6.5).

| Experiment | Result | Status |
| --- | --- | --- |
| 30 s wait, nothing sent | Blocked for the full 30 s, returned `timeout` | Verified (GUI) |
| 150 s wait, peer sends mid-wait | Returned `message_received` after **15.4 s**, then read the message and replied **in the same turn** — total turn 43 s, no re-prompt | Verified (GUI) |
| Blocked call spinning the model | Did not happen | Verified (GUI) |

Confirmed from the other side: the reply landed 11 s after the send, in the
same thread, with the wake itself sub-second.

**Conclusion: a Codex MCP client tolerates a blocked stdio tool call and
resumes work inline when it returns.** That is the premise of this release,
now verified on both clients.

Still unverified: Codex behaviour past the two-minute mark (no documented
backgrounding equivalent is known either way), and whether `StopFailure`
fires end-to-end — it needs a genuine API error, which cannot be manufactured
on demand. The handler is unit-tested and was pipe-tested with real
documented payloads.

---

## Deliberately not relied upon

- No Anthropic or OpenAI API key. The bridge never contacts a model provider,
  so both clients keep using their own subscription login.
- No network transport. stdio only; the bridge opens no socket.
- No wake-up mechanism. Claude Code hooks/channels and any Codex equivalent
  were read about but not used — V1 requires the user to prompt the recipient.
