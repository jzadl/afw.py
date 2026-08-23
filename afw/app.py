from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .terminal import Terminal
from .canvas import Canvas
from .renderer import Renderer
from ._input import InputManager, KeyEvent, MouseEvent, Key
from .animation import AnimationManager, Tween, _DummyTarget, Ticker
from .widgets import Scene
from .style import Style
from .color import Colors


class FPSCounter:
    def __init__(self):
        self._last_time: float = 0.0
        self._frames: int = 0
        self.value: float = 0.0

    def update(self, dt: float = 0.0) -> None:
        now = time.perf_counter()
        if self._last_time == 0.0:
            self._last_time = now
            if dt > 0:
                self.value = 1.0 / dt
            return
        self._frames += 1
        elapsed = now - self._last_time
        if elapsed >= 0.2:
            self.value = self._frames / elapsed
            self._frames = 0
            self._last_time = now
        elif self.value == 0.0 and dt > 0:
            self.value = 1.0 / dt

    def __str__(self) -> str:
        return f"{self.value:.1f}"


class App:
    def __init__(
        self,
        title: str = "Terminal Animation",
        target_fps: float = 60.0,
        *,
        mouse: bool = False,
        show_fps: bool = False,
    ):
        if isinstance(title, (int, float)):
            target_fps = float(title)
            title = "Terminal Animation"
        self.title = str(title)
        self.target_fps = float(target_fps) if target_fps and target_fps > 0 else 60.0
        self.mouse = mouse
        self.show_fps = show_fps
        self.scene = Scene()
        self.anim = self.scene.animations
        self.animations = self.anim
        self.fps = FPSCounter()
        self.frame_count: int = 0
        self.elapsed_time: float = 0.0
        self.running: bool = False
        self.canvas: Optional[Canvas] = None
        self._update_callbacks: list[Callable[[float], None]] = []
        self._render_callbacks: list[Callable[[Canvas], None]] = []
        self._key_callbacks: list[Callable[[KeyEvent], Any]] = []
        self._mouse_callbacks: list[Callable[[MouseEvent], Any]] = []
        self._ticker_lock = threading.Lock()
        self._tickers: list[Ticker] = []

    def on_update(self, fn: Callable[[float], None]) -> Callable[[float], None]:
        self._update_callbacks.append(fn)
        return fn

    def on_render(self, fn: Callable[[Canvas], None]) -> Callable[[Canvas], None]:
        self._render_callbacks.append(fn)
        return fn

    def on_key(self, fn: Callable[[KeyEvent], Any]) -> Callable[[KeyEvent], Any]:
        self._key_callbacks.append(fn)
        return fn

    def on_mouse(self, fn: Callable[[MouseEvent], Any]) -> Callable[[MouseEvent], Any]:
        self._mouse_callbacks.append(fn)
        return fn

    def stop(self) -> None:
        self.running = False

    def ticker(self, interval: float, fn: Callable[[], None]) -> Ticker:
        t = Ticker(interval, fn)
        with self._ticker_lock:
            self._tickers.append(t)
        return t

    def _run_tickers(self, dt: float) -> None:
        with self._ticker_lock:
            for t in self._tickers:
                t._tick(dt)

    def run(
        self,
        setup: Optional[Callable[[Canvas], None]] = None,
        frame: Optional[Callable[[Canvas, float], None]] = None,
    ) -> None:
        self.running = True
        self.frame_count = 0
        self.elapsed_time = 0.0
        with Terminal(mouse=self.mouse) as term:
            canvas = Canvas.create_from_terminal(term.width, term.height)
            self.canvas = canvas
            renderer = Renderer(term)
            inp = InputManager(term.stdin_fd)

            def on_resize(w: int, h: int) -> None:
                canvas.resize(w, h)

            term.on_resize(on_resize)
            if setup is not None:
                setup(canvas)

            target_dt = 1.0 / self.target_fps if self.target_fps > 0 else 1.0 / 60.0
            prev_time = time.perf_counter()

            while self.running:
                now = time.perf_counter()
                dt = now - prev_time
                if dt < target_dt:
                    wait = target_dt - dt
                    if wait > 0.001:
                        time.sleep(wait * 0.8)
                    time.sleep(0.0001)
                    now = time.perf_counter()
                    dt = now - prev_time
                prev_time = now
                self.elapsed_time += dt
                self.frame_count += 1

                for event in inp.poll():
                    if isinstance(event, KeyEvent):
                        if event.key == Key.CTRL_C:
                            self.running = False
                            break
                        for cb in self._key_callbacks:
                            res = cb(event)
                            if res is False:
                                self.running = False
                                break
                    elif isinstance(event, MouseEvent):
                        self._handle_mouse(event, canvas)
                        for cb in self._mouse_callbacks:
                            cb(event)

                if not self.running:
                    break

                term.poll_resize()
                self.animations.update(dt)
                self.scene.update(dt)
                self._run_tickers(dt)

                for cb in self._update_callbacks:
                    cb(dt)

                if not self.running:
                    break

                if frame is not None:
                    frame(canvas, dt)
                else:
                    canvas.clear()
                    self.scene.draw(canvas)
                    for cb in self._render_callbacks:
                        cb(canvas)

                self.fps.update(dt)
                if self.show_fps:
                    fps_val = self.fps.value if self.fps.value > 0 else (1.0 / max(0.001, dt))
                    fps_text = f" {fps_val:.1f} FPS "
                    canvas.draw_text(
                        max(0, canvas.width - len(fps_text)),
                        0,
                        fps_text,
                        Style(fg=Colors.BLACK, bg=Colors.CYAN, bold=True),
                    )

                renderer.render(canvas)

    def _handle_mouse(self, event: MouseEvent, canvas: Canvas) -> None:
        pass
