# afw: Animation Frame Work

A terminal rendering framework for real 60fps animated output. Pure
Python for the framework and player, a Zig CLI (`afw_media`) that
converts images and videos into terminal-playable assets, and a Zig
shared library (`libafw_render.so`) that accelerates fullscreen video
playback to 1000+ fps. Includes **real-time streaming playback**, so
you can watch a video while it's still being converted.

---

## Index

- [Install](#install)
- [Build from source](#build-from-source)
- [afw.py: the framework](#1-afwpy-the-framework)
  - [Integrating it into your own programs](#integrating-it-into-your-own-programs)
  - [Performance](#performance)
- [afw_media: convert images & videos](#2-afw_media-convert-images--videos)
  - [File mode](#file-mode)
  - [.afwframes format](#afwframes-format)
- [Real-time streaming](#3-real-time-streaming-watch-it-while-it-converts)
  - [afw_stream_player.py](#afw_stream_playerpy)
  - [Examples](#examples)
- [Requirements](#requirements)

---

## Install

Download the zip for your OS from the
[latest release](https://github.com/jzadl/afw.py/releases). Each zip
contains everything you need:

```
afw.py                 the framework (single file, zero dependencies)
afw_media              the CLI converter
libafw_render.so       native renderer (.dylib on macOS, .dll on Windows)
afw_stream_player.py   streaming video player
examples/              demos and loader scripts
```

Extract and run:

```bash
# Linux
unzip afw-linux.zip -d afw
cd afw && python3 examples/fireworks.py

# macOS
unzip afw-macos.zip -d afw
cd afw && python3 examples/fireworks.py

# Windows (PowerShell)
Expand-Archive afw-windows.zip -DestinationPath afw
cd afw && python examples\fireworks.py
```

To build from source instead, see [Build from source](#build-from-source).

---

## Build from source

Requirements:

- Python 3.10+
- Zig 0.16+
- `ffmpeg`/`ffprobe` on PATH

### Quick build (all platforms)

```bash
# Linux / macOS
bash builders/compiler.sh

# Windows (PowerShell)
.\builders\compiler.ps1
```

The compiler script detects your package manager, installs missing
dependencies, then builds everything: the render library, the CLI,
the `afw.py` bundle, and syntax-checks all Python files.

### Manual build

```bash
# 1. render library
zig build-lib afw_render.zig -dynamic -fPIC -O ReleaseFast -femit-bin=libafw_render.so

# 2. CLI
zig build-exe afw_media.zig -O ReleaseFast -femit-bin=afw_media

# 3. single-file bundle
python3 builders/bundle.py
```

On macOS, replace `libafw_render.so` with `libafw_render.dylib`.
On Windows, use `afw_render.dll` and `afw_media.exe`.

---

## 1. afw.py: the framework

Drop `afw.py` next to your script and import it. No pip install, no
external packages.

```python
from afw import App, TextWidget, Colors, Style, Easing

app = App(target_fps=60)
label = app.scene.add(TextWidget("hello", 0, 0, Style(fg=Colors.CYAN)))
app.anim.animate(label, "x", 40.0, 2.0, easing=Easing.ease_in_out_sine, loops=-1, yoyo=True)
app.run()
```

### Integrating it into your own programs

**As the main loop.** `App` owns the terminal for as long as `app.run()`
is running (raw mode + alt-screen). Build your scene, register
callbacks, call `run()`, it blocks until you call `app.stop()`:

```python
import afw

app = afw.App(target_fps=60, show_fps=True)

box = app.scene.add(afw.BoxWidget(2, 2, 30, 8, title="status"))
label = app.scene.add(afw.TextWidget("waiting...", 4, 4))

@app.on_update
def update(dt):
    label.text = f"uptime: {app.elapsed_time:.1f}s"

@app.on_key
def handle_key(ev: afw.KeyEvent):
    if ev.is_char("q") or ev.key == afw.Key.ESCAPE:
        app.stop()

app.run()
```

**Multiple `App()` instances in the same process** are fine. Run one
to completion, then create and run another (e.g. a menu, then a player,
then back to the menu). Each `App.run()` fully restores the terminal on
exit before the next one starts.

**Driving it from your own loop instead of `app.run()`.** If you need
tighter control (embedding afw.py inside a larger event loop), use the
pieces directly instead of `App`:

```python
from afw import Terminal, Canvas, Renderer, Style, Colors

with Terminal() as term:
    canvas = Canvas(term.width, term.height)
    renderer = Renderer(term)
    for frame in range(120):
        canvas.clear()
        canvas.draw_text(2, 2, f"frame {frame}", Style(fg=Colors.GREEN))
        renderer.render(canvas.buffer)
```

**Old terminal fallback.** Call `afw.old()` once, anywhere, before
`app.run()` if you need 256-color instead of truecolor:

```python
import afw
afw.old()          # or afw.old(ascii_only=True) to also drop unicode glyphs
```

**Key building blocks:**

| Piece | Use |
|---|---|
| `App` | main loop, fixed-timestep updates, input dispatch, terminal lifecycle |
| `Scene` / `Widget` subclasses | `TextWidget`, `BoxWidget`, `SpriteWidget`, `ProgressBarWidget` |
| `Canvas` | low-level drawing: `draw_text`, `draw_line`, `draw_rect`, `draw_box`, `put_subpixel`/`flush_subpixels` (half-block 2x vertical resolution) |
| `AnimationManager` / `Tween` | `app.anim.animate(obj, "attr", target, duration, easing=..., loops=..., yoyo=...)` |
| `Sprite` | reusable ASCII/unicode art, `Sprite.from_text(...)`, blit via `canvas.blit()` |

Run `python3 -c "import afw; help(afw)"` for the full API, or just read
the file. Every class has a docstring explaining what it's for.

### Performance

Hot paths are tuned end-to-end: cached character widths, zero-copy
buffer clears, bulk subpixel blits, sorted-scene caching, and an early
out in the renderer for unchanged frames (plus `libafw_render.so` for
the ANSI hot loop when present). Measured on the benchmark suite
(`python3 bench.py`), representative full-rate numbers at 120x40:

| path | before | after |
|---|---|---|
| `VideoFrameWidget.draw` (per-cell -> bulk blit) | 11 fps | ~18,000 fps |
| video frame end-to-end (clear + blit + native render) | 145 fps | ~12,300 fps |
| `Canvas.clear` | 250 fps | ~70,000 fps |
| `Buffer.clear` | 735 fps | ~108,000 fps |
| UI app frame end-to-end (pure Python, no native lib) | 83 fps | 207 fps |

With the native renderer loaded, a fully static frame renders at
~13,000 fps (the renderer now skips its overlay scan and ANSI rebuild
when nothing changed), and diffed video frames stream at up to
17,000+ fps into the ANSI encoder.

---

## 2. afw_media: convert images & videos

A Zig CLI that turns a photo or video into terminal cells (half-block
▀▄ packing, box-filter downscaling, aspect-ratio aware). Two output
modes:

- **File mode** (default): writes a `.afwframes` asset + a ready-to-run
  `.py` loader. Good for anything you'll play back more than once.
- **Streaming mode** (`--stream`): pipes frames to stdout as ffmpeg
  decodes them. Nothing touches disk, nothing is buffered up front.
  This is what makes real-time playback possible (see section 3).

### File mode

```bash
afw_media photo.jpg --cols 100
afw_media clip.mp4 --cols 120 --fps 24 -o clip.afwframes
afw_media banner.png --cols 200 --fit cover
```

Produces `<name>.afwframes` (binary frame data) and `<name>.py` (a
self-contained loader). Run the loader directly:

```bash
python3 clip.py
```

Or use the asset from your own code:

```python
import afw
from clip import AfwFrames   # the class the loader generates for you

app = afw.App(target_fps=24)
with AfwFrames("clip.afwframes") as frames:
    state = {"i": 0}

    @app.on_update
    def update(dt):
        state["i"] = (state["i"] + 1) % frames.frame_count

    @app.on_render
    def render(canvas):
        frames.draw_frame(canvas, state["i"])

    app.run()
```

Flags: `--cols`, `--rows`, `--fit contain|cover|stretch`, `--fps`
(downsample on extraction), `--max-frames`, `--pad-color`, `--no-loader`,
`--module`, `-q/--quiet`. Full list: `afw_media --help`.

### `.afwframes` format

```
magic:        4 bytes   "AFW1"
cols/rows:    u32 LE each
frame_count:  u32 LE     (0 = live/unbounded stream, see below)
fps:          f32 LE     (0.0 for a still image)
---- repeated frame_count times (or until EOF, in stream mode) ----
per cell (row-major): top_r,top_g,top_b, bottom_r,bottom_g,bottom_b (6 bytes)
```

Every frame is exactly `cols * rows * 6` bytes. File-mode assets are
seekable by frame index with no scanning needed.

---

## 3. Real-time streaming: watch it while it converts

`--stream` writes the header immediately, then flushes each frame to
stdout the instant ffmpeg decodes it. No temp file, no waiting for the
source to finish. Measured on a 15s 640x480 clip: **first frame
available in 0.17s**, while full decode took 3.0s. Playback starts and
runs live through essentially the entire conversion, not after it.

### afw_stream_player.py

A ready-made player: spawns `afw_media --stream`, reads frames off the
pipe on a background thread (with bounded backpressure so decoding
can't run away with memory on a fast source), and renders them live.

```bash
python3 afw_stream_player.py video.mp4 --cols 100
```

As a library:

```python
from afw_stream_player import StreamPlayer

player = StreamPlayer("video.mp4", cols=100, afw_media_path="./afw_media")
player.play()   # blocks until the video ends or the user presses q
```

`StreamPlayer(..., loop=True)` restarts the subprocess automatically
when the stream ends, for seamless looping.

#### Zig renderer (required for playback)

`StreamPlayer` uses `libafw_render.so`, a Zig shared library that
converts RGB frame data to diffed half-block ANSI in a single tight
loop, with zero allocations and per-cell diffing against the previous
frame so only changed cells are emitted.

Python owns terminal setup, input parsing, and fps pacing via
`afw.Terminal` and `afw.InputManager`, but the render hot path runs in
compiled code. The old pure-Python render path has been removed: it
topped out at ~12fps on fullscreen, while the Zig renderer hits
1000+ fps.

Performance (render path only, fullscreen):

| cols | cells | old Python | Zig | speedup |
|------|-------|------------|-----|---------|
| 200  | 22600 | ~3 fps     | 1028 fps | 340x |
| 120  | 8160  | 6.7 fps    | 2311 fps | 345x |
| 80   | 3600  | 27 fps     | 5712 fps | 211x |

The render path is no longer the bottleneck. Fullscreen 60fps is
trivial. The actual ceiling is the terminal emulator's ANSI parsing
speed and ffmpeg's decode rate, both well above 60fps.

#### Automatic fps detection

`StreamPlayer` probes the source video's native frame rate via
`ffprobe` and paces the render loop to match. A 30fps source plays at
30, a 60fps source plays at 60. No forced upsampling. Pass
`target_fps=...` to override:

```python
player = StreamPlayer("video.mp4", cols=120, target_fps=60)
```

### Examples

`examples/` contains demos and ready-to-run loader scripts:

- `fireworks.py` - sub-pixel firework particles with true color
- `widget_showcase.py` - all built-in widgets on one screen
- `example*.py` - auto-generated loaders for bundled `.afwframes`
  assets (run from the repo root; they expect `afw` on PYTHONPATH)

---

## Requirements

- Python 3.10+, no pip packages, for `afw.py` and the `.py` scripts
- Zig 0.16+ to build `afw_media` and `libafw_render.so` (only needed
  once each, to produce the binaries)
- `ffmpeg`/`ffprobe` on PATH for all media input (video and images)
