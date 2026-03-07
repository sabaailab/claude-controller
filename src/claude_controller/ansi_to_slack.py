"""Convert ANSI-colored terminal output to Slack mrkdwn.

Claude Code uses 24-bit RGB ANSI codes for syntax highlighting, diff
backgrounds, bold tool names, and dim collapsed text.  Slack doesn't
render ANSI at all, but does support *bold*, _italic_, `code`, and
```code blocks```.  This module bridges the gap.

Conversion strategy:
  - Bold ANSI (ESC[1m) → Slack *bold*
  - Green-background lines (diff additions) → ``+`` prefix
  - Red-background lines (diff removals) → ``-`` prefix
  - Dim text (ESC[2m) → stripped (collapsed content, not useful)
  - All other ANSI codes → stripped
  - Unicode symbols (⏺, checkmarks, etc.) → kept as-is
  - Consecutive blank lines → collapsed to one
"""

import re

# Match any ANSI escape sequence
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# 24-bit color: ESC[48;2;R;G;B m  (background)
_BG_RGB_RE = re.compile(r"\x1b\[48;2;(\d+);(\d+);(\d+)m")

# Bold on/off
_BOLD_ON = "\x1b[1m"
_DIM_ON = "\x1b[2m"
_RESET = "\x1b[0m"


def _classify_line_bg(line: str) -> str | None:
    """Return 'green', 'red', or None based on dominant background color."""
    bgs = _BG_RGB_RE.findall(line)
    if not bgs:
        return None
    # Take the first background color set on the line
    r, g, b = int(bgs[0][0]), int(bgs[0][1]), int(bgs[0][2])
    # Green-ish background (Claude uses ~2,40,0 for additions)
    if g > r * 2 and g > b * 2 and g > 20:
        return "green"
    # Red-ish background (Claude uses ~61,1,0 for removals)
    if r > g * 2 and r > b * 2 and r > 20:
        return "red"
    return None


def _extract_bold_spans(line: str) -> str:
    """Convert ANSI bold spans to Slack *bold*, then strip remaining ANSI."""
    result = []
    in_bold = False
    pos = 0

    while pos < len(line):
        # Check for ANSI escape
        m = _ANSI_RE.match(line, pos)
        if m:
            seq = m.group()
            if seq == _BOLD_ON:
                if not in_bold:
                    result.append("*")
                    in_bold = True
            elif seq == _RESET or seq == _DIM_ON:
                if in_bold:
                    # Close bold before reset
                    result.append("*")
                    in_bold = False
            # Skip all ANSI sequences from output
            pos = m.end()
            continue
        result.append(line[pos])
        pos += 1

    if in_bold:
        result.append("*")

    text = "".join(result)
    # Clean up empty bold markers (**)
    text = text.replace("**", "")
    return text


def ansi_to_slack(text: str) -> str:
    """Convert ANSI terminal output to Slack mrkdwn.

    Returns formatted text suitable for posting as a Slack message
    (not wrapped in triple backticks — the caller decides formatting).
    """
    lines = text.split("\n")
    out: list[str] = []
    in_diff_block = False
    diff_lines: list[str] = []

    def _flush_diff() -> None:
        nonlocal in_diff_block
        if diff_lines:
            out.append("```")
            out.extend(diff_lines)
            out.append("```")
            diff_lines.clear()
        in_diff_block = False

    for raw_line in lines:
        bg = _classify_line_bg(raw_line)

        if bg in ("green", "red"):
            if not in_diff_block:
                in_diff_block = True
            # Strip ANSI and add diff prefix
            clean = _ANSI_RE.sub("", raw_line).rstrip()
            prefix = "+" if bg == "green" else "-"
            # Line often starts with a line number — keep it
            diff_lines.append(f"{prefix} {clean}")
            continue

        # Not a diff line — flush any pending diff block
        if in_diff_block:
            _flush_diff()

        # Process regular line: extract bold, strip ANSI
        converted = _extract_bold_spans(raw_line).rstrip()

        # Skip dim-only lines (collapsed tool output markers)
        if not converted.strip():
            # Collapse consecutive blank lines
            if out and out[-1] == "":
                continue
            out.append("")
            continue

        out.append(converted)

    # Flush any trailing diff block
    if in_diff_block:
        _flush_diff()

    return "\n".join(out).strip()
