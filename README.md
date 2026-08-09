# agent-bridge

A persistent local mailbox that lets Claude Code and Codex leave messages for
each other, so you stop being the copy-paste relay between two chat panels.

Both agents keep running as their normal selves in the VS Code GUI, on their
own subscription logins. agent-bridge is a small MCP server they both call.

```
                       agent-bridge
                  ~/.agent-bridge/agent-bridge.db
                  ┌──────────────────────────────┐
                  │  agents · threads · messages │
                  │  read/unread state           │
                  └───────────┬──────────────────┘
                              │  same SQLite file
              ┌───────────────┴───────────────┐
              │                               │
   agent-bridge-mcp                  agent-bridge-mcp
   AGENT_BRIDGE_AGENT                AGENT_BRIDGE_AGENT
       = claude                          = codex
        (stdio)                           (stdio)
              │                               │
     Claude Code window              Claude Code window
       or Codex panel                  or Codex panel
   subscription login               subscription login
              │                               │
              └──────────  Git repo  ─────────┘
```

Two processes, one database. Each process's identity is fixed when it starts,
from its environment or an explicit flag, so no tool ever exposes a `from`
parameter and neither model can send mail as the other. The same registration
serves every window; only the environment differs.

## What it does

- Stores messages between agents in SQLite, so a message waits until it is read.
- Exposes eight MCP tools: send, check inbox, read, reply, mark read, list
  threads, read thread, status.
- Keeps replies in threads, so a review conversation stays together.
- Carries optional context (`project`, `working_directory`, `git_branch`,
  `git_commit`) that the *sender* supplies.
- Ships a debug CLI so you can inspect everything without either AI client.

## What it explicitly does NOT do

