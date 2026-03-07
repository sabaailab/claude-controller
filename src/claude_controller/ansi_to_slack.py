"""Convert ANSI-colored terminal output to Slack mrkdwn.

Claude Code uses 24-bit RGB ANSI codes for syntax highlighting, diff
backgrounds, bold tool names, and dim collapsed text.  Slack has very
limited formatting (no colors), so we use:

  - Emoji indicators for visual cues (🟢 🔴 🔵 etc.)
  - Slack *bold* for tool names and key text
  - Single code block for diffs (no mixing code + mrkdwn)
  - Clean structure with consistent formatting
"""

import re

# Match any ANSI escape sequence
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 24-bit color: ESC[48;2;R;G;B m  (background)
_BG_RGB_RE = re.compile(r"\x1b\[48;2;(\d+);(\d+);(\d+)m")

# 24-bit foreground: ESC[38;2;R;G;B m
_FG_RGB_RE = re.compile(r"\x1b\[38;2;(\d+);(\d+);(\d+)m")

# Bold
_BOLD_ON = "\x1b[1m"
_DIM_ON = "\x1b[2m"
_RESET = "\x1b[0m"

# Claude Code patterns (on stripped text)
_TOOL_LINE_RE = re.compile(r"^[⏺●]\s+(\w+)\((.+)\)$")
_RESULT_ARROW = re.compile(r"^\s*⎿\s*(.*)$")
_COLLAPSED_RE = re.compile(r"^\s*…\s*\+\d+ lines")
_PROMPT_RE = re.compile(r"^❯\s*(.*)$")


def _classify_line_bg(line: str) -> str | None:
    """Return 'green', 'red', or None based on dominant background color."""
    bgs = _BG_RGB_RE.findall(line)
    if not bgs:
        return None
    r, g, b = int(bgs[0][0]), int(bgs[0][1]), int(bgs[0][2])
    if g > r * 2 and g > b * 2 and g > 20:
        return "green"
    if r > g * 2 and r > b * 2 and r > 20:
        return "red"
    return None


def _get_fg_color(line: str) -> str | None:
    """Return 'green', 'red', 'blue', or None for the first fg color."""
    fgs = _FG_RGB_RE.findall(line)
    if not fgs:
        return None
    r, g, b = int(fgs[0][0]), int(fgs[0][1]), int(fgs[0][2])
    if g > 120 and g > r and g > b:
        return "green"
    if r > 120 and r > g and r > b:
        return "red"
    if b > 120 and b > r and b > g:
        return "blue"
    return None


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def ansi_to_slack(text: str) -> str:
    """Convert ANSI terminal output to clean Slack mrkdwn."""
    lines = text.split("\n")
    out: list[str] = []
    diff_lines: list[str] = []

    def _flush_diff() -> None:
        if diff_lines:
            out.append("```")
            out.extend(diff_lines)
            out.append("```")
            diff_lines.clear()

    for raw_line in lines:
        bg = _classify_line_bg(raw_line)
        clean = _strip_ansi(raw_line).rstrip()

        # Diff lines (green/red background) → collect into code block
        if bg in ("green", "red"):
            prefix = "+" if bg == "green" else "-"
            diff_lines.append(f"{prefix} {clean}")
            continue

        # Flush any pending diff block before regular content
        if diff_lines:
            _flush_diff()

        # Skip empty lines (collapse consecutive)
        if not clean.strip():
            if out and out[-1] == "":
                continue
            out.append("")
            continue

        # Tool call line: ⏺ Bash(command) → 🔵 *Bash* `command`
        tool_m = _TOOL_LINE_RE.match(clean)
        if tool_m:
            tool_name, tool_arg = tool_m.group(1), tool_m.group(2)
            # Truncate long commands
            if len(tool_arg) > 120:
                tool_arg = tool_arg[:117] + "..."
            out.append(f"🔵 *{tool_name}* `{tool_arg}`")
            continue

        # Result arrow: ⎿ text → indented result
        result_m = _RESULT_ARROW.match(clean)
        if result_m:
            result_text = result_m.group(1).strip()
            if not result_text:
                continue
            # Check for success/error indicators
            if any(w in result_text.lower() for w in ("error", "failed", "fatal")):
                out.append(f"    🔴 {result_text}")
            elif any(w in result_text.lower() for w in ("success", "created", "passed", "pushed")):
                out.append(f"    🟢 {result_text}")
            else:
                out.append(f"    ↳ {result_text}")
            continue

        # Collapsed content: … +N lines → dim indicator
        if _COLLAPSED_RE.match(clean):
            out.append(f"    _({clean.strip()})_")
            continue

        # Prompt line: ❯ command
        prompt_m = _PROMPT_RE.match(clean)
        if prompt_m:
            out.append(f"❯ _{prompt_m.group(1)}_")
            continue

        # Regular text — check first fg color for emoji hint
        fg = _get_fg_color(raw_line)
        if fg == "green" and clean.strip().startswith(("✓", "✔", "●")):
            out.append(f"🟢 {clean.strip()}")
        elif fg == "red" and clean.strip().startswith(("✗", "✘", "●", "Error")):
            out.append(f"🔴 {clean.strip()}")
        else:
            out.append(clean)

    # Flush any trailing diff block
    if diff_lines:
        _flush_diff()

    result = "\n".join(out).strip()

    # Collapse 3+ consecutive blank lines to 1
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result
