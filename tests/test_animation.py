"""Tests for afw/animation.py: Ticker(0) and Tween.retarget()."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw import Ticker, Tween


class TestTicker(unittest.TestCase):
    def test_zero_interval_fires_once_per_tick(self):
        # Regression: Ticker(0) spun forever in _tick (acc >= 0 is
        # always true) and hung the app.
        calls = []
        t = Ticker(0, lambda: calls.append(1))
        for _ in range(5):
            t._tick(1 / 60)
        self.assertEqual(len(calls), 5)

    def test_negative_interval_fires_once_per_tick(self):
        calls = []
        t = Ticker(-1, lambda: calls.append(1))
        for _ in range(3):
            t._tick(1 / 60)
        self.assertEqual(len(calls), 3)

    def test_positive_interval_fires_on_schedule(self):
        calls = []
        t = Ticker(0.1, lambda: calls.append(1))
        t._tick(0.25)
        self.assertEqual(len(calls), 2)

    def test_cancel_stops_calls(self):
        calls = []
        t = Ticker(0, lambda: calls.append(1))
        t.cancel()
        t._tick(1 / 60)
        self.assertEqual(len(calls), 0)


class TestTweenRetarget(unittest.TestCase):
    def test_retarget_resets_loop_state(self):
        # Regression: retarget() left _loop_count set, so a retargeted
        # loops=1 tween could finish immediately.
        class Dummy:
            x = 0.0

        d = Dummy()
        tw = Tween(d, "x", 10.0, 1.0, loops=1)
        tw.update(1.0)
        self.assertTrue(tw.finished)
        tw.retarget(20.0, 1.0)
        self.assertFalse(tw.finished)
        self.assertEqual(tw._loop_count, 0)
        self.assertEqual(tw._delay_elapsed, 0.0)
        tw.update(0.5)
        self.assertAlmostEqual(d.x, 15.0)

    def test_loops_one_finishes_after_one_pass(self):
        class Dummy:
            x = 0.0

        d = Dummy()
        tw = Tween(d, "x", 10.0, 1.0, loops=1)
        tw.update(0.5)
        self.assertFalse(tw.finished)
        tw.update(0.5)
        self.assertTrue(tw.finished)
        self.assertEqual(d.x, 10.0)


if __name__ == "__main__":
    unittest.main()
