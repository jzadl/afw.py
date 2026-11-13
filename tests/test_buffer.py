"""Tests for afw/buffer.py: Cell immutability and shared clears."""

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw.buffer import Buffer, Cell


class TestCellFrozen(unittest.TestCase):
    def test_cell_is_frozen(self):
        # Cells are shared by reference across the whole grid after a
        # clear(); one in-place mutation used to corrupt every slot.
        c = Cell("x")
        with self.assertRaises(FrozenInstanceError):
            c.ch = "y"

    def test_buffer_clear_shares_one_instance(self):
        b = Buffer(3, 2)
        b.clear()
        self.assertIs(b.cells[0], b.cells[1])
        self.assertIs(b.cells[0], b.cells[5])

    def test_set_replaces_without_affecting_shared_cells(self):
        b = Buffer(3, 2)
        b.clear()
        b.set(0, 0, Cell("Z"))
        self.assertEqual(b.get(0, 0).ch, "Z")
        self.assertEqual(b.get(1, 0).ch, " ")


if __name__ == "__main__":
    unittest.main()
