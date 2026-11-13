"""Tests for afw/canvas.py: clear(), put_subpixel(), flush_subpixels()."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw import Canvas, Color


class TestCanvasClear(unittest.TestCase):
    def test_clear_bg_keeps_rgb_length(self):
        # Regression: clear(bg) used to write 3 bytes/cell into a
        # 6-byte/cell buffer, shrinking the bytearray by half and
        # corrupting every later RGB read (native renderer included).
        c = Canvas(10, 5)
        c.clear(Color(255, 0, 0))
        self.assertEqual(len(c._rgb), 10 * 5 * 6)
        self.assertEqual(bytes(c._rgb), bytes((255, 0, 0, 255, 0, 0)) * 50)

    def test_clear_no_bg_zeroes_rgb(self):
        c = Canvas(10, 5)
        c.clear(Color(255, 0, 0))
        c.clear()
        self.assertEqual(bytes(c._rgb), bytes(10 * 5 * 6))
        self.assertFalse(c.has_rgb_data())

    def test_clear_bg_then_full_blit_still_fits(self):
        c = Canvas(4, 2)
        c.clear(Color(1, 2, 3))
        # Used to raise / write past the shrunken buffer.
        c.blit_subpixel_frame(bytes(range(48)), 4, 2)
        self.assertEqual(len(c._rgb), 48)


class TestPutSubpixel(unittest.TestCase):
    def test_bottom_only_defaults_fg_to_black(self):
        c = Canvas(4, 2)
        c.put_subpixel(1, 1, Color(255, 0, 0))  # bottom half, fresh cell
        cell = c.get_buffer().get(1, 0)
        self.assertEqual(cell.ch, "\u2580")
        self.assertEqual(cell.style.fg, Color(0, 0, 0))
        self.assertEqual(cell.style.bg, Color(255, 0, 0))

    def test_top_only_defaults_bg_to_black(self):
        c = Canvas(4, 2)
        c.put_subpixel(1, 0, Color(0, 255, 0))  # top half, fresh cell
        cell = c.get_buffer().get(1, 0)
        self.assertEqual(cell.ch, "\u2580")
        self.assertEqual(cell.style.fg, Color(0, 255, 0))
        self.assertEqual(cell.style.bg, Color(0, 0, 0))


class TestFlushSubpixels(unittest.TestCase):
    def test_flush_merges_rgb_into_text_buffer(self):
        c = Canvas(2, 1)
        c.blit_subpixel_frame(bytes((255, 0, 0, 0, 0, 255) * 2), 2, 1)
        c.flush_subpixels()
        cell = c.get_buffer().get(0, 0)
        self.assertEqual(cell.ch, "\u2580")
        self.assertEqual(cell.style.fg, Color(255, 0, 0))
        self.assertEqual(cell.style.bg, Color(0, 0, 255))

    def test_flush_does_not_clobber_text(self):
        c = Canvas(2, 1)
        c.draw_text(0, 0, "A")
        c.blit_subpixel_frame(bytes((255, 0, 0, 0, 0, 255) * 2), 2, 1)
        c.flush_subpixels()
        self.assertEqual(c.get_buffer().get(0, 0).ch, "A")

    def test_flush_without_rgb_is_noop(self):
        c = Canvas(2, 1)
        c.draw_text(0, 0, "A")
        c.flush_subpixels()
        self.assertEqual(c.get_buffer().get(0, 0).ch, "A")


if __name__ == "__main__":
    unittest.main()
