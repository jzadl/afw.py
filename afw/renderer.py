from __future__ import annotations

import ctypes
import io
import os
import sys
from typing import Optional

from .terminal import Terminal
from .buffer import Buffer, Cell
from .style import Style, DEFAULT_STYLE
from .color import Color
from ._compat import _compat
from ._native import _load_native_render_lib, _load_native_render_fast


class Renderer:
    """Turns a Canvas into terminal output, once per frame.

    With libafw_render present and RGB data on the canvas, the video
    frame is encoded by the native Zig renderer (diffed against the
    previous frame; a zero return means "nothing changed") and text
    widgets are layered on top as cursor-addressed overlay runs. The
    fast native entry point (afw_render_frame_fast, when the loaded
    library provides it) keeps its own previous frame inside the .so,
    so the current frame is passed as a zero-copy view of the canvas
    RGB bytearray. Without the library, _build_ansi renders the same
    frame in pure Python — including the RGB layer as half-block
    glyphs, so video output is identical, just slower.
    """

    def __init__(self, terminal: Terminal):
        self.terminal = terminal
        self._prev_rgb: Optional[bytes] = None
        self._native_lib = _load_native_render_lib()
        self._native_fast = _load_native_render_fast()
        self._out_buf = None
        self._out_buf_cap = 0
        # Pure-Python path: per-row "was this row already blank last
        # frame" memory, so empty rows can be skipped (see _build_ansi).
        self._prev_row_empty: Optional[list[bool]] = None
        # Fast native path: whether the .so's internal previous frame
        # currently matches what is on screen. Anything rendered
        # through the pure-Python path in between (or a resize, or the
        # first frame) forces a full re-render.
        self._last_was_native = False
        self._fast_ready = False
        self._fast_size: tuple[int, int] = (0, 0)

    def render_subpixel(self, canvas: "Canvas") -> None:
        self.render(canvas)

    def render_subpixel_fast(self, canvas: "Canvas") -> None:
        self.render(canvas)

    def render(self, canvas: "Canvas") -> None:
        if self._native_lib is not None and canvas.has_rgb_data():
            cols = canvas.width
            rows = canvas.height
            needed_cap = cols * rows * 40
            if self._out_buf is None or self._out_buf_cap < needed_cap:
                self._out_buf_cap = needed_cap
                self._out_buf = ctypes.create_string_buffer(needed_cap)
            if self._native_fast is not None:
                # Zero-copy: a ctypes view of the canvas's live RGB
                # bytearray (no per-frame bytes() copy, no allocation).
                # The .so diffs against its own internal previous
                # frame, so force a full render whenever that stored
                # frame can't match the screen: first frame, resize,
                # or a pure-Python render in between.
                view = canvas.get_rgb_view()
                buf = (ctypes.c_ubyte * len(view)).from_buffer(view)
                force_full = (
                    not self._fast_ready
                    or (cols, rows) != self._fast_size
                    or not self._last_was_native
                )
                written = self._native_fast(
                    buf, cols, rows, self._out_buf, self._out_buf_cap, force_full
                )
                self._fast_ready = True
                self._fast_size = (cols, rows)
            else:
                rgb = canvas.get_rgb_bytes()
                written = self._native_lib.afw_render_frame(
                    rgb,
                    self._prev_rgb,
                    cols,
                    rows,
                    self._out_buf,
                    self._out_buf_cap,
                )
                # A return value of 0 means "every cell is identical to the
                # previous frame" -- a successful no-op, NOT a failure. The
                # old code fell through to the pure-Python ANSI rebuild in
                # that case, making unchanged frames the *slowest* frames.
                self._prev_rgb = rgb
            try:
                if written > 0:
                    # string_at copies only `written` bytes; slicing
                    # .raw would copy the entire multi-MB out buffer.
                    chunk = ctypes.string_at(self._out_buf, written)
                    if hasattr(self.terminal, "write_bytes"):
                        self.terminal.write_bytes(chunk)
                    else:
                        sys.stdout.buffer.write(chunk)

                # Text widgets layered on top of video are re-emitted as
                # cursor-addressed overlay runs. Skip the scan entirely
                # when nothing touched the text layer this frame.
                if canvas.buffer_touched():
                    text_overlay = self._scan_text_overlay(canvas)
                    if text_overlay:
                        if hasattr(self.terminal, "write"):
                            self.terminal.write(text_overlay)
                        else:
                            sys.stdout.write(text_overlay)

                self.terminal.flush()
                self._last_was_native = True
                return
            except Exception:
                # A native write failure falls through to the
                # pure-Python path, which now renders the RGB layer too
                # instead of blanking the screen.
                pass

        buf = canvas.get_buffer()
        ansi = self._build_ansi(buf, canvas)
        self.terminal.write(ansi)
        self.terminal.flush()
        self._last_was_native = False

    @staticmethod
    def _scan_text_overlay(canvas: "Canvas") -> str:
        """Collect non-video cells as cursor-positioned ANSI runs.

        Consecutive cells with the same style merge into one run: one
        cursor move + one style sequence for the whole run, instead of
        one escape sequence per character."""
        buf = canvas.get_buffer()
        width = buf.width
        parts = []
        append = parts.append
        run = None  # [y, x, style, chars]
        for i, cell in enumerate(buf.cells):
            ch = cell.ch
            if ch == " " or ch == "\x00" or ch == "\u2580" or ch == "\u2588":
                if run is not None:
                    append(f"\x1b[{run[0] + 1};{run[1] + 1}H{run[2].to_ansi_seq()}{''.join(run[3])}\x1b[0m")
                    run = None
                continue
            y, x = divmod(i, width)
            if (
                run is not None
                and run[2] == cell.style
                and y == run[0]
                and x == run[1] + len(run[3])
            ):
                run[3].append(ch)
            else:
                if run is not None:
                    append(f"\x1b[{run[0] + 1};{run[1] + 1}H{run[2].to_ansi_seq()}{''.join(run[3])}\x1b[0m")
                run = [y, x, cell.style, [ch]]
        if run is not None:
            append(f"\x1b[{run[0] + 1};{run[1] + 1}H{run[2].to_ansi_seq()}{''.join(run[3])}\x1b[0m")
        return "".join(parts)

    def _build_ansi(self, buf: Buffer, canvas: Optional["Canvas"] = None) -> str:
        """Pure-Python ANSI builder: full-frame repaint from the text
        buffer, with the RGB layer rendered as half-block '▀' glyphs
        wherever the text layer is blank.

        Rows that are blank now and were blank on the previous frame
        are skipped entirely (the terminal still shows blank there);
        the first frame and resizes always repaint everything.
        """
        out = io.StringIO()
        write = out.write
        cells = buf.cells
        width = buf.width
        height = buf.height
        force_ascii = _compat.force_ascii
        rgb = canvas.get_rgb_view() if (canvas is not None and canvas.has_rgb_data()) else None

        # A row is empty when it paints nothing: every cell blank with
        # no colors and (when an RGB layer is present) all-zero RGB.
        row_empty: list[bool] = []
        for y in range(height):
            base = y * width
            empty = True
            for i in range(base, base + width):
                cell = cells[i]
                if cell.ch not in (" ", "\x00") or cell.style.fg is not None or cell.style.bg is not None:
                    empty = False
                    break
                if rgb is not None and any(rgb[i * 6:i * 6 + 6]):
                    empty = False
                    break
            row_empty.append(empty)
        prev_empty = self._prev_row_empty
        skip_rows = prev_empty is not None and len(prev_empty) == height

        pos = 0
        prev_style: Optional[Style] = None
        for y in range(height):
            if row_empty[y] and skip_rows and prev_empty[y]:
                pos += width
                continue
            write(f"\x1b[{y + 1};1H")
            prev_style = None
            for x in range(width):
                cell = cells[pos]
                if (
                    rgb is not None
                    and (cell.ch == " " or cell.ch == "\x00")
                    and cell.style.fg is None
                    and cell.style.bg is None
                ):
                    base = pos * 6
                    tr, tg, tb, br, bg, bb = rgb[base:base + 6]
                    if not (tr == tg == tb == br == bg == bb == 0):
                        style = Style(fg=Color(tr, tg, tb), bg=Color(br, bg, bb))
                        if style != prev_style:
                            if prev_style is not None:
                                write("\x1b[0m")
                            write(style.to_ansi_seq())
                            prev_style = style
                        write("\u2580")
                        pos += 1
                        continue
                pos += 1
                if cell.ch == "\x00":
                    continue
                if cell.style != prev_style:
                    if prev_style is not None:
                        write("\x1b[0m")
                    write(cell.style.to_ansi_seq())
                    prev_style = cell.style
                if force_ascii:
                    try:
                        cell.ch.encode("ascii")
                        write(cell.ch)
                    except UnicodeEncodeError:
                        write("?")
                else:
                    write(cell.ch)
        if prev_style is not None:
            write("\x1b[0m")
        self._prev_row_empty = row_empty
        return out.getvalue()
