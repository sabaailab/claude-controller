"""Entry point — starts the Slack MCP client and polling loop."""

import asyncio
import logging
import signal
import sys

from claude_controller.slack_mcp import SlackMCPClient
from claude_controller.claude_session import ClaudeSession
from claude_controller.poller import Poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def async_main() -> None:
    slack = SlackMCPClient()
    session = ClaudeSession()
    poller = Poller(slack, session)

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        poller.stop()
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await slack.start()
        logger.info("claude-controller running — send /claude commands in Slack")
        await poller.run()
    except asyncio.CancelledError:
        pass
    finally:
        await session.stop()
        await slack.stop()
        logger.info("claude-controller stopped")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
