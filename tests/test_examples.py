"""Smoke tests for the examples and the stream player: every example
must at least import (which exercises its sys.path fix), and the
flagship fireworks demo must launch on a real pty and quit on 'q'.
"""

import fcntl
import importlib
import importlib.util
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = (
    "fireworks.py",
    "widget_showcase.py",
    "example.py",
    "example1.py",
    "example2.py",
    "example3.py",
)


class TestExamplesImport(unittest.TestCase):
    def test_all_examples_import(self):
        for name in EXAMPLES:
            with self.subTest(name=name):
                spec = importlib.util.spec_from_file_location(
                    f"_afw_example_{name[:-3]}", ROOT / "examples" / name)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

    def test_stream_player_imports(self):
        importlib.import_module("afw_stream_player")


@unittest.skipUnless(sys.platform != "win32", "pty smoke test needs a Unix pty")
class TestFireworksRuns(unittest.TestCase):
    def test_fireworks_launches_and_quits_on_q(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "examples" / "fireworks.py")],
            cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        stop = threading.Event()

        def drain():
            # The app repaints at 60fps; if nobody reads the master,
            # the pty buffer fills and the app blocks on write.
            while not stop.is_set():
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        try:
            time.sleep(2.0)
            self.assertIsNone(proc.poll(), "fireworks exited early")
            os.write(master, b"q")
            rc = proc.wait(timeout=5)
            self.assertEqual(rc, 0)
        finally:
            stop.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
