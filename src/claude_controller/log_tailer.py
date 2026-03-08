"""Tail Claude Code's JSONL conversation log for structured updates.

Finds the active session's log file, tracks the byte offset, and on
each call to ``get_new_entries()`` returns only the lines added since
the last read.  This is far more reliable than tmux pane capture —
no ANSI parsing, no content diffing, no lost output.
"""

import glob
import json
import logging
import os

logger = logging.getLogger(__name__)

# Where Claude Code stores conversation logs
_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def _find_latest_log(project_hint: str | None = None) -> str | None:
    """Return the path to the most recently modified .jsonl session file."""
    pattern = os.path.join(_PROJECTS_DIR, "*", "*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return files[0]


class LogTailer:
    """Track a Claude Code JSONL log file by byte offset."""

    def __init__(self) -> None:
        self._path: str | None = None
        self._offset: int = 0

    @property
    def path(self) -> str | None:
        return self._path

    def attach(self, path: str | None = None) -> str | None:
        """Attach to a log file.  If *path* is None, find the latest one.

        Seeks to the end so only future entries are returned by
        ``get_new_entries()``.
        """
        self._path = path or _find_latest_log()
        if self._path and os.path.isfile(self._path):
            self._offset = os.path.getsize(self._path)
            logger.info("Attached to log: %s (offset %d)", self._path, self._offset)
        else:
            self._offset = 0
            logger.warning("No log file found to attach to")
        return self._path

    def get_new_entries(self) -> list[dict]:
        """Read and parse any new JSONL lines since the last read.

        Returns a list of parsed JSON objects (dicts).  Advances the
        internal offset so the same lines are never returned twice.
        """
        if not self._path or not os.path.isfile(self._path):
            # Try to re-discover if the file disappeared (new session)
            self._path = _find_latest_log()
            if not self._path:
                return []
            self._offset = 0

        size = os.path.getsize(self._path)
        if size <= self._offset:
            # File hasn't grown (or was truncated/rotated — reset)
            if size < self._offset:
                logger.info("Log file shrunk/rotated — resetting offset")
                self._offset = 0
            return []

        entries: list[dict] = []
        with open(self._path, "r", encoding="utf-8") as f:
            f.seek(self._offset)
            raw = f.read()
            self._offset = f.tell()

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("Skipping malformed JSONL line: %s", line[:120])
        return entries


def format_entries_for_slack(entries: list[dict]) -> str:
    """Convert JSONL entries into a readable Slack message.

    Extracts assistant text, tool calls, and tool results into a
    clean, concise format.
    """
    parts: list[str] = []

    for entry in entries:
        # Skip queue operations and other meta entries
        if entry.get("type") in ("queue-operation", "summary"):
            continue

        msg = entry.get("message", {})
        role = msg.get("role") or entry.get("type", "")

        if role == "assistant":
            content = msg.get("content", [])
            if isinstance(content, str):
                parts.append(content)
                continue
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        parts.append(text)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    inp = block.get("input", {})
                    # Show a compact summary of the tool call
                    if name in ("Read", "Bash", "Edit", "Write", "Grep", "Glob"):
                        detail = _summarize_tool_input(name, inp)
                        parts.append(f"*{name}*  `{detail}`")
                    else:
                        parts.append(f"*{name}*")

        elif role == "user":
            # Tool results — show compact output
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Extract text from content blocks
                            texts = [b.get("text", "") for b in result_content
                                     if isinstance(b, dict) and b.get("type") == "text"]
                            result_content = "\n".join(texts)
                        if isinstance(result_content, str) and result_content.strip():
                            trimmed = result_content.strip()
                            if len(trimmed) > 300:
                                trimmed = trimmed[:300] + "..."
                            parts.append(f"```{trimmed}```")

    return "\n\n".join(parts)


def _summarize_tool_input(name: str, inp: dict) -> str:
    """Return a short string summarizing a tool call's input."""
    if name == "Read":
        return inp.get("file_path", "?")
    if name == "Bash":
        cmd = inp.get("command", "?")
        return cmd[:120] + "..." if len(cmd) > 120 else cmd
    if name == "Edit":
        return inp.get("file_path", "?")
    if name == "Write":
        return inp.get("file_path", "?")
    if name == "Grep":
        pat = inp.get("pattern", "?")
        path = inp.get("path", "")
        return f'{pat}  {path}' if path else pat
    if name == "Glob":
        return inp.get("pattern", "?")
    return str(inp)[:80]
