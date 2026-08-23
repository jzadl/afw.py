from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Optional, Union


class AudioPlayer:
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
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=0.2)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
                self._paused = False

    def pause(self) -> None:
        with self._lock:
            if self._proc is not None and not self._paused:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGSTOP)
                    self._paused = True
                except Exception:
                    pass

    def resume(self) -> None:
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
    pass
