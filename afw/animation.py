from __future__ import annotations

import math
from typing import Any, Callable, Optional


def _interpolate_value(a: Any, b: Any, t: float) -> Any:
    if isinstance(a, float) and isinstance(b, float):
        return a + (b - a) * t
    if isinstance(a, int) and isinstance(b, int):
        return int(a + (b - a) * t)
    if isinstance(a, tuple) and isinstance(b, tuple) and len(a) == len(b):
        return type(a)(_interpolate_value(x, y, t) for x, y in zip(a, b))
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [_interpolate_value(x, y, t) for x, y in zip(a, b)]
    return b if t >= 1.0 else a


class Easing:
    @staticmethod
    def linear(t: float) -> float:
        return t

    @staticmethod
    def ease_in_quad(t: float) -> float:
        return t * t

    @staticmethod
    def ease_out_quad(t: float) -> float:
        return 1 - (1 - t) * (1 - t)

    @staticmethod
    def ease_in_out_quad(t: float) -> float:
        return 2 * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 2) / 2

    @staticmethod
    def ease_in_cubic(t: float) -> float:
        return t ** 3

    @staticmethod
    def ease_out_cubic(t: float) -> float:
        return 1 - pow(1 - t, 3)

    @staticmethod
    def ease_in_out_cubic(t: float) -> float:
        return 4 * t ** 3 if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2

    @staticmethod
    def ease_out_bounce(t: float) -> float:
        n1, d1 = 7.5625, 2.75
        if t < 1 / d1:
            return n1 * t * t
        elif t < 2 / d1:
            t -= 1.5 / d1
            return n1 * t * t + 0.75
        elif t < 2.5 / d1:
            t -= 2.25 / d1
            return n1 * t * t + 0.9375
        else:
            t -= 2.625 / d1
            return n1 * t * t + 0.984375

    @staticmethod
    def ease_in_bounce(t: float) -> float:
        return 1 - Easing.ease_out_bounce(1 - t)

    @staticmethod
    def ease_out_elastic(t: float) -> float:
        if t == 0 or t == 1:
            return t
        c4 = (2 * math.pi) / 3
        return pow(2, -10 * t) * math.sin((t * 10 - 0.75) * c4) + 1

    @staticmethod
    def ease_in_elastic(t: float) -> float:
        if t == 0 or t == 1:
            return t
        c4 = (2 * math.pi) / 3
        return -pow(2, 10 * t - 10) * math.sin((t * 10 - 10.75) * c4)

    @staticmethod
    def ease_out_back(t: float) -> float:
        c1 = 1.70158
        c3 = c1 + 1
        return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)

    @staticmethod
    def ease_in_back(t: float) -> float:
        c1 = 1.70158
        c3 = c1 + 1
        return c3 * t * t * t - c1 * t * t

    @staticmethod
    def ease_in_out_sine(t: float) -> float:
        return -(math.cos(math.pi * t) - 1) / 2


EasingFn = Callable[[float], float]


class Tween:
    def __init__(
        self,
        target: Any,
        attr: str,
        end_value: Any,
        duration: float,
        *,
        easing: EasingFn = Easing.linear,
        delay: float = 0.0,
        loops: int = 1,
        yoyo: bool = False,
        on_update: Optional[Callable[[Any], None]] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        self.target = target
        self.attr = attr
        self.start_value = getattr(target, attr)
        self.end_value = end_value
        self.duration = max(0.0, duration)
        self.easing = easing
        self.delay = max(0.0, delay)
        self.loops = loops
        self.yoyo = yoyo
        self.on_update = on_update
        self.on_complete = on_complete
        self._elapsed = 0.0
        self._delay_elapsed = 0.0
        self._loop_count = 0
        self._forward = True
        self.finished = False
        self._started = False

    def retarget(self, new_end_value: Any, duration: Optional[float] = None) -> None:
        current = getattr(self.target, self.attr)
        self.start_value = current
        self.end_value = new_end_value
        if duration is not None:
            self.duration = max(0.0, duration)
        self._elapsed = 0.0
        self.finished = False
        self._forward = True

    def stop(self, *, finish: bool = False) -> None:
        if finish and not self.finished:
            self._apply(1.0 if self._forward else 0.0)
        self.finished = True

    def _apply(self, t: float) -> None:
        eased_t = self.easing(t)
        value = _interpolate_value(self.start_value, self.end_value, eased_t)
        setattr(self.target, self.attr, value)
        if self.on_update:
            try:
                self.on_update(value)
            except Exception:
                pass

    def update(self, dt: float) -> None:
        if self.finished:
            return
        if self._delay_elapsed < self.delay:
            self._delay_elapsed += dt
            if self._delay_elapsed < self.delay:
                return
            dt = self._delay_elapsed - self.delay
        if self.duration <= 0:
            self._apply(1.0 if self._forward else 0.0)
            self._advance_loop()
            return
        self._elapsed += dt
        t = min(1.0, self._elapsed / self.duration)
        self._apply(t if self._forward else 1.0 - t)
        if t >= 1.0:
            self._advance_loop()

    def _advance_loop(self) -> None:
        self._loop_count += 1
        if self.loops >= 0 and self._loop_count >= self.loops and not (self.yoyo and self._loop_count == self.loops and self._forward):
            if self.yoyo and self._forward and self.loops == 1:
                pass
            else:
                self.finished = True
                if self.on_complete:
                    try:
                        self.on_complete()
                    except Exception:
                        pass
                return
        if self.yoyo:
            self._forward = not self._forward
        self._elapsed = 0.0
        if self.loops >= 0 and self._loop_count >= self.loops and not self.yoyo:
            self.finished = True
            if self.on_complete:
                try:
                    self.on_complete()
                except Exception:
                    pass


class _DummyTarget:
    v: float = 0.0


class AnimationManager:
    def __init__(self):
        self._tweens: list[Tween] = []

    def add(self, tween: Tween) -> Tween:
        self._tweens.append(tween)
        return tween

    def animate(self, target: Any, attr: str, end_value: Any, duration: float, **kwargs) -> Tween:
        tween = Tween(target, attr, end_value, duration, **kwargs)
        return self.add(tween)

    def update(self, dt: float) -> None:
        if not self._tweens:
            return
        any_finished = False
        for tween in self._tweens:
            tween.update(dt)
            if tween.finished:
                any_finished = True
        # Rebuilding the active list every frame costs a full list
        # traversal + allocation even in the common "everything still
        # running" case; only compact when something actually finished.
        if any_finished:
            self._tweens = [t for t in self._tweens if not t.finished]

    def clear(self) -> None:
        self._tweens.clear()

    def count(self) -> int:
        return len(self._tweens)


class Ticker:
    def __init__(self, interval: float, fn: Callable[[], None]):
        self.interval = max(0.0, interval)
        self.fn = fn
        self._acc = 0.0
        self._cancelled = False

    def _tick(self, dt: float) -> None:
        if self._cancelled:
            return
        self._acc += dt
        while self._acc >= self.interval and not self._cancelled:
            self._acc -= self.interval
            try:
                self.fn()
            except Exception:
                pass

    def cancel(self) -> None:
        self._cancelled = True
