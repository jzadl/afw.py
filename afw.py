from __future__ import annotations

# Keep in sync with afw/__init__.py.
__version__ = "0.1.0-dev"

import atexit
import ctypes
import io
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union



class _CompatState:
    """Global old-terminal fallback state: truecolor (default True)
    and force_ascii (default False). Changed by old()/modern(); read by
    Color (256-color quantization) and Canvas (ASCII box chars)."""

    truecolor: bool = True
    force_ascii: bool = False


_compat = _CompatState()


def old(ascii_only: bool = False) -> None:
    _compat.truecolor = False
    _compat.force_ascii = ascii_only


def modern() -> None:
    _compat.truecolor = True
    _compat.force_ascii = False


def is_old() -> bool:
    return not _compat.truecolor


_native_render_lib = None
_native_render_tried = False

if sys.platform == "win32":
    _NATIVE_LIB_NAME = "afw_render.dll"
elif sys.platform == "darwin":
    _NATIVE_LIB_NAME = "libafw_render.dylib"
else:
    _NATIVE_LIB_NAME = "libafw_render.so"


def _load_native_render_lib():
    """Load libafw_render (ctypes CDLL) once, or None if it is not
    built/installed. Searches next to the project root (package layout)
    or next to the single-file bundle, then the system library path."""
    global _native_render_lib, _native_render_tried
    if _native_render_tried:
        return _native_render_lib
    _native_render_tried = True
    try:
        candidates = []
        try:
            here = Path(__file__).resolve()
            # Package layout (afw/_native.py): lib lives one dir up,
            # next to the project root. Single-file layout (afw.py
            # produced by builders/bundle.py): lib sits beside the file.
            candidates.append(here.parent.parent / _NATIVE_LIB_NAME)
            candidates.append(here.parent / _NATIVE_LIB_NAME)
        except NameError:
            pass
        candidates.append(_NATIVE_LIB_NAME)
        lib = None
        for candidate in candidates:
            try:
                lib = ctypes.CDLL(str(candidate))
                break
            except OSError:
                continue
        if lib is None:
            return None
        lib.afw_render_frame.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_char), ctypes.c_size_t,
        ]
        lib.afw_render_frame.restype = ctypes.c_size_t
        _native_render_lib = lib
    except Exception:
        _native_render_lib = None
    return _native_render_lib


def _load_native_render_fast():
    """Return the afw_render_frame_fast entry point if the loaded lib
    provides it, else None.

    The fast entry point keeps its own previous-frame buffer inside
    the .so, so callers pass only the current frame (as a zero-copy
    view of the canvas RGB bytearray) plus a force_full flag — no
    per-frame copy of the RGB buffer on the Python side. Libraries
    built before this entry point existed simply lack it, and the
    caller falls back to afw_render_frame.
    """
    lib = _load_native_render_lib()
    if lib is None:
        return None
    try:
        fn = lib.afw_render_frame_fast
    except AttributeError:
        return None
    fn.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_char), ctypes.c_size_t,
        ctypes.c_bool,
    ]
    fn.restype = ctypes.c_size_t
    return fn


def native_render_available() -> bool:
    return _load_native_render_lib() is not None


