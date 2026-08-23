from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AFW_DIR = ROOT / "afw"
OUT_FILE = ROOT / "afw.py"

MODULE_ORDER = [
    "_compat.py",
    "_native.py",
    "color.py",
    "style.py",
    "buffer.py",
    "terminal.py",
    "_input.py",
    "sprite.py",
    "animation.py",
    "canvas.py",
    "renderer.py",
    "audio.py",
    "widgets.py",
    "app.py",
]

HEADER = """from __future__ import annotations

import atexit
import ctypes
import io
import math
import os
import random
import re
import select
import shutil
import signal
import struct
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
"""


def bundle():
    collected_code = [HEADER]

    for filename in MODULE_ORDER:
        filepath = AFW_DIR / filename
        if not filepath.exists():
            continue
        text = filepath.read_text(encoding="utf-8")
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("from __future__ import"):
                continue
            if stripped.startswith("from ."):
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                parts = stripped.split()
                if len(parts) >= 2 and parts[1] in (
                    "dataclasses", "enum", "math", "os", "sys", "time", "threading",
                    "typing", "unicodedata", "shutil", "signal", "termios", "tty",
                    "atexit", "re", "select", "io", "ctypes", "pathlib", "subprocess", "struct"
                ):
                    continue
            lines.append(line)

        cleaned = "\n".join(lines).strip()
        if cleaned:
            collected_code.append(cleaned)

    all_export = """
__all__ = [
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
"""
    collected_code.append(all_export.strip())
    final_text = "\n\n\n".join(collected_code) + "\n"
    OUT_FILE.write_text(final_text, encoding="utf-8")
    print(f"Generated unified {OUT_FILE} ({len(final_text)} bytes)")


if __name__ == "__main__":
    bundle()
