from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Union


class AudioPlayer:
    """Plays an audio file in a background process (ffplay, mpv, or
    aplay for .wav), so playback never blocks the render loop.

    play() returns False when no player backend is installed or the
    source file does not exist (URLs are exempt from the existence
    check). pause()/resume() use POSIX process groups and are not
    available on Windows, where they raise NotImplementedError instead
    of silently doing nothing. stop() always reaps the child process
    (terminate, then kill + wait) so no zombie is left behind.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._paused = False

    def play(
        self,
        source: Union[str, Path],
        *,
        loop: bool = False,
        volume: float = 1.0,
        start_time: float = 0.0,
    ) -> bool:
        self.stop()
        source_str = str(source)
        # A missing file used to return True and fail silently inside
        # the player (whose output goes to DEVNULL). URLs are exempt:
        # ffplay/mpv can stream them, and they are not local paths.
        if "://" not in source_str and not Path(source_str).exists():
            return False
        if not shutil.which("ffplay") and not shutil.which("mpv") and not shutil.which("aplay"):
            return False

        cmd: list[str] = []
        if shutil.which("ffplay"):
            cmd = [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel", "quiet",
            ]
            if loop:
                cmd += ["-loop", "0"]
            if start_time > 0:
                cmd += ["-ss", str(start_time)]
            if volume != 1.0:
                vol_val = max(0.0, min(2.0, volume)) * 100.0
                cmd += ["-volume", str(int(vol_val))]
            cmd.append(source_str)
        elif shutil.which("mpv"):
            cmd = [
                "mpv",
                "--no-video",
                "--really-quiet",
            ]
            if loop:
                cmd.append("--loop=inf")
            if start_time > 0:
                cmd += [f"--start={start_time}"]
            if volume != 1.0:
                cmd += [f"--volume={int(volume * 100)}"]
            cmd.append(source_str)
        elif shutil.which("aplay") and source_str.lower().endswith(".wav"):
            cmd = ["aplay", "-q", source_str]
        else:
            return False

        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._paused = False
                return True
            except Exception:
                self._proc = None
                return False

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None:
                proc = self._proc
                try:
                    proc.terminate()
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        # Always wait() after kill(): otherwise the
                        # child stays a zombie until the parent exits.
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=0.2)
                    except Exception:
                        pass
                self._proc = None
                self._paused = False

    def _require_posix_process_groups(self) -> None:
        if sys.platform == "win32":
            raise NotImplementedError(
                "pause()/resume() need POSIX process groups (os.killpg), "
                "which Windows does not provide"
            )

    def pause(self) -> None:
        self._require_posix_process_groups()
        with self._lock:
            if self._proc is not None and not self._paused:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGSTOP)
                    self._paused = True
                except Exception:
                    pass

    def resume(self) -> None:
        self._require_posix_process_groups()
        with self._lock:
            if self._proc is not None and self._paused:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGCONT)
                    self._paused = False
                except Exception:
                    pass

    def is_playing(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            return self._proc.poll() is None


class Sound:
    """A one-shot sound effect: wraps one audio file in its own
    AudioPlayer. See AudioPlayer for play/stop/pause semantics."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._player = AudioPlayer()

    def play(self, *, loop: bool = False, volume: float = 1.0) -> bool:
        return self._player.play(self.path, loop=loop, volume=volume)

    def stop(self) -> None:
        self._player.stop()

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        self._player.resume()

    @property
    def is_playing(self) -> bool:
        return self._player.is_playing()


class Music(Sound):
    """A music track: identical to Sound (a longer audio file played
    through its own AudioPlayer). Kept as a separate name so call sites
    can say what the audio is for."""

    pass
