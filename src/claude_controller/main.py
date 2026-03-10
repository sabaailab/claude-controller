"""Entry point — starts the Slack MCP client and polling loop."""

import argparse
import asyncio
import logging
import os
import signal

from claude_controller import config
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
    tmux = TmuxSession(config.TMUX_TARGET) if config.TMUX_TARGET else None
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
        # Retry initial startup — Docker/OrbStack may need time to come up
        for attempt in range(5):
            try:
                await slack.start()
                break
            except (ConnectionError, RuntimeError, OSError) as e:
                if attempt == 4:
                    raise
                wait = 5 * (attempt + 1)
                logger.warning("Startup failed (attempt %d/5): %s — retrying in %ds", attempt + 1, e, wait)
                await slack.stop()
                await asyncio.sleep(wait)

        mode = f"tmux mode → `{config.TMUX_TARGET}`" if tmux else "subprocess mode"
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
        help="Slack channel/DM ID to poll and respond in (overrides CONTROLLER_SLACK_CHANNEL_ID env var)",
    )
    parser.add_argument(
        "--tmux",
        default=None,
        metavar="TARGET",
        help="tmux target pane, e.g. 'claude:0.0' (overrides TMUX_TARGET env var). Enables tmux mode",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="DIR",
        help="Working directory for spawned Claude Code processes (overrides CLAUDE_CWD env var)",
    )
    parser.add_argument(
        "--poll-interval",
        default=None,
        type=float,
        metavar="SECONDS",
        help="Seconds between polling Slack for new messages (overrides POLL_INTERVAL env var, default: 3)",
    )
    args = parser.parse_args()

    if args.channel:
        config.SLACK_CHANNEL_ID = args.channel
    if args.tmux:
        config.TMUX_TARGET = args.tmux
    if args.cwd:
        config.CLAUDE_CWD = args.cwd
    if args.poll_interval is not None:
        config.POLL_INTERVAL_SECONDS = args.poll_interval

    if not config.SLACK_CHANNEL_ID:
        parser.error(
            "No Slack channel ID configured. Set CONTROLLER_SLACK_CHANNEL_ID "
            "environment variable or pass --channel CHANNEL_ID."
        )

    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
