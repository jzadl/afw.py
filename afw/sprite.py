from __future__ import annotations

from typing import Optional

from .buffer import char_width
from .style import Style, DEFAULT_STYLE


class Sprite:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self._cells: dict[tuple[int, int], tuple[Optional[str], Style]] = {}

    @staticmethod
    def from_text(
        art: str,
        style: Style = DEFAULT_STYLE,
        *,
        transparent_char: str = " ",
        palette: Optional[dict[str, Style]] = None,
    ) -> "Sprite":
        lines = art.split("\n")
        while lines and lines[0].strip() == "":
            lines.pop(0)
        while lines and lines[-1].strip() == "":
            lines.pop()
        height = len(lines)
        width = max((sum(char_width(c) for c in line) for line in lines), default=0)
        sprite = Sprite(width, height)
        for y, line in enumerate(lines):
            x = 0
            for ch in line:
                if ch == transparent_char:
                    x += char_width(ch) or 1
                    continue
                cell_style = (palette or {}).get(ch, style)
                sprite.set(x, y, ch, cell_style)
                x += char_width(ch)
        return sprite

    def set(self, x: int, y: int, ch: Optional[str], style: Style = DEFAULT_STYLE) -> None:
        self._cells[(x, y)] = (ch, style)

    def clear_cell(self, x: int, y: int) -> None:
        self._cells.pop((x, y), None)

    def iter_cells(self):
        return self._cells.items()

    def with_style(self, style: Style) -> "Sprite":
        new = Sprite(self.width, self.height)
        for (x, y), (ch, _) in self._cells.items():
            new.set(x, y, ch, style)
        return new
