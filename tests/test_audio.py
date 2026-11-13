"""Tests for afw/audio.py: missing files, and stop() reaping."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afw import AudioPlayer


class TestAudioPlayerMissingFile(unittest.TestCase):
    def test_play_missing_file_returns_false(self):
        # Regression: play() returned True for a nonexistent file and
        # the failure vanished silently into the player's DEVNULL.
        player = AudioPlayer()
        self.assertFalse(player.play("/nonexistent/nope.mp3"))
        self.assertFalse(player.play("also-missing.wav"))

    def test_play_missing_file_returns_false_for_sound(self):
        from afw import Sound

        self.assertFalse(Sound("/nonexistent/nope.mp3").play())


@unittest.skipUnless(
    shutil.which("ffplay") and shutil.which("ffmpeg"),
    "ffplay/ffmpeg not available",
)
class TestAudioPlayerStop(unittest.TestCase):
    def test_stop_reaps_child_process(self):
        tmp = Path(tempfile.mkdtemp())
        wav = tmp / "tone.wav"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=30", "-y", str(wav)],
            check=True, timeout=60,
        )
        # Headless machines have no audio device; SDL's dummy driver
        # lets ffplay run (and be stopped) anyway.
        old_driver = os.environ.get("SDL_AUDIODRIVER")
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        try:
            player = AudioPlayer()
            self.assertTrue(player.play(wav))
            time.sleep(0.5)
            if not player.is_playing():
                self.skipTest("ffplay could not play audio in this environment")
            pid = player._proc.pid
            player.stop()
            self.assertFalse(player.is_playing())
            # Reaped: the pid no longer exists (a zombie would still
            # answer os.kill(pid, 0)).
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if old_driver is None:
                os.environ.pop("SDL_AUDIODRIVER", None)
            else:
                os.environ["SDL_AUDIODRIVER"] = old_driver


if __name__ == "__main__":
    unittest.main()
