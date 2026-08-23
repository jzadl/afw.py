from __future__ import annotations

import os
import shutil
import signal
import sys
import termios
import threading
import tty
import atexit
from typing import Any, Callable, Literal, Optional

_ALT_SCREEN_ON = "\x1b[?1049h"
_ALT_SCREEN_OFF = "\x1b[?1049l"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"
_CURSOR_HOME = "\x1b[H"
_CLEAR_SCREEN = "\x1b[2J"
_MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
_MOUSE_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1006l"
_BRACKETED_PASTE_ON = "\x1b[?2004h"
_BRACKETED_PASTE_OFF = "\x1b[?2004l"


class TerminalError(Exception):
    pass


class Terminal:
    def __init__(
        self,
        *,
        alt_screen: bool = True,
        hide_cursor: bool = True,
        mouse: bool = False,
        bracketed_paste: bool = False,
    ):
        if not sys.stdout.isatty():
            raise TerminalError(
                "stdout is not a tty. afw needs to run in a real "
                "interactive terminal (won't work redirected to a file/pipe)."
            )
        self.alt_screen = alt_screen
        self.hide_cursor = hide_cursor
        self.mouse_enabled = mouse
        self.bracketed_paste = bracketed_paste
        self._fd = sys.stdin.fileno()
        self._out = sys.stdout
        self._original_termios: Optional[list] = None
        self._restored = True
        self._entered = False
        self.width, self.height = self._query_size()
        self._resize_lock = threading.Lock()
        self._on_resize_cb: Optional[Callable[[int, int], None]] = None
        self._prev_signal_handlers: dict[int, Any] = {}

    @property
    def stdin_fd(self) -> int:
        return self._fd

    def _query_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines

    def poll_resize(self) -> bool:
        w, h = self._query_size()
        if w != self.width or h != self.height:
            self.width, self.height = w, h
            return True
        return False

    def on_resize(self, cb: Callable[[int, int], None]) -> None:
        self._on_resize_cb = cb

    def _handle_sigwinch(self, signum, frame) -> None:
        with self._resize_lock:
            w, h = self._query_size()
            if (w, h) != (self.width, self.height):
                self.width, self.height = w, h
                if self._on_resize_cb is not None:
                    try:
                        self._on_resize_cb(w, h)
                    except Exception:
                        pass

    def __enter__(self) -> "Terminal":
        self._entered = True
        self._original_termios = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        self._restored = False
        buf = []
        if self.alt_screen:
            buf.append(_ALT_SCREEN_ON)
        if self.hide_cursor:
            buf.append(_CURSOR_HIDE)
        if self.mouse_enabled:
            buf.append(_MOUSE_ON)
        if self.bracketed_paste:
            buf.append(_BRACKETED_PASTE_ON)
        buf.append(_CLEAR_SCREEN)
        buf.append(_CURSOR_HOME)
        self._out.write("".join(buf))
        self._out.flush()
        atexit.register(self.restore)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                self._prev_signal_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._signal_cleanup)
            except (ValueError, OSError):
                pass
        try:
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
        except (ValueError, AttributeError, OSError):
            pass
        return self

    def _signal_cleanup(self, signum, frame) -> None:
        self.restore()
        prev = self._prev_signal_handlers.get(signum)
        if prev in (signal.SIG_DFL, None):
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        elif prev == signal.SIG_IGN:
            pass
        else:
            signal.signal(signum, prev)
            os.kill(os.getpid(), signum)

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        try:
            buf = []
            if self.mouse_enabled:
                buf.append(_MOUSE_OFF)
            if self.bracketed_paste:
                buf.append(_BRACKETED_PASTE_OFF)
            if self.hide_cursor:
                buf.append(_CURSOR_SHOW)
            if self.alt_screen:
                buf.append(_ALT_SCREEN_OFF)
            self._out.write("".join(buf))
            self._out.flush()
        except (OSError, ValueError):
            pass
        try:
            if self._original_termios is not None:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original_termios)
        except (termios.error, OSError):
            pass

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        self.restore()
        return False

    def write(self, s: str) -> None:
        self._out.write(s)

    def write_bytes(self, b: bytes) -> None:
        sys.stdout.buffer.write(b)

    def flush(self) -> None:
        self._out.flush()
