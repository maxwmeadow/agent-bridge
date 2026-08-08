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
     --agent claude                    --agent codex
        (stdio)                           (stdio)
              │                               │
        Claude Code                        Codex
      VS Code panel                    VS Code panel
   subscription login               subscription login
              │                               │
              └──────────  Git repo  ─────────┘
```

Two processes, one database. Each process's identity is fixed by its
`--agent` flag, so no tool ever exposes a `from` parameter and neither model
can send mail as the other.

## What it does

- Stores messages between agents in SQLite, so a message waits until it is read.
- Exposes eight MCP tools: send, check inbox, read, reply, mark read, list
  threads, read thread, status.
- Keeps replies in threads, so a review conversation stays together.
- Carries optional context (`project`, `working_directory`, `git_branch`,
  `git_commit`) that the *sender* supplies.
- Ships a debug CLI so you can inspect everything without either AI client.

## What it explicitly does NOT do

- **No wake-up.** A new message does not start a turn in the other panel. You
  still say "check your agent-bridge inbox." That is deliberate for V1.
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
  -- "$HOME/.local/bin/agent-bridge-mcp" --agent claude
```

On Windows, use the full path to the `.exe`:

```powershell
claude mcp add --scope user --transport stdio agent-bridge -- "C:\Users\<you>\.local\bin\agent-bridge-mcp.exe" --agent claude
```

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

**Note the different `--agent` value in each client.** That flag is the whole
identity model. Getting it wrong is the one setup mistake that matters.

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
| `send_message(to, subject, body, context?)` | Start a new thread with the other agent |
| `check_inbox(unread_only=true, limit=20)` | Compact listing of messages addressed to you |
| `read_message(message_id, mark_read=true)` | Full body plus thread info |
| `reply(message_id, body, context?)` | Answer in the same thread, back to the sender |
| `mark_read(message_id)` | Clear something you already handled |
| `list_threads(limit=10)` | Recent conversations, participants, unread counts |
| `read_thread(thread_id, limit=50)` | Every message in one thread, oldest first |
| `bridge_status()` | Identity, database path, schema version, unread counts |

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

If all seven pass, V1 works.

## Troubleshooting

**A client shows the server as failed.**
Run the exact configured command yourself: `agent-bridge-mcp --agent claude`.
It should sit there silently waiting on stdin. Any error prints to stderr.
Ctrl-C to exit.

**Claude sees its own messages, or an agent looks like the wrong one.**
Its `--agent` flag is wrong. Check `claude mcp list` and `codex mcp list`; the
two entries must use different values.

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

**A tool call fails and the message is unhelpful.**
Every validation failure returns a specific sentence (unknown agent, malformed
id, non-participant, oversized field). Unexpected errors are logged with a
traceback rather than swallowed.

## Security model

- **Local only.** stdio transport; no listener, no port, no remote endpoint.
- **No credentials.** No API keys, no tokens, no telemetry, no uploads.
- **Fixed identity.** Sender comes from the process's `--agent` flag, decided
  by your config file, not by the model.
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
| `store.py` | Every message operation; knows nothing about MCP |
| `formatting.py` | Rendering for tool results and the CLI |
| `server.py` | MCP tool definitions and the stdio entry point |
| `cli.py` | The human debug CLI |

Schema (version 1): `agents`, `threads`, `messages`. Ids are prefixed ULIDs
(`msg_…`, `thr_…`) — sortable by creation time and safe to generate from two
processes at once. SQLite runs in WAL mode with a 5-second busy timeout, and
each operation uses its own short-lived connection.

## Roadmap

Deliberately out of scope for V1, but the schema and module boundaries leave
room:

- **Automatic delivery.** Push new messages into a running session (Claude Code
  hooks or channels; whatever Codex ends up supporting). Nothing in the current
  design forbids a watcher process.
- **Goals and tasks.** `create_goal` / `update_goal`, `create_task` /
  `claim_task` / `complete_task` — new tables, additive migration.
- **Bounded autonomous collaboration.** Implement → review → fix → re-review
  without a human relay, with max handoffs, max runtime, explicit stop states,
  a disagreement escalation path, and an ask-user state.

None of it is started. V1 is the communication primitive, and it should earn
trust before anything hands work around on its own.
