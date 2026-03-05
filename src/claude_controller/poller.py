"""Slack message poller — watches for @claude commands and dispatches them."""

import asyncio
import json
import logging
import re
from typing import Any

from claude_controller.config import SLACK_CHANNEL_ID, POLL_INTERVAL_SECONDS, COMMAND_PREFIX
from claude_controller.slack_mcp import SlackMCPClient
from claude_controller.claude_session import ClaudeSession

logger = logging.getLogger(__name__)

# Parse messages from the MCP history response
_TS_PATTERN = re.compile(r'"ts":\s*"([^"]+)"')


def _parse_messages(raw: str) -> list[dict[str, Any]]:
    """Parse Slack messages from MCP conversations_history response.

    The MCP tool returns formatted text, not raw JSON.
    We try JSON first, then fall back to text parsing.
    """
    # Try parsing as JSON
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "messages" in data:
            return data["messages"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    # The MCP response is typically formatted text — extract message blocks
    messages = []
    # Split by message separators (timestamps or dashes)
    blocks = re.split(r"\n---\n|\n\n(?=\*\*)", raw.strip())
    for block in blocks:
        if not block.strip():
            continue
        # Extract timestamp
        ts_match = re.search(r"ts[\":][\s]*[\"']?(\d+\.\d+)", block)
        ts = ts_match.group(1) if ts_match else ""
        # Extract text content — look for the actual message text
        text = block.strip()
        messages.append({"ts": ts, "text": text})

    return messages


class Poller:
    """Polls Slack for @claude commands and dispatches to Claude session."""

    def __init__(self, slack: SlackMCPClient, session: ClaudeSession) -> None:
        self.slack = slack
        self.session = session
        self._last_ts: str | None = None
        self._running = False
        self._my_messages: set[str] = set()  # timestamps of our own messages

    async def run(self) -> None:
        """Main polling loop."""
        self._running = True
        logger.info("Poller started — watching channel %s every %.0fs",
                     SLACK_CHANNEL_ID, POLL_INTERVAL_SECONDS)

        # Get initial timestamp to avoid processing old messages
        await self._init_last_ts()

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error("Poll error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _init_last_ts(self) -> None:
        """Set _last_ts to the most recent message so we skip history."""
        try:
            raw = await self.slack.read_history(SLACK_CHANNEL_ID, limit=1)
            messages = _parse_messages(raw)
            if messages:
                self._last_ts = messages[0].get("ts", "")
                logger.info("Initialized last_ts: %s", self._last_ts)
        except Exception as e:
            logger.warning("Could not init last_ts: %s", e)

    async def _poll_once(self) -> None:
        """Check for new messages and process commands."""
        raw = await self.slack.read_history(SLACK_CHANNEL_ID, limit=5)
        messages = _parse_messages(raw)

        # Process only new messages (newer than _last_ts)
        new_messages = []
        for msg in messages:
            ts = msg.get("ts", "")
            if not ts:
                continue
            if self._last_ts and ts <= self._last_ts:
                continue
            if ts in self._my_messages:
                continue
            new_messages.append(msg)

        # Process oldest first
        new_messages.sort(key=lambda m: m.get("ts", ""))

        for msg in new_messages:
            ts = msg.get("ts", "")
            text = msg.get("text", "").strip()
            self._last_ts = ts

            # Check for @claude prefix
            if not text.startswith(COMMAND_PREFIX):
                continue

            # Strip prefix and parse command
            remainder = text[len(COMMAND_PREFIX):].strip()
            await self._dispatch(remainder)

    async def _dispatch(self, command: str) -> None:
        """Route a @claude command to the appropriate handler."""
        if command.startswith("-reply"):
            answer = command[len("-reply"):].strip()
            await self._handle_reply(answer)
        elif command.startswith("-status"):
            await self._handle_status()
        elif command.startswith("-stop"):
            await self._handle_stop()
        elif command:
            await self._handle_prompt(command)
        else:
            await self._send("Usage: `@claude <prompt>` | `@claude -reply <answer>` | `@claude -status` | `@claude -stop`")

    async def _handle_prompt(self, prompt: str) -> None:
        """Start a new Claude Code task."""
        if self.session.state.running:
            if self.session.state.pending_question:
                await self._send("Claude is waiting for your reply. Use `@claude -reply <answer>`")
            else:
                await self._send("Claude is already working. Use `@claude -status` to check progress.")
            return

        await self._send(f"Starting Claude session...\n> {prompt[:200]}")

        async def on_message(text: str) -> None:
            # Truncate long outputs for Slack
            if len(text) > 3000:
                text = text[:3000] + "\n... (truncated)"
            await self._send(text)

        await self.session.start(prompt, on_message=on_message)

        # Wait briefly for the session to start, then post any question
        await asyncio.sleep(1)
        if self.session.state.pending_question:
            await self._post_question()

    async def _handle_reply(self, answer: str) -> None:
        """Answer a pending Claude question."""
        if not answer:
            await self._send("Usage: `@claude -reply <your answer>`")
            return

        if not self.session.state.pending_question:
            if not self.session.state.running:
                # Resume with a follow-up prompt
                await self._handle_prompt(answer)
                return
            await self._send("No pending question. Claude is still working.")
            return

        ok = await self.session.reply(answer)
        if ok:
            await self._send(f"Replied: {answer}")
        else:
            await self._send("Failed to deliver reply.")

    async def _handle_status(self) -> None:
        """Post current session status."""
        status = self.session.get_status()
        lines = [f"*Status:* {status['status']}"]

        if status.get("session_id"):
            lines.append(f"*Session:* `{status['session_id'][:12]}...`")
        if status.get("cost_usd"):
            lines.append(f"*Cost:* ${status['cost_usd']}")
        if status.get("pending_question"):
            q = status["pending_question"][0]
            lines.append(f"\n*Question:* {q.get('question', '')}")
            for i, opt in enumerate(q.get("options", [])):
                lines.append(f"  {i+1}. {opt.get('label', '')} — {opt.get('description', '')}")
            lines.append("\nReply with `@claude -reply <answer>`")
        if status.get("last_output"):
            last = status["last_output"][-1]
            if len(last) > 500:
                last = last[-500:]
            lines.append(f"\n*Last output:*\n```{last}```")

        await self._send("\n".join(lines))

    async def _handle_stop(self) -> None:
        """Stop the running session."""
        if not self.session.state.running:
            await self._send("No active session.")
            return
        await self.session.stop()
        await self._send("Session stopped.")

    async def _post_question(self) -> None:
        """Post a pending AskUserQuestion to Slack."""
        pq = self.session.state.pending_question
        if not pq:
            return

        lines = ["*Claude has a question:*\n"]
        for q in pq.questions:
            lines.append(f"*{q.get('header', '')}:* {q.get('question', '')}")
            for i, opt in enumerate(q.get("options", [])):
                lines.append(f"  {i+1}. *{opt.get('label', '')}* — {opt.get('description', '')}")
            lines.append("")

        lines.append("Reply with `@claude -reply <answer or number>`")
        await self._send("\n".join(lines))

    async def _send(self, text: str) -> None:
        """Send a message to Slack and track it to avoid re-processing."""
        try:
            result = await self.slack.send_message(SLACK_CHANNEL_ID, text)
            # Try to extract ts from result to track our own messages
            ts_match = re.search(r'"ts":\s*"([^"]+)"', str(result))
            if ts_match:
                self._my_messages.add(ts_match.group(1))
        except Exception as e:
            logger.error("Failed to send Slack message: %s", e)

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
