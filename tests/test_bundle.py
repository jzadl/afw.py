"""Tests for the generated single-file bundle (afw.py).

The bundle is regenerated from the package sources first, so it always
matches, then imported standalone from a clean directory (no afw/
package on the path) to prove it is self-contained.
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


class TestBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(
            [sys.executable, "builders/bundle.py"],
            cwd=ROOT, check=True, timeout=60,
        )

    def test_bundle_imports_standalone(self):
        import afw

        tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "afw.py", tmp / "afw.py")
        spec = importlib.util.spec_from_file_location("afw_bundle", tmp / "afw.py")
        mod = importlib.util.module_from_spec(spec)
        # Dataclasses with slots=True resolve types via sys.modules,
        # so the module must be registered before executing it.
        sys.modules["afw_bundle"] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop("afw_bundle", None)
        self.assertEqual(mod.__version__, afw.__version__)
        for name in ("App", "Canvas", "Renderer", "TextWidget", "Ticker",
                     "Cell", "VideoFrameWidget", "AnimationManager"):
            self.assertTrue(hasattr(mod, name), name)
        # The P0 clear() fix must be in the bundle, not just the package.
        c = mod.Canvas(10, 5)
        c.clear(mod.Color(255, 0, 0))
        self.assertEqual(len(c._rgb), 300)

    def test_package_version_present(self):
        import afw

        self.assertIsInstance(afw.__version__, str)
        self.assertTrue(afw.__version__)

    def test_bundle_has_no_unused_random_or_struct_import(self):
        text = (ROOT / "afw.py").read_text()
        self.assertNotIn("import random", text)
        self.assertNotIn("import struct", text)


if __name__ == "__main__":
    unittest.main()
