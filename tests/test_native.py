"""Tests for the native Zig renderer (libafw_render.so).

Skipped gracefully when the library is not built; the CI test job
builds it first, so these run there.
"""

import ctypes
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _unique_frame(seed):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(4 * 2 * 6))


class TestNativeRenderer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib_path = ROOT / "libafw_render.so"
        if not cls.lib_path.exists():
            raise unittest.SkipTest("libafw_render.so not built")

    def test_legacy_entry_point_round_trip(self):
        from afw._native import _load_native_render_lib

        lib = _load_native_render_lib()
        self.assertIsNotNone(lib)
        frame = _unique_frame(1)
        out = ctypes.create_string_buffer(4 * 2 * 40)
        n = lib.afw_render_frame(frame, None, 4, 2, out, len(out))
        self.assertGreater(n, 0)
        self.assertIn("\u2580".encode(), out.raw[:n])
        n2 = lib.afw_render_frame(frame, frame, 4, 2, out, len(out))
        self.assertEqual(n2, 0)  # identical frame: nothing to emit

    def test_fast_entry_point_round_trip(self):
        from afw._native import _load_native_render_fast

        fast = _load_native_render_fast()
        if fast is None:
            self.skipTest("lib without afw_render_frame_fast (old build)")
        frame = _unique_frame(2)
        out = ctypes.create_string_buffer(4 * 2 * 40)
        n = fast(frame, 4, 2, out, len(out), True)
        self.assertGreater(n, 0)
        n2 = fast(frame, 4, 2, out, len(out), False)
        self.assertEqual(n2, 0)
        frame2 = _unique_frame(3)
        n3 = fast(frame2, 4, 2, out, len(out), False)
        self.assertGreater(n3, 0)

    def test_renderer_end_to_end_with_native_lib(self):
        from afw import Canvas, Renderer, VideoFrameWidget

        class FakeTerminal:
            width = 80
            height = 24

            def __init__(self):
                self.text = []
                self.raw = []

            def write(self, s):
                self.text.append(s)

            def write_bytes(self, b):
                self.raw.append(bytes(b))

            def flush(self):
                pass

        term = FakeTerminal()
        r = Renderer(term)
        self.assertIsNotNone(r._native_lib)
        c = Canvas(4, 2)
        w = VideoFrameWidget(0, 0, cols=4, rows=2)
        w.set_frame_rgb(_unique_frame(4))
        w.draw(c)
        r.render(c)
        out = b"".join(term.raw)
        self.assertIn("\u2580".encode(), out)
        # Immediate re-render of the identical frame emits nothing.
        term.raw.clear()
        r.render(c)
        self.assertEqual(b"".join(term.raw), b"")


if __name__ == "__main__":
    unittest.main()
