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
            logger.debug("Will resume session: %s", CLAUDE_SESSION_ID)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._on_message: Callable[[str], Awaitable[None]] | None = None

    async def start(self, prompt: str, on_message: Any = None) -> None:
        """Start a Claude Code CLI subprocess with the given prompt."""
        if self.state.running:
            raise RuntimeError("Session already running")

        self._on_message = on_message
        self.state.last_output = []

        self._reader_task = asyncio.create_task(self._run(prompt))
        self.state.running = True

    async def _run(self, prompt: str) -> None:
        """Spawn claude CLI and read streaming output."""
        try:
            cmd = [
                "claude", "-p", prompt,
                "--output-format", "stream-json",
                "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
                "--permission-mode", "acceptEdits",
            ]

            if self.state.session_id:
                cmd.extend(["--resume", self.state.session_id])

            if CLAUDE_MODEL:
                cmd.extend(["--model", CLAUDE_MODEL])

            logger.info(">>> %s", prompt[:200])
            logger.debug("CLI cmd: %s", cmd)
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

            logger.debug("Claude CLI PID: %s", self._process.pid)

            # Read streaming NDJSON lines as they arrive
            assert self._process.stdout is not None
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                await self._handle_stream_event(line.decode().strip())

            # Wait for process to finish and capture stderr
            stderr_data = await self._process.stderr.read() if self._process.stderr else b""
            await self._process.wait()

            exit_code = self._process.returncode
            logger.debug("Claude CLI exited with code %s", exit_code)

            if stderr_data:
                logger.warning("Claude CLI stderr: %s", stderr_data.decode().strip()[:500])

        except asyncio.CancelledError:
            logger.debug("Claude session cancelled")
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

    async def _handle_stream_event(self, raw: str) -> None:
        """Parse a single streaming NDJSON event from claude CLI.

        stream-json emits one JSON object per line. Key event types:
        - {"type":"assistant","message":{"content":[{"type":"text","text":"..."},{"type":"tool_use",...}]}}
        - {"type":"result","result":"...","session_id":"...","total_cost_usd":...,"usage":{...}}
        - {"type":"system","message":"..."}
        """
        if not raw:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Non-JSON stream line: %s", raw[:200])
            return

        etype = event.get("type", "")

        # Extract session ID from any event that has it
        sid = event.get("session_id")
        if sid:
            self.state.session_id = sid

        if etype == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "")
                    if text.strip():
                        logger.info("<<< %s", text[:200])
                        self._append_output(text)
                        if self._on_message:
                            await self._on_message(text)
                elif btype == "tool_use":
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    summary = self._summarize_tool_use(tool_name, tool_input)
                    if summary:
                        logger.info("🔧 %s", summary[:200])
                        self._append_output(summary)
                        if self._on_message:
                            await self._on_message(summary)

        elif etype == "tool_result":
            # Optionally log tool results (usually verbose, skip by default)
            pass

        elif etype == "result":
            # Final result event — extract stats
            result_text = event.get("result", "")
            if result_text:
                logger.info("<<< %s", result_text[:200])
                self._append_output(result_text)
                if self._on_message:
                    await self._on_message(result_text)

            cost = event.get("total_cost_usd")
            if cost:
                self.state.total_cost_usd += float(cost)

            usage = event.get("usage", {})
            input_tokens = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            duration_ms = event.get("duration_ms", 0)
            num_turns = event.get("num_turns", 0)

            parts = []
            if input_tokens or output_tokens:
                parts.append(f"{input_tokens}in/{output_tokens}out tokens")
            if cost:
                parts.append(f"${float(cost):.4f}")
            if duration_ms:
                parts.append(f"{duration_ms / 1000:.1f}s")
            if num_turns and num_turns > 1:
                parts.append(f"{num_turns} turns")

            if parts and self._on_message:
                await self._on_message(f"_{'  ·  '.join(parts)}_")

    @staticmethod
    def _summarize_tool_use(tool_name: str, tool_input: dict) -> str:
        """Create a human-readable summary of a tool call."""
        if tool_name == "Edit":
            fp = tool_input.get("file_path", "?")
            old = (tool_input.get("old_string", "") or "")[:60]
            return f"✏️  *Edit* `{fp}` — replacing `{old}...`"
        elif tool_name == "Write":
            fp = tool_input.get("file_path", "?")
            return f"📝  *Write* `{fp}`"
        elif tool_name == "Read":
            fp = tool_input.get("file_path", "?")
            return f"📖  *Read* `{fp}`"
        elif tool_name == "Bash":
            cmd = (tool_input.get("command", "") or "")[:100]
            return f"💻  *Bash* `{cmd}`"
        elif tool_name == "Glob":
            pat = tool_input.get("pattern", "?")
            return f"🔍  *Glob* `{pat}`"
        elif tool_name == "Grep":
            pat = tool_input.get("pattern", "?")
            return f"🔍  *Grep* `{pat}`"
        elif tool_name == "Agent":
            desc = tool_input.get("description", "?")
            return f"🤖  *Agent* {desc}"
        else:
            return f"🔧  *{tool_name}*"

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
            logger.debug("Stopping Claude CLI process...")
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
