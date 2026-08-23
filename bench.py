#!/usr/bin/env python3
"""
afw benchmark suite.

Measures every hot path in the framework:
  * primitives      char_width, Cell, Color, Style ANSI generation
  * buffer/canvas   alloc, clear, put, draw_text, filled rect, box,
                    sprite blit, subpixel writes
  * video paths     pure-Python VideoFrameWidget vs native Zig renderer
  * renderer        pure-Python ANSI builder vs libafw_render.so diffing
  * scene/anim      AnimationManager.update, Scene.draw at various sizes
  * full frame      end-to-end "App loop body" simulation at terminal size

Run:
    python3 bench.py            # human-readable report
    python3 bench.py --json     # machine-readable output
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import afw
from afw import (
    Canvas, Buffer, Renderer, Scene, Sprite, Style, Colors, Color,
    TextWidget, BoxWidget, ProgressBarWidget, VideoFrameWidget, SpriteWidget,
    AnimationManager, Easing, DEFAULT_STYLE, Tween,
)
from afw.buffer import char_width
from afw.color import _quantize, _quant_cache

COLS, ROWS = 120, 40          # typical fullscreen terminal
CELLS = COLS * ROWS


# --------------------------------------------------------------------------
# timing harness
# --------------------------------------------------------------------------

class _Result:
    def __init__(self, group, name, rate, unit, note=""):
        self.group = group
        self.name = name
        self.rate = rate          # ops per second (median of repeats)
        self.unit = unit
        self.note = note

    @property
    def ns_per_op(self) -> float:
        return 1e9 / self.rate if self.rate > 0 else float("inf")


def _measure(fn, target_time=0.20):
    """Return ops/sec for fn() using an adaptive loop count."""
    n = 1
    while True:
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        dt = time.perf_counter() - t0
        if dt >= target_time or n >= 1 << 24:
            return n / dt if dt > 0 else 0.0
        n = min(n * max(2, int(target_time / max(dt, 1e-9) / 4)), 1 << 24)


def bench(group, name, fn, *, unit="ops/s", repeats=5, note_fn=None):
    rates = []
    for _ in range(repeats):
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            rates.append(_measure(fn))
        finally:
            if gc_was_enabled:
                gc.enable()
    r = statistics.median(rates)
    note = note_fn(r) if note_fn else ""
    return _Result(group, name, r, unit, note)


def fps_note(rate):
    return f"{rate:,.0f} fps"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

class NullTerminal:
    """Stand-in for Terminal that discards all output."""
    width = COLS
    height = ROWS

    def __init__(self):
        self.bytes_written = 0

    def write(self, s):
        pass

    def write_bytes(self, b):
        self.bytes_written += len(b)

    def flush(self):
        pass


ART = "\n".join(
    "".join(random.choice("abcdeABCDE12345@#$%*+=~") for _ in range(40))
    for _ in range(20)
)


def make_video_frame(cols=COLS, rows=ROWS, seed=0):
    """Mostly-static gradient background with a moving block — like real
    footage where only part of the frame changes between frames."""
    buf = bytearray(cols * rows * 6)
    bx = (seed * 9) % max(1, cols - 12)
    by = (seed * 4) % max(1, rows - 6)
    i = 0
    for y in range(rows):
        for x in range(cols):
            t = ((x * 3 + y * 5) & 255)
            r = t
            g = (t * 2) & 255
            b = (x * y) & 255
            if bx <= x < bx + 10 and by <= y < by + 5:
                r, g, b = 255 - r, 255 - g, 255 - b
            top = (r, g, b)
            bottom = ((r + 10) & 255, (g + 10) & 255, (b + 10) & 255)
            buf[i:i + 6] = bytes(top + bottom)
            i += 6
    return bytes(buf)


def make_noise_frame(cols=COLS, rows=ROWS):
    return random.randbytes(cols * rows * 6)


def styled_buffer(width=COLS, height=ROWS):
    """Buffer with a realistic mix of styles/text."""
    b = Buffer(width, height)
    c = Canvas(b)
    for j in range(0, height - 3, 8):
        c.draw_box(2, j, width - 4, 7, Style(fg=Colors.CYAN), title=f"panel {j}")
        c.draw_text(4, j + 2, "lorem ipsum dolor sit amet 0123456789", Style(fg=Colors.GREEN))
        c.draw_text(4, j + 3, "consectetur adipiscing elit", Style(fg=Color(200, 100, 50), bold=True))
    return b


def load_native():
    from afw._native import _load_native_render_lib
    return _load_native_render_lib()


# --------------------------------------------------------------------------
# suites
# --------------------------------------------------------------------------

def suite_primitives(results):
    g = "primitives"

    results.append(bench(g, "char_width ascii 'a'",
                         lambda: char_width("a")))
    results.append(bench(g, "char_width CJK '中'",
                         lambda: char_width("中")))
    results.append(bench(g, "char_width emoji",
                         lambda: char_width("\U0001F600")))
    results.append(bench(g, "Cell() default",
                         lambda: __import__("afw.buffer", fromlist=["Cell"]).Cell()))
    results.append(bench(g, "Color(120, 80, 40)",
                         lambda: Color(120, 80, 40)))
    s = Style(fg=Colors.CYAN, bg=Color(10, 20, 30), bold=True)
    results.append(bench(g, "Style.to_ansi_seq (fg+bg+bold)",
                         lambda: s.to_ansi_seq()))
    st = Style(fg=Colors.GREEN)
    results.append(bench(g, "Style.merged (two styles)",
                         lambda: DEFAULT_STYLE.merged(st)))
    key = (7, 7, 7)
    _quant_cache.pop(key, None)
    results.append(bench(g, "_quantize cold miss",
                         lambda: (_quant_cache.pop(key, None), _quantize(*key))))
    results.append(bench(g, "_quantize warm hit",
                         lambda: _quantize(1, 2, 3)))


def suite_buffer_canvas(results):
    g = "buffer/canvas"

    results.append(bench(g, f"Buffer({COLS},{ROWS}) alloc",
                         lambda: Buffer(COLS, ROWS),
                         note_fn=lambda r: f"{r * CELLS:,.0f} cells/s"))
    buf = Buffer(COLS, ROWS)
    results.append(bench(g, f"Buffer.clear {COLS}x{ROWS}",
                         lambda: buf.clear(),
                         note_fn=fps_note))
    canvas = Canvas(COLS, ROWS)
    results.append(bench(g, f"Canvas.clear {COLS}x{ROWS}",
                         lambda: canvas.clear(),
                         note_fn=fps_note))

    results.append(bench(g, "Canvas.put single cell",
                         lambda: canvas.put(10, 10, "x", DEFAULT_STYLE)))

    text = "hello world, this is a fairly long line of text!"
    results.append(bench(g, f"draw_text ({len(text)} chars)",
                         lambda: canvas.draw_text(0, 0, text, Style(fg=Colors.RED))))

    results.append(bench(g, f"draw_filled_rect fullscreen {COLS}x{ROWS}",
                         lambda: canvas.draw_filled_rect(0, 0, COLS, ROWS, Style(bg=Color(30, 30, 60))),
                         note_fn=fps_note))

    results.append(bench(g, "draw_box 30x8",
                         lambda: canvas.draw_box(1, 1, 30, 8, Style(fg=Colors.CYAN), title="hi")))

    sprite = Sprite.from_text(ART)
    results.append(bench(g, f"blit sprite {sprite.width}x{sprite.height} ({len(sprite._cells)} cells)",
                         lambda: canvas.blit(5, 5, sprite)))

    color = Color(255, 128, 64)
    results.append(bench(g, "put_subpixel single write",
                         lambda: canvas.put_subpixel(50, 20, color)))

    results.append(bench(g, f"put_subpixel fullscreen ({CELLS} cells x2)",
                         lambda: [canvas.put_subpixel(x, y * 2, color) for y in range(ROWS) for x in range(COLS)],
                         note_fn=lambda r: f"= {r / CELLS:,.1f} frames/s"))


def suite_video(results):
    g = "video paths"

    frame = make_video_frame()
    canvas = Canvas(COLS, ROWS)

    widget = VideoFrameWidget(0, 0, cols=COLS, rows=ROWS)
    widget.set_frame_rgb(frame)
    fresh = [Canvas(COLS, ROWS) for _ in range(8)]
    k = [0]

    def draw_py():
        c = fresh[k[0] & 7]
        c.clear()
        widget.draw(c)
        k[0] += 1

    results.append(bench(g, f"VideoFrameWidget.draw PURE PY {COLS}x{ROWS}",
                         draw_py, note_fn=fps_note))

    frames = [make_video_frame(seed=s) for s in range(30)]
    idx = [0]

    def blit_rgb():
        canvas.clear()
        canvas.blit_rgb(frames[idx[0] % 30])
        idx[0] += 1

    results.append(bench(g, "canvas.clear + blit_rgb",
                         blit_rgb, note_fn=fps_note))

    results.append(bench(g, "canvas.get_rgb_bytes (copy 57.6KB)",
                         lambda: canvas.get_rgb_bytes()))


def suite_renderer(results):
    g = "renderer"

    term = NullTerminal()

    # --- pure-python ANSI builder ---
    r = Renderer(term)
    r._native_lib = None  # force python path
    buf = styled_buffer()
    results.append(bench(g, f"_build_ansi PYTHON styled {COLS}x{ROWS}",
                         lambda: r._build_ansi(buf), note_fn=fps_note))

    plain = Canvas(COLS, ROWS)
    for y in range(ROWS):
        for x in range(COLS):
            plain.put(x, y, ".")
    pbuf = plain.get_buffer()
    results.append(bench(g, f"_build_ansi PYTHON uniform '.' {COLS}x{ROWS}",
                         lambda: r._build_ansi(pbuf), note_fn=fps_note))

    # --- end-to-end render(), python path ---
    results.append(bench(g, "Renderer.render PYTHON styled (incl write)",
                         lambda: r.render(Canvas(buf)), note_fn=fps_note))

    # --- native Zig ---
    lib = load_native()
    if lib is None:
        results.append(_Result(g, "NATIVE (libafw_render.so not found)", 0, "ops/s"))
        return

    out_cap = CELLS * 40
    out = ctypes.create_string_buffer(out_cap)

    noise = make_noise_frame()
    video = [make_video_frame(seed=s) for s in range(30)]
    static = make_video_frame(seed=999)

    # first frame: no prev
    vi = [0]
    results.append(bench(g, "ZIG first frame (prev=None)",
                         lambda: lib.afw_render_frame(video[0], None, COLS, ROWS, out, out_cap),
                         note_fn=fps_note))

    # worst case: random noise, everything changed
    ni = [0]
    def zig_noise():
        ni[0] ^= 1
        lib.afw_render_frame(noise, video[0], COLS, ROWS, out, out_cap)
    results.append(bench(g, "ZIG noise vs prev (all cells change)",
                         zig_noise, note_fn=fps_note))

    # realistic: consecutive video frames
    def zig_video():
        vi[0] += 1
        lib.afw_render_frame(video[vi[0] % 30], video[(vi[0] - 1) % 30],
                             COLS, ROWS, out, out_cap)
    results.append(bench(g, "ZIG video frames (diff vs prev)",
                         zig_video, note_fn=fps_note))

    # best case: identical frame
    results.append(bench(g, "ZIG identical frame (all skipped)",
                         lambda: lib.afw_render_frame(static, static, COLS, ROWS, out, out_cap),
                         note_fn=fps_note))

    # README comparison sizes
    for cols, rows in ((80, 45), (120, 68), (200, 113)):
        n_cells = cols * rows
        cap = n_cells * 40
        o = ctypes.create_string_buffer(cap)
        v = [make_video_frame(cols, rows, s) for s in range(6)]
        ii = [0]

        def z():
            ii[0] += 1
            lib.afw_render_frame(v[ii[0] % 6], v[(ii[0] - 1) % 6], cols, rows, o, cap)

        results.append(bench(g, f"ZIG diff-render {cols}x{rows} ({n_cells} cells)",
                             z, note_fn=fps_note))

    # --- end-to-end render() through the native path ---
    rn = Renderer(term)
    cv = Canvas(COLS, ROWS)
    fi = [0]

    def render_native():
        cv.clear()
        cv.blit_rgb(video[fi[0] % 30])
        fi[0] += 1
        rn.render(cv)

    results.append(bench(g, "Renderer.render NATIVE clear+blit+overlay-scan",
                         render_native, note_fn=fps_note))

    # informational: bytes emitted per realistic diffed frame
    vi2 = 0
    total = 0
    for _ in range(30):
        vi2 += 1
        n = lib.afw_render_frame(video[vi2 % 30], video[(vi2 - 1) % 30],
                                 COLS, ROWS, out, out_cap)
        total += n
    results.append(_Result(g, "ZIG bytes emitted / video frame (diffed)",
                           total / 30, "B/s", note=f"vs {CELLS * 6} B raw RGB"))


def suite_scene_anim(results):
    g = "scene/animation"

    class Dummy:
        def __init__(self):
            self.x = 0.0
            self.y = 0.0
            self.opacity = 1.0

    for n_tweens in (1, 50, 250):
        mgr = AnimationManager()
        dummies = [Dummy() for _ in range(n_tweens)]
        for d in dummies:
            mgr.add(__import__("afw.animation", fromlist=["Tween"]).Tween(
                d, "x", 100.0, 600.0, easing=Easing.ease_in_out_sine))

        def upd(mgr=mgr):
            for d in dummies:
                d.x = 0.0
            mgr.update(1 / 60)

        # reset finished state each call by re-adding; simpler: never finishes (600s)
        results.append(bench(g, f"AnimationManager.update ({n_tweens} active tweens)",
                             upd, unit="frames/s", note_fn=lambda _r, n=n_tweens: f"{n * 60} tween-updates/s @60fps"))

    # scene draw
    small = Scene()
    for i in range(5):
        small.add(TextWidget(f"label {i}", i * 12, 2, Style(fg=Colors.GREEN)))
    small.add(BoxWidget(0, 0, 60, 10, title="box"))
    results.append(bench(g, "Scene.draw 6 widgets",
                         lambda: (lambda c: (c.clear(), small.draw(c)))(Canvas(COLS, ROWS)),
                         unit="frames/s", note_fn=fps_note))

    big = Scene()
    rng = random.Random(42)
    for i in range(50):
        kind = i % 5
        if kind == 0:
            big.add(BoxWidget(rng.randrange(0, 90), rng.randrange(0, 30), 25, 6,
                              style=Style(fg=Colors.MAGENTA), title=f"b{i}"))
        elif kind == 1:
            big.add(TextWidget(f"widget number {i} some text here", rng.randrange(0, 60),
                               rng.randrange(0, 38), Style(fg=Colors.YELLOW)))
        elif kind == 2:
            big.add(ProgressBarWidget(rng.randrange(0, 60), rng.randrange(0, 38), 30, 1, i / 50))
        elif kind == 3:
            big.add(SpriteWidget(rng.randrange(0, 60), rng.randrange(0, 20),
                                 Sprite.from_text(ART[: ART.find("\n") + 1] + "line two of art here.....")))
        else:
            big.add(TextWidget("z-layer filler text", rng.randrange(0, 90), rng.randrange(0, 38),
                               Style(fg=Colors.BLUE, dim=True), z=i % 3))

    def draw_big():
        c = Canvas(COLS, ROWS)
        big.draw(c)

    results.append(bench(g, "Scene.draw 50 mixed widgets (fresh canvas)",
                         draw_big, unit="frames/s", note_fn=fps_note))

    def update_big():
        big.update(1 / 60)

    results.append(bench(g, "Scene.update 50 widgets (hasattr path)",
                         update_big, unit="frames/s"))

    sorted_children = sorted(big.children, key=lambda w: getattr(w, "z", 0))
    results.append(bench(g, "Scene.draw sort overhead only (50 children)",
                         lambda: sorted(big.children, key=lambda w: getattr(w, "z", 0)),
                         note_fn=lambda r: f"{r / 60:,.0f} sorts spare per frame @60fps"))


def suite_full_frame(results):
    g = "full frame"

    term = NullTerminal()

    # typical UI app frame: clear -> scene -> render (python path)
    rp = Renderer(term)
    rp._native_lib = None
    scene = Scene()
    scene.add(BoxWidget(0, 0, COLS - 1, ROWS - 1, title="main", style=Style(fg=Colors.CYAN)))
    scene.add(BoxWidget(2, 2, 38, 10, title="status", style=Style(fg=Colors.GREEN)))
    scene.add(TextWidget("hello afw benchmark, this is a status line", 4, 4, Style(fg=Colors.WHITE)))
    scene.add(ProgressBarWidget(4, 6, 34, 1, 0.7))
    scene.add(BoxWidget(42, 2, COLS - 46, 10, title="logs", style=Style(fg=Colors.GRAY)))
    for i in range(6):
        scene.add(TextWidget(f"[12:{i:02d}] log line {i} something happened", 44, 4 + i,
                             Style(fg=Colors.GRAY)))
    scene.add(TextWidget("q quit | space pause", 2, ROWS - 2, Style(fg=Colors.YELLOW)))

    def ui_frame_python():
        c = Canvas(COLS, ROWS)
        c.clear()
        scene.draw(c)
        rp.render(c)

    results.append(bench(g, "UI app frame END-TO-END (pure Python)",
                         ui_frame_python, unit="frames/s", note_fn=fps_note))

    # video frame through native path incl overlay scan
    rn = Renderer(term)
    lib = load_native()
    if lib is not None:
        video = [make_video_frame(seed=s) for s in range(30)]
        cv = Canvas(COLS, ROWS)
        fi = [0]

        def video_frame_native():
            cv.clear()
            cv.blit_rgb(video[fi[0] % 30])
            fi[0] += 1
            rn.render(cv)

        results.append(bench(g, "video frame END-TO-END (clear+blit+native render)",
                             video_frame_native, unit="frames/s", note_fn=fps_note))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="output JSON instead of a table")
    args = ap.parse_args()

    random.seed(1337)
    print(f"afw benchmark  |  python {sys.version.split()[0]}  |  "
          f"frame {COLS}x{ROWS} ({CELLS} cells)  |  "
          f"native lib: {'yes' if load_native() else 'NO'}")
    print()

    results = []
    for suite in (suite_primitives, suite_buffer_canvas, suite_video,
                  suite_renderer, suite_scene_anim, suite_full_frame):
        suite(results)

    if args.json:
        print(json.dumps(
            [{"group": r.group, "name": r.name, "ops_per_sec": round(r.rate, 1),
              "ns_per_op": round(r.ns_per_op, 1), "note": r.note}
             for r in results], indent=2))
        return

    cur_group = None
    name_w = max(len(r.name) for r in results) + 2
    for r in results:
        if r.group != cur_group:
            cur_group = r.group
            print(f"\n== {cur_group} " + "=" * (70 - len(cur_group)))
            print(f"{'benchmark':<{name_w}} {'ops/sec':>14} {'ns/op':>12}   note")
        ns = f"{r.ns_per_op:,.0f}" if r.ns_per_op < 1e7 else "-"
        print(f"{r.name:<{name_w}} {r.rate:>14,.0f} {ns:>12}   {r.note}")

    print("\nnotes: 'fps' = full-frame equivalents per second; "
          "gc disabled during timing; median of 5 runs")


if __name__ == "__main__":
    main()