def _build_256_palette() -> list[tuple[int, int, int]]:
    palette: list[tuple[int, int, int]] = []
    base16 = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
        (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
        (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    palette.extend(base16)
    ramp = [0, 95, 135, 175, 215, 255]
    for r in ramp:
        for g in ramp:
            for b in ramp:
                palette.append((r, g, b))
    for i in range(24):
        v = 8 + i * 10
        palette.append((v, v, v))
    return palette


_PALETTE_256 = _build_256_palette()


def _rgb_to_256(r: int, g: int, b: int) -> int:
    best_idx = 0
    best_dist = float("inf")
    for idx, (pr, pg, pb) in enumerate(_PALETTE_256):
        dist = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


_quant_cache: dict[tuple[int, int, int], int] = {}


def _quantize(r: int, g: int, b: int) -> int:
    key = (r, g, b)
    cached = _quant_cache.get(key)
    if cached is not None:
        return cached
    idx = _rgb_to_256(r, g, b)
    _quant_cache[key] = idx
    return idx


@dataclass(frozen=True, slots=True)
class Color:
    """An RGB color (channels clamped to 0..255). Build with Color(r, g,
    b) or Color.hex("#rrggbb"); lerp() blends toward another color and
    with_alpha_over() composites over a background. to_ansi_fg/bg emit
    truecolor sequences, or the nearest 256-color index in old() mode."""

    r: int
    g: int
    b: int

    def __post_init__(self):
        object.__setattr__(self, "r", max(0, min(255, int(self.r))))
        object.__setattr__(self, "g", max(0, min(255, int(self.g))))
        object.__setattr__(self, "b", max(0, min(255, int(self.b))))

    @staticmethod
    def hex(code: str) -> "Color":
        code = code.lstrip("#")
        if len(code) == 3:
            code = "".join(c * 2 for c in code)
        if len(code) != 6:
            raise ValueError(f"Invalid hex color: {code!r}")
        return Color(int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16))

    def to_ansi_fg(self) -> str:
        if _compat.truecolor:
            return f"38;2;{self.r};{self.g};{self.b}"
        return f"38;5;{_quantize(self.r, self.g, self.b)}"

    def to_ansi_bg(self) -> str:
        if _compat.truecolor:
            return f"48;2;{self.r};{self.g};{self.b}"
        return f"48;5;{_quantize(self.r, self.g, self.b)}"

    def lerp(self, other: "Color", t: float) -> "Color":
        t = max(0.0, min(1.0, t))
        return Color(
            round(self.r + (other.r - self.r) * t),
            round(self.g + (other.g - self.g) * t),
            round(self.b + (other.b - self.b) * t),
        )

    def with_alpha_over(self, bg: "Color", alpha: float) -> "Color":
        return bg.lerp(self, alpha)


class Colors:
    """Named palette (Solarized-inspired) plus TRANSPARENT=None, which
    Sprite.from_text treats as "leave this cell alone"."""

    BLACK = Color(0, 0, 0)
    WHITE = Color(255, 255, 255)
    RED = Color(220, 50, 47)
    GREEN = Color(133, 153, 0)
    YELLOW = Color(181, 137, 0)
    BLUE = Color(38, 139, 210)
    MAGENTA = Color(211, 54, 130)
    CYAN = Color(42, 161, 152)
    GRAY = Color(128, 128, 128)
    ORANGE = Color(203, 75, 22)
    TRANSPARENT = None


@dataclass(frozen=True, slots=True)
class Style:
    """How one cell looks: optional truecolor fg/bg plus bold, dim,
    italic, underline, blink, reverse, strikethrough. Immutable and
    hashable; merged() overlays another style on top of this one
    (only its set fields win); to_ansi_seq() renders the SGR sequence
    (truecolor 38;2/48;2, or 256-color 38;5/48;5 in old() mode)."""

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


@dataclass(frozen=True, slots=True)
class Cell:
    """One grid position: a character plus its Style. Immutable by
    design — drawing always *replaces* a slot via Buffer.set(), never
    mutates a Cell in place, so Buffer.clear() can safely fill the
    whole grid with one shared instance instead of allocating
    width*height copies."""

    ch: str = " "
    style: Style = DEFAULT_STYLE

    def copy(self) -> "Cell":
        return Cell(self.ch, self.style)


EMPTY_CELL = Cell(" ", DEFAULT_STYLE)
WIDE_TAIL = Cell("\x00", DEFAULT_STYLE)


class Buffer:
    """The text layer: a width*height grid of Cells, row-major.

    get()/set() address cells by (x, y); clear() fills the grid with a
    single shared Cell instance (cells are immutable, so sharing is
    safe); resize() preserves the overlapping region.
    """

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
        # Cells are immutable value objects (see Cell), so every slot
        # can share a single instance instead of allocating
        # width*height copies per clear.
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
    """Raised when the terminal can't be used: stdout is not a tty, or
    raw mode / size queries fail."""


class Terminal:
    """Owns the terminal for one App run: raw mode, alt screen, hidden
    cursor, optional mouse/bracketed-paste, resize (SIGWINCH) handling,
    and guaranteed restore (atexit + SIGINT/SIGTERM/SIGHUP) so a crash
    never leaves the user's terminal broken. Use as a context manager.
    """

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


class Key(Enum):
    """Special keys InputManager can report (arrows, function keys,
    editing keys, and Ctrl-C). Printable characters arrive as KeyEvent
    with key=None and char set instead."""

    UP = auto(); DOWN = auto(); LEFT = auto(); RIGHT = auto()
    ENTER = auto(); ESCAPE = auto(); TAB = auto(); BACKSPACE = auto()
    DELETE = auto(); HOME = auto(); END = auto(); PAGE_UP = auto(); PAGE_DOWN = auto()
    F1 = auto(); F2 = auto(); F3 = auto(); F4 = auto(); F5 = auto(); F6 = auto()
    F7 = auto(); F8 = auto(); F9 = auto(); F10 = auto(); F11 = auto(); F12 = auto()
    INSERT = auto(); SPACE = auto()
    CTRL_C = auto()


_ESCAPE_SEQUENCES: dict[str, Key] = {
    "\x1b[A": Key.UP, "\x1bOA": Key.UP,
    "\x1b[B": Key.DOWN, "\x1bOB": Key.DOWN,
    "\x1b[C": Key.RIGHT, "\x1bOC": Key.RIGHT,
    "\x1b[D": Key.LEFT, "\x1bOD": Key.LEFT,
    "\x1b[H": Key.HOME, "\x1bOH": Key.HOME, "\x1b[1~": Key.HOME,
    "\x1b[F": Key.END, "\x1bOF": Key.END, "\x1b[4~": Key.END,
    "\x1b[2~": Key.INSERT,
    "\x1b[3~": Key.DELETE,
    "\x1b[5~": Key.PAGE_UP,
    "\x1b[6~": Key.PAGE_DOWN,
    "\x1bOP": Key.F1, "\x1b[11~": Key.F1,
    "\x1bOQ": Key.F2, "\x1b[12~": Key.F2,
    "\x1bOR": Key.F3, "\x1b[13~": Key.F3,
    "\x1bOS": Key.F4, "\x1b[14~": Key.F4,
    "\x1b[15~": Key.F5,
    "\x1b[17~": Key.F6,
    "\x1b[18~": Key.F7,
    "\x1b[19~": Key.F8,
    "\x1b[20~": Key.F9,
    "\x1b[21~": Key.F10,
    "\x1b[23~": Key.F11,
    "\x1b[24~": Key.F12,
}

_MAX_SEQ_LEN = max(len(s) for s in _ESCAPE_SEQUENCES)


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One key press: either a special Key, a printable char, or both
    (e.g. Ctrl-C sets key=CTRL_C, ctrl=True; a letter sets char).
    Use is_char("q") to match a plain character."""

    key: Optional[Key]
    char: Optional[str]
    ctrl: bool = False
    alt: bool = False

    def is_char(self, c: str) -> bool:
        return self.char == c


@dataclass(frozen=True, slots=True)
class MouseEvent:
    """One mouse report (SGR 1006 encoding): 0-based cell coordinates,
    which button, and whether it is pressed. Motion events arrive with
    button="move"; wheel events with button="wheel_up"/"wheel_down"."""

    x: int
    y: int
    button: Literal["left", "middle", "right", "release", "wheel_up", "wheel_down", "move"]
    pressed: bool


_SGR_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


class InputManager:
    """Non-blocking terminal input: reads whatever bytes are available
    on fd, decodes UTF-8 (a multi-byte character split across two reads
    is reassembled via _pending_bytes), and turns escape sequences into
    KeyEvent/MouseEvent objects. poll() never blocks."""

    def __init__(self, fd: int):
        self._fd = fd
        self._buf = ""
        self._pending_bytes = b""

    def _read_available(self, timeout: float = 0.0) -> str:
        chunks = []
        try:
            r, _, _ = select.select([self._fd], [], [], timeout)
            while r:
                data = os.read(self._fd, 4096)
                if not data:
                    break
                chunks.append(data)
                r, _, _ = select.select([self._fd], [], [], 0)
        except (OSError, ValueError):
            pass
        if not chunks:
            return ""
        # A multi-byte UTF-8 character may have been split across two
        # reads; prepend the tail saved by the previous call so the
        # character decodes whole instead of losing bytes.
        raw = self._pending_bytes + b"".join(chunks)
        self._pending_bytes = b""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            for cut in range(len(raw) - 1, max(len(raw) - 4, 0) - 1, -1):
                try:
                    text = raw[:cut].decode("utf-8")
                    self._pending_bytes = raw[cut:]
                    return text
                except UnicodeDecodeError:
                    continue
            return ""

    def poll(self) -> list[Union[KeyEvent, MouseEvent]]:
        self._buf += self._read_available(0)
        events: list[Union[KeyEvent, MouseEvent]] = []
        while self._buf:
            if self._buf.startswith("\x1b[<"):
                m = _SGR_MOUSE_RE.match(self._buf)
                if m:
                    code, x, y, kind = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
                    self._buf = self._buf[m.end():]
                    events.append(self._decode_mouse(code, x, y, kind == "M"))
                    continue
                else:
                    if len(self._buf) < 12:
                        break
                    else:
                        self._buf = self._buf[1:]
                        continue
            if self._buf[0] == "\x1b" and len(self._buf) > 1:
                matched = False
                for length in range(min(_MAX_SEQ_LEN, len(self._buf)), 1, -1):
                    candidate = self._buf[:length]
                    key = _ESCAPE_SEQUENCES.get(candidate)
                    if key is not None:
                        events.append(KeyEvent(key=key, char=None))
                        self._buf = self._buf[length:]
                        matched = True
                        break
                if matched:
                    continue
                if len(self._buf) < _MAX_SEQ_LEN and self._looks_like_prefix(self._buf):
                    more = self._read_available(0.02)
                    if more:
                        self._buf += more
                        continue
                events.append(KeyEvent(key=Key.ESCAPE, char=None))
                self._buf = self._buf[1:]
                continue
            if self._buf == "\x1b":
                more = self._read_available(0.025)
                if more:
                    self._buf += more
                    continue
                events.append(KeyEvent(key=Key.ESCAPE, char=None))
                self._buf = ""
                break
            ch = self._buf[0]
            self._buf = self._buf[1:]
            events.append(self._decode_plain_char(ch))
        return events

    @staticmethod
    def _looks_like_prefix(buf: str) -> bool:
        return any(seq.startswith(buf) for seq in _ESCAPE_SEQUENCES)

    @staticmethod
    def _decode_plain_char(ch: str) -> KeyEvent:
        o = ord(ch)
        if ch == "\r" or ch == "\n":
            return KeyEvent(key=Key.ENTER, char=None)
        if ch == "\t":
            return KeyEvent(key=Key.TAB, char=None)
        if ch in ("\x7f", "\x08"):
            return KeyEvent(key=Key.BACKSPACE, char=None)
        if ch == " ":
            return KeyEvent(key=Key.SPACE, char=" ")
        if o == 3:
            return KeyEvent(key=Key.CTRL_C, char=None, ctrl=True)
        if 1 <= o <= 26 and ch not in ("\r", "\n", "\t"):
            letter = chr(o + 96)
            return KeyEvent(key=None, char=letter, ctrl=True)
        return KeyEvent(key=None, char=ch)

    @staticmethod
    def _decode_mouse(code: int, x: int, y: int, is_press: bool) -> MouseEvent:
        gx, gy = x - 1, y - 1
        btn_code = code & 0b11
        is_motion = bool(code & 32)
        is_wheel = bool(code & 64)
        if is_wheel:
            button = "wheel_up" if btn_code == 0 else "wheel_down"
            return MouseEvent(gx, gy, button, True)
        if is_motion:
            return MouseEvent(gx, gy, "move", is_press)
        button_map = {0: "left", 1: "middle", 2: "right", 3: "release"}
        button = button_map.get(btn_code, "release")
        pressed = is_press and button != "release"
        return MouseEvent(gx, gy, button if pressed else "release", pressed)


class Sprite:
    """Reusable ASCII/unicode art: a sparse grid of (char, style)
    cells. Build with Sprite.from_text() (leading/trailing blank lines
    trimmed, spaces transparent, optional per-character palette), draw
    with Canvas.blit() or SpriteWidget. Sprites are positionless; the
    caller supplies x/y at draw time."""

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


def _interpolate_value(a: Any, b: Any, t: float) -> Any:
    if isinstance(a, float) and isinstance(b, float):
        return a + (b - a) * t
    if isinstance(a, int) and isinstance(b, int):
        return int(a + (b - a) * t)
    if isinstance(a, tuple) and isinstance(b, tuple) and len(a) == len(b):
        return type(a)(_interpolate_value(x, y, t) for x, y in zip(a, b))
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [_interpolate_value(x, y, t) for x, y in zip(a, b)]
    return b if t >= 1.0 else a


class Easing:
    """Static easing functions: each maps a linear 0..1 progress value
    to an eased 0..1 value (e.g. ease_in_quad starts slow, ease_out_bounce
    overshoots at the end). Pass one as the easing= argument of Tween."""

    @staticmethod
    def linear(t: float) -> float:
        return t

    @staticmethod
    def ease_in_quad(t: float) -> float:
        return t * t

    @staticmethod
    def ease_out_quad(t: float) -> float:
        return 1 - (1 - t) * (1 - t)

    @staticmethod
    def ease_in_out_quad(t: float) -> float:
        return 2 * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 2) / 2

    @staticmethod
    def ease_in_cubic(t: float) -> float:
        return t ** 3

    @staticmethod
    def ease_out_cubic(t: float) -> float:
        return 1 - pow(1 - t, 3)

    @staticmethod
    def ease_in_out_cubic(t: float) -> float:
        return 4 * t ** 3 if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2

    @staticmethod
    def ease_out_bounce(t: float) -> float:
        n1, d1 = 7.5625, 2.75
        if t < 1 / d1:
            return n1 * t * t
        elif t < 2 / d1:
            t -= 1.5 / d1
            return n1 * t * t + 0.75
        elif t < 2.5 / d1:
            t -= 2.25 / d1
            return n1 * t * t + 0.9375
        else:
            t -= 2.625 / d1
            return n1 * t * t + 0.984375

    @staticmethod
    def ease_in_bounce(t: float) -> float:
        return 1 - Easing.ease_out_bounce(1 - t)

    @staticmethod
    def ease_out_elastic(t: float) -> float:
        if t == 0 or t == 1:
            return t
        c4 = (2 * math.pi) / 3
        return pow(2, -10 * t) * math.sin((t * 10 - 0.75) * c4) + 1

    @staticmethod
    def ease_in_elastic(t: float) -> float:
        if t == 0 or t == 1:
            return t
        c4 = (2 * math.pi) / 3
        return -pow(2, 10 * t - 10) * math.sin((t * 10 - 10.75) * c4)

    @staticmethod
    def ease_out_back(t: float) -> float:
        c1 = 1.70158
        c3 = c1 + 1
        return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)

    @staticmethod
    def ease_in_back(t: float) -> float:
        c1 = 1.70158
        c3 = c1 + 1
        return c3 * t * t * t - c1 * t * t

    @staticmethod
    def ease_in_out_sine(t: float) -> float:
        return -(math.cos(math.pi * t) - 1) / 2


EasingFn = Callable[[float], float]


class Tween:
    """Animates one attribute of a target object from its current value
    to end_value over duration seconds.

    loops counts individual passes, not full cycles: loops=2 with
    yoyo=True runs one out-and-back (a forward pass, then a reverse
    pass) and then finishes. loops=-1 loops forever. retarget()
    restarts the tween from the attribute's current value and resets
    the loop and delay state, so a retargeted tween always runs its
    full loop count again.
    """

    def __init__(
        self,
        target: Any,
        attr: str,
        end_value: Any,
        duration: float,
        *,
        easing: EasingFn = Easing.linear,
        delay: float = 0.0,
        loops: int = 1,
        yoyo: bool = False,
        on_update: Optional[Callable[[Any], None]] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        self.target = target
        self.attr = attr
        self.start_value = getattr(target, attr)
        self.end_value = end_value
        self.duration = max(0.0, duration)
        self.easing = easing
        self.delay = max(0.0, delay)
        self.loops = loops
        self.yoyo = yoyo
        self.on_update = on_update
        self.on_complete = on_complete
        self._elapsed = 0.0
        self._delay_elapsed = 0.0
        self._loop_count = 0
        self._forward = True
        self.finished = False
        self._started = False

    def retarget(self, new_end_value: Any, duration: Optional[float] = None) -> None:
        current = getattr(self.target, self.attr)
        self.start_value = current
        self.end_value = new_end_value
        if duration is not None:
            self.duration = max(0.0, duration)
        self._elapsed = 0.0
        self._delay_elapsed = 0.0
        self._loop_count = 0
        self.finished = False
        self._forward = True

    def stop(self, *, finish: bool = False) -> None:
        if finish and not self.finished:
            self._apply(1.0 if self._forward else 0.0)
        self.finished = True

    def _apply(self, t: float) -> None:
        eased_t = self.easing(t)
        value = _interpolate_value(self.start_value, self.end_value, eased_t)
        setattr(self.target, self.attr, value)
        if self.on_update:
            try:
                self.on_update(value)
            except Exception:
                pass

    def update(self, dt: float) -> None:
        if self.finished:
            return
        if self._delay_elapsed < self.delay:
            self._delay_elapsed += dt
            if self._delay_elapsed < self.delay:
                return
            dt = self._delay_elapsed - self.delay
        if self.duration <= 0:
            self._apply(1.0 if self._forward else 0.0)
            self._advance_loop()
            return
        self._elapsed += dt
        t = min(1.0, self._elapsed / self.duration)
        self._apply(t if self._forward else 1.0 - t)
        if t >= 1.0:
            self._advance_loop()

    def _advance_loop(self) -> None:
        self._loop_count += 1
        if self.loops >= 0 and self._loop_count >= self.loops and not (self.yoyo and self._loop_count == self.loops and self._forward):
            if self.yoyo and self._forward and self.loops == 1:
                pass
            else:
                self.finished = True
                if self.on_complete:
                    try:
                        self.on_complete()
                    except Exception:
                        pass
                return
        if self.yoyo:
            self._forward = not self._forward
        self._elapsed = 0.0
        if self.loops >= 0 and self._loop_count >= self.loops and not self.yoyo:
            self.finished = True
            if self.on_complete:
                try:
                    self.on_complete()
                except Exception:
                    pass


class _DummyTarget:
    """Stand-in tween target: a bare object with one numeric attribute,
    for tests and for callbacks that need something to animate."""

    v: float = 0.0


class AnimationManager:
    """Owns the active tweens for a scene: add()/animate() to start one,
    update(dt) advances them all and drops the finished ones."""

    def __init__(self):
        self._tweens: list[Tween] = []

    def add(self, tween: Tween) -> Tween:
        self._tweens.append(tween)
        return tween

    def animate(self, target: Any, attr: str, end_value: Any, duration: float, **kwargs) -> Tween:
        tween = Tween(target, attr, end_value, duration, **kwargs)
        return self.add(tween)

    def update(self, dt: float) -> None:
        if not self._tweens:
            return
        any_finished = False
        for tween in self._tweens:
            tween.update(dt)
            if tween.finished:
                any_finished = True
        # Rebuilding the active list every frame costs a full list
        # traversal + allocation even in the common "everything still
        # running" case; only compact when something actually finished.
        if any_finished:
            self._tweens = [t for t in self._tweens if not t.finished]

    def clear(self) -> None:
        self._tweens.clear()

    def count(self) -> int:
        return len(self._tweens)


class Ticker:
    """Calls fn on a fixed interval, driven by App._run_tickers(dt).

    An interval of 0 (or negative) means "once per frame", not "as
    fast as possible": _tick calls fn exactly once per call and resets
    the accumulator, so a zero interval can never hang the app.
    """

    def __init__(self, interval: float, fn: Callable[[], None]):
        self.interval = max(0.0, interval)
        self.fn = fn
        self._acc = 0.0
        self._cancelled = False

    def _tick(self, dt: float) -> None:
        if self._cancelled:
            return
        if self.interval <= 0:
            # "Once per frame": a zero interval used to spin forever in
            # the while loop below (acc >= 0 is always true).
            self._acc = 0.0
            try:
                self.fn()
            except Exception:
                pass
            return
        self._acc += dt
        while self._acc >= self.interval and not self._cancelled:
            self._acc -= self.interval
            try:
                self.fn()
            except Exception:
                pass

    def cancel(self) -> None:
        self._cancelled = True


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
    """A drawable grid of text cells plus a per-cell RGB layer.

    The text layer holds one Cell per grid position (written by put /
    draw_* / blit). The RGB layer holds 6 bytes per cell (top RGB +
    bottom RGB) and is written by put_subpixel / blit_subpixel_frame;
    the renderer turns it into half-block glyphs, natively when
    libafw_render is present and through the pure-Python ANSI builder
    otherwise.
    """

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

    def get_rgb_view(self) -> bytearray:
        """Zero-copy view of the internal RGB layer (6 bytes per cell:
        top RGB, bottom RGB). Intended for the native renderer only;
        callers must not mutate it. Use get_rgb_bytes() for a snapshot."""
        return self._rgb

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
                bg = cell.style.bg or Color(0, 0, 0)
            else:
                fg = cell.style.fg or Color(0, 0, 0)
                bg = color

        if fg is not None and bg is not None and fg == bg:
            self._buffer.set(cx, cy, Cell("\u2588", Style(fg=fg)))
        elif fg is not None or bg is not None:
            self._buffer.set(cx, cy, Cell("\u2580", Style(fg=fg, bg=bg)))

    def flush_subpixels(self) -> None:
        """Merge the RGB layer into the text buffer.

        Every cell whose text content is blank or already a half/full
        block becomes a '▀' cell with fg/bg taken from the RGB layer,
        so the frame also renders correctly through the pure-Python
        path (which reads the text buffer). Cells holding real text
        are left alone, and all-black RGB cells stay blank. A no-op
        when the canvas has no RGB data.
        """
        if not self._has_rgb:
            return
        rgb = self._rgb
        width = self.width
        for i, cell in enumerate(self._buffer.cells):
            if cell.ch not in (" ", "\u2580", "\u2588"):
                continue
            base = i * 6
            tr, tg, tb, br, bg, bb = rgb[base:base + 6]
            if tr == tg == tb == br == bg == bb == 0:
                continue
            self._buffer.set(
                i % width, i // width,
                Cell("\u2580", Style(fg=Color(tr, tg, tb), bg=Color(br, bg, bb))),
            )

    def clear(self, bg: Optional[Color] = None) -> None:
        style = Style(bg=bg) if bg is not None else DEFAULT_STYLE
        self._buffer.clear(Cell(" ", style))
        if bg is not None:
            # 6 bytes per cell: the same color for the top and bottom
            # halves. (A 3-byte pattern would shrink the bytearray by
            # half and corrupt every later RGB read.)
            self._rgb[:] = bytes((bg.r, bg.g, bg.b, bg.r, bg.g, bg.b)) * (self.width * self.height)
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


class AudioPlayer:
    """Plays an audio file in a background process (ffplay, mpv, or
    aplay for .wav), so playback never blocks the render loop.

    play() returns False when no player backend is installed or the
    source file does not exist (URLs are exempt from the existence
    check). pause()/resume() use POSIX process groups and are not
    available on Windows, where they raise NotImplementedError instead
    of silently doing nothing. stop() always reaps the child process
    (terminate, then kill + wait) so no zombie is left behind.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._paused = False

    def play(
        self,
        source: Union[str, Path],
        *,
        loop: bool = False,
        volume: float = 1.0,
        start_time: float = 0.0,
    ) -> bool:
        self.stop()
        source_str = str(source)
        # A missing file used to return True and fail silently inside
        # the player (whose output goes to DEVNULL). URLs are exempt:
        # ffplay/mpv can stream them, and they are not local paths.
        if "://" not in source_str and not Path(source_str).exists():
            return False
        if not shutil.which("ffplay") and not shutil.which("mpv") and not shutil.which("aplay"):
            return False

        cmd: list[str] = []
        if shutil.which("ffplay"):
            cmd = [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel", "quiet",
            ]
            if loop:
                cmd += ["-loop", "0"]
            if start_time > 0:
                cmd += ["-ss", str(start_time)]
            if volume != 1.0:
                vol_val = max(0.0, min(2.0, volume)) * 100.0
                cmd += ["-volume", str(int(vol_val))]
            cmd.append(source_str)
        elif shutil.which("mpv"):
            cmd = [
                "mpv",
                "--no-video",
                "--really-quiet",
            ]
            if loop:
                cmd.append("--loop=inf")
            if start_time > 0:
                cmd += [f"--start={start_time}"]
            if volume != 1.0:
                cmd += [f"--volume={int(volume * 100)}"]
            cmd.append(source_str)
        elif shutil.which("aplay") and source_str.lower().endswith(".wav"):
            cmd = ["aplay", "-q", source_str]
        else:
            return False

        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._paused = False
                return True
            except Exception:
                self._proc = None
                return False

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                proc = self._proc
                try:
                    proc.terminate()
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        # Always wait() after kill(): otherwise the
                        # child stays a zombie until the parent exits.
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass
                self._proc = None
                self._paused = False

    def _require_posix_process_groups(self) -> None:
        if sys.platform == "win32":
            raise NotImplementedError(
                "pause()/resume() need POSIX process groups (os.killpg), "
                "which Windows does not provide"
            )

    def pause(self) -> None:
        self._require_posix_process_groups()
        with self._lock:
            if self._proc is not None and not self._paused:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGSTOP)
                    self._paused = True
                except Exception:
                    pass

    def resume(self) -> None:
        self._require_posix_process_groups()
        with self._lock:
            if self._proc is not None and self._paused:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGCONT)
                    self._paused = False
                except Exception:
                    pass

    def is_playing(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            return self._proc.poll() is None


class Sound:
    """A one-shot sound effect: wraps one audio file in its own
    AudioPlayer. See AudioPlayer for play/stop/pause semantics."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._player = AudioPlayer()

    def play(self, *, loop: bool = False, volume: float = 1.0) -> bool:
        return self._player.play(self.path, loop=loop, volume=volume)

    def stop(self) -> None:
        self._player.stop()

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        self._player.resume()

    @property
    def is_playing(self) -> bool:
        return self._player.is_playing()


class Music(Sound):
    """A music track: identical to Sound (a longer audio file played
    through its own AudioPlayer). Kept as a separate name so call sites
    can say what the audio is for."""

    pass


import inspect



class Widget:
    """Base class for everything a Scene can draw.

    x/y are the widget's position in cells (floats; drawn at the
    truncated cell), z orders drawing (higher z draws later, on top).
    visible=False skips drawing entirely. opacity is visibility-only,
    not blending: opacity <= 0 hides the widget, any value above 0
    draws it fully opaque (animate it for fade in/out effects).
    Subclasses implement draw(canvas); update(dt) is optional.
    """

    def __init__(self, x: float = 0, y: float = 0, width: int = 0, height: int = 0, *, z: int = 0):
        self.x = float(x)
        self.y = float(y)
        self.width = int(width)
        self.height = int(height)
        self.z = int(z)
        self.visible: bool = True
        self.opacity: float = 1.0
        self.offset_x: float = 0.0
        self.offset_y: float = 0.0

    def draw(self, canvas: Canvas) -> None:
        pass


class TextWidget(Widget):
    """A (possibly multi-line) text label.

    Canonical form: TextWidget(text, x, y, style). The legacy positional
    form TextWidget(x, y, text, style) is also accepted — the bundled
    examples use it — but prefer the canonical form in new code: a
    numeric-looking text in the legacy form is ambiguous and is NOT
    swapped (TextWidget(0, 0, 0) means text "0" at 0,0).
    """

    def __init__(
        self,
        text: str = "",
        x: float = 0,
        y: float = 0,
        style: Style = DEFAULT_STYLE,
        *,
        anchor: Literal["left", "center", "right"] = "left",
        z: int = 0,
    ):
        if isinstance(text, (int, float)) and isinstance(x, (int, float)) and isinstance(y, str):
            actual_x = float(text)
            actual_y = float(x)
            actual_text = y
            text = actual_text
            x = actual_x
            y = actual_y

        lines = str(text).split("\n")
        w = max((sum(char_width(c) for c in line) for line in lines), default=0)
        h = max(1, len(lines))
        super().__init__(x, y, w, h, z=z)
        self.text = str(text)
        self.style = style
        self.anchor = anchor

    def draw(self, canvas: Canvas) -> None:
        if not self.visible or self.opacity <= 0.0:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        for i, line in enumerate(self.text.split("\n")):
            line_w = sum(char_width(c) for c in line)
            if self.anchor == "center":
                draw_x = ix - line_w // 2
            elif self.anchor == "right":
                draw_x = ix - line_w
            else:
                draw_x = ix
            canvas.draw_text(draw_x, iy + i, line, self.style)


class BoxWidget(Widget):
    """A bordered box, optionally with a title drawn on the top edge."""

    def __init__(
        self,
        x: float = 0,
        y: float = 0,
        width: int = 0,
        height: int = 0,
        style: Style = DEFAULT_STYLE,
        *,
        title: Optional[str] = None,
        z: int = 0,
    ):
        super().__init__(x, y, width, height, z=z)
        self.style = style
        self.title = title

    def draw(self, canvas: Canvas) -> None:
        if not self.visible:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        canvas.draw_box(ix, iy, self.width, self.height, self.style, title=self.title)


class SpriteWidget(Widget):
    """Draws a Sprite (reusable ASCII/unicode art) at x, y."""

    def __init__(self, x: float, y: float, sprite: Sprite, *, z: int = 0):
        super().__init__(x, y, sprite.width, sprite.height, z=z)
        self.sprite = sprite

    def draw(self, canvas: Canvas) -> None:
        if not self.visible:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        canvas.blit(ix, iy, self.sprite)


class ProgressBarWidget(Widget):
    """A horizontal progress bar: value/max_value fills width cells
    with fill_char, the rest with empty_char."""

    def __init__(
        self,
        x: float = 0,
        y: float = 0,
        width: int = 0,
        height: int = 1,
        value: float = 0.0,
        max_value: float = 1.0,
        style: Style = DEFAULT_STYLE,
        fill_char: str = "\u2588",
        empty_char: str = "\u2591",
        *,
        z: int = 0,
    ):
        super().__init__(x, y, width, height, z=z)
        self.value = float(value)
        self.max_value = float(max_value)
        self.style = style
        self.fill_char = fill_char
        self.empty_char = empty_char

    def draw(self, canvas: Canvas) -> None:
        if not self.visible:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        ratio = max(0.0, min(1.0, self.value / max(0.0001, self.max_value)))
        filled = int(self.width * ratio)
        for i in range(self.width):
            ch = self.fill_char if i < filled else self.empty_char
            canvas.put(ix + i, iy, ch, self.style)


class VideoFrameWidget(Widget):
    """Displays one packed RGB video frame (cols*rows*6 bytes, top+bottom
    RGB per cell) at x, y.

    Frame-access contract (shared with FrameAsset.get_frame and the
    generated AfwFrames.read_frame, which clamp out-of-range indices):
    set_frame_rgb() accepts any bytes; a frame shorter than
    cols*rows*6 bytes is ignored (the previous frame stays on screen)
    instead of raising, so a truncated stream degrades to a held frame
    rather than crashing playback.
    """

    def __init__(
        self,
        x: float = 0,
        y: float = 0,
        width: int = 0,
        height: int = 0,
        *,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
        z: int = 0,
    ):
        w = cols if cols is not None else width
        h = rows if rows is not None else height
        super().__init__(x, y, int(w), int(h), z=z)
        self.cols = int(w)
        self.rows = int(h)
        self.rgb_data: Optional[bytes] = None
        self._prev_rgb: Optional[bytes] = None

    def set_frame_rgb(self, data: bytes) -> None:
        self.rgb_data = data

    def update(self, dt_or_frame: Any = None) -> None:
        pass

    def render(self, canvas: Canvas) -> None:
        self.draw(canvas)

    def draw(self, canvas: Canvas) -> None:
        if not self.visible or self.rgb_data is None:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        data = self.rgb_data
        cols, rows = self.cols, self.rows
        if len(data) < cols * rows * 6:
            return
        # Bulk path: one row-sliced memory copy per row instead of the
        # per-cell loop that used to build ~2 Color objects and 2 cells
        # per column. The RGB layer is all the renderer needs from a
        # video frame; it never emits the half-block buffer cells as
        # overlay text anyway.
        canvas.blit_subpixel_frame(data, cols, rows, ix, iy)


class Scene:
    """A list of widgets plus an AnimationManager. draw() renders the
    children in z order (skipping invisible / fully transparent ones);
    update(dt) advances the animations and calls each child's update()
    if it defines one. A child's update() may take dt or no arguments;
    the arity is probed once per widget type, and errors raised inside
    a child's update() propagate instead of being swallowed."""

    def __init__(self):
        self.children: list[Widget] = []
        self.animations = AnimationManager()
        self._draw_order: Optional[list[Widget]] = None
        self._z_signature: Optional[tuple[int, ...]] = None
        self._update_takes_arg: dict[type, bool] = {}

    def add(self, widget: Widget) -> Widget:
        self.children.append(widget)
        self._draw_order = None
        return widget

    def remove(self, widget: Widget) -> None:
        if widget in self.children:
            self.children.remove(widget)
            self._draw_order = None

    def _sorted_children(self) -> list[Widget]:
        children = self.children
        # Re-sort only when the membership or z-order actually changes;
        # the z signature check is O(n) per frame versus an O(n log n)
        # sort, and z can be animated so it can't be invalidated by
        # add/remove alone.
        sig = tuple(getattr(w, "z", 0) for w in children)
        if self._draw_order is None or sig != self._z_signature:
            self._draw_order = sorted(children, key=lambda w: getattr(w, "z", 0))
            self._z_signature = sig
        return self._draw_order

    def draw(self, canvas: Canvas) -> None:
        for child in self._sorted_children():
            if child.visible and child.opacity > 0.0:
                child.draw(canvas)

    def _update_takes_dt(self, child: Widget) -> bool:
        """True if child's update() accepts a dt argument. Probed once
        per widget type via inspect; if the signature can't be read,
        default to passing dt (an error inside update() then surfaces
        instead of being hidden)."""
        cls = type(child)
        cached = self._update_takes_arg.get(cls)
        if cached is not None:
            return cached
        takes_arg = True
        try:
            sig = inspect.signature(child.update)
            params = list(sig.parameters.values())
            positional = [
                p for p in params
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            var_positional = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
            )
            takes_arg = bool(positional) or var_positional
        except (ValueError, TypeError):
            takes_arg = True
        self._update_takes_arg[cls] = takes_arg
        return takes_arg

    def update(self, dt: float) -> None:
        self.animations.update(dt)
        for child in list(self.children):
            if hasattr(child, "update") and callable(child.update):
                if self._update_takes_dt(child):
                    child.update(dt)
                else:
                    child.update()


