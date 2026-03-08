"""Slack message poller — watches for !claude commands and dispatches them."""

import asyncio
import logging
import os
from typing import Any

from claude_controller.config import SLACK_CHANNEL_ID, POLL_INTERVAL_SECONDS, COMMAND_PREFIX
from claude_controller.slack_mcp import SlackMCPClient
from claude_controller.claude_session import ClaudeSession
from claude_controller.tmux_session import TmuxSession
from claude_controller.log_tailer import LogTailer, format_entries_for_slack

logger = logging.getLogger(__name__)


def _parse_messages(raw: str) -> list[dict[str, Any]]:
    """Parse Slack messages from MCP conversations_history CSV response.

    Format: MsgID,UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Reactions,Cursor
    """
    import csv
    import io
    import html

    messages = []
    reader = csv.reader(io.StringIO(raw))

    header = None
    for row in reader:
        if not row:
            continue
        # Detect header row
        if row[0] == "MsgID":
            header = row
            continue
        if len(row) < 7:
            continue

        ts = row[0].strip()
        text = row[6].strip() if len(row) > 6 else ""
        # Unescape HTML entities (e.g. &lt; &gt; &amp;)
        text = html.unescape(text)

        if ts:
            messages.append({"ts": ts, "text": text})

    return messages


class Poller:
    """Polls Slack for !claude commands and dispatches to Claude session."""

    # Accepted command prefixes (case-insensitive)
    PREFIXES = ("claude", "c")

    def __init__(self, slack: SlackMCPClient, session: ClaudeSession, tmux: TmuxSession | None = None) -> None:
        self.slack = slack
        self.session = session
        self.tmux = tmux
        self._last_ts: str | None = None
        self._running = False
        self._interactive = False  # auto-send new output after each poll
        self._my_messages: set[str] = set()  # timestamps of our own messages
        self._log_tailer = LogTailer()  # tracks Claude's JSONL log by byte offset

    async def run(self) -> None:
        """Main polling loop — resilient to transient connection failures."""
        self._running = True
        self._consecutive_errors = 0
        logger.info("Poller started — watching channel %s every %.0fs",
                     SLACK_CHANNEL_ID, POLL_INTERVAL_SECONDS)

        # Get initial timestamp to avoid processing old messages
        await self._init_last_ts()

        while self._running:
            try:
                await self._poll_once()
                self._consecutive_errors = 0  # reset on success
            except (ConnectionError, RuntimeError, asyncio.TimeoutError, OSError) as e:
                self._consecutive_errors += 1
                backoff = min(30, POLL_INTERVAL_SECONDS * self._consecutive_errors)
                logger.warning("Poll connection error (%d consecutive): %s — retrying in %.0fs",
                               self._consecutive_errors, e, backoff)
                # Restart MCP container after 3 consecutive failures
                if self._consecutive_errors >= 3:
                    await self._restart_slack()
                await asyncio.sleep(backoff)
                continue
            except Exception as e:
                self._consecutive_errors += 1
                logger.error("Poll error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _init_last_ts(self) -> None:
        """Set _last_ts to the most recent message so we skip history."""
        for attempt in range(3):
            try:
                raw = await self.slack.read_history(SLACK_CHANNEL_ID, limit=1)
                messages = _parse_messages(raw)
                if messages:
                    self._last_ts = messages[0].get("ts", "")
                    logger.debug("Initialized last_ts: %s", self._last_ts)
                return
            except Exception as e:
                logger.warning("Could not init last_ts (attempt %d/3): %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(5)

    async def _restart_slack(self) -> None:
        """Restart the Slack MCP container to recover from connection failures."""
        logger.info("Restarting Slack MCP container...")
        try:
            await self.slack.stop()
        except Exception:
            pass
        try:
            await self.slack.start()
            self._consecutive_errors = 0
            logger.debug("Slack MCP container restarted successfully")
        except Exception as e:
            logger.error("Failed to restart Slack MCP container: %s", e)

    def _match_prefix(self, text: str) -> str | None:
        """Return the matched command prefix or None."""
        lower = text.lower()
        for pfx in self.PREFIXES:
            if lower.startswith(pfx):
                after = text[len(pfx):]
                if not after or after[0] in (" ", "-"):
                    return pfx
        return None

    async def _poll_once(self) -> None:
        """Check for new messages and process commands.

        Fetches a small window (3 messages) to avoid missing commands that
        arrive between polls.  Non-command messages are skipped immediately
        after a cheap prefix check — only matching commands are dispatched.
        """
        raw = await self.slack.read_history(SLACK_CHANNEL_ID, limit=3)
        messages = _parse_messages(raw)

        if messages:
            # Filter to new messages only, oldest first
            new_msgs = [
                m for m in messages
                if m.get("ts")
                and (not self._last_ts or m["ts"] > self._last_ts)
                and m["ts"] not in self._my_messages
            ]
            if new_msgs:
                new_msgs.sort(key=lambda m: m["ts"])
                # Advance _last_ts to the newest message regardless of whether it's a command
                self._last_ts = new_msgs[-1]["ts"]

                for msg in new_msgs:
                    text = msg.get("text", "").strip()
                    pfx = self._match_prefix(text)
                    if not pfx:
                        continue
                    logger.info("Command [%s]: %s", msg["ts"], text[:200])
                    remainder = text[len(pfx):].strip()
                    await self._dispatch(remainder)

        # Interactive mode: auto-send new log output every poll cycle
        if self._interactive:
            await self._auto_update()

    # Map of flag name -> handler method name (and whether it takes an argument)
    _COMMANDS = {
        "resume": ("_handle_resume", True),
        "sessions": ("_handle_sessions", False),
        "update": ("_handle_update", False),
        "interactive": ("_handle_interactive", True),
        "stop": ("_handle_stop", False),
        "help": ("_handle_help", False),
    }

    async def _dispatch(self, command: str) -> None:
        """Route a command to the appropriate handler with prefix matching."""
        if not command:
            await self._handle_help()
            return

        if not command.startswith("-"):
            await self._handle_prompt(command)
            return

        # Parse flag: split on first space to get flag and argument
        parts = command[1:].split(None, 1)
        flag = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # Find matching commands by prefix
        matches = [name for name in self._COMMANDS if name.startswith(flag)]

        if len(matches) == 1:
            handler_name, takes_arg = self._COMMANDS[matches[0]]
            handler = getattr(self, handler_name)
            if takes_arg:
                await handler(arg)
            else:
                await handler()
        elif len(matches) > 1:
            opts = ", ".join(f"`-{m}`" for m in sorted(matches))
            await self._send(f"Ambiguous flag `-{flag}`. Did you mean: {opts}?")
        else:
            await self._send(f"Unknown flag `-{flag}`. Use `c -help` for commands.")

    async def _handle_prompt(self, prompt: str) -> None:
        """Start a new Claude Code task (or send to tmux)."""
        if self.tmux:
            # Re-attach log tailer so -update captures output from this prompt onward
            self._log_tailer.attach()
            await self._send(f"Sending to live session...\n> {prompt[:200]}")
            try:
                await self.tmux.send_keys(prompt)
            except RuntimeError as e:
                await self._send(f"tmux error: {e}")
            return

        if self.session.state.running:
            await self._send("Claude is already working. Use `claude -status` to check progress.")
            return

        await self._send(f"Starting Claude session...\n> {prompt[:200]}")

        async def on_message(text: str) -> None:
            # Truncate long outputs for Slack
            if len(text) > 3000:
                text = text[:3000] + "\n... (truncated)"
            await self._send(text)

        await self.session.start(prompt, on_message=on_message)

    async def _handle_update(self) -> None:
        """Post new output since last check by tailing Claude's JSONL log.

        Reads new lines from the log file by byte offset — no tmux
        capture, no ANSI parsing, no content diffing.  Just structured
        data straight from Claude Code's conversation log.
        """
        if self.tmux:
            # Attach to log on first call (seeks to current end)
            if not self._log_tailer.path:
                path = self._log_tailer.attach()
                if path:
                    await self._send(f"Tracking log: `{os.path.basename(path)}`\nUse `c -u` again to see new output.")
                else:
                    await self._send("Could not find Claude Code log file.")
                return

            entries = self._log_tailer.get_new_entries()
            if not entries:
                await self._send("No new output since last check.")
                return

            text = format_entries_for_slack(entries)
            if not text.strip():
                await self._send("No new output since last check.")
                return

            if len(text) > 3800:
                text = text[-3800:]
            await self._send(f"*Update:*\n{text}")
            return

        status = self.session.get_status()
        lines = [f"*Status:* {status['status']}"]

        if status.get("session_id"):
            lines.append(f"*Session:* `{status['session_id'][:12]}...`")
        if status.get("cost_usd"):
            lines.append(f"*Cost:* ${status['cost_usd']}")
        if status.get("last_output"):
            last = status["last_output"][-1]
            if len(last) > 500:
                last = last[-500:]
            lines.append(f"\n*Last output:*\n```{last}```")

        await self._send("\n".join(lines))

    async def _handle_interactive(self, arg: str = "") -> None:
        """Toggle interactive mode — auto-send new output after each poll."""
        arg = arg.strip().lower()
        if arg in ("off", "0", "false", "no"):
            self._interactive = False
            await self._send("Interactive mode *off*. Use `c -u` for manual updates.")
            return
        if arg in ("on", "1", "true", "yes", ""):
            # Ensure log tailer is attached
            if self.tmux and not self._log_tailer.path:
                self._log_tailer.attach()
            self._interactive = True
            await self._send("Interactive mode *on*. New output will be sent automatically each poll cycle.")
            return
        await self._send("Usage: `c -interactive [on|off]`")

    async def _auto_update(self) -> None:
        """Send new log output if any — called automatically in interactive mode."""
        if self.tmux:
            if not self._log_tailer.path:
                path = self._log_tailer.attach()
                if not path:
                    return
            entries = self._log_tailer.get_new_entries()
            if not entries:
                return
            text = format_entries_for_slack(entries)
            if not text.strip():
                return
            if len(text) > 3800:
                text = text[-3800:]
            await self._send(text)
            return

        # Subprocess mode: send last output if session is running
        if self.session and self.session.state.running:
            status = self.session.get_status()
            if status.get("last_output"):
                last = status["last_output"][-1]
                if len(last) > 1500:
                    last = last[-1500:]
                await self._send(f"```{last}```")

    async def _handle_help(self) -> None:
        """Show available commands."""
        interactive_status = "on" if self._interactive else "off"
        await self._send(
            "*Commands* (prefix: `claude` or `c`)\n\n"
            "`c <prompt>` — send a prompt to Claude\n"
            "`c -update` / `c -u` — show update since last check\n"
            "`c -interactive` / `c -i` — toggle auto-update mode (currently *" + interactive_status + "*)\n"
            "`c -interactive off` — turn off auto-update\n"
            "`c -stop` — stop the running session\n"
            "`c -sessions` — list recent sessions\n"
            "`c -resume <id>` — attach to a session\n"
            "`c -help` — show this help"
        )

    async def _handle_sessions(self) -> None:
        """List available Claude Code sessions."""
        import glob
        import os
        from datetime import datetime

        pattern = os.path.expanduser("~/.claude/projects/-Users-*//*.jsonl")
        files = glob.glob(pattern)
        if not files:
            await self._send("No sessions found.")
            return

        # Sort by modification time, newest first
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)

        lines = ["*Recent Claude Code sessions:*\n"]
        for f in files[:10]:
            session_id = os.path.basename(f).replace(".jsonl", "")
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            size_mb = os.path.getsize(f) / (1024 * 1024)
            project = os.path.basename(os.path.dirname(f))
            age = datetime.now() - mtime
            if age.total_seconds() < 3600:
                ago = f"{int(age.total_seconds() / 60)}m ago"
            elif age.total_seconds() < 86400:
                ago = f"{int(age.total_seconds() / 3600)}h ago"
            else:
                ago = f"{int(age.days)}d ago"

            active = " *(current)*" if session_id == self.session.state.session_id else ""
            lines.append(f"`{session_id}`{active}\n  {project} · {size_mb:.1f}MB · {ago}")

        lines.append(f"\nUse `claude -resume <id>` to attach.")
        await self._send("\n".join(lines))

    async def _handle_resume(self, session_id: str = "") -> None:
        """Attach to an existing Claude Code session."""
        session_id = session_id.strip()
        if not session_id:
            await self._send("Usage: `c -resume <session_id>`")
            return
        if self.session.state.running:
            await self._send("Session already running. Use `claude -stop` first.")
            return
        self.session.state.session_id = session_id
        await self._send(f"Attached to session `{session_id[:16]}...`\nUse `claude <prompt>` to continue it.")

    async def _handle_stop(self) -> None:
        """Stop the running session."""
        if not self.session.state.running:
            await self._send("No active session.")
            return
        await self.session.stop()
        await self._send("Session stopped.")

    async def _send(self, text: str) -> None:
        """Send a message to Slack and track it to avoid re-processing."""
        try:
            result = await self.slack.send_message(SLACK_CHANNEL_ID, text)
            logger.debug("Send result: %s", result[:300] if result else "empty")
            # Extract MsgID (timestamp) from CSV response to track our own messages
            messages = _parse_messages(result)
            for msg in messages:
                ts = msg.get("ts", "")
                if ts:
                    self._my_messages.add(ts)
                    # Advance _last_ts so our own message is never re-processed
                    if not self._last_ts or ts > self._last_ts:
                        self._last_ts = ts
                    logger.debug("Tracking own message ts: %s", ts)
        except Exception as e:
            logger.error("Failed to send Slack message: %s", e)

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
