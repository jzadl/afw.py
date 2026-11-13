"""Tests for afw_media (the Zig converter): header round-trips, the
--fps cap, parallel vs sequential output, and audio extraction.

Skipped when ffmpeg is unavailable; the afw_media binary is built with
zig on demand if it is not already next to the repo root.
"""

import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _header(path):
    with open(path, "rb") as fh:
        return struct.unpack("<4sIIIf", fh.read(20))


class TestAfwMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise unittest.SkipTest("ffmpeg/ffprobe not available")
        cls.bin = ROOT / "afw_media"
        if not cls.bin.exists():
            zig = shutil.which("zig")
            if not zig:
                raise unittest.SkipTest("afw_media not built and zig not available")
            subprocess.run(
                [zig, "build-exe", "afw_media.zig", "-O", "ReleaseFast",
                 "-femit-bin", str(cls.bin)],
                cwd=ROOT, check=True, timeout=300,
            )
        cls.tmp = Path(tempfile.mkdtemp())
        # 2s, 30fps, 64x48 clip with an audio track.
        subprocess.run(
            ["ffmpeg", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=2:size=64x48:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-shortest", "-y", str(cls.tmp / "clip.mp4")],
            check=True, timeout=60,
        )
        subprocess.run(
            ["ffmpeg", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=0.1:size=32x24:rate=1",
             "-frames:v", "1", "-y", str(cls.tmp / "img.png")],
            check=True, timeout=60,
        )
        # Sequential + parallel conversions, shared by the tests below.
        cls.seq = cls.tmp / "seq.afwframes"
        subprocess.run(
            [str(cls.bin), str(cls.tmp / "clip.mp4"), "--cols", "20",
             "--fps", "12", "-o", str(cls.seq)],
            check=True, timeout=120, capture_output=True,
        )
        cls.par = cls.tmp / "par.afwframes"
        subprocess.run(
            [str(cls.bin), str(cls.tmp / "clip.mp4"), "--cols", "20",
             "--fps", "12", "--jobs", "2", "-o", str(cls.par)],
            check=True, timeout=120, capture_output=True,
        )

    def test_sequential_header_reports_capped_fps(self):
        magic, cols, rows, n, fps = _header(self.seq)
        self.assertEqual(magic, b"AFW1")
        self.assertEqual((cols, rows), (20, 15))
        self.assertEqual(fps, 12.0)  # --fps 12 on a 30fps source
        self.assertEqual(n, 24)  # 2s * 12fps

    def test_parallel_matches_sequential_frame_count(self):
        _, _, _, n_seq, fps_seq = _header(self.seq)
        _, _, _, n_par, fps_par = _header(self.par)
        self.assertEqual(n_par, n_seq)
        self.assertEqual(fps_par, fps_seq)

    def test_parallel_extracts_audio(self):
        # Regression: --jobs N never called extractAudioIfPresent.
        mp3 = self.tmp / "par.mp3"
        self.assertTrue(mp3.exists(), "parallel run produced no stem.mp3")
        self.assertGreater(mp3.stat().st_size, 0)

    def test_sequential_extracts_audio(self):
        mp3 = self.tmp / "seq.mp3"
        self.assertTrue(mp3.exists())
        self.assertGreater(mp3.stat().st_size, 0)

    def test_stream_header_reports_capped_fps(self):
        proc = subprocess.run(
            [str(self.bin), str(self.tmp / "clip.mp4"), "--cols", "20",
             "--fps", "12", "--stream"],
            stdout=subprocess.PIPE, timeout=120,
        )
        magic, cols, rows, n, fps = struct.unpack(
            "<4sIIIf", proc.stdout[:20])
        self.assertEqual(magic, b"AFW1")
        self.assertEqual(n, 0)  # live stream: unbounded
        self.assertEqual(fps, 12.0)

    def test_image_conversion_is_a_single_still_frame(self):
        out = self.tmp / "img.afwframes"
        subprocess.run(
            [str(self.bin), str(self.tmp / "img.png"), "--cols", "20",
             "-o", str(out)],
            check=True, timeout=120, capture_output=True,
        )
        _, _, _, n, fps = _header(out)
        self.assertEqual(n, 1)
        self.assertEqual(fps, 0.0)

    def test_generated_loader_has_no_ternary_statement(self):
        text = (self.tmp / "seq.py").read_text()
        self.assertNotIn("app.stop() if ", text)
        self.assertIn("elif app.frame_count > 1:", text)
        self.assertIn("clamped into", text)


if __name__ == "__main__":
    unittest.main()
