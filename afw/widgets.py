from __future__ import annotations

import inspect
from typing import Any, Literal, Optional

from .canvas import Canvas
from .style import Style, DEFAULT_STYLE
from .buffer import char_width
from .sprite import Sprite
from .animation import AnimationManager


class Widget:
    """Base class for everything a Scene can draw.

    x/y are the widget's position in cells (floats; drawn at the
    truncated cell), z orders drawing (higher z draws later, on top).
    visible=False skips drawing entirely. opacity is visibility-only,
    not blending: opacity <= 0 hides the widget, any value above 0
    draws it fully opaque (animate it for fade in/out effects).
    Subclasses implement draw(canvas); update(dt) is optional.
    """

    def __init__(self, x: float = 0, y: float = 0, width: int = 0, height: int = 0, *, z: int = 0):
        self.x = float(x)
        self.y = float(y)
        self.width = int(width)
        self.height = int(height)
        self.z = int(z)
        self.visible: bool = True
        self.opacity: float = 1.0
        self.offset_x: float = 0.0
        self.offset_y: float = 0.0

    def draw(self, canvas: Canvas) -> None:
        pass


class TextWidget(Widget):
    """A (possibly multi-line) text label.

    Canonical form: TextWidget(text, x, y, style). The legacy positional
    form TextWidget(x, y, text, style) is also accepted — the bundled
    examples use it — but prefer the canonical form in new code: a
    numeric-looking text in the legacy form is ambiguous and is NOT
    swapped (TextWidget(0, 0, 0) means text "0" at 0,0).
    """

    def __init__(
        self,
        text: str = "",
        x: float = 0,
        y: float = 0,
        style: Style = DEFAULT_STYLE,
        *,
        anchor: Literal["left", "center", "right"] = "left",
        z: int = 0,
    ):
        if isinstance(text, (int, float)) and isinstance(x, (int, float)) and isinstance(y, str):
            actual_x = float(text)
            actual_y = float(x)
            actual_text = y
            text = actual_text
            x = actual_x
            y = actual_y

        lines = str(text).split("\n")
        w = max((sum(char_width(c) for c in line) for line in lines), default=0)
        h = max(1, len(lines))
        super().__init__(x, y, w, h, z=z)
        self.text = str(text)
        self.style = style
        self.anchor = anchor

    def draw(self, canvas: Canvas) -> None:
        if not self.visible or self.opacity <= 0.0:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        for i, line in enumerate(self.text.split("\n")):
            line_w = sum(char_width(c) for c in line)
            if self.anchor == "center":
                draw_x = ix - line_w // 2
            elif self.anchor == "right":
                draw_x = ix - line_w
            else:
                draw_x = ix
            canvas.draw_text(draw_x, iy + i, line, self.style)


class BoxWidget(Widget):
    """A bordered box, optionally with a title drawn on the top edge."""

    def __init__(
        self,
        x: float = 0,
        y: float = 0,
        width: int = 0,
        height: int = 0,
        style: Style = DEFAULT_STYLE,
        *,
        title: Optional[str] = None,
        z: int = 0,
    ):
        super().__init__(x, y, width, height, z=z)
        self.style = style
        self.title = title

    def draw(self, canvas: Canvas) -> None:
        if not self.visible:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        canvas.draw_box(ix, iy, self.width, self.height, self.style, title=self.title)


class SpriteWidget(Widget):
    """Draws a Sprite (reusable ASCII/unicode art) at x, y."""

    def __init__(self, x: float, y: float, sprite: Sprite, *, z: int = 0):
        super().__init__(x, y, sprite.width, sprite.height, z=z)
        self.sprite = sprite

    def draw(self, canvas: Canvas) -> None:
        if not self.visible:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        canvas.blit(ix, iy, self.sprite)


class ProgressBarWidget(Widget):
    """A horizontal progress bar: value/max_value fills width cells
    with fill_char, the rest with empty_char."""

    def __init__(
        self,
        x: float = 0,
        y: float = 0,
        width: int = 0,
        height: int = 1,
        value: float = 0.0,
        max_value: float = 1.0,
        style: Style = DEFAULT_STYLE,
        fill_char: str = "\u2588",
        empty_char: str = "\u2591",
        *,
        z: int = 0,
    ):
        super().__init__(x, y, width, height, z=z)
        self.value = float(value)
        self.max_value = float(max_value)
        self.style = style
        self.fill_char = fill_char
        self.empty_char = empty_char

    def draw(self, canvas: Canvas) -> None:
        if not self.visible:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        ratio = max(0.0, min(1.0, self.value / max(0.0001, self.max_value)))
        filled = int(self.width * ratio)
        for i in range(self.width):
            ch = self.fill_char if i < filled else self.empty_char
            canvas.put(ix + i, iy, ch, self.style)


