from __future__ import annotations

from typing import Literal, Optional, Union

from ._compat import _compat
from .color import Color
from .style import Style, DEFAULT_STYLE
from .buffer import Cell, Buffer, char_width
from .sprite import Sprite


def _box_chars() -> dict[str, str]:
    if _compat.force_ascii:
        return {
            "tl": "+", "tr": "+", "bl": "+", "br": "+",
            "h": "-", "v": "|",
            "lt": "+", "rt": "+", "tt": "+", "bt": "+", "x": "+",
            "dh": "-", "dv": "|",
        }
    return {
        "tl": "\u256d", "tr": "\u256e", "bl": "\u256f", "br": "\u2570",
        "h": "\u2500", "v": "\u2502",
        "lt": "\u251c", "rt": "\u2524", "tt": "\u252c", "bt": "\u2534", "x": "\u253c",
        "dh": "\u2550", "dv": "\u2551",
    }


class Canvas:
    def __init__(self, buffer_or_width: Union[Buffer, int], height: Optional[int] = None):
        if isinstance(buffer_or_width, Buffer):
            self._buffer = buffer_or_width
            self.width = self._buffer.width
            self.height = self._buffer.height
        else:
            w = int(buffer_or_width)
            h = int(height) if height is not None else 24
            self._buffer = Buffer(w, h)
            self.width = w
            self.height = h
        self._rgb = bytearray(self.width * self.height * 6)
        self._rgb_zeros = bytes(len(self._rgb))
        self._has_rgb = False
        self._touched = False

    @staticmethod
    def create_from_terminal(width: int, height: int, double_height: bool = False) -> "Canvas":
        return Canvas(Buffer(width, height))

    def resize(self, width: int, height: int) -> None:
        self._buffer.resize(width, height)
        self.width = width
        self.height = height
        self._rgb = bytearray(width * height * 6)
        self._rgb_zeros = bytes(len(self._rgb))
        self._has_rgb = False
        self._touched = False

    def get_buffer(self) -> Buffer:
        return self._buffer

    def blit_rgb(self, data: bytes) -> None:
        needed = self.width * self.height * 6
        if len(data) >= needed:
            self._rgb[:needed] = data[:needed]
            self._has_rgb = True

    def blit_subpixel_frame(self, data: bytes, cols: int, rows: int, x_offset: int = 0, y_offset: int = 0) -> None:
        """Bulk-copy a packed half-block frame (cols*rows*6 bytes) into
        this canvas's RGB layer at (x_offset, y_offset), clipping to the
        canvas bounds. Row-wise memory copies — no per-cell Python work.
        Writes only the RGB layer; text-buffer cells are left alone
        (the renderer excludes the half-block glyphs from its overlay
        scan anyway)."""
        cw = self.width
        ch = self.height
        sx0 = max(0, -x_offset)
        sy0 = max(0, -y_offset)
        sx1 = min(cols, cw - x_offset)
        sy1 = min(rows, ch - y_offset)
        if sx0 >= sx1 or sy0 >= sy1:
            return
        row_bytes = cols * 6
        copy_bytes = (sx1 - sx0) * 6
        dst_col = x_offset + sx0
        src_base = sy0 * row_bytes + sx0 * 6
        for r in range(sy0, sy1):
            dst_start = ((y_offset + r) * cw + dst_col) * 6
            src_start = src_base + (r - sy0) * row_bytes
            self._rgb[dst_start : dst_start + copy_bytes] = data[src_start : src_start + copy_bytes]
        self._has_rgb = True

    def has_rgb_data(self) -> bool:
        return self._has_rgb

    def buffer_touched(self) -> bool:
        """True if any text-layer drawing (put/draw_*/blit) happened
        since the last clear(). Lets the renderer skip its per-cell
        overlay scan on pure-video frames."""
        return self._touched

    def get_rgb_bytes(self) -> bytes:
        return bytes(self._rgb)

    def buffer_has_content(self) -> bool:
        for c in self._buffer.cells:
            if c.ch != " " or (c.style.bg is not None and c.style.bg != Color(0, 0, 0)):
                return True
        return False

    def put(self, x: int, y: int, ch: str, style: Style = DEFAULT_STYLE) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        self._touched = True
        w = char_width(ch)
        if w == 0:
            return
        if w == 2:
            self._buffer.set(x, y, Cell(ch, style))
            if x + 1 < self.width:
                self._buffer.set(x + 1, y, Cell("\x00", style))
        else:
            self._buffer.set(x, y, Cell(ch, style))

    def put_char(self, x: int, y: int, ch: str, style: Style = DEFAULT_STYLE) -> None:
        self.put(x, y, ch, style)

    def put_subpixel(self, x: int, sub_y: int, color: Color) -> None:
        cx = int(x)
        cy = int(sub_y) // 2
        if not (0 <= cx < self.width and 0 <= cy < self.height):
            return
        is_top = (int(sub_y) % 2 == 0)
        idx = (cy * self.width + cx) * 6
        if is_top:
            self._rgb[idx] = color.r
            self._rgb[idx + 1] = color.g
            self._rgb[idx + 2] = color.b
        else:
            self._rgb[idx + 3] = color.r
            self._rgb[idx + 4] = color.g
            self._rgb[idx + 5] = color.b
        self._has_rgb = True

        cell = self._buffer.get(cx, cy)
        if cell is None:
            return

        fg: Optional[Color] = None
        bg: Optional[Color] = None

        if cell.ch == "\u2580":
            if is_top:
                fg = color
                bg = cell.style.bg
            else:
                fg = cell.style.fg or Color(0, 0, 0)
                bg = color
        elif cell.ch == "\u2588":
            if is_top:
                fg = color
                bg = cell.style.fg or Color(0, 0, 0)
            else:
                fg = cell.style.fg or Color(0, 0, 0)
                bg = color
        else:
            if is_top:
                fg = color
                bg = cell.style.bg
            else:
                fg = cell.style.fg
                bg = color

        if fg is not None and bg is not None and fg == bg:
            self._buffer.set(cx, cy, Cell("\u2588", Style(fg=fg)))
        elif fg is not None or bg is not None:
            self._buffer.set(cx, cy, Cell("\u2580", Style(fg=fg, bg=bg)))

    def flush_subpixels(self) -> None:
        pass

    def clear(self, bg: Optional[Color] = None) -> None:
        style = Style(bg=bg) if bg is not None else DEFAULT_STYLE
        self._buffer.clear(Cell(" ", style))
        if bg is not None:
            self._rgb[:] = bytes((bg.r, bg.g, bg.b)) * (self.width * self.height)
        else:
            self._rgb[:] = self._rgb_zeros
        self._has_rgb = False
        self._touched = False

    def draw_rect(self, x: int, y: int, w: int, h: int, style: Style = DEFAULT_STYLE) -> None:
        if w <= 0 or h <= 0:
            return
        box = _box_chars()
        for i in range(x + 1, x + w - 1):
            self.put(i, y, box["h"], style)
            self.put(i, y + h - 1, box["h"], style)
        for j in range(y + 1, y + h - 1):
            self.put(x, j, box["v"], style)
            self.put(x + w - 1, j, box["v"], style)
        self.put(x, y, box["tl"], style)
        self.put(x + w - 1, y, box["tr"], style)
        self.put(x, y + h - 1, box["bl"], style)
        self.put(x + w - 1, y + h - 1, box["br"], style)

    def draw_box(self, x: int, y: int, w: int, h: int, style: Style = DEFAULT_STYLE, title: Optional[str] = None) -> None:
        self.draw_rect(x, y, w, h, style)
        if title and w > 4:
            avail = w - 4
            t = title[:avail]
            self.draw_text(x + 2, y, t, style)

    def draw_filled_rect(self, x: int, y: int, w: int, h: int, style: Style = DEFAULT_STYLE) -> None:
        for j in range(y, y + h):
            for i in range(x, x + w):
                self.put(i, j, "\u2588", style)

    def draw_text(self, x: int, y: int, text: str, style: Style = DEFAULT_STYLE) -> int:
        cx = x
        for ch in text:
            if ch == "\n":
                y += 1
                cx = x
                continue
            self.put(cx, y, ch, style)
            cx += char_width(ch) or 1
        return cx - x

    def draw_text_centered(self, y: int, text: str, style: Style = DEFAULT_STYLE) -> None:
        total_w = sum(char_width(c) for c in text)
        x = max(0, (self.width - total_w) // 2)
        self.draw_text(x, y, text, style)

    def draw_circle(self, cx: int, cy: int, radius: int, char: str = "\u25cf", style: Style = DEFAULT_STYLE) -> None:
        if radius <= 0:
            return
        sq_radius = radius * radius
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                dist_sq = dx * dx + dy * dy
                if dist_sq <= sq_radius:
                    self.put(cx + dx, cy + dy, char, style)

    def draw_circle_outline(self, cx: int, cy: int, radius: int, char: str = "\u25cf", style: Style = DEFAULT_STYLE) -> None:
        if radius <= 0:
            return
        sq_radius = radius * radius
        margin = max(0, radius - 1)
        sq_inner = max(0, margin * margin)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                dist_sq = dx * dx + dy * dy
                if sq_inner <= dist_sq <= sq_radius:
                    self.put(cx + dx, cy + dy, char, style)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, char: str = "\u2500", style: Style = DEFAULT_STYLE) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.put(x0, y0, char, style)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def blit(self, x: int, y: int, sprite: Sprite, style: Optional[Style] = None) -> None:
        for (sx, sy), (ch, cell_style) in sprite.iter_cells():
            real_style = style.merged(cell_style) if style else cell_style
            self.put(x + sx, y + sy, ch, real_style)
