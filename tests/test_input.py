"""Tests for afw/_input.py: split multi-byte UTF-8 input."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw import InputManager


class TestSplitUtf8(unittest.TestCase):
    def _pipe_manager(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        return InputManager(r), w

    def test_split_two_byte_char_reassembled(self):
        # Regression: the tail bytes of a character split across two
        # reads were stored in _pending_bytes but never read back.
        im, w = self._pipe_manager()
        encoded = "é".encode("utf-8")  # b'\xc3\xa9'
        os.write(w, encoded[:1])
        self.assertEqual(im.poll(), [])
        os.write(w, encoded[1:])
        events = im.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].char, "é")

    def test_split_three_byte_char_reassembled(self):
        im, w = self._pipe_manager()
        encoded = "€".encode("utf-8")  # b'\xe2\x82\xac'
        os.write(w, encoded[:1])
        self.assertEqual(im.poll(), [])
        os.write(w, encoded[1:2])
        self.assertEqual(im.poll(), [])
        os.write(w, encoded[2:])
        events = im.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].char, "€")

    def test_ascii_unaffected(self):
        im, w = self._pipe_manager()
        os.write(w, b"ab")
        events = im.poll()
        self.assertEqual([e.char for e in events], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