class FPSCounter:
    """Smoothed frames-per-second readout: recomputes from the frame
    count every 0.2s so the displayed value doesn't jitter frame to
    frame. str(counter) gives one decimal place."""

    def __init__(self):
        self._last_time: float = 0.0
        self._frames: int = 0
        self.value: float = 0.0

    def update(self, dt: float = 0.0) -> None:
        now = time.perf_counter()
        if self._last_time == 0.0:
            self._last_time = now
            if dt > 0:
                self.value = 1.0 / dt
            return
        self._frames += 1
        elapsed = now - self._last_time
        if elapsed >= 0.2:
            self.value = self._frames / elapsed
            self._frames = 0
            self._last_time = now
        elif self.value == 0.0 and dt > 0:
            self.value = 1.0 / dt

    def __str__(self) -> str:
        return f"{self.value:.1f}"


class App:
    """The main loop: owns the terminal (raw mode + alt screen), paces
    frames to target_fps, dispatches input, and drives the scene.

    Build the scene, register callbacks with the @app.on_* decorators
    (or app.ticker() for periodic callbacks), then call run(). run()
    blocks until app.stop() is called or Ctrl-C is pressed, and fully
    restores the terminal on exit, so several App() instances can run
    one after another in the same process.

    Constructor: App(title="Terminal Animation", target_fps=60.0,
    mouse=False, show_fps=False). target_fps must be a positive number.
    """

    def __init__(
        self,
        title: str = "Terminal Animation",
        target_fps: float = 60.0,
        *,
        mouse: bool = False,
        show_fps: bool = False,
    ):
        self.title = str(title)
        self.target_fps = float(target_fps) if target_fps and target_fps > 0 else 60.0
        self.mouse = mouse
        self.show_fps = show_fps
        self.scene = Scene()
        self.anim = self.scene.animations
        self.animations = self.anim
        self.fps = FPSCounter()
        self.frame_count: int = 0
        self.elapsed_time: float = 0.0
        self.running: bool = False
        self.canvas: Optional[Canvas] = None
        self._update_callbacks: list[Callable[[float], None]] = []
        self._render_callbacks: list[Callable[[Canvas], None]] = []
        self._key_callbacks: list[Callable[[KeyEvent], Any]] = []
        self._mouse_callbacks: list[Callable[[MouseEvent], Any]] = []
        self._ticker_lock = threading.Lock()
        self._tickers: list[Ticker] = []

    def on_update(self, fn: Callable[[float], None]) -> Callable[[float], None]:
        self._update_callbacks.append(fn)
        return fn

    def on_render(self, fn: Callable[[Canvas], None]) -> Callable[[Canvas], None]:
        self._render_callbacks.append(fn)
        return fn

    def on_key(self, fn: Callable[[KeyEvent], Any]) -> Callable[[KeyEvent], Any]:
        self._key_callbacks.append(fn)
        return fn

    def on_mouse(self, fn: Callable[[MouseEvent], Any]) -> Callable[[MouseEvent], Any]:
        self._mouse_callbacks.append(fn)
        return fn

    def stop(self) -> None:
        self.running = False

    def ticker(self, interval: float, fn: Callable[[], None]) -> Ticker:
        t = Ticker(interval, fn)
        with self._ticker_lock:
            self._tickers.append(t)
        return t

    def _run_tickers(self, dt: float) -> None:
        with self._ticker_lock:
            for t in self._tickers:
                t._tick(dt)

    def run(
        self,
        setup: Optional[Callable[[Canvas], None]] = None,
        frame: Optional[Callable[[Canvas, float], None]] = None,
    ) -> None:
        self.running = True
        self.frame_count = 0
        self.elapsed_time = 0.0
        with Terminal(mouse=self.mouse) as term:
            canvas = Canvas.create_from_terminal(term.width, term.height)
            self.canvas = canvas
            renderer = Renderer(term)
            inp = InputManager(term.stdin_fd)

            def on_resize(w: int, h: int) -> None:
                canvas.resize(w, h)

            term.on_resize(on_resize)
            if setup is not None:
                setup(canvas)

            target_dt = 1.0 / self.target_fps if self.target_fps > 0 else 1.0 / 60.0
            prev_time = time.perf_counter()

            while self.running:
                # Pace to the frame deadline: sleep the bulk of the
                # remaining wait, then spin the last sliver (<=1ms) so
                # the frame starts on time instead of early. Sleeping a
                # fixed fraction of the wait and proceeding unchecked
                # used to run frames faster than target_fps.
                deadline = prev_time + target_dt
                while self.running:
                    now = time.perf_counter()
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    if remaining > 0.001:
                        time.sleep(remaining - 0.0005)
                    else:
                        time.sleep(0)
                now = time.perf_counter()
                dt = now - prev_time
                prev_time = now
                self.elapsed_time += dt
                self.frame_count += 1

                for event in inp.poll():
                    if isinstance(event, KeyEvent):
                        if event.key == Key.CTRL_C:
                            self.running = False
                            break
                        for cb in self._key_callbacks:
                            res = cb(event)
                            if res is False:
                                self.running = False
                                break
                    elif isinstance(event, MouseEvent):
                        self._handle_mouse(event, canvas)
                        for cb in self._mouse_callbacks:
                            cb(event)

                if not self.running:
                    break

                term.poll_resize()
                self.animations.update(dt)
                self.scene.update(dt)
                self._run_tickers(dt)

                for cb in self._update_callbacks:
                    cb(dt)

                if not self.running:
                    break

                if frame is not None:
                    frame(canvas, dt)
                else:
                    canvas.clear()
                    self.scene.draw(canvas)
                    for cb in self._render_callbacks:
                        cb(canvas)

                self.fps.update(dt)
                if self.show_fps:
                    fps_val = self.fps.value if self.fps.value > 0 else (1.0 / max(0.001, dt))
                    fps_text = f" {fps_val:.1f} FPS "
                    canvas.draw_text(
                        max(0, canvas.width - len(fps_text)),
                        0,
                        fps_text,
                        Style(fg=Colors.BLACK, bg=Colors.CYAN, bold=True),
                    )

                renderer.render(canvas)

    def _handle_mouse(self, event: MouseEvent, canvas: Canvas) -> None:
        pass


__all__ = [
    "__version__",
    "old", "modern", "is_old",
    "native_render_available",
    "Color", "Colors", "Style", "DEFAULT_STYLE",
    "Cell", "Buffer", "char_width",
    "Terminal", "TerminalError",
    "Key", "KeyEvent", "MouseEvent", "InputManager",
    "Renderer", "Canvas",
    "Sprite",
    "Easing", "Tween", "AnimationManager", "Ticker",
    "Widget", "TextWidget", "BoxWidget", "SpriteWidget", "ProgressBarWidget", "VideoFrameWidget", "Scene",
    "AudioPlayer", "Sound", "Music",
    "App", "FPSCounter",
]
