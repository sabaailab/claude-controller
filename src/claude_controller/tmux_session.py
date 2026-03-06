"""Send commands to a live Claude Code session running inside tmux."""

import asyncio
import logging
import shlex

logger = logging.getLogger(__name__)


class TmuxSession:
    """Interface to a tmux pane running an interactive Claude Code session."""

    def __init__(self, target: str) -> None:
        self.target = target

    async def send_keys(self, text: str) -> None:
        """Type *text* into the tmux pane followed by Enter."""
        # Send text literally (-l) so key names in the prompt aren't interpreted,
        # then send Enter separately as a key name.
        literal_cmd = ["tmux", "send-keys", "-t", self.target, "-l", text]
        logger.debug("send_keys → %s", " ".join(shlex.quote(c) for c in literal_cmd))
        proc = await asyncio.create_subprocess_exec(
            *literal_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {stderr.decode().strip()}")

        enter_cmd = ["tmux", "send-keys", "-t", self.target, "Enter"]
        proc = await asyncio.create_subprocess_exec(
            *enter_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux send-keys Enter failed: {stderr.decode().strip()}")

    async def capture_pane(self, lines: int = 50) -> str:
        """Return the last *lines* lines visible in the tmux pane."""
        cmd = [
            "tmux", "capture-pane", "-t", self.target, "-p", "-S", f"-{lines}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux capture-pane failed: {stderr.decode().strip()}")
        return stdout.decode()
