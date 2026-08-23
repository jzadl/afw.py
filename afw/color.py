from __future__ import annotations

from dataclasses import dataclass

from ._compat import _compat


def _build_256_palette() -> list[tuple[int, int, int]]:
    palette: list[tuple[int, int, int]] = []
    base16 = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
        (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
        (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    palette.extend(base16)
    ramp = [0, 95, 135, 175, 215, 255]
    for r in ramp:
        for g in ramp:
            for b in ramp:
                palette.append((r, g, b))
    for i in range(24):
        v = 8 + i * 10
        palette.append((v, v, v))
    return palette


_PALETTE_256 = _build_256_palette()


def _rgb_to_256(r: int, g: int, b: int) -> int:
    best_idx = 0
    best_dist = float("inf")
    for idx, (pr, pg, pb) in enumerate(_PALETTE_256):
        dist = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


_quant_cache: dict[tuple[int, int, int], int] = {}


def _quantize(r: int, g: int, b: int) -> int:
    key = (r, g, b)
    cached = _quant_cache.get(key)
    if cached is not None:
        return cached
    idx = _rgb_to_256(r, g, b)
    _quant_cache[key] = idx
    return idx


@dataclass(frozen=True, slots=True)
class Color:
    r: int
    g: int
    b: int

    def __post_init__(self):
        object.__setattr__(self, "r", max(0, min(255, int(self.r))))
        object.__setattr__(self, "g", max(0, min(255, int(self.g))))
        object.__setattr__(self, "b", max(0, min(255, int(self.b))))

    @staticmethod
    def hex(code: str) -> "Color":
        code = code.lstrip("#")
        if len(code) == 3:
            code = "".join(c * 2 for c in code)
        if len(code) != 6:
            raise ValueError(f"Invalid hex color: {code!r}")
        return Color(int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16))

    def to_ansi_fg(self) -> str:
        if _compat.truecolor:
            return f"38;2;{self.r};{self.g};{self.b}"
        return f"38;5;{_quantize(self.r, self.g, self.b)}"

    def to_ansi_bg(self) -> str:
        if _compat.truecolor:
            return f"48;2;{self.r};{self.g};{self.b}"
        return f"48;5;{_quantize(self.r, self.g, self.b)}"

    def lerp(self, other: "Color", t: float) -> "Color":
        t = max(0.0, min(1.0, t))
        return Color(
            round(self.r + (other.r - self.r) * t),
            round(self.g + (other.g - self.g) * t),
            round(self.b + (other.b - self.b) * t),
        )

    def with_alpha_over(self, bg: "Color", alpha: float) -> "Color":
        return bg.lerp(self, alpha)


class Colors:
    BLACK = Color(0, 0, 0)
    WHITE = Color(255, 255, 255)
    RED = Color(220, 50, 47)
    GREEN = Color(133, 153, 0)
    YELLOW = Color(181, 137, 0)
    BLUE = Color(38, 139, 210)
    MAGENTA = Color(211, 54, 130)
    CYAN = Color(42, 161, 152)
    GRAY = Color(128, 128, 128)
    ORANGE = Color(203, 75, 22)
    TRANSPARENT = None
