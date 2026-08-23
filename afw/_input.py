from __future__ import annotations

import os
import re
import select
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal, Optional, Union


class Key(Enum):
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
    key: Optional[Key]
    char: Optional[str]
    ctrl: bool = False
    alt: bool = False

    def is_char(self, c: str) -> bool:
        return self.char == c


@dataclass(frozen=True, slots=True)
class MouseEvent:
    x: int
    y: int
    button: Literal["left", "middle", "right", "release", "wheel_up", "wheel_down", "move"]
    pressed: bool


_SGR_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


class InputManager:
    def __init__(self, fd: int):
        self._fd = fd
        self._buf = ""

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
        raw = b"".join(chunks)
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
