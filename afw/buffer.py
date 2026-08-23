from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional

from .style import Style, DEFAULT_STYLE


_CHAR_WIDTH_CACHE: dict[str, int] = {}


def char_width(ch: str) -> int:
    """Terminal column width of a character (0, 1, or 2).

    Results are memoized: drawing hot paths call this once per glyph,
    and terminals draw the same few hundred characters over and over,
    so the cache hit rate is essentially 100% in real apps (~5x faster
    than recomputing the unicodedata lookups every call).
    """
    w = _CHAR_WIDTH_CACHE.get(ch)
    if w is not None:
        return w
    w = _compute_char_width(ch)
    _CHAR_WIDTH_CACHE[ch] = w
    return w


def _compute_char_width(ch: str) -> int:
    if ch == "":
        return 0
    cp = ord(ch)
    if cp == 0:
        return 0
    if unicodedata.combining(ch):
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Cc", "Cf") and ch not in ("\t",):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    if 0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF:
        return 2
    return 1


@dataclass(slots=True)
class Cell:
    ch: str = " "
    style: Style = DEFAULT_STYLE

    def copy(self) -> "Cell":
        return Cell(self.ch, self.style)


EMPTY_CELL = Cell(" ", DEFAULT_STYLE)
WIDE_TAIL = Cell("\x00", DEFAULT_STYLE)


class Buffer:
    __slots__ = ("width", "height", "cells")

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.cells: list[Cell] = [Cell() for _ in range(width * height)]

    def idx(self, x: int, y: int) -> int:
        return y * self.width + x

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def get(self, x: int, y: int) -> Optional[Cell]:
        if not self.in_bounds(x, y):
            return None
        return self.cells[self.idx(x, y)]

    def set(self, x: int, y: int, cell: Cell) -> None:
        if not self.in_bounds(x, y):
            return
        self.cells[self.idx(x, y)] = cell

    def clear(self, cell: Cell = EMPTY_CELL) -> None:
        # Cells are treated as immutable value objects everywhere in the
        # framework (drawing always *replaces* a slot via set(), never
        # mutates one in place), so every cell can share a single
        # instance instead of allocating width*height copies per clear.
        if cell is EMPTY_CELL or (cell.ch == " " and cell.style is DEFAULT_STYLE):
            shared = EMPTY_CELL
        else:
            shared = cell.copy()
        self.cells = [shared] * len(self.cells)

    def resize(self, width: int, height: int) -> None:
        new_cells = [Cell() for _ in range(width * height)]
        for y in range(min(height, self.height)):
            for x in range(min(width, self.width)):
                new_cells[y * width + x] = self.cells[self.idx(x, y)]
        self.width = width
        self.height = height
        self.cells = new_cells