- **No idle wake-up for Codex.** Codex exposes no lifecycle hook that can
  start a turn, so its sessions are never wake targets and `wait_for_event`
  remains its mechanism. Claude Code *does* wake automatically — see
  [Automatic wake-up](#automatic-wake-up).
- **No model traffic.** It never calls the Anthropic or OpenAI API and needs no
  API key. It is a local tool the clients invoke, nothing more.
- **No orchestration.** No schedulers, no tmux, no process management, no
  autonomous loops, no worktrees.
- **No execution.** Message bodies are stored and returned as data. The bridge
  never runs anything a message contains and never touches your repositories.
- **No network.** stdio transport only. It opens no socket and sends no
  telemetry.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repo> agent-bridge
cd agent-bridge
uv tool install --editable .
```

That installs two executables into `~/.local/bin` (`%USERPROFILE%\.local\bin`
on Windows):

- `agent-bridge-mcp` — the MCP server, for the clients to launch
- `agent-bridge` — the debug CLI, for you

`--editable` means edits in this repo take effect without reinstalling. Drop
it if you would rather pin a copy.

## Database location

`~/.agent-bridge/agent-bridge.db`, with `agent-bridge.log` beside it.

Deliberately outside any source repository: one mailbox serves every project
you work in. Override with `AGENT_BRIDGE_HOME` if you need to (the tests do).

```bash
agent-bridge paths     # print both paths
```

## Register with Claude Code

Run once, from any terminal (the standalone `claude` CLI must be installed —
the VS Code extension does not put it on your PATH):

```bash
claude mcp add --scope user --transport stdio agent-bridge \
  -- "$HOME/.local/bin/agent-bridge-mcp"
```

On Windows, use the full path to the `.exe`:

```powershell
claude mcp add --scope user --transport stdio agent-bridge -- "C:\Users\<you>\.local\bin\agent-bridge-mcp.exe"
```

No `--agent` here: identity comes from `AGENT_BRIDGE_AGENT`, so one
registration serves every Claude Code window. Set that variable per VS Code
profile - see [One config, many windows](#one-config-many-windows).

`--scope user` makes it available in every project. Verify:

```bash
claude mcp list        # expect: agent-bridge: … ✔ Connected
```

The VS Code extension reads the same configuration. Type `/mcp` in the chat
panel to confirm it shows up there too.

## Register with Codex

```bash
codex mcp add agent-bridge -- "$HOME/.local/bin/agent-bridge-mcp" --agent codex
codex mcp list
```

On Windows, use the full `.exe` path as above. If `codex` is not on your PATH,
the VS Code extension bundles it:

```
~/.vscode/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe
```

Either way it writes to `~/.codex/config.toml`, which the CLI, the IDE
extension, and the ChatGPT desktop app all read. The equivalent hand-written
entry is:

```toml
[mcp_servers.agent-bridge]
command = 'C:\Users\<you>\.local\bin\agent-bridge-mcp.exe'
args = ["--agent", "codex"]
```

**Each client must have a different identity.** That is the whole identity
model, and getting it wrong is the one setup mistake that matters. Supply it
with `--agent`, or leave the flag off and set `AGENT_BRIDGE_AGENT` per client
— see [One config, many windows](#one-config-many-windows).

## VS Code GUI usage

After registering, keep using the Claude Code and Codex panels exactly as
before. No terminal wrapper, no separate UI. Restart or reload VS Code once so
each extension picks up the new server.

A session looks like this:

> **You → Claude:** Ask Codex to implement the canvas virtualization work.
> *Claude calls `send_message`.*
>
> **You → Codex:** Check your agent-bridge inbox.
> *Codex calls `check_inbox`, reads it, does the work, calls `reply` with the commit hash.*
>
> **You → Claude:** Check your inbox.
> *Claude calls `check_inbox` and reviews.*

## MCP tools

| Tool | Purpose |
| --- | --- |
| `send_message(to, subject, body, context?, intent?, requires_response?)` | Start a new thread with the other agent |
| `check_inbox(unread_only=true, limit=20)` | Compact listing of messages addressed to you |
| `read_message(message_id, mark_read=true)` | Full body plus thread info |
| `reply(message_id, body, context?, intent?, requires_response?)` | Answer in the same thread, back to the sender |
| `mark_read(message_id)` | Clear something you already handled |
| `list_threads(limit=10)` | Recent conversations, participants, unread counts |
| `read_thread(thread_id, limit=50)` | Every message in one thread, oldest first |
| `bridge_status()` | Identity, database path, schema version, unread counts |
| `wait_for_event(timeout_seconds=60, wake_on_peer_status=true)` | Block until something happens instead of polling |
| `wait_for_mail(...)` | Deprecated alias for `wait_for_event`, identical behaviour |
| `set_status(status, reason?, resume_after?)` | Report your own availability |
| `peer_status()` | What the other agent last reported |

### Waiting instead of polling

`wait_for_mail` blocks the tool call until one of these happens, and says
which in its `reason`:

| Reason | Meaning |
| --- | --- |
| `message_received` | Unread mail is waiting. Returns the inbox listing |
| `peer_unavailable` | A peer reported `usage_exhausted`, `auth_error`, `client_closed`, or `unresponsive` |
| `peer_available` | A peer that was not available reported itself available again |
| `cancelled` | Someone ran `agent-bridge cancel-wait` |
| `timeout` | Nothing happened in the allotted time |
| `bridge_shutdown` | The server is stopping. Nothing is lost |
| `goal_cancelled` | Reserved for the future goal system; nothing produces it yet |

If mail is already waiting, it returns immediately — it never blocks on
something that already happened. A wait does not consume or mark messages, so
two agents waiting on the same inbox both see it.

Timeouts are clamped server-side to 1–600 seconds. Keep them at or under
**120 seconds**: past two minutes, Claude Code v2.1.212+ moves the call to a
background task, and the agent stops waiting inline and gets notified later
instead. The wait emits a progress notification every 15 seconds so clients
can see it is alive.

### Availability

Statuses are **reported, never inferred**:

`available` · `busy` · `waiting` · `usage_exhausted` · `auth_error` ·
`client_closed` · `unresponsive` · `unknown`

Each record carries `last_seen_at` (observed by the bridge),
`status_changed_at`, who reported it, an optional human-readable `reason`, and
an optional `resume_after`.

The bridge will never decide an agent is out of quota because it has gone
quiet. Silence shows up as an observation ("not seen for 40 minutes") next to
the unchanged reported status. See
[docs/research.md](docs/research.md#detecting-usage-exhaustion-and-client-failure)
for the official signals that could feed these statuses, and why none of them
are wired up automatically yet.

Any status may follow any other. No transition graph is enforced: a client can
die in any state, and rejecting a "wrong" transition would only preserve a
staler record than the one being rejected.

## One config, many windows

Identity resolves in this order, with no default:

1. an explicit `--agent` argument;
2. the `AGENT_BRIDGE_AGENT` environment variable;
3. **failure** — a clear error, never a guess.

There is no fallback on purpose. Two Claude Code windows can run the identical
command, so guessing would silently let one send mail as the other, which is
the single failure this design exists to prevent.

Because identity comes from the environment, **one MCP registration and one
hook block serve every window**. Register them without `--agent`:

```bash
claude mcp add --scope user --transport stdio agent-bridge -- "…\agent-bridge-mcp.exe"
```

Then set the variable per VS Code profile, under
**Extensions → Claude Code → Environment Variables**
(`claudeCode.environmentVariables`):

| Profile | Setting |
| --- | --- |
| Your normal profile | `AGENT_BRIDGE_AGENT=claude` |
| A second profile | `AGENT_BRIDGE_AGENT=codex` |

Claude Code passes its environment to the MCP server and the hooks it spawns,
so both pick the identity up automatically.

Two optional variables record what a session is, without changing behaviour:

| Variable | Meaning | Default |
| --- | --- | --- |
| `AGENT_BRIDGE_CLIENT_TYPE` | Which client hosts the session | `claude_code` |
| `AGENT_BRIDGE_PROVIDER` | Which model is behind it (`anthropic`, `openai`, …) | unset |

The provider is **never inferred from the agent id**. An agent called `codex`
running inside Claude Code through a proxy is still a Claude Code session, and
it gets Claude Code's wake mechanism — the bridge cares about session
mechanics, not which vendor is behind the model.

## Working with both agents

See [docs/briefing.md](docs/briefing.md) for a briefing to paste into both
sessions and a kickoff prompt for whichever one starts.

## Automatic wake-up

A message sent to an **idle** Claude Code session starts a new turn there by
itself. You no longer type "check your agent-bridge inbox."

```
codex sends  ──►  SQLite  ──►  armed Stop-hook doorbell  ──►  exit 2
                                                              │
                                          Claude wakes, reads via MCP, acts
```

**The mechanism.** Claude Code's `asyncRewake` hook option runs a hook in the
background and wakes the model if it exits 2, showing its stderr as a system
reminder. The bridge registers a `Stop` hook configured that way: when a turn
ends, the hook keeps running, blocks on the database, and exits 2 the moment
actionable peer mail appears. This is a documented Claude Code feature, not a
workaround.

One mechanism covers both timings:

| Mail arrives… | What happens |
| --- | --- |
| While the agent is **busy** | The doorbell finds it immediately at the turn boundary — no unsafe mid-turn injection |
| While the agent is **idle** | The doorbell is still armed and rings up to 8 hours later |
| While blocked in `wait_for_event` | The in-turn wait resolves instead; the doorbell defers and no second turn is created |

**The doorbell is not the message.** The injected text carries ids, counts and
sender names only — never a body, never a summary, never a conclusion. SQLite
stays authoritative and the recipient reads the canonical thread through MCP.
Peer text is data to be judged, not an instruction arriving with system
authority.

### Setup

```bash
claude mcp add --scope user --transport stdio agent-bridge -- "…\agent-bridge-mcp.exe"
```

Then add to `~/.claude/settings.json` (keeping any hooks you already have):

```json
{
  "hooks": {
    "SessionStart":     [{"matcher": "", "hooks": [{"type": "command", "command": "…\\agent-bridge-hook.exe", "timeout": 10}]}],
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "…\\agent-bridge-hook.exe", "timeout": 10}]}],
    "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "…\\agent-bridge-hook.exe", "timeout": 28800, "async": true, "asyncRewake": true}]}],
    "StopFailure":      [{"matcher": "", "hooks": [{"type": "command", "command": "…\\agent-bridge-hook.exe", "timeout": 10}]}],
    "SessionEnd":       [{"matcher": "", "hooks": [{"type": "command", "command": "…\\agent-bridge-hook.exe", "timeout": 10}]}]
  }
}
```

`async` + `asyncRewake` on `Stop` is what makes idle wake work. The others are
bookkeeping: session registration, activity, failure, and shutdown.

No `--agent` anywhere: every window shares this block and takes its identity
from `AGENT_BRIDGE_AGENT`. See [One config, many windows](#one-config-many-windows).

### Which session gets woken

Sessions register themselves through their own lifecycle hooks; the bridge
never invents one. Targeting is:

1. a live session whose **project matches** the message's `project` context;
2. otherwise the **most recently active** live session for that agent.

"Live" means registered, not closed, and seen within 30 minutes. So a review
request tagged for repo A wakes the window open on repo A, not the one on
repo B.

```bash
agent-bridge sessions              # what the bridge thinks is alive
agent-bridge wake-target claude    # who would be woken, and why
agent-bridge pending-wakes         # mail that would ring a doorbell
agent-bridge session prune         # drop closed and long-stale sessions
```

### Three brakes against runaway loops

Automatic delivery makes acknowledgement ping-pong a real risk, so there are
three independent stops:

1. **Explicit silence.** Every message wakes the peer unless the sender passes
   `requires_response=false`. Intent is a label for the reader and does not
   suppress delivery on its own — labelling a reply `info` while asking a
   question used to notify nobody, which is worse than one wake too many.
2. **Announce-once.** Each message rings at most one doorbell, and several
   messages coalesce into a single wake. A restart cannot re-ring for mail
   already announced.
3. **Circuit breaker.** After 6 consecutive automatic wakes with no human
   input, wakes are suppressed and mail simply waits. Typing anything clears
   it.

The third brake needed a subtlety: Claude Code delivers the injected wake
through the same path as a typed prompt, so `UserPromptSubmit` fires for the
bridge's own injection about 113 ms later. A prompt that soon after a wake is
treated as its echo, not as human input — otherwise the breaker would reset
itself every cycle and never engage.

### Availability, failure, and usage are three different things

They are stored separately and must not be conflated:

| Concern | Question it answers | Example |
| --- | --- | --- |
| **Availability** | Can this agent make progress now? | `usage_exhausted` |
| **Failure** | What went wrong last, in the client's own words? | `rate_limit`, `billing_error`, `overloaded` |
| **Usage** | How much quota is consumed? | 71% of `seven_day`, resets at ... |

**A high usage percentage is not unavailability.** An agent at 99% of its
window is still available; only a reported failure or an explicit status makes
it otherwise. `record_usage` cannot change availability at all.

Failures keep Claude Code's own vocabulary rather than collapsing into one
bucket, and only project onto availability through a documented map:

| `StopFailure.error_type` | Availability becomes |
| --- | --- |
| `rate_limit`, `billing_error` | `usage_exhausted` |
| `authentication_failed`, `oauth_org_not_allowed` | `auth_error` |
| `overloaded`, `server_error` | `unresponsive` — **never** `usage_exhausted` |
| `invalid_request`, `model_not_found`, `max_output_tokens`, `unknown` | unchanged; the failure is recorded, availability is not touched |

### Feeding availability automatically (Claude Code)

`agent-bridge-hook` is a local process that reads Claude Code's hook JSON on
stdin. **No model turn, no tokens, no cost** — the peer blocked in
`wait_for_event` learns about a rate limit on its next poll.

Register it with the other lifecycle hooks in `~/.claude/settings.json` — see
the single hook block under [Setup](#setup). The same block covers session
registration, the idle-wake doorbell, failures, and shutdown, and carries no
`--agent`, so every profile shares it.

The hook uses only documented fields — `error_type` and
`error_message` on `StopFailure`, `end_reason` on `SessionEnd`. An
`error_type` outside the documented set is recorded as `unknown` with the raw
value in the detail rather than guessed at.

Optionally, chain the same binary in front of your status line with
`--statusline` to record `rate_limits.*.used_percentage` and capture
`resets_at` as a real `resume_after`. It prints nothing.

A hook must never break the session it is attached to, so every failure path
exits 0 after logging.

### Feeding usage from Codex (optional, off by default)

Codex has no official machine-readable usage feed. It does write
`~/.codex/sessions/**/rollout-*.jsonl`, whose `token_count` events carry a
`rate_limits` payload. `agent-bridge codex-usage` reads it:

```bash
agent-bridge codex-usage            # print a sample, change nothing
agent-bridge codex-usage --apply    # also store it as a usage metric
```

**Classification: local but undocumented.** No network calls, no credentials,
no private OpenAI endpoints — only files Codex already wrote to this machine.
But the format is unpublished and can change without notice, so nothing runs
it automatically, every parse failure returns nothing, and messaging and
waiting do not depend on it at all. If a Codex upgrade breaks the format, the
bridge carries on unchanged.

Results are readable text, not JSON blobs, and always include the ids needed
for the next call:

```
1 unread message for codex:

