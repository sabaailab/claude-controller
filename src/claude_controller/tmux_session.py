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

    async def capture_pane(self, lines: int = 50, ansi: bool = False) -> str:
        """Return the last *lines* lines visible in the tmux pane.

        If *ansi* is True, preserves ANSI escape sequences (``-e`` flag).
        """
        cmd = ["tmux", "capture-pane"]
        if ansi:
            cmd.append("-e")
        cmd.extend(["-p", "-t", self.target, "-S", f"-{lines}"])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux capture-pane failed: {stderr.decode().strip()}")
        return stdout.decode()

    async def capture_full_scrollback(self, ansi: bool = False) -> str:
        """Capture the entire scrollback buffer plus visible pane content.

        Uses ``-S -`` (start of history) and ``-E -`` (end) to get everything.
        """
        cmd = ["tmux", "capture-pane"]
        if ansi:
            cmd.append("-e")
        cmd.extend(["-p", "-t", self.target, "-S", "-", "-E", "-"])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"tmux capture-pane failed: {stderr.decode().strip()}")
        return stdout.decode()
