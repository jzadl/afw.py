"""Tests for afw/widgets.py Scene.update arity handling and opacity."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw import Canvas, Scene, TextWidget


class TestSceneUpdateArity(unittest.TestCase):
    def test_zero_arg_update_called_without_dt(self):
        class Plain:
            def __init__(self):
                self.calls = 0

            def update(self):
                self.calls += 1

        s = Scene()
        w = Plain()
        s.add(w)
        s.update(1 / 60)
        self.assertEqual(w.calls, 1)

    def test_dt_update_called_with_dt(self):
        class WithDt:
            def __init__(self):
                self.seen = None

            def update(self, dt):
                self.seen = dt

        s = Scene()
        w = WithDt()
        s.add(w)
        s.update(0.5)
        self.assertEqual(w.seen, 0.5)

    def test_error_inside_update_propagates(self):
        # Regression: Scene.update swallowed TypeError, hiding real
        # bugs inside widget update() methods.
        class Bad:
            def update(self, dt):
                raise RuntimeError("boom")

        s = Scene()
        s.add(Bad())
        with self.assertRaises(RuntimeError):
            s.update(1 / 60)


class TestOpacity(unittest.TestCase):
    def test_opacity_zero_hides_widget(self):
        scene = Scene()
        c = Canvas(10, 2)
        w = TextWidget("hi", 0, 0)
        w.opacity = 0.0
        scene.add(w)
        scene.draw(c)
        self.assertEqual(c.get_buffer().get(0, 0).ch, " ")

    def test_opacity_partial_still_draws_fully(self):
        # opacity is visibility-only, not blending.
        scene = Scene()
        c = Canvas(10, 2)
        w = TextWidget("hi", 0, 0)
        w.opacity = 0.5
        scene.add(w)
        scene.draw(c)
        self.assertEqual(c.get_buffer().get(0, 0).ch, "h")


if __name__ == "__main__":
    unittest.main()
