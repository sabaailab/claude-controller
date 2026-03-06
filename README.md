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
│  └─ TmuxSession         │  Sends keystrokes to a live tmux pane (optional mode)
└─────────────────────────┘
```

**Key modules:**

| File | Purpose |
|---|---|
| `config.py` | All configuration — environment variables and defaults |
| `main.py` | Entry point, signal handling, startup/shutdown lifecycle |
| `poller.py` | Polls Slack for new messages, parses commands, dispatches handlers |
| `slack_mcp.py` | Manages the Slack MCP Docker container (JSON-RPC over stdio) |
| `claude_session.py` | Spawns and manages Claude Code CLI as a subprocess |
| `tmux_session.py` | Sends commands to / captures output from a tmux pane |

**Two execution modes:**

1. **Subprocess mode** (default) — Each prompt spawns a fresh `claude -p <prompt>` process. Output is posted back to Slack when complete.
2. **Tmux mode** — If `TMUX_TARGET` is set, prompts are typed into an existing interactive Claude Code session running in tmux. Use `c -update` to get new terminal output.

## Installation

Requires Python 3.10+ and Docker (for the Slack MCP server container).

```bash
# Clone the repo
git clone <repo-url>
cd claude-controller

# Create a virtual environment and install
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

The `claude` CLI (Claude Code) must be available on your `PATH`.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_MCP_XOXC_TOKEN` | Yes | — | Slack `xoxc-` session token for the MCP server |
| `SLACK_MCP_XOXD_TOKEN` | Yes | — | Slack `xoxd-` cookie token for the MCP server |
| `CONTROLLER_SLACK_CHANNEL_ID` | No | `D09412DATSL` | Slack channel or DM ID to poll for commands and post responses |
| `POLL_INTERVAL` | No | `3` | Seconds between polling Slack for new messages |
| `CLAUDE_CWD` | No | Current directory | Working directory for spawned Claude Code processes |
| `CLAUDE_MODEL` | No | Default model | Model to pass to `claude -p --model` |
| `CLAUDE_SESSION_ID` | No | — | Resume an existing Claude Code session on startup |
| `TMUX_TARGET` | No | — | tmux target pane (e.g. `claude:0.0`). Enables tmux mode instead of subprocess mode |

## Usage

### Starting the controller

```bash
# Basic — polls your DM channel
claude-controller

# Poll a specific channel (env var or flag)
CONTROLLER_SLACK_CHANNEL_ID=C0123ABCDEF claude-controller
claude-controller --channel C0123ABCDEF

# Tmux mode — forward prompts to a live Claude session
TMUX_TARGET=claude:0.0 claude-controller
```

### Slack commands

All commands use the prefix `claude` or the shorthand `c`. Type them in the watched Slack channel.

| Command | Description |
|---|---|
| `claude <prompt>` or `c <prompt>` | Send a prompt to Claude Code |
| `c -update` or `c -u` | Show new output since last check |
| `c -stop` | Stop the running Claude Code session |
| `c -sessions` | List recent Claude Code sessions |
| `c -resume <id>` | Attach to an existing session by ID |
| `c -help` | Show available commands |

Flags support prefix matching — `c -u` matches `-update`, `c -s` is ambiguous between `-stop` and `-sessions` and will prompt you.

### CLI options

```bash
claude-controller --help            # Show all options
claude-controller --channel C0XYZ   # Override the Slack channel to poll
```

| Flag | Description |
|---|---|
| `--channel CHANNEL_ID` | Slack channel ID to poll (overrides `CONTROLLER_SLACK_CHANNEL_ID` env var) |
| `--help` | Show usage information and exit |

## How It Works

1. On startup, claude-controller launches a Slack MCP server as a Docker container and connects via JSON-RPC 2.0 over stdio.
2. The poller reads the last message timestamp to avoid replaying history, then enters a polling loop.
3. Each poll fetches recent messages from the configured Slack channel. New messages starting with `claude` or `c` are parsed as commands.
4. Prompts are dispatched to either a Claude Code subprocess or a tmux pane depending on the configured mode.
5. Responses (and cost/token stats in subprocess mode) are posted back to the same Slack channel.
6. The controller handles transient MCP connection failures with exponential backoff and automatic container restarts after 3 consecutive errors.
7. Graceful shutdown on SIGINT/SIGTERM — stops active sessions and the MCP container.