class VideoFrameWidget(Widget):
    """Displays one packed RGB video frame (cols*rows*6 bytes, top+bottom
    RGB per cell) at x, y.

    Frame-access contract (shared with FrameAsset.get_frame and the
    generated AfwFrames.read_frame, which clamp out-of-range indices):
    set_frame_rgb() accepts any bytes; a frame shorter than
    cols*rows*6 bytes is ignored (the previous frame stays on screen)
    instead of raising, so a truncated stream degrades to a held frame
    rather than crashing playback.
    """

    def __init__(
        self,
        x: float = 0,
        y: float = 0,
        width: int = 0,
        height: int = 0,
        *,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
        z: int = 0,
    ):
        w = cols if cols is not None else width
        h = rows if rows is not None else height
        super().__init__(x, y, int(w), int(h), z=z)
        self.cols = int(w)
        self.rows = int(h)
        self.rgb_data: Optional[bytes] = None
        self._prev_rgb: Optional[bytes] = None

    def set_frame_rgb(self, data: bytes) -> None:
        self.rgb_data = data

    def update(self, dt_or_frame: Any = None) -> None:
        pass

    def render(self, canvas: Canvas) -> None:
        self.draw(canvas)

    def draw(self, canvas: Canvas) -> None:
        if not self.visible or self.rgb_data is None:
            return
        ix = int(self.x + self.offset_x)
        iy = int(self.y + self.offset_y)
        data = self.rgb_data
        cols, rows = self.cols, self.rows
        if len(data) < cols * rows * 6:
            return
        # Bulk path: one row-sliced memory copy per row instead of the
        # per-cell loop that used to build ~2 Color objects and 2 cells
        # per column. The RGB layer is all the renderer needs from a
        # video frame; it never emits the half-block buffer cells as
        # overlay text anyway.
        canvas.blit_subpixel_frame(data, cols, rows, ix, iy)


class Scene:
    """A list of widgets plus an AnimationManager. draw() renders the
    children in z order (skipping invisible / fully transparent ones);
    update(dt) advances the animations and calls each child's update()
    if it defines one. A child's update() may take dt or no arguments;
    the arity is probed once per widget type, and errors raised inside
    a child's update() propagate instead of being swallowed."""

    def __init__(self):
        self.children: list[Widget] = []
        self.animations = AnimationManager()
        self._draw_order: Optional[list[Widget]] = None
        self._z_signature: Optional[tuple[int, ...]] = None
        self._update_takes_arg: dict[type, bool] = {}

    def add(self, widget: Widget) -> Widget:
        self.children.append(widget)
        self._draw_order = None
        return widget

    def remove(self, widget: Widget) -> None:
        if widget in self.children:
            self.children.remove(widget)
            self._draw_order = None

    def _sorted_children(self) -> list[Widget]:
        children = self.children
        # Re-sort only when the membership or z-order actually changes;
        # the z signature check is O(n) per frame versus an O(n log n)
        # sort, and z can be animated so it can't be invalidated by
        # add/remove alone.
        sig = tuple(getattr(w, "z", 0) for w in children)
        if self._draw_order is None or sig != self._z_signature:
            self._draw_order = sorted(children, key=lambda w: getattr(w, "z", 0))
            self._z_signature = sig
        return self._draw_order

    def draw(self, canvas: Canvas) -> None:
        for child in self._sorted_children():
            if child.visible and child.opacity > 0.0:
                child.draw(canvas)

    def _update_takes_dt(self, child: Widget) -> bool:
        """True if child's update() accepts a dt argument. Probed once
        per widget type via inspect; if the signature can't be read,
        default to passing dt (an error inside update() then surfaces
        instead of being hidden)."""
        cls = type(child)
        cached = self._update_takes_arg.get(cls)
        if cached is not None:
            return cached
        takes_arg = True
        try:
            sig = inspect.signature(child.update)
            params = list(sig.parameters.values())
            positional = [
                p for p in params
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            var_positional = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
            )
            takes_arg = bool(positional) or var_positional
        except (ValueError, TypeError):
            takes_arg = True
        self._update_takes_arg[cls] = takes_arg
        return takes_arg

    def update(self, dt: float) -> None:
        self.animations.update(dt)
        for child in list(self.children):
            if hasattr(child, "update") and callable(child.update):
                if self._update_takes_dt(child):
                    child.update(dt)
                else:
                    child.update()
