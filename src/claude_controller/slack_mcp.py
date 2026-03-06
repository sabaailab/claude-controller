"""Slack MCP client — spawns Docker container and communicates via JSON-RPC over stdio."""

import asyncio
import json
import logging
import os
from typing import Any

from claude_controller.config import SLACK_MCP_IMAGE

logger = logging.getLogger(__name__)


class SlackMCPClient:
    """Communicates with the Slack MCP Docker container via JSON-RPC 2.0 over stdio."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the Docker container and perform MCP handshake."""
        env = os.environ.copy()

        image = SLACK_MCP_IMAGE
        # Use pinned sha if available in user's environment
        full_image = f"{image}@sha256:6ccb90df28979737fe27ffbef5e4fb7d78da77cae719dacb94b9e941bfae6000"

        cmd = [
            "docker", "run", "-i", "--rm",
            "-e", "SLACK_MCP_ADD_MESSAGE_TOOL",
            "-e", "SLACK_MCP_CUSTOM_TLS",
            "-e", "SLACK_MCP_USER_AGENT",
            "-e", "SLACK_MCP_XOXC_TOKEN",
            "-e", "SLACK_MCP_XOXD_TOKEN",
            full_image,
            "mcp-server", "--transport", "stdio",
        ]

        logger.debug("Starting Slack MCP container...")
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=10 * 1024 * 1024,  # 10MB buffer for large Slack responses
        )

        # MCP handshake
        await self._initialize()
        logger.debug("Slack MCP container ready")

    async def _initialize(self) -> None:
        """Perform MCP protocol handshake."""
        # Send initialize
        resp = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "claude-controller", "version": "0.1.0"},
        })
        logger.debug("MCP init response: %s", resp)

        # Send initialized notification (no response expected)
        await self._send_notification("notifications/initialized", {})

        # Discover available tools
        tools_resp = await self._send_request("tools/list", {})
        self._tools = {t["name"]: t for t in tools_resp.get("tools", [])}
        logger.debug("MCP tools available: %s", list(self._tools.keys()))

    async def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        async with self._lock:
            self._request_id += 1
            req_id = self._request_id

            message = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }

            return await self._write_and_read(message)

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        async with self._lock:
            message = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
            await self._write(message)
            # Small delay to let the server process the notification
            await asyncio.sleep(0.1)

    async def _write(self, message: dict[str, Any]) -> None:
        """Write a JSON-RPC message to stdin."""
        assert self._process and self._process.stdin
        line = json.dumps(message) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    async def _write_and_read(self, message: dict[str, Any]) -> dict[str, Any]:
        """Write a JSON-RPC message and read the response."""
        assert self._process and self._process.stdout
        await self._write(message)

        # Read response lines, skip notifications
        while True:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=30,
            )
            if not line:
                # Process died — capture stderr for diagnostics
                stderr_msg = ""
                if self._process.stderr:
                    stderr_bytes = await self._process.stderr.read()
                    stderr_msg = stderr_bytes.decode(errors="replace").strip()
                rc = self._process.returncode
                raise ConnectionError(
                    f"MCP process closed stdout (exit={rc})"
                    + (f": {stderr_msg}" if stderr_msg else "")
                )

            data = json.loads(line.decode().strip())

            # Skip notifications (no "id" field)
            if "id" not in data:
                logger.debug("MCP notification: %s", data.get("method"))
                continue

            if "error" in data:
                raise RuntimeError(f"MCP error: {data['error']}")

            return data.get("result", {})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool and return the result."""
        result = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        # Extract text from content blocks
        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) if texts else result

    async def read_history(self, channel_id: str, limit: int = 10) -> str:
        """Read recent messages from a Slack channel."""
        return await self.call_tool("conversations_history", {
            "channel_id": channel_id,
            "limit": limit,
        })

    async def send_message(self, channel_id: str, text: str) -> str:
        """Send a message to a Slack channel."""
        return await self.call_tool("conversations_add_message", {
            "channel_id": channel_id,
            "payload": text,
            "content_type": "text/plain",
        })

    async def stop(self) -> None:
        """Kill the Docker container."""
        if self._process:
            logger.debug("Stopping Slack MCP container...")
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None
