"""Entry point — starts the Slack MCP client and polling loop."""

import argparse
import asyncio
import logging
import os
import signal
import sys

from claude_controller import config
from claude_controller.config import TMUX_TARGET
from claude_controller.slack_mcp import SlackMCPClient
from claude_controller.claude_session import ClaudeSession
from claude_controller.tmux_session import TmuxSession
from claude_controller.poller import Poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def async_main() -> None:
    slack = SlackMCPClient()
    tmux = TmuxSession(TMUX_TARGET) if TMUX_TARGET else None
    session = ClaudeSession() if not tmux else None
    poller = Poller(slack, session, tmux=tmux)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        poller.stop()
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Warn if SLACK_MCP_ADD_MESSAGE_TOOL is not configured
    add_msg_tool = os.environ.get("SLACK_MCP_ADD_MESSAGE_TOOL", "")
    if not add_msg_tool or add_msg_tool == "0":
        logger.warning(
            "SLACK_MCP_ADD_MESSAGE_TOOL is not set — the controller will NOT be able to "
            "post responses to Slack. Set it to 'true' or include channel %s.",
            config.SLACK_CHANNEL_ID,
        )
    elif add_msg_tool not in ("true", "1") and config.SLACK_CHANNEL_ID not in add_msg_tool:
        logger.warning(
            "SLACK_MCP_ADD_MESSAGE_TOOL=%s does not include control channel %s — "
            "the controller may not be able to post responses.",
            add_msg_tool, config.SLACK_CHANNEL_ID,
        )

    try:
        await slack.start()
        mode = f"tmux mode → `{TMUX_TARGET}`" if tmux else "subprocess mode"
        await slack.send_message(
            config.SLACK_CHANNEL_ID,
            f"Controller is online ({mode}). Use `claude <prompt>` to start a task.",
        )
        logger.info("claude-controller running (%s) — send 'claude <prompt>' in Slack", mode)
        await poller.run()
    except asyncio.CancelledError:
        pass
    finally:
        if session:
            await session.stop()
        await slack.stop()
        logger.info("claude-controller stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-controller",
        description="Slack-based controller that dispatches Claude Code agents. "
        "Listens for 'claude <prompt>' messages in a Slack channel and runs them via the Claude CLI.",
    )
    parser.add_argument(
        "--channel",
        default=None,
        metavar="CHANNEL_ID",
        help="Slack channel/DM ID to poll and respond in (overrides CONTROLLER_SLACK_CHANNEL_ID env var, default: %(default)s)",
    )
    args = parser.parse_args()

    if args.channel:
        config.SLACK_CHANNEL_ID = args.channel

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
