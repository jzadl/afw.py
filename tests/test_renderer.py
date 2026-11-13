"""Tests for afw/renderer.py: pure-Python video rendering, overlay run
merging, and empty-row skipping."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw import Canvas, Renderer, VideoFrameWidget, Style, Colors


class FakeTerminal:
    """Stand-in for Terminal that captures output instead of writing it."""

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


def _pure_renderer():
    term = FakeTerminal()
    r = Renderer(term)
    r._native_lib = None  # force the pure-Python path
    r._native_fast = None
    return term, r


class TestPurePythonVideo(unittest.TestCase):
    def test_video_frame_is_not_blank_without_native_lib(self):
        # Regression: blit_subpixel_frame wrote only the RGB layer and
        # _build_ansi read only the text buffer, so video rendered as
        # all spaces when the .so was absent.
        term, r = _pure_renderer()
        c = Canvas(4, 2)
        frame = bytes((255, 0, 0, 0, 0, 255) * 8)
        w = VideoFrameWidget(0, 0, cols=4, rows=2)
        w.set_frame_rgb(frame)
        w.draw(c)
        r.render(c)
        out = "".join(term.text)
        self.assertIn("\u2580", out)
        self.assertIn("38;2;255;0;0", out)
        self.assertIn("48;2;0;0;255", out)

    def test_text_drawn_over_video_still_wins(self):
        term, r = _pure_renderer()
        c = Canvas(4, 2)
        frame = bytes((255, 0, 0, 0, 0, 255) * 8)
        w = VideoFrameWidget(0, 0, cols=4, rows=2)
        w.set_frame_rgb(frame)
        w.draw(c)
        c.draw_text(0, 0, "A")
        r.render(c)
        out = "".join(term.text)
        self.assertIn("A", out)


class TestEmptyRowSkipping(unittest.TestCase):
    def test_empty_rows_skipped_on_second_frame(self):
        term, r = _pure_renderer()
        c = Canvas(4, 2)
        c.draw_text(0, 0, "ab")
        r.render(c)  # first frame repaints everything
        term.text.clear()
        r.render(c)  # second frame: row 1 is (and was) blank
        out = "".join(term.text)
        self.assertIn("\x1b[1;1H", out)
        self.assertNotIn("\x1b[2;1H", out)

    def test_row_repainted_when_it_becomes_empty(self):
        term, r = _pure_renderer()
        c = Canvas(4, 2)
        c.draw_text(0, 1, "ab")
        r.render(c)
        term.text.clear()
        c.clear()
        r.render(c)  # row 1 was non-empty, now empty: must repaint
        out = "".join(term.text)
        self.assertIn("\x1b[2;1H", out)


class TestOverlayRunMerging(unittest.TestCase):
    def test_same_style_run_emits_one_position_escape(self):
        c = Canvas(10, 1)
        c.draw_text(0, 0, "hello", Style(fg=Colors.RED))
        out = Renderer._scan_text_overlay(c)
        self.assertEqual(out.count("\x1b[1;"), 1)
        self.assertIn("hello", out)

    def test_style_change_breaks_the_run(self):
        c = Canvas(10, 1)
        c.draw_text(0, 0, "ab", Style(fg=Colors.RED))
        c.draw_text(2, 0, "cd", Style(fg=Colors.GREEN))
        out = Renderer._scan_text_overlay(c)
        self.assertEqual(out.count("\x1b[1;"), 2)
        self.assertIn("ab", out)
        self.assertIn("cd", out)


if __name__ == "__main__":
    unittest.main()
