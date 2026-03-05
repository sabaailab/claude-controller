"""Claude Code session manager — spawns CLI as subprocess with stdin/stdout pipes."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from claude_controller.config import CLAUDE_CWD, CLAUDE_MODEL, CLAUDE_SESSION_ID

logger = logging.getLogger(__name__)

# Max output to keep in memory
_MAX_OUTPUT_LINES = 50


@dataclass
class SessionState:
    """Tracks the current Claude Code session."""
    session_id: str | None = None
    running: bool = False
    last_output: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0


class ClaudeSession:
    """Manages Claude Code CLI as a subprocess.

    Uses `claude -p <prompt> --output-format json` which outputs a single
    JSON result on completion. Uses `script` to force a pseudo-TTY so the
    CLI doesn't buffer output.
    """

    def __init__(self) -> None:
        self.state = SessionState()
        if CLAUDE_SESSION_ID:
            self.state.session_id = CLAUDE_SESSION_ID
            logger.info("Will resume session: %s", CLAUDE_SESSION_ID)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._on_message: Callable[[str], Awaitable[None]] | None = None

    async def start(self, prompt: str, on_message: Any = None) -> None:
        """Start a Claude Code CLI subprocess with the given prompt."""
        if self.state.running:
            raise RuntimeError("Session already running")

        self._on_message = on_message
        self.state.running = True
        self.state.last_output = []

        self._reader_task = asyncio.create_task(self._run(prompt))

    async def _run(self, prompt: str) -> None:
        """Spawn claude CLI and read output."""
        try:
            cmd = [
                "claude", "-p", prompt,
                "--output-format", "json",
                "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
                "--permission-mode", "acceptEdits",
            ]

            if self.state.session_id:
                cmd.extend(["--resume", self.state.session_id])

            if CLAUDE_MODEL:
                cmd.extend(["--model", CLAUDE_MODEL])

            logger.info("Starting Claude CLI: %s", cmd)
            # Remove Claude env vars to allow spawning as a separate session
            _strip = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
            env = {k: v for k, v in os.environ.items() if k not in _strip}
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=CLAUDE_CWD,
                env=env,
                limit=10 * 1024 * 1024,
            )

            logger.info("Claude CLI PID: %s", self._process.pid)

            # Read all stdout (json format outputs everything at once on completion)
            stdout_data, stderr_data = await self._process.communicate()

            exit_code = self._process.returncode
            logger.info("Claude CLI exited with code %s", exit_code)

            if stderr_data:
                logger.warning("Claude CLI stderr: %s", stderr_data.decode().strip()[:500])

            if stdout_data:
                await self._handle_result(stdout_data.decode().strip())

        except asyncio.CancelledError:
            logger.info("Claude session cancelled")
            if self._process:
                self._process.terminate()
        except Exception as e:
            logger.error("Claude session error: %s", e, exc_info=True)
            self._append_output(f"Error: {e}")
            if self._on_message:
                await self._on_message(f"Error: {e}")
        finally:
            self.state.running = False
            self._process = None

    async def _handle_result(self, raw: str) -> None:
        """Parse the JSON result from claude CLI."""
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            # Not JSON — treat as plain text
            logger.debug("Non-JSON output: %s", raw[:200])
            self._append_output(raw)
            if self._on_message:
                await self._on_message(raw)
            return

        # Extract session ID
        sid = event.get("session_id")
        if sid:
            self.state.session_id = sid
            logger.info("Session ID: %s", sid)

        # Extract result text
        result_text = event.get("result", "")
        if result_text:
            self._append_output(result_text)
            if self._on_message:
                await self._on_message(result_text)

        # Track cost
        cost = event.get("total_cost_usd")
        if cost:
            self.state.total_cost_usd += float(cost)

    def _append_output(self, text: str) -> None:
        """Append to output buffer, trimming old entries."""
        self.state.last_output.append(text)
        if len(self.state.last_output) > _MAX_OUTPUT_LINES:
            self.state.last_output = self.state.last_output[-_MAX_OUTPUT_LINES:]

    def get_status(self) -> dict[str, Any]:
        """Return current session status."""
        status = "running" if self.state.running else "idle"

        result: dict[str, Any] = {
            "status": status,
            "session_id": self.state.session_id,
            "cost_usd": round(self.state.total_cost_usd, 4),
        }

        if self.state.last_output:
            result["last_output"] = self.state.last_output[-3:]

        return result

    async def stop(self) -> None:
        """Kill the running CLI process."""
        if self._process and self._process.returncode is None:
            logger.info("Stopping Claude CLI process...")
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self.state.running = False
        self._process = None