[msg_01KZG1BWJDEY6YFXSACENYK3NT]  (UNREAD)
From:    claude
Subject: V1 bridge test
Sent:    2026-08-08 06:36:02Z
Thread:  thr_01KZG1BWJEXAAEVXVPHW1KTSY3
Preview: Hello from Claude.
```

## Debug CLI

```bash
agent-bridge status --agent claude
agent-bridge inbox claude
agent-bridge inbox codex --all --limit 5
agent-bridge threads
agent-bridge read msg_01KZG1BWJDEY6YFXSACENYK3NT
agent-bridge thread thr_01KZG1BWJEXAAEVXVPHW1KTSY3
agent-bridge send --from claude --to codex --subject "test" --body "hello"
agent-bridge send --from codex --to claude --subject "review" --body-file notes.md
agent-bridge reply msg_01KZG… --from codex --body "on it"
agent-bridge mark-read msg_01KZG… --agent codex
agent-bridge agents
agent-bridge paths

# availability and waiting
agent-bridge status                 # everything
agent-bridge status claude          # one agent's availability record
agent-bridge status codex
agent-bridge set-status codex usage_exhausted \
    --reason "5-hour limit" --resume-after 2026-08-08T18:00:00Z
agent-bridge wait claude --timeout 60      # exit 0 on an event, 3 on timeout
agent-bridge cancel-wait claude --reason "changed my mind"
```

`--from` is allowed here because a person at a terminal already decides who
they are. The MCP tools never accept it.

## Manual end-to-end verification

1. **Reset the view.** `agent-bridge status --agent claude`
2. **In the Claude Code panel:** "Send Codex a message through agent-bridge.
   Subject: V1 bridge test. Body: Hello from Claude."
   Claude should call `send_message` and report a `msg_…` id.
3. **Confirm from outside:** `agent-bridge inbox codex` shows one unread.
4. **In the Codex panel:** "Check your agent-bridge inbox."
   Codex should call `check_inbox` and see Claude's message.
5. **In the Codex panel:** "Reply to that message with: Hello from Codex."
   Codex should call `reply`.
6. **In the Claude Code panel:** "Check your inbox."
   Claude should see the reply, in the same `thr_…` thread.
7. **Confirm the thread:** `agent-bridge threads` — one thread, two messages,
   participants `claude, codex`.

If all seven pass, messaging works.

## Verifying blocking waits

First, a machine-driven version of the same experiments over real stdio, with
no chat panel involved:

```bash
uv run python scripts/wait_experiment.py
```

It runs a 30-second blocked call woken at 8 s, a 120-second blocked call woken
at 75 s, and a 20-second call that is allowed to time out, and it asserts the
wake latency.

Then the GUI versions, which are what actually prove an agent can sit blocked
in a panel:

1. **Reload first.** After upgrading, run **Developer: Reload Window**, or
   reconnect `agent-bridge` from the `/mcp` panel. Each client launched its
   server process at startup and will not have the new tools until it
   relaunches it.
2. **30-second blocked call.** In one panel: "Use wait_for_mail with a
   30-second timeout." It should sit in the tool call, then return `timeout`.
3. **Woken blocked call.** In panel A: "Wait for mail for 90 seconds." While
   it is blocked, in panel B: "Send the other agent a message saying the build
   is green." Panel A should return `message_received` within a second or two
   and **carry on in the same turn**.
4. **Peer status.** In panel A: "Wait for mail for 60 seconds." While blocked,
   run `agent-bridge set-status codex usage_exhausted --reason "test"`. Panel A
   should return `peer_unavailable`.
5. **Cancellation.** Start a 120-second wait, then run
   `agent-bridge cancel-wait <agent>`. It should return `cancelled` promptly.

Watch for two things: the panel should not spin the model while blocked (no
repeated turns), and the agent should keep working in the same turn after the
wait returns.

## Troubleshooting

**A client shows the server as failed.**
Run the exact configured command yourself, with the same environment:
`AGENT_BRIDGE_AGENT=claude agent-bridge-mcp`.
It should sit there silently waiting on stdin. Any error prints to stderr.
Ctrl-C to exit.

**Claude sees its own messages, or an agent looks like the wrong one.**
Its identity is wrong. Run `agent-bridge sessions` and check the `agent` line
for each window, then check `AGENT_BRIDGE_AGENT` in that profile's
`claudeCode.environmentVariables`. Two windows must never share a value.

**"No agent identity configured."**
That profile has no `AGENT_BRIDGE_AGENT` set. This is deliberate: the bridge
refuses to guess rather than risk two windows sharing one identity.

**"Unknown agent 'x'."**
Only `claude` and `codex` exist by default. Add more by setting
`AGENT_BRIDGE_AGENTS=claude,codex,gemini` in both clients' server environments.

**Nothing arrives.**
Both sides must point at the same database. `bridge_status` (or
`agent-bridge status`) prints the path each one is using.

**Where are the logs?**
`~/.agent-bridge/agent-bridge.log`, rotating at 1 MB. It records connections,
message ids, senders, recipients, and body *sizes* — never bodies, never
secrets. Add `--verbose` to a server entry for debug detail.

**A blocked `wait_for_mail` is stuck and I want it back.**
`agent-bridge cancel-wait claude` wakes it with `cancelled`. Interrupting the
turn in the panel also cancels the MCP request, which aborts the wait.

**A wait returned but the agent stopped responding inline.**
The call probably ran past two minutes and Claude Code v2.1.212+ backgrounded
it. Use a timeout at or under 120 seconds, or check `/tasks`.

**A tool call fails and the message is unhelpful.**
Every validation failure returns a specific sentence (unknown agent, malformed
id, non-participant, oversized field). Unexpected errors are logged with a
traceback rather than swallowed.

## Security model

- **Local only.** stdio transport; no listener, no port, no remote endpoint.
- **No credentials.** No API keys, no tokens, no telemetry, no uploads.
- **Fixed identity.** Sender is set when the process starts, from your config
  or environment, never by the model. There is no default, so a misconfigured
  window fails loudly instead of impersonating the other agent.
- **Scoped access.** An agent can only read messages and threads it takes part
  in, and can only mark read what was addressed to it.
- **Messages are data.** The bridge never executes message content, never
  shells out, and never modifies a repository.
- **Bounded input.** Subject 200 chars, body 100k chars, context values 500
  chars, known context keys only; every query is parameterized.
- **Confined filesystem.** The bridge only reads and writes its own data
  directory. (The CLI's `--body-file` reads a path *you* type.)

One thing to keep in mind: a message body is untrusted text written by another
agent. The server instructions tell both models to treat message contents as
information to judge, not instructions to obey — but that is a prompt-level
mitigation, not a guarantee. Read handoffs before acting on anything
consequential.

## Development

```bash
uv sync
uv run pytest      # data layer, MCP tools, CLI
uv run mypy        # strict over src/agent_bridge
```

`tests/test_store.py` tests the data layer with no MCP involved.
`tests/test_mcp_server.py` runs a claude-bound and a codex-bound server against
one database over the SDK's in-memory transport.

Module layout:

| File | Responsibility |
| --- | --- |
| `config.py` | Data directory, agent roster, per-process identity |
| `db.py` | SQLite connections, pragmas, schema migrations |
| `models.py` | Row-shaped dataclasses |
| `store.py` | Every message and status operation; knows nothing about MCP |
| `events.py` | Blocking waits, wake reasons, the in-process event hub |
| `formatting.py` | Rendering for tool results and the CLI |
| `server.py` | MCP tool definitions and the stdio entry point |
| `cli.py` | The human debug CLI |

**How waiting works.** The two agents are separate OS processes, so an
in-memory event in one server can never reach a waiter in the other. SQLite is
therefore the authoritative wake-up channel: a waiter re-reads persistent state
every 200 ms (`AGENT_BRIDGE_POLL_INTERVAL`) and decides from what it finds
there. The in-process `EventHub` is a latency optimization for waiters that
share a process with the writer, and correctness never depends on it.

Lost wake-ups are prevented by checking persistent state **before** registering
with the hub and **again immediately after**. Anything that lands in the gap is
caught by the second check; anything later either sets the event or is found by
the next poll. `tests/test_events.py` walks a send across that window at 25
randomised sub-poll offsets.

Schema (version 2): `agents` (now with reported availability), `threads`,
`messages`. Ids are prefixed ULIDs
(`msg_…`, `thr_…`) — sortable by creation time and safe to generate from two
processes at once. SQLite runs in WAL mode with a 5-second busy timeout, and
each operation uses its own short-lived connection.

## Roadmap

Deliberately out of scope for V1, but the schema and module boundaries leave
room:

- **Automatic delivery.** Waking an agent that is *idle*, rather than one that
  chose to block in `wait_for_mail`. Claude Code hooks or channels; whatever
  Codex ends up supporting.
- **Feeding availability automatically.** A `StopFailure` hook mapping
  `error_type` to `usage_exhausted` / `auth_error`, a status-line reader for
  `rate_limits.*.resets_at`, and an opt-in reader for Codex's local session
  rollout files. All researched, none wired up — see
  [docs/research.md](docs/research.md#detecting-usage-exhaustion-and-client-failure).
- **Goals and tasks.** `create_goal` / `update_goal`, `create_task` /
  `claim_task` / `complete_task` — new tables, additive migration.
- **Bounded autonomous collaboration.** Implement → review → fix → re-review
  without a human relay, with max handoffs, max runtime, explicit stop states,
  a disagreement escalation path, and an ask-user state.

None of it is started. V1 is the communication primitive, and it should earn
trust before anything hands work around on its own.
