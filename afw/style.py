from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .color import Color


@dataclass(frozen=True, slots=True)
class Style:
    fg: Optional[Color] = None
    bg: Optional[Color] = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    reverse: bool = False
    strikethrough: bool = False

    def merged(self, other: "Style") -> "Style":
        return Style(
            fg=other.fg if other.fg is not None else self.fg,
            bg=other.bg if other.bg is not None else self.bg,
            bold=other.bold or self.bold,
            dim=other.dim or self.dim,
            italic=other.italic or self.italic,
            underline=other.underline or self.underline,
            blink=other.blink or self.blink,
            reverse=other.reverse or self.reverse,
            strikethrough=other.strikethrough or self.strikethrough,
        )

    def to_ansi_seq(self) -> str:
        parts = ["0"]
        if self.bold:
            parts.append("1")
        if self.dim:
            parts.append("2")
        if self.italic:
            parts.append("3")
        if self.underline:
            parts.append("4")
        if self.blink:
            parts.append("5")
        if self.reverse:
            parts.append("7")
        if self.strikethrough:
            parts.append("9")
        if self.fg is not None:
            parts.append(self.fg.to_ansi_fg())
        if self.bg is not None:
            parts.append(self.bg.to_ansi_bg())
        return "\x1b[" + ";".join(parts) + "m"


DEFAULT_STYLE = Style()
