from __future__ import annotations

from typing import Any, Literal, Optional

from .canvas import Canvas
from .style import Style, DEFAULT_STYLE
from .buffer import char_width
from .sprite import Sprite
from .animation import AnimationManager


class Widget:
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
    def __init__(self):
        self.children: list[Widget] = []
        self.animations = AnimationManager()
        self._draw_order: Optional[list[Widget]] = None
        self._z_signature: Optional[tuple[int, ...]] = None

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

    def update(self, dt: float) -> None:
        self.animations.update(dt)
        for child in list(self.children):
            if hasattr(child, "update") and callable(child.update):
                try:
                    child.update(dt)
                except TypeError:
                    pass
