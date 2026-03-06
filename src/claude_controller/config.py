"""Configuration constants and environment variables."""

import os

# Slack
SLACK_CHANNEL_ID = os.getenv("CONTROLLER_SLACK_CHANNEL_ID", "D09412DATSL")
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL", "3"))

# Slack MCP Docker container
SLACK_MCP_IMAGE = "ghcr.io/korotovsky/slack-mcp-server:v1.1.28"
SLACK_MCP_XOXC_TOKEN = os.environ.get("SLACK_MCP_XOXC_TOKEN", "")
SLACK_MCP_XOXD_TOKEN = os.environ.get("SLACK_MCP_XOXD_TOKEN", "")

# Claude Code
CLAUDE_CWD = os.getenv("CLAUDE_CWD", os.getcwd())
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", None)
CLAUDE_SESSION_ID = os.getenv("CLAUDE_SESSION_ID", None)

# tmux integration
TMUX_TARGET = os.getenv("TMUX_TARGET", "")  # e.g. "claude:0.0"

# Command prefix
COMMAND_PREFIX = "claude"
