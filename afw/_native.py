from __future__ import annotations

import sys
from pathlib import Path

_native_render_lib = None
_native_render_tried = False

if sys.platform == "win32":
    _NATIVE_LIB_NAME = "afw_render.dll"
elif sys.platform == "darwin":
    _NATIVE_LIB_NAME = "libafw_render.dylib"
else:
    _NATIVE_LIB_NAME = "libafw_render.so"


def _load_native_render_lib():
    global _native_render_lib, _native_render_tried
    if _native_render_tried:
        return _native_render_lib
    _native_render_tried = True
    try:
        import ctypes
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


def native_render_available() -> bool:
    return _load_native_render_lib() is not None
