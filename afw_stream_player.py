"""
afw_stream_player.py: real-time "watch it while it converts" playback.

Launches `afw_media --stream <video>` as a subprocess, reads decoded
frames off its stdout pipe as they arrive, and plays them back live
through a real afw.App/Scene; the video starts rendering before the
source file has finished being converted, exactly like a streaming
video player, and composes normally with any other afw.py widgets.

Rendering goes through afw.VideoFrameWidget, which uses
libafw_render.so (a Zig shared library, if built) to convert RGB frame
data to diffed half-block ANSI in a tight loop with zero allocations;
this is what makes full-resolution video playback fast on ordinary
(non-server) hardware, where the pure-Python per-cell path alone tends
to top out around 12fps on a fullscreen frame. The native path is used
automatically whenever a frame is pure video (nothing else drawn that
frame); as soon as you add other widgets on top (captions, a border,
controls), afw.py composites everything through the normal Python path
instead; same correct visual output, just the tradeoff you'd expect
for mixing UI with video. If libafw_render isn't built at all, every
codepath here still works, just slower; nothing requires the .so.

Two player classes and two widgets:
    StreamPlayer    : plays a live video file via afw_media --stream
    EmbeddedPlayer   : plays pre-converted .afwframes files, loops by
                       default, optional play/pause controls
    VideoWidget      : plays a FrameAsset at its own fixed pace,
                       composable in any Scene (used by EmbeddedPlayer)
    LiveVideoWidget  : shows whatever frame a live _PipeReader most
                       recently produced (used by StreamPlayer)

Usage as a script:
    python3 afw_stream_player.py path/to/video.mp4 --cols 100
    python3 afw_stream_player.py --embed clip.afwframes --controls

Usage as a library:
    from afw_stream_player import StreamPlayer, EmbeddedPlayer

    # live streaming
    player = StreamPlayer("video.mp4", cols=100, afw_media_path="./afw_media")
    player.play()  # blocks until the video ends or the user quits

    # pre-converted frames, looping with play/pause
    player = EmbeddedPlayer("clip.afwframes")
    player.play(controls=True)  # space to pause, q/ESC to quit

    # overlay your own UI on top of either player, via scene_setup:
    def add_caption(app, video_widget):
        app.scene.add(afw.TextWidget("My Video", 2, 1, z=1))
    player.play(scene_setup=add_caption)
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afw  # noqa: E402


HEADER_FORMAT = "<4sIIIf"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


def _detect_fps_ffprobe(video_path: str) -> Optional[float]:
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "csv=p=0",
                video_path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode().strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        num, den = out.split("/")
        den_f = float(den)
        if den_f == 0:
            return None
        return float(num) / den_f
    except ValueError:
        return None


class StreamDecodeError(Exception):
    """Raised when the afw_media subprocess fails or emits a malformed stream."""


class _PipeReader(threading.Thread):
    """
    Background thread that pulls frames off the afw_media subprocess's
    stdout pipe as fast as they arrive and pushes them onto a bounded
    deque for the render loop to consume.

    Running this on its own thread (rather than reading synchronously
    inside App's render loop) is what lets decoding run ahead of
    playback: ffmpeg/afw_media keep producing frames continuously while
    the main thread renders at its own steady 60fps pace, instead of the
    render loop stalling on every single blocking read() call.

    The deque is bounded (maxlen) to provide backpressure: if decoding
    runs far ahead of playback (fast source, slow terminal), we simply
    stop appending new frames until the render side consumes some,
    rather than buffering an unbounded amount of decoded video in
    memory. Old frames are never silently dropped from the middle;
    this is a FIFO queue, not a "keep only the latest" ring buffer,
    because dropping frames would make playback stutter/skip rather
    than just briefly pause, and pausing is the better tradeoff for a
    video player.
    """

    def __init__(self, pipe, cols: int, rows: int, max_buffered_frames: int = 8):
        super().__init__(daemon=True)
        self._pipe = pipe
        self._frame_bytes = cols * rows * 6
        self.frames: deque[bytes] = deque(maxlen=None)  # unbounded storage...
        self._max_buffered = max_buffered_frames  # ...but gated by this via a condition
        self._lock = threading.Lock()
        self._not_full = threading.Condition(self._lock)
        self._not_empty = threading.Condition(self._lock)
        self.error: Optional[Exception] = None
        self.finished = False
        self.frames_read = 0

    def run(self) -> None:
        try:
            while True:
                data = _read_exact(self._pipe, self._frame_bytes)
                if data is None:
                    break  # clean EOF: stream ended normally
                with self._not_full:
                    while len(self.frames) >= self._max_buffered:
                        # Backpressure: decoding is ahead of playback.
                        # Wait for the render side to consume a frame
                        # instead of growing memory unboundedly. A short
                        # timeout keeps this responsive to stop().
                        self._not_full.wait(timeout=0.5)
                        if self.finished:
                            return
                    self.frames.append(data)
                    self.frames_read += 1
                    self._not_empty.notify()
        except Exception as e:  # noqa: BLE001 - surfaced to the main thread via .error
            self.error = e
        finally:
            with self._lock:
                self.finished = True
                self._not_empty.notify_all()

    def pop_frame(self, timeout: float = 0.5) -> Optional[bytes]:
        """Blocks up to `timeout` seconds for the next frame. Returns
        None if none arrived in time (caller should re-check .finished
        and .error) or if the stream has genuinely ended with nothing
        left buffered."""
        with self._not_empty:
            if not self.frames and not self.finished:
                self._not_empty.wait(timeout=timeout)
            if self.frames:
                data = self.frames.popleft()
                self._not_full.notify()
                return data
            return None

    def stop(self) -> None:
        with self._lock:
            self.finished = True
            self._not_full.notify_all()
            self._not_empty.notify_all()


def _read_exact(pipe, n: int) -> Optional[bytes]:
    """Reads exactly n bytes from a pipe, or None on a clean EOF that
    happens right at a frame boundary (zero bytes read before anything
    else came in). Raises on a truncated read (partial frame, pipe
    closed unexpectedly mid-write) so a cut-off stream is never silently
    treated as "video just ended normally"."""
    buf = bytearray()
    while len(buf) < n:
        chunk = pipe.read(n - len(buf))
        if not chunk:
            if len(buf) == 0:
                return None
            raise StreamDecodeError(
                f"stream ended mid-frame ({len(buf)}/{n} bytes); afw_media "
                f"may have crashed or the source video is corrupt"
            )
        buf.extend(chunk)
    return bytes(buf)


class StreamPlayer:
    """
    Orchestrates: spawn afw_media --stream, read its header, launch a
    background _PipeReader, and drive a render loop that uses
    libafw_render.so (Zig) to convert frames to ANSI at 1000+ fps.
    Python handles terminal setup, input, and fps pacing.
    """

    def __init__(
        self,
        video_path: str,
        *,
        cols: int = 100,
        rows: Optional[int] = None,
        fit: str = "contain",
        afw_media_path: str = "./afw_media",
        max_buffered_frames: int = 8,
        loop: bool = False,
        target_fps: Optional[float] = None,
        audio: bool = False,
    ):
        self.video_path = video_path
        self.cols = cols
        self.rows = rows
        self.fit = fit
        self.afw_media_path = afw_media_path
        self.max_buffered_frames = max_buffered_frames
        self.loop = loop
        self.target_fps = target_fps
        self.audio = audio
        self._audio_player = afw.AudioPlayer() if hasattr(afw, "AudioPlayer") else None

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[_PipeReader] = None
        self.stream_cols = 0
        self.stream_rows = 0
        self.stream_fps = 0.0

    def _spawn(self) -> None:
        cmd = [self.afw_media_path, self.video_path, "--stream", "--cols", str(self.cols), "--fit", self.fit]
        if self.rows is not None:
            cmd += ["--rows", str(self.rows)]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,  # never let the child compete with
            # afw.py's InputManager for bytes off the terminal's stdin;
            # without this, afw_media (and the ffmpeg it spawns) inherit
            # the real tty fd and can silently steal keypresses meant for
            # the Python-side app, which is exactly the kind of bug that
            # only shows up the second time you open an App() in the same
            # process (e.g. returning to a video picker after playback).
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # unbuffered: we do our own exact-size reads
            start_new_session=True,  # detach from our controlling
            # terminal entirely (own process group/session) so the child
            # (and ffmpeg, which it spawns) can never become the
            # foreground process group of our tty and steal signals like
            # SIGINT/SIGWINCH or interfere with termios state; without
            # this, a second afw.py App() opened later in the same
            # process can hang initializing raw mode.
        )
        # Drain the subprocess's stderr (progress/log lines) on a
        # background thread for the same reason afw_media itself drains
        # ffmpeg's stderr: an unread pipe fills its OS buffer and blocks
        # the writer, which would eventually stall frame production.
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        header = _read_exact(self._proc.stdout, HEADER_SIZE)
        if header is None:
            raise StreamDecodeError(
                f"afw_media produced no output at all; check that {self.afw_media_path!r} "
                f"is a valid path to the compiled binary, and that {self.video_path!r} exists"
            )
        magic, cols, rows, _frame_count_field, fps = struct.unpack(HEADER_FORMAT, header)
        if magic != b"AFW1":
            raise StreamDecodeError(f"bad stream header magic {magic!r} (expected b'AFW1')")
        self.stream_cols = cols
        self.stream_rows = rows
        self.stream_fps = fps if fps > 0 else 24.0

        self._reader = _PipeReader(
            self._proc.stdout, cols, rows, max_buffered_frames=self.max_buffered_frames
        )
        self._reader.start()
        if self.audio and self._audio_player is not None:
            self._audio_player.play(self.video_path, loop=self.loop)

    def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                chunk = self._proc.stderr.read(4096)
                if not chunk:
                    break
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self._audio_player is not None:
            self._audio_player.stop()
        if self._reader is not None:
            self._reader.stop()
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()

    def play(self, *, show_fps: bool = True, on_key=None, scene_setup=None) -> None:
        """
        Blocks, playing the video through a real afw.App until the
        stream ends (or loops forever if loop=True) or the user quits.
        Runs through the normal App/Scene/Widget pipeline; the native
        Zig renderer is used automatically under the hood for the video
        frame itself (via LiveVideoWidget -> VideoFrameWidget), and
        falls back to pure Python transparently if libafw_render isn't
        built, with identical visual output either way.

        `scene_setup(app, video_widget)`, if given, is called once after
        the video widget is added to the scene (at z=0) and before
        app.run(); use it to add your own widgets on top (captions,
        controls, a border) at z=1+. Adding anything this way means
        each frame composites through the normal path rather than the
        pure-native fast path (Canvas.buffer_has_content() will be
        True), which is the correct and expected tradeoff for mixing UI
        with video.
        """
        self._spawn()
        assert self._reader is not None

        fps = self.target_fps
        if not fps:
            fps = _detect_fps_ffprobe(self.video_path) or self.stream_fps
        if not fps or fps <= 0:
            fps = 24.0

        app = afw.App(target_fps=fps, show_fps=show_fps)
        video = app.scene.add(
            LiveVideoWidget(self._reader, self.stream_cols, self.stream_rows, 0, 0, z=0)
        )

        if scene_setup is not None:
            scene_setup(app, video)

        @app.on_update
        def update(dt):
            if video._reader.finished and not video._reader.frames and video._have_frame:
                if video._reader.error is not None:
                    raise video._reader.error
                if self.loop:
                    self.close()
                    self._spawn()
                    video._reader = self._reader
                    video._have_frame = False
                else:
                    app.stop()

        @app.on_key
        def handle_key(ev):
            if on_key is not None:
                on_key(ev, app)
            if ev.key == afw.Key.ESCAPE or ev.is_char("q") or ev.key == afw.Key.CTRL_C:
                app.stop()

        try:
            app.run()
        finally:
            self.close()


class FrameAsset:
    """
    Loads a .afwframes file and provides random-access frame retrieval
    via seek. Does not load all frames into memory: each get_frame()
    seeks to the right offset and reads exactly one frame.

    Usage:
        asset = FrameAsset("clip.afwframes")
        print(f"{asset.frame_count} frames at {asset.fps}fps, "
              f"{asset.cols}x{asset.rows}")
        frame_bytes = asset.get_frame(0)
    """

    def __init__(self, path: str):
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"afwframes file not found: {path}")
        if p.stat().st_size < HEADER_SIZE:
            raise StreamDecodeError(
                f"file too small to contain a valid header: {path} "
                f"({p.stat().st_size} bytes)"
            )
        self._path = p
        self._fh = open(p, "rb")
        header = self._fh.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise StreamDecodeError(
                f"could not read full header from {path}"
            )
        magic, cols, rows, frame_count, fps = struct.unpack(
            HEADER_FORMAT, header
        )
        if magic != b"AFW1":
            raise StreamDecodeError(
                f"bad magic {magic!r} in {path} (expected b'AFW1')"
            )
        if cols == 0 or rows == 0:
            raise StreamDecodeError(
                f"invalid dimensions in {path}: {cols}x{rows}"
            )
        self._cols = cols
        self._rows = rows
        self._fps = float(fps) if fps > 0 else 0.0
        self._frame_bytes = cols * rows * 6

        file_size = p.stat().st_size
        max_frames_by_size = (file_size - HEADER_SIZE) // self._frame_bytes

        if frame_count > 0:
            if frame_count > max_frames_by_size:
                raise StreamDecodeError(
                    f"header claims {frame_count} frames but file only "
                    f"contains {max_frames_by_size} (file may be truncated)"
                )
            self._frame_count = frame_count
        else:
            self._frame_count = max_frames_by_size

        if self._frame_count == 0:
            raise StreamDecodeError(
                f"file contains no frames: {path}"
            )

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def get_frame(self, index: int) -> bytes:
        if index < 0 or index >= self._frame_count:
            raise IndexError(
                f"frame index {index} out of range [0, {self._frame_count})"
            )
        offset = HEADER_SIZE + index * self._frame_bytes
        self._fh.seek(offset)
        data = self._fh.read(self._frame_bytes)
        if len(data) < self._frame_bytes:
            raise StreamDecodeError(
                f"truncated read at frame {index}: got {len(data)}/"
                f"{self._frame_bytes} bytes (file may be corrupt)"
            )
        return data

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_HALF = "▀"
_FULL = "█"


class VideoWidget(afw.VideoFrameWidget):
    """
    Plays frames from a FrameAsset as a background widget, on its own
    playback clock (independent of the frame afw.py itself renders at).
    Thin wrapper around afw.VideoFrameWidget; it handles frame timing
    and pulls bytes from the asset; VideoFrameWidget handles getting
    those bytes on screen (through the native Zig fast path when nothing
    else is drawn that frame, or the composited path automatically when
    something is, e.g. UI widgets layered on top at a higher z).

    Designed for compositing: add it at z=0 and put your UI widgets at
    z=1+ to draw on top. Loops by default.

    Example: a video background with UI on top

        import afw
        from afw_stream_player import FrameAsset, VideoWidget

        app = afw.App(target_fps=30)
        asset = FrameAsset("login_bg.afwframes")

        bg = app.scene.add(VideoWidget(asset, x=0, y=0, z=0))
        title = app.scene.add(afw.TextWidget(
            "Welcome", 10, 20, afw.Style(fg=afw.Colors.WHITE, bold=True), z=1,
        ))

        @app.on_key
        def handle(ev):
            if ev.is_char("q") or ev.key == afw.Key.ESCAPE:
                app.stop()

        app.run()

    Controls:
        .paused = True/False  to pause/resume playback
        .loop = True/False    to toggle looping
    """

    def __init__(
        self,
        asset: FrameAsset,
        x: float = 0,
        y: float = 0,
        *,
        z: int = 0,
        loop: bool = True,
        paused: bool = False,
        play_fps: Optional[float] = None,
    ):
        super().__init__(x, y, cols=asset.cols, rows=asset.rows, z=z)
        self._asset = asset
        self._fps = play_fps if play_fps else (asset.fps if asset.fps > 0 else 15.0)
        self._frame_dur = 1.0 / self._fps if self._fps > 0 else 0.0
        self._acc = 0.0
        self._frame_idx = 0
        self.loop = loop
        self.paused = paused
        self.finished = False
        self.set_frame_rgb(asset.get_frame(0))

    def update(self, dt: float) -> None:
        if self.paused or self.finished:
            return
        if self._frame_dur <= 0:
            return
        self._acc += dt
        advanced = False
        while self._acc >= self._frame_dur:
            self._acc -= self._frame_dur
            self._frame_idx += 1
            advanced = True
            if self._frame_idx >= self._asset.frame_count:
                if self.loop:
                    self._frame_idx = 0
                else:
                    self._frame_idx = self._asset.frame_count - 1
                    self.finished = True
                    break
        if advanced:
            try:
                self.set_frame_rgb(self._asset.get_frame(self._frame_idx))
            except (IndexError, StreamDecodeError):
                pass

    def reset(self) -> None:
        self._frame_idx = 0
        self._acc = 0.0
        self.finished = False
        try:
            self.set_frame_rgb(self._asset.get_frame(0))
        except (IndexError, StreamDecodeError):
            pass


class LiveVideoWidget(afw.VideoFrameWidget):
    """
    Displays frames as they arrive from a live _PipeReader (used by
    StreamPlayer). Unlike VideoWidget (which paces itself against a
    known, fixed asset framerate), this shows whatever frame is most
    recently available; decoding may briefly lag playback, in which
    case the last frame is held on screen rather than left blank.
    """

    def __init__(self, reader: "_PipeReader", cols: int, rows: int, x: float = 0, y: float = 0, *, z: int = 0):
        super().__init__(x, y, cols=cols, rows=rows, z=z)
        self._reader = reader
        self._have_frame = False
        self.stalled = False

    def update(self, dt: float) -> None:
        data = self._reader.pop_frame(timeout=0.0)
        if data is not None:
            self.set_frame_rgb(data)
            self._have_frame = True
            self.stalled = False
        elif self._have_frame:
            # Decoding hasn't produced the next frame yet: hold the last
            # one rather than showing blank; a brief pause reads far
            # better than a stutter/skip.
            self.stalled = True

    def render(self, canvas: "afw.Canvas") -> None:
        if not self._have_frame:
            return
        super().render(canvas)


class EmbeddedPlayer:
    """
    Plays pre-converted frames from a .afwframes file (or a FrameAsset)
    using the Zig renderer. Loops by default. Optionally supports
    play/pause controls.

    Usage:
        from afw_stream_player import EmbeddedPlayer

        player = EmbeddedPlayer("clip.afwframes")
        player.play()  # loops forever, q/ESC to quit

        player = EmbeddedPlayer("clip.afwframes")
        player.play(controls=True)  # space to pause/play, q/ESC to quit

        # Non-looping:
        player = EmbeddedPlayer("clip.afwframes", loop=False)
        player.play()  # plays once, then exits

    Edge cases handled:
        - Still image (fps=0): shown once, then waits for user input
        - Single frame: shown, loop or wait depending on loop flag
        - Terminal resize: full repaint on next frame
        - Missing Zig lib: RuntimeError with build instructions
        - Empty/corrupt file: caught at FrameAsset construction
    """

    def __init__(
        self,
        source,
        *,
        loop: bool = True,
    ):
        if isinstance(source, FrameAsset):
            self._asset = source
            self._owns_asset = False
        elif isinstance(source, (str, Path)):
            self._asset = FrameAsset(str(source))
            self._owns_asset = True
        else:
            raise TypeError(
                "source must be a path string, Path, or FrameAsset, "
                f"got {type(source).__name__}"
            )
        self.loop = loop

    def play(
        self,
        *,
        show_fps: bool = True,
        controls: bool = False,
        on_key=None,
        scene_setup=None,
    ) -> None:
        """
        Blocks, playing frames through a real afw.App until the user
        quits (q/ESC). If loop=False, exits after one pass.

        controls=True adds: space to toggle play/pause, q/ESC to quit.
        controls=False: only q/ESC to quit (no pause).

        `scene_setup(app, video_widget)`, if given, is called once
        before app.run() so you can add extra widgets (captions,
        controls) on top at z=1+; same contract as StreamPlayer.play().
        """
        fps = self._asset.fps if self._asset.fps > 0 else 15.0
        app = afw.App(target_fps=fps, show_fps=show_fps)
        video = app.scene.add(VideoWidget(self._asset, 0, 0, z=0, loop=self.loop))

        if scene_setup is not None:
            scene_setup(app, video)

        paused_label = app.scene.add(afw.TextWidget("", 0, 0, z=2))

        @app.on_update
        def update(dt):
            if video.finished:
                app.stop()

        @app.on_key
        def handle_key(ev):
            if on_key is not None:
                on_key(ev, app)
            if controls and ev.is_char(" "):
                video.paused = not video.paused
                if video.paused:
                    label = " paused "
                    paused_label.text = label
                    paused_label.style = afw.Style(fg=afw.Colors.BLACK, bg=afw.Colors.YELLOW)
                    paused_label.x = max(0, app.canvas.width - len(label))
                else:
                    paused_label.text = ""
            if ev.key == afw.Key.ESCAPE or ev.is_char("q") or ev.key == afw.Key.CTRL_C:
                app.stop()

        try:
            app.run()
        finally:
            if self._owns_asset:
                self._asset.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Play video in the terminal via afw.py + afw_media."
    )
    parser.add_argument("input", help="Path to a video file or .afwframes asset.")
    parser.add_argument("--cols", type=int, default=100)
    parser.add_argument("--rows", type=int, default=None)
    parser.add_argument("--fit", choices=["contain", "cover", "stretch"], default="contain")
    parser.add_argument("--afw-media", default="./afw_media", help="Path to the compiled afw_media binary.")
    parser.add_argument("--loop", action="store_true", help="Loop playback (stream mode).")
    parser.add_argument("--audio", action="store_true", help="Play audio along with video.")
    parser.add_argument("--no-fps", action="store_true", help="Hide the fps counter overlay.")
    parser.add_argument("--embed", action="store_true",
                        help="Play a pre-converted .afwframes file instead of live streaming.")
    parser.add_argument("--controls", action="store_true",
                        help="Enable play/pause (space) in embed mode.")
    parser.add_argument("--no-loop-embed", action="store_true",
                        help="Disable looping in embed mode (play once).")
    args = parser.parse_args()

    if args.embed:
        player = EmbeddedPlayer(
            args.input,
            loop=not args.no_loop_embed,
        )
        player.play(show_fps=not args.no_fps, controls=args.controls)
    else:
        player = StreamPlayer(
            args.input,
            cols=args.cols,
            rows=args.rows,
            fit=args.fit,
            afw_media_path=args.afw_media,
            loop=args.loop,
            audio=args.audio,
        )
        player.play(show_fps=not args.no_fps)


if __name__ == "__main__":
    main()
