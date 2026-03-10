# claude-controller

A Slack-based controller that listens for commands in a Slack channel and dispatches Claude Code agents to execute tasks. Send a prompt from Slack, and claude-controller spawns a Claude Code CLI session (or forwards to a live tmux session), streaming results back to the same channel.

## Architecture

```
Slack channel
    |  (poll every N seconds via MCP)
    v
┌─────────────────────────┐
│  Poller                 │  Watches for "claude <prompt>" or "c <prompt>" messages
│  ├─ SlackMCPClient      │  JSON-RPC 2.0 client talking to a Slack MCP Docker container
│  ├─ ClaudeSession       │  Spawns `claude -p` CLI subprocess (default mode)
│  ├─ TmuxSession         │  Sends keystrokes to a live tmux pane (optional mode)
│  └─ LogTailer           │  Tails Claude's JSONL conversation log by byte offset
└─────────────────────────┘
```

**Key modules:**

| File | Purpose |
|---|---|
| `config.py` | All configuration — environment variables and defaults |
| `main.py` | Entry point, signal handling, startup/shutdown lifecycle |
| `poller.py` | Polls Slack for new messages, parses commands, dispatches handlers |
| `slack_mcp.py` | Manages the Slack MCP Docker container (JSON-RPC over stdio) |
| `claude_session.py` | Spawns and manages Claude Code CLI as a subprocess, streams NDJSON output |
| `tmux_session.py` | Sends commands to / captures output from a tmux pane |
| `log_tailer.py` | Tails `~/.claude/projects/**/*.jsonl` by byte offset for structured updates |
| `ansi_to_slack.py` | Converts ANSI terminal output (24-bit RGB, bold, dim) to Slack mrkdwn |

**Two execution modes:**

1. **Subprocess mode** (default) — Each prompt spawns a fresh `claude -p <prompt> --output-format stream-json` process. Output is streamed back to Slack in real time as assistant text, tool calls, and results arrive. Runs with `--permission-mode acceptEdits` so file edits are auto-accepted.
2. **Tmux mode** — If `TMUX_TARGET` is set, prompts are typed into an existing interactive Claude Code session running in tmux. Use `c -update` to pull new output from Claude's JSONL log (no tmux capture or ANSI parsing needed).

## Installation

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/), and Docker (for the Slack MCP server container).

```bash
# Clone the repo
git clone git@github.com:sabaailab/claude-controller.git
cd claude-controller

# Install with uv (creates .venv automatically)
uv sync
```

The `claude` CLI (Claude Code) must be available on your `PATH`.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_MCP_XOXC_TOKEN` | Yes | — | Slack `xoxc-` session token for the MCP server |
| `SLACK_MCP_XOXD_TOKEN` | Yes | — | Slack `xoxd-` cookie token for the MCP server |
| `CONTROLLER_SLACK_CHANNEL_ID` | Yes | — | Slack channel or DM ID to poll for commands and post responses |
| `SLACK_MCP_ADD_MESSAGE_TOOL` | Yes | — | Must be set to `true` or to a comma-separated list of channel IDs the MCP server is allowed to write to. The control channel **must** be included or the controller cannot post responses. Example: `true` (allow all) or `D09412DATSL,C0123ABCDEF` (restrict to specific channels) |
| `POLL_INTERVAL` | No | `3` | Seconds between polling Slack for new messages |
| `CLAUDE_CWD` | No | Current directory | Working directory for spawned Claude Code processes |
| `CLAUDE_MODEL` | No | Default model | Model to pass to `claude -p --model` |
| `CLAUDE_SESSION_ID` | No | — | Resume an existing Claude Code session on startup |
| `TMUX_TARGET` | No | — | tmux target pane (e.g. `claude:0.0`). Enables tmux mode instead of subprocess mode |

## Usage

### Starting the controller

A channel ID is always required — via `CONTROLLER_SLACK_CHANNEL_ID` env var or `--channel` flag.

```bash
# Using env var
uv run claude-controller

# Using flag
uv run claude-controller --channel C0123ABCDEF
```

### Tmux mode (recommended for live sessions)

Tmux mode forwards prompts to a live interactive Claude Code session. This is the typical setup for ongoing work.

**Step 1 — Start Claude Code in a tmux session:**

```bash
# Create a new tmux session named "claude"
tmux new -s claude

# Inside the tmux shell, start Claude Code
claude                      # starts interactive Claude Code
```

Detach from tmux with `Ctrl-b d` (Claude keeps running in the background).

**Step 2 — Start the controller pointing at that tmux pane:**

```bash
# Using env var
TMUX_TARGET=claude:0.0 uv run claude-controller

# Or using flag
uv run claude-controller --tmux claude:0.0
```

`claude:0.0` refers to session `claude`, window `0`, pane `0`. The controller types prompts directly into that pane and pulls new output by tailing Claude's JSONL conversation log (not tmux capture).

Now send commands from Slack — they'll be forwarded to the live Claude session in tmux.

### Slack commands

All commands use the prefix `claude` or the shorthand `c`. Type them in the watched Slack channel.

| Command | Description |
|---|---|
| `claude <prompt>` or `c <prompt>` | Send a prompt to Claude Code |
| `c -update` / `c -u` | Show new output since last check (reads JSONL log in tmux mode; shows session status in subprocess mode) |
| `c -interactive [on\|off]` / `c -i` | Toggle auto-update mode — automatically sends new log output every poll cycle |
| `c -stop` | Stop the running Claude Code session |
| `c -sessions` | List the 10 most recent Claude Code sessions from `~/.claude/projects/` |
| `c -resume <id>` | Attach to an existing session by ID |
| `c -help` | Show available commands |

Flags support prefix matching — `c -u` matches `-update`, `c -s` is ambiguous between `-stop` and `-sessions` and will prompt you.

### CLI options

```bash
uv run claude-controller --help
```

| Flag | Description |
|---|---|
| `--channel CHANNEL_ID` | Slack channel/DM ID to poll and respond in (overrides `CONTROLLER_SLACK_CHANNEL_ID`) |
| `--tmux TARGET` | tmux target pane, e.g. `claude:0.0` (overrides `TMUX_TARGET`). Enables tmux mode |
| `--cwd DIR` | Working directory for spawned Claude Code processes (overrides `CLAUDE_CWD`) |
| `--poll-interval SECONDS` | Seconds between polls (overrides `POLL_INTERVAL`, default: 3) |
| `--help` | Show usage information and exit |

## How It Works

1. On startup, claude-controller launches the Slack MCP server (`ghcr.io/korotovsky/slack-mcp-server:v1.1.28`) as a Docker container and connects via JSON-RPC 2.0 over stdio.
2. The poller reads the last message timestamp to avoid replaying history, then enters a polling loop.
3. Each poll fetches the 3 most recent messages from the configured Slack channel. New messages starting with `claude` or `c` are parsed as commands.
4. **Subprocess mode:** Prompts are run via `claude -p <prompt> --output-format stream-json --permission-mode acceptEdits`. Output is streamed back to Slack in real time — assistant text, tool call summaries (✏️ Edit, 💻 Bash, etc.), and a final cost/token summary.
5. **Tmux mode:** Prompts are typed into the live tmux pane via `tmux send-keys`. The `-update` and `-interactive` commands tail Claude's JSONL conversation log (`~/.claude/projects/**/*.jsonl`) by byte offset for structured, reliable output — no ANSI parsing or tmux capture involved.
6. The controller handles transient MCP connection failures with exponential backoff and automatic container restarts after 3 consecutive errors.
7. Graceful shutdown on SIGINT/SIGTERM — stops active sessions and the MCP container.
