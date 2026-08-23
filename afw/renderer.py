from __future__ import annotations

import io
import os
import sys
from typing import Optional

from .terminal import Terminal
from .buffer import Buffer, Cell
from .style import Style, DEFAULT_STYLE
from ._compat import _compat
from ._native import _load_native_render_lib


class Renderer:
    def __init__(self, terminal: Terminal):
        self.terminal = terminal
        self._prev_rgb: Optional[bytes] = None
        self._native_lib = _load_native_render_lib()
        self._out_buf = None
        self._out_buf_cap = 0

    def render_subpixel(self, canvas: "Canvas") -> None:
        self.render(canvas)

    def render_subpixel_fast(self, canvas: "Canvas") -> None:
        self.render(canvas)

    def render(self, canvas: "Canvas") -> None:
        if self._native_lib is not None and canvas.has_rgb_data():
            rgb = canvas.get_rgb_bytes()
            cols = canvas.width
            rows = canvas.height
            needed_cap = cols * rows * 40
            if self._out_buf is None or self._out_buf_cap < needed_cap:
                import ctypes
                self._out_buf_cap = needed_cap
                self._out_buf = ctypes.create_string_buffer(needed_cap)
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
                    import ctypes
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
                return
            except Exception:
                pass

        buf = canvas.get_buffer()
        ansi = self._build_ansi(buf)
        self.terminal.write(ansi)
        self.terminal.flush()

    @staticmethod
    def _scan_text_overlay(canvas: "Canvas") -> str:
        """Collect non-video cells as cursor-positioned ANSI runs."""
        buf = canvas.get_buffer()
        width = buf.width
        parts = []
        append = parts.append
        for i, cell in enumerate(buf.cells):
            ch = cell.ch
            if ch == " " or ch == "\x00" or ch == "\u2580" or ch == "\u2588":
                continue
            y, x = divmod(i, width)
            append(f"\x1b[{y + 1};{x + 1}H{cell.style.to_ansi_seq()}{ch}\x1b[0m")
        return "".join(parts)

    def _build_ansi(self, buf: Buffer) -> str:
        out = io.StringIO()
        write = out.write
        cells = buf.cells
        width = buf.width
        prev_style: Optional[Style] = None
        pos = 0
        force_ascii = _compat.force_ascii
        for y in range(buf.height):
            write(f"\x1b[{y + 1};1H")
            prev_style = None
            for x in range(width):
                cell = cells[pos]
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
        return out.getvalue()
