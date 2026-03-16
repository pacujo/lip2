"""Parse IRC formatting control codes into a list of styled text spans.

Each span is a (text, styles) tuple where styles is a dict that may contain:
  bold, italic, underline, strikethrough, monospace (bool)
  fg, bg (str — hex color like "#rrggbb")
"""

from __future__ import annotations

import re

BOLD = "\x02"
ITALIC = "\x1D"
UNDERLINE = "\x1F"
STRIKETHROUGH = "\x1E"
MONOSPACE = "\x11"
REVERSE = "\x16"
COLOR = "\x03"
HEX_COLOR = "\x04"
RESET = "\x0F"

_IRC_COLORS: dict[int, str] = {
    0: "#FFFFFF", 1: "#000000", 2: "#00007F", 3: "#009300",
    4: "#FF0000", 5: "#7F0000", 6: "#9C009C", 7: "#FC7F00",
    8: "#FFFF00", 9: "#00FC00", 10: "#009393", 11: "#00FFFF",
    12: "#0000FC", 13: "#FF00FF", 14: "#7F7F7F", 15: "#D2D2D2",
}

# Extended palette (16–98): generate from the well-known 83-entry table.
_EXT = [
    "#470000", "#472100", "#474700", "#324700", "#004700", "#00472C",
    "#004747", "#002747", "#000047", "#2E0047", "#470047", "#47002A",
    "#740000", "#743A00", "#747400", "#517400", "#007400", "#007449",
    "#007474", "#004074", "#000074", "#4B0074", "#740074", "#740045",
    "#B50000", "#B56300", "#B5B500", "#7DB500", "#00B500", "#00B571",
    "#00B5B5", "#0063B5", "#0000B5", "#7500B5", "#B500B5", "#B5006B",
    "#FF0000", "#FF8C00", "#FFFF00", "#B2FF00", "#00FF00", "#00FFA0",
    "#00FFFF", "#008CFF", "#0000FF", "#A500FF", "#FF00FF", "#FF0098",
    "#FF5959", "#FFB459", "#FFFF71", "#CFFF60", "#6FFF6F", "#65FFC9",
    "#6DFFFF", "#59B4FF", "#5959FF", "#C459FF", "#FF66FF", "#FF59BC",
    "#FF9C9C", "#FFD39C", "#FFFF9C", "#E2FF9C", "#9CFF9C", "#9CFFDB",
    "#9CFFFF", "#9CD3FF", "#9C9CFF", "#DC9CFF", "#FF9CFF", "#FF94D3",
    "#000000", "#131313", "#282828", "#363636", "#4D4D4D", "#656565",
    "#818181", "#9F9F9F", "#BCBCBC", "#E2E2E2", "#FFFFFF",
]
for _i, _hex in enumerate(_EXT, start=16):
    _IRC_COLORS[_i] = _hex

_COLOR_RE = re.compile(r"(\d{1,2})(?:,(\d{1,2}))?")
_HEX_COLOR_RE = re.compile(r"([0-9A-Fa-f]{6})(?:,([0-9A-Fa-f]{6}))?")

_CONTROL_CHARS = set(
    BOLD + ITALIC + UNDERLINE + STRIKETHROUGH + MONOSPACE
    + REVERSE + COLOR + HEX_COLOR + RESET,
)

Styles = dict[str, bool | str]
Span = tuple[str, Styles]


def _irc_color(n: int) -> str | None:
    return _IRC_COLORS.get(n)


def parse(text: str) -> list[Span]:
    """Parse IRC-formatted text into a list of (text, styles) spans."""
    spans: list[Span] = []
    bold = italic = underline = strikethrough = monospace = False
    fg: str | None = None
    bg: str | None = None
    buf: list[str] = []
    i = 0

    def flush() -> None:
        if buf:
            styles: Styles = {}
            if bold:
                styles["bold"] = True
            if italic:
                styles["italic"] = True
            if underline:
                styles["underline"] = True
            if strikethrough:
                styles["strikethrough"] = True
            if monospace:
                styles["monospace"] = True
            if fg:
                styles["fg"] = fg
            if bg:
                styles["bg"] = bg
            spans.append(("".join(buf), styles))
            buf.clear()

    while i < len(text):
        ch = text[i]
        if ch == BOLD:
            flush()
            bold = not bold
            i += 1
        elif ch == ITALIC:
            flush()
            italic = not italic
            i += 1
        elif ch == UNDERLINE:
            flush()
            underline = not underline
            i += 1
        elif ch == STRIKETHROUGH:
            flush()
            strikethrough = not strikethrough
            i += 1
        elif ch == MONOSPACE:
            flush()
            monospace = not monospace
            i += 1
        elif ch == REVERSE:
            flush()
            fg, bg = bg, fg
            i += 1
        elif ch == COLOR:
            flush()
            i += 1
            m = _COLOR_RE.match(text, i)
            if m:
                fg = _irc_color(int(m.group(1)))
                if m.group(2) is not None:
                    bg = _irc_color(int(m.group(2)))
                i = m.end()
            else:
                fg = None
                bg = None
        elif ch == HEX_COLOR:
            flush()
            i += 1
            m = _HEX_COLOR_RE.match(text, i)
            if m:
                fg = f"#{m.group(1)}"
                if m.group(2) is not None:
                    bg = f"#{m.group(2)}"
                i = m.end()
            else:
                fg = None
                bg = None
        elif ch == RESET:
            flush()
            bold = italic = underline = strikethrough = monospace = False
            fg = bg = None
            i += 1
        else:
            buf.append(ch)
            i += 1

    flush()
    return spans
