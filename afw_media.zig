//! afw_media - converts images and videos into `.afwframes` assets that
//! afw.py can play back at full frame rate, plus a ready-to-run Python
//! loader script.
//!
//! Zig rewrite of the original Rust CLI. Same CLI surface, same
//! `.afwframes` binary format, byte-for-byte compatible assets. Like the
//! Rust version, decoding is delegated to an ffmpeg subprocess (raw PPM
//! over a pipe); unlike the Rust version there is no native image-decode
//! fallback, so still images also route through ffmpeg, which handles
//! png/jpg/gif/bmp/webp natively.
//!
//! Rust version will not be released 
//!
//! Build:
//!     zig build-exe afw_media.zig -O ReleaseFast -femit-bin=afw_media
//!
//! Requires ffmpeg/ffprobe on PATH.

const std = @import("std");
const builtin = @import("builtin");
const Io = std.Io;

const MAGIC = "AFW1";
const CELL_ASPECT: f32 = 0.5;

// ---------------------------------------------------------------------------
// CLI arguments
// ---------------------------------------------------------------------------

const FitMode = enum { stretch, contain, cover };

const Args = struct {
    input: []const u8,
    output: []const u8,
    cols: u32 = 80,
    rows: ?u32 = null,
    fit: FitMode = .contain,
    max_fps: ?f32 = null,
    max_frames: ?u32 = null,
    pad_color: [3]u8 = .{ 0, 0, 0 },
    emit_loader: bool = true,
    module_hint: []const u8 = "afw",
    quiet: bool = false,
    stream: bool = false,
    jobs: usize = 1,
};

fn printUsage(io: Io) void {
    eprint(io,
        \\afw_media: convert images/video into afw.py-playable assets
        \\
        \\USAGE:
        \\    afw_media <input> [OPTIONS]
        \\
        \\ARGS:
        \\    <input>               Path to an image (png/jpg/gif/bmp/webp) or a
        \\                             video file (anything ffmpeg can read).
        \\
        \\OPTIONS:
        \\    -o, --output <path>   Output .afwframes path. Defaults to the input
        \\                             filename with its extension replaced.
        \\    --cols <N>            Target width in terminal columns. Default: 80.
        \\    --rows <N>            Target height in terminal rows. Default:
        \\                             derived automatically from the source's aspect
        \\                             ratio.
        \\    --fit <mode>          One of: contain (default), cover, stretch.
        \\    --fps <N>             Cap the extracted video frame rate (downsamples
        \\                             at extraction time). Ignored for still images.
        \\    --max-frames <N>      Stop after converting N frames (video only).
        \\    --pad-color <hex>     Fill color for unused margin under --fit contain.
        \\                             Default: 000000 (black). Format: RRGGBB or #RRGGBB.
        \\    --no-loader           Skip generating the companion .py loader script.
        \\    --module <name>       Module name the generated loader imports
        \\                             (default: afw).
        \\    --stream              Write frames to stdout as ffmpeg decodes them,
        \\                             instead of writing a .afwframes file. Video only.
        \\    -j, --jobs <N>        Number of parallel ffmpeg workers for file-mode
        \\                             video conversion. Default: 1.
        \\    -q, --quiet           Suppress progress output.
        \\    -h, --help            Show this help and exit.
        \\
        \\EXAMPLES:
        \\    afw_media photo.jpg --cols 100
        \\    afw_media clip.mp4 --cols 120 --fps 24 -o clip.afwframes
        \\    afw_media banner.png --cols 200 --fit cover
        \\    afw_media movie.mp4 --cols 150 --jobs 8
        \\
    , .{});
}

fn parseHexColor(s_in: []const u8) ![3]u8 {
    const s = if (s_in.len > 0 and s_in[0] == '#') s_in[1..] else s_in;
    if (s.len != 6) return error.InvalidHexColor;
    const r = std.fmt.parseInt(u8, s[0..2], 16) catch return error.InvalidHexColor;
    const g = std.fmt.parseInt(u8, s[2..4], 16) catch return error.InvalidHexColor;
    const b = std.fmt.parseInt(u8, s[4..6], 16) catch return error.InvalidHexColor;
    return .{ r, g, b };
}

fn defaultOutput(alloc: std.mem.Allocator, input: []const u8) ![]const u8 {
    const base = std.fs.path.basename(input);
    const dot = std.mem.lastIndexOfScalar(u8, base, '.');
    const stem = if (dot) |d| base[0..d] else base;
    return std.fmt.allocPrint(alloc, "{s}.afwframes", .{stem});
}

const image_extensions = [_][]const u8{ "png", "jpg", "jpeg", "gif", "bmp", "webp" };

fn isImagePath(path: []const u8) bool {
    const ext = std.fs.path.extension(path);
    if (ext.len < 2) return false;
    var buf: [16]u8 = undefined;
    if (ext.len - 1 > buf.len) return false;
    const lower = std.ascii.lowerString(buf[0 .. ext.len - 1], ext[1..]);
    for (image_extensions) |e| {
        if (std.mem.eql(u8, lower, e)) return true;
    }
    return false;
}

/// Derives target rows from the source aspect ratio when --rows wasn't
/// given. Terminal cells are ~2:1 (w:h) and half-blocks give 2 vertical
/// samples per row, so rows = cols * (h/w).
fn deriveRows(cols: u32, src_w: u32, src_h: u32) u32 {
    if (src_w == 0 or src_h == 0) return @max(cols / 2, 1);
    const aspect = @as(f32, @floatFromInt(src_h)) / @as(f32, @floatFromInt(src_w));
    const r = @round(@as(f32, @floatFromInt(cols)) * aspect);
    return @max(@as(u32, @intFromFloat(r)), 1);
}

// ---------------------------------------------------------------------------
// small io helpers
// ---------------------------------------------------------------------------

fn eprint(io: Io, comptime fmt: []const u8, args: anytype) void {
    var buf: [4096]u8 = undefined;
    var w = Io.File.stderr().writer(io, &buf);
    w.interface.print(fmt, args) catch return;
    w.interface.flush() catch {};
}

const FfmpegNotFound = error.FfmpegNotFound;

// ---------------------------------------------------------------------------
// ffprobe
// ---------------------------------------------------------------------------

const ProbeResult = struct { width: u32, height: u32, fps: f32 };

fn probe(gpa: std.mem.Allocator, io: Io, path: []const u8) !ProbeResult {
    const result = std.process.run(gpa, io, .{
        .argv = &.{
            "ffprobe",          "-v",   "error",
            "-select_streams",  "v:0",
            "-show_entries",    "stream=width,height,avg_frame_rate",
            "-of",              "csv=p=0",
            path,
        },
    }) catch |err| switch (err) {
        error.FileNotFound => return FfmpegNotFound,
        else => return err,
    };
    switch (result.term) {
        .exited => |code| if (code != 0) return error.ProbeFailed,
        else => return error.ProbeFailed,
    }

    var lines = std.mem.splitScalar(u8, result.stdout, '\n');
    const line = lines.next() orelse return error.ProbeFailed;
    var parts = std.mem.splitScalar(u8, line, ',');
    const w_str = parts.next() orelse return error.ProbeFailed;
    const h_str = parts.next() orelse return error.ProbeFailed;
    const fps_str = parts.next() orelse return error.ProbeFailed;

    const width = std.fmt.parseInt(u32, std.mem.trim(u8, w_str, " \r"), 10) catch return error.ProbeFailed;
    const height = std.fmt.parseInt(u32, std.mem.trim(u8, h_str, " \r"), 10) catch return error.ProbeFailed;

    // avg_frame_rate comes as "N/D"; some inputs report 0/0, fall back
    // to 24 rather than propagating a zero playback clock.
    var fps: f32 = 24.0;
    var frac = std.mem.splitScalar(u8, std.mem.trim(u8, fps_str, " \r"), '/');
    if (frac.next()) |n_s| {
        if (frac.next()) |d_s| {
            const n = std.fmt.parseFloat(f32, n_s) catch null;
            const d = std.fmt.parseFloat(f32, d_s) catch null;
            if (n != null and d != null and d.? > 0 and n.? > 0) fps = n.? / d.?;
        }
    }
    return .{ .width = width, .height = height, .fps = fps };
}

fn probeDuration(gpa: std.mem.Allocator, io: Io, path: []const u8) !f64 {
    const result = std.process.run(gpa, io, .{
        .argv = &.{ "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path },
    }) catch |err| switch (err) {
        error.FileNotFound => return FfmpegNotFound,
        else => return err,
    };
    switch (result.term) {
        .exited => |code| if (code != 0) return error.ProbeFailed,
        else => return error.ProbeFailed,
    }
    const s = std.mem.trim(u8, result.stdout, " \t\r\n");
    const d = std.fmt.parseFloat(f64, s) catch return error.ProbeFailed;
    if (!std.math.isFinite(d) or d <= 0) return error.ProbeFailed;
    return d;
}

// ---------------------------------------------------------------------------
// ffmpeg PPM pipe source
// ---------------------------------------------------------------------------

const RgbBuffer = struct {
    width: u32,
    height: u32,
    /// w*h*3 bytes, row-major RGB triplets.
    data: []u8,
};

const VideoSource = struct {
    child: std.process.Child,
    file_reader: Io.File.Reader,
    width: u32,
    height: u32,
    fps: f32,

    const Options = struct {
        max_fps: ?f32,
        range: ?struct { start: f64, duration: f64 } = null,
    };

    fn spawnSource(gpa: std.mem.Allocator, io: Io, path: []const u8, probed: ProbeResult, opts: Options) !VideoSource {
        var filter_buf: [64]u8 = undefined;
        var filter: ?[]const u8 = null;
        if (opts.max_fps) |cap| {
            if (cap > 0 and cap < probed.fps) {
                filter = try std.fmt.bufPrint(&filter_buf, "fps={d}", .{cap});
            }
        }

        var argv_buf: [16][]const u8 = undefined;
        var argc: usize = 0;
        argv_buf[argc] = "ffmpeg";
        argc += 1;
        argv_buf[argc] = "-v";
        argc += 1;
        argv_buf[argc] = "error";
        argc += 1;
        var ss_buf: [32]u8 = undefined;
        var t_buf: [32]u8 = undefined;
        if (opts.range) |range| {
            argv_buf[argc] = "-ss";
            argc += 1;
            // -ss before -i: fast keyframe seeking.
            argv_buf[argc] = try std.fmt.bufPrint(&ss_buf, "{d:.3}", .{range.start});
            argc += 1;
        }
        argv_buf[argc] = "-i";
        argc += 1;
        argv_buf[argc] = path;
        argc += 1;
        if (opts.range) |range| {
            argv_buf[argc] = "-t";
            argc += 1;
            argv_buf[argc] = try std.fmt.bufPrint(&t_buf, "{d:.3}", .{range.duration});
            argc += 1;
        }
        if (filter) |f| {
            argv_buf[argc] = "-vf";
            argc += 1;
            argv_buf[argc] = f;
            argc += 1;
        }
        argv_buf[argc] = "-f";
        argc += 1;
        argv_buf[argc] = "image2pipe";
        argc += 1;
        argv_buf[argc] = "-vcodec";
        argc += 1;
        argv_buf[argc] = "ppm";
        argc += 1;
        argv_buf[argc] = "pipe:1";
        argc += 1;

        var child = try std.process.spawn(io, .{
            .argv = argv_buf[0..argc],
            .stdin = .ignore,
            .stdout = .pipe,
            .stderr = .pipe,
        });

        // Drain stderr on a thread so ffmpeg can never block on a full
        // pipe buffer mid-stream.
        if (child.stderr) |stderr_file| {
            const t = try std.Thread.spawn(.{}, drainToNull, .{io, stderr_file});
            t.detach();
        }

        const rd_buf = try gpa.alloc(u8, 1 << 20);
        const fr = child.stdout.?.reader(io, rd_buf);

        const effective_fps = if (opts.max_fps) |cap|
            if (cap > 0 and cap < probed.fps) cap else probed.fps
        else
            probed.fps;

        return .{
            .child = child,
            .file_reader = fr,
            .width = probed.width,
            .height = probed.height,
            .fps = effective_fps,
        };
    }

    fn drainToNull(io: Io, file: Io.File) void {
        var buf: [16384]u8 = undefined;
        var r = file.reader(io, &buf);
        while (true) {
            const n = r.interface.readSliceShort(&buf) catch break;
            if (n == 0) break;
        }
    }

    /// Reads the next PPM frame. Returns null at a clean end-of-stream;
    /// errors on truncation mid-frame.
    fn nextFrame(self: *VideoSource, io: Io, gpa: std.mem.Allocator) !?RgbBuffer {
        const r = &self.file_reader.interface;

        const m0 = r.takeByte() catch |err| switch (err) {
            error.EndOfStream => return null, // clean EOF
            error.ReadFailed => return error.PpmTruncated,
        };
        const m1 = r.takeByte() catch return error.PpmTruncated;
        if (m0 != 'P' or m1 != '6') return error.BadPpmMagic;

        const width = try readPpmUint(r);
        const height = try readPpmUint(r);
        const maxval = try readPpmUint(r);
        if (maxval != 255) return error.BadPpmMaxval;

        const count = @as(usize, width) * @as(usize, height) * 3;
        const data = try gpa.alloc(u8, count);
        errdefer gpa.free(data);
        r.readSliceAll(data) catch {
            gpa.free(data);
            return error.FrameTruncated;
        };
        _ = io;
        return .{ .width = width, .height = height, .data = data };
    }

    fn destroy(self: *VideoSource, io: Io, gpa: std.mem.Allocator) void {
        // Best-effort cleanup: don't leave ffmpeg running if we stopped
        // reading early (--max-frames). A forced kill is required on
        // POSIX: Child.kill sends SIGTERM first, and ffmpeg's graceful-
        // shutdown handler can block forever on a stdout pipe nobody is
        // draining anymore. Windows' TerminateProcess is already
        // immediate, so Child.kill is safe there.
        if (self.child.id) |pid| {
            switch (builtin.os.tag) {
                .windows => self.child.kill(io),
                .macos => {
                    _ = std.c.kill(pid, .KILL);
                    _ = self.child.wait(io) catch {};
                },
                else => {
                    _ = std.os.linux.kill(pid, .KILL);
                    _ = self.child.wait(io) catch {};
                },
            }
        }
        gpa.free(self.file_reader.interface.buffer[0..]);
    }
};

fn readPpmUint(r: *std.Io.Reader) !u32 {
    var digits: [10]u8 = undefined;
    var n: usize = 0;
    var started = false;
    while (true) {
        const c = r.takeByte() catch return error.PpmTruncated;
        switch (c) {
            '#' => {
                // Comment line: skip to newline.
                while (true) {
                    const cc = r.takeByte() catch return error.PpmTruncated;
                    if (cc == '\n') break;
                }
            },
            ' ', '\t', '\r', '\n' => {
                if (started) {
                    return std.fmt.parseInt(u32, digits[0..n], 10) catch error.BadPpmHeader;
                }
            },
            else => {
                if (!std.ascii.isDigit(c)) return error.BadPpmHeader;
                if (n >= digits.len) return error.BadPpmHeader;
                digits[n] = c;
                n += 1;
                started = true;
            },
        }
    }
}

// ---------------------------------------------------------------------------
// frame conversion (box-filter downscale + half-block packing)
// ---------------------------------------------------------------------------

/// Converts an RGB buffer into packed cell data: cols*rows cells, 6
/// bytes each (top RGB, bottom RGB). Exact port of frame.rs's
/// convert_frame, including fit-mode math and pad fill.
fn convertFrame(
    src_pixels: []const u8,
    src_w: u32,
    src_h: u32,
    cols: u32,
    rows: u32,
    fit: FitMode,
    pad: [3]u8,
    out: []u8,
) void {
    const sub_rows = rows * 2;
    const sw: f32 = @floatFromInt(src_w);
    const sh: f32 = @floatFromInt(src_h);

    const dest_w: f32 = @floatFromInt(cols);
    const dest_h = @as(f32, @floatFromInt(sub_rows)) * CELL_ASPECT;

    const src_aspect = sw / sh;
    const dest_aspect = dest_w / dest_h;

    var draw_x0: f32 = 0;
    var draw_y0: f32 = 0;
    var draw_w: f32 = dest_w;
    var draw_h: f32 = dest_h;
    var crop_x0: f32 = 0;
    var crop_y0: f32 = 0;
    var crop_w: f32 = sw;
    var crop_h: f32 = sh;

    switch (fit) {
        .stretch => {},
        .contain => {
            if (src_aspect > dest_aspect) {
                draw_w = dest_w;
                draw_h = dest_w / src_aspect;
            } else {
                draw_w = dest_h * src_aspect;
                draw_h = dest_h;
            }
            draw_x0 = @max((dest_w - draw_w) / 2.0, 0.0);
            draw_y0 = @max((dest_h - draw_h) / 2.0, 0.0);
        },
        .cover => {
            if (src_aspect > dest_aspect) {
                crop_w = sh * dest_aspect;
                crop_h = sh;
            } else {
                crop_w = sw;
                crop_h = sw / dest_aspect;
            }
            crop_x0 = @max((sw - crop_w) / 2.0, 0.0);
            crop_y0 = @max((sh - crop_h) / 2.0, 0.0);
            crop_w = @max(crop_w, 1.0);
            crop_h = @max(crop_h, 1.0);
            draw_x0 = 0;
            draw_y0 = 0;
            draw_w = dest_w;
            draw_h = dest_h;
        },
    }

    // Initialize everything to pad color.
    var ci: usize = 0;
    while (ci < cols * rows) : (ci += 1) {
        out[ci * 6 + 0] = pad[0];
        out[ci * 6 + 1] = pad[1];
        out[ci * 6 + 2] = pad[2];
        out[ci * 6 + 3] = pad[0];
        out[ci * 6 + 4] = pad[1];
        out[ci * 6 + 5] = pad[2];
    }

    var sy: u32 = 0;
    while (sy < sub_rows) : (sy += 1) {
        const dy: f32 = @floatFromInt(sy);
        if (dy < draw_y0 or dy >= draw_y0 + draw_h) continue;
        const v = (dy - draw_y0) / @max(draw_h, 1e-6);
        const fy0 = crop_y0 + v * crop_h;
        const fy1 = crop_y0 + ((dy + 1.0 - draw_y0) / @max(draw_h, 1e-6)) * crop_h;

        var sx: u32 = 0;
        while (sx < cols) : (sx += 1) {
            const dx: f32 = @floatFromInt(sx);
            if (dx < draw_x0 or dx >= draw_x0 + draw_w) continue;
            const u = (dx - draw_x0) / @max(draw_w, 1e-6);
            const fx0 = crop_x0 + u * crop_w;
            const fx1 = crop_x0 + ((dx + 1.0 - draw_x0) / @max(draw_w, 1e-6)) * crop_w;

            const color = sampleBlock(src_pixels, src_w, src_h, fx0, fy0, fx1, fy1);

            const cell_idx = (sy / 2 * cols + sx);
            const half: usize = if (sy % 2 == 0) 0 else 3;
            out[cell_idx * 6 + half + 0] = color[0];
            out[cell_idx * 6 + half + 1] = color[1];
            out[cell_idx * 6 + half + 2] = color[2];
        }
    }
}

fn getPixel(pixels: []const u8, w: u32, x: u32, y: u32) [3]u8 {
    const idx = (@as(usize, y) * w + x) * 3;
    return .{ pixels[idx], pixels[idx + 1], pixels[idx + 2] };
}

/// Averages every source pixel whose center falls within the box
/// ([x0,x1) x [y0,y1)). Falls back to strided sampling for huge boxes
/// so pathological downscale ratios stay bounded.
fn sampleBlock(
    pixels: []const u8,
    w: u32,
    h: u32,
    x0: f32,
    y0: f32,
    x1: f32,
    y1: f32,
) [3]u8 {
    const wi: i64 = @intCast(w);
    const hi: i64 = @intCast(h);

    var ix0: i64 = @intFromFloat(@floor(x0));
    ix0 = std.math.clamp(ix0, 0, wi - 1);
    var iy0: i64 = @intFromFloat(@floor(y0));
    iy0 = std.math.clamp(iy0, 0, hi - 1);
    var ix1: i64 = @as(i64, @intFromFloat(@ceil(x1))) - 1;
    ix1 = @max(ix1, ix0);
    ix1 = std.math.clamp(ix1, 0, wi - 1);
    var iy1: i64 = @as(i64, @intFromFloat(@ceil(y1))) - 1;
    iy1 = @max(iy1, iy0);
    iy1 = std.math.clamp(iy1, 0, hi - 1);

    const bw: u64 = @intCast(ix1 - ix0 + 1);
    const bh: u64 = @intCast(iy1 - iy0 + 1);
    const box_pixels = bw * bh;

    var acc: [3]u64 = .{ 0, 0, 0 };

    if (box_pixels > 4096) {
        // Strided sample; visually indistinguishable at such ratios.
        const step_x: i64 = @max(@divTrunc(ix1 - ix0, 32), 1);
        const step_y: i64 = @max(@divTrunc(iy1 - iy0, 32), 1);
        var samples: u64 = 0;
        var y = iy0;
        while (y <= iy1) : (y += step_y) {
            var x = ix0;
            while (x <= ix1) : (x += step_x) {
                const p = getPixel(pixels, w, @intCast(x), @intCast(y));
                acc[0] += p[0];
                acc[1] += p[1];
                acc[2] += p[2];
                samples += 1;
            }
        }
        return .{
            @intCast(acc[0] / samples),
            @intCast(acc[1] / samples),
            @intCast(acc[2] / samples),
        };
    }

    var y = iy0;
    while (y <= iy1) : (y += 1) {
        var x = ix0;
        while (x <= ix1) : (x += 1) {
            const p = getPixel(pixels, w, @intCast(x), @intCast(y));
            acc[0] += p[0];
            acc[1] += p[1];
            acc[2] += p[2];
        }
    }
    const n: u64 = box_pixels;
    return .{
        @intCast(acc[0] / n),
        @intCast(acc[1] / n),
        @intCast(acc[2] / n),
    };
}

// ---------------------------------------------------------------------------
// .afwframes serialization
// ---------------------------------------------------------------------------

fn writeHeader(w: *std.Io.Writer, cols: u32, rows: u32, frame_count: u32, fps: f32) !void {
    var hdr: [16]u8 = undefined;
    @memcpy(hdr[0..4], MAGIC);
    std.mem.writeInt(u32, hdr[4..8], cols, .little);
    std.mem.writeInt(u32, hdr[8..12], rows, .little);
    std.mem.writeInt(u32, hdr[12..16], frame_count, .little);
    try w.writeAll(hdr[0..16]);
    var fps_bits: [4]u8 = undefined;
    std.mem.writeInt(u32, &fps_bits, @bitCast(fps), .little);
    try w.writeAll(&fps_bits);
}

// ---------------------------------------------------------------------------
// generated loader script
// ---------------------------------------------------------------------------

fn writeLoaderScript(
    gpa: std.mem.Allocator,
    io: Io,
    dir: Io.Dir,
    output_path: []const u8,
    asset_filename: []const u8,
    module_hint: []const u8,
) !void {
    const loader_path = try loaderPathFor(gpa, output_path);
    defer gpa.free(loader_path);

    // Loader module name: output path minus .afwframes, basename only.
    const suffix = ".afwframes";
    const stripped = if (std.mem.endsWith(u8, output_path, suffix))
        output_path[0 .. output_path.len - suffix.len]
    else
        output_path;
    const slash = std.mem.lastIndexOfScalar(u8, stripped, '/');
    const loader_base = if (slash) |s| stripped[s + 1 ..] else stripped;

    const script = try std.fmt.allocPrint(gpa,
        \\'''
        \\Auto-generated by afw_media: plays back "{s}" using {s}.
        \\
        \\Usage:
        \\    python3 {s}.py
        \\
        \\Requires {s}.py to be importable (same directory, or on PYTHONPATH).
        \\Works with both truecolor and old() compatibility mode: this script
        \\never bakes in a color mode, it just feeds raw RGB into Canvas:
        \\call {s}.old() before running if your terminal needs 256-color
        \\fallback.
        \\'''
        \\import argparse
        \\import struct
        \\import sys
        \\from pathlib import Path
        \\
        \\sys.path.insert(0, str(Path(__file__).resolve().parent))
        \\import {s} as afw  # noqa: E402
        \\
        \\
        \\class AfwFrames:
        \\    HEADER_FORMAT = "<4sIIIf"
        \\    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
        \\
        \\    def __init__(self, path):
        \\        self._f = open(path, "rb")
        \\        header = self._f.read(self.HEADER_SIZE)
        \\        if len(header) < self.HEADER_SIZE:
        \\            raise ValueError(f"{{path}}: file too small to be a valid .afwframes asset")
        \\        magic, cols, rows, frame_count, fps = struct.unpack(self.HEADER_FORMAT, header)
        \\        if magic != b"AFW1":
        \\            raise ValueError(f"{{path}}: not an AFW1 asset (bad magic bytes)")
        \\        self.cols = cols
        \\        self.rows = rows
        \\        self.frame_count = frame_count
        \\        self.fps = fps
        \\        self._frame_bytes = cols * rows * 6
        \\        self._data_start = self.HEADER_SIZE
        \\
        \\    def __len__(self):
        \\        return self.frame_count
        \\
        \\    def close(self):
        \\        self._f.close()
        \\
        \\    def __enter__(self):
        \\        return self
        \\
        \\    def __exit__(self, *exc):
        \\        self.close()
        \\
        \\    def read_frame(self, index: int) -> bytes:
        \\        if self.frame_count == 0:
        \\            raise ValueError("asset has zero frames")
        \\        index = max(0, min(index, self.frame_count - 1))
        \\        offset = self._data_start + index * self._frame_bytes
        \\        self._f.seek(offset)
        \\        data = self._f.read(self._frame_bytes)
        \\        if len(data) < self._frame_bytes:
        \\            raise IOError(
        \\                f"truncated .afwframes asset: expected {{self._frame_bytes}} bytes "
        \\                f"for frame {{index}}, got {{len(data)}} (file may be corrupt or still writing)"
        \\            )
        \\        return data
        \\
        \\    def draw_frame(self, canvas: "afw.Canvas", index: int, *, x_offset: int = 0, y_offset: int = 0) -> None:
        \\        data = self.read_frame(index)
        \\        if hasattr(canvas, "blit_subpixel_frame"):
        \\            canvas.blit_subpixel_frame(data, self.cols, self.rows, x_offset=x_offset, y_offset=y_offset)
        \\            return
        \\        cols, rows = self.cols, self.rows
        \\        mv = memoryview(data)
        \\        i = 0
        \\        for cy in range(rows):
        \\            for cx in range(cols):
        \\                tr, tg, tb, br, bg, bb = mv[i:i + 6]
        \\                i += 6
        \\                top = afw.Color(tr, tg, tb)
        \\                bottom = afw.Color(br, bg, bb)
        \\                canvas.put_subpixel(x_offset + cx, (y_offset + cy) * 2, top)
        \\                canvas.put_subpixel(x_offset + cx, (y_offset + cy) * 2 + 1, bottom)
        \\
        \\
        \\def main() -> None:
        \\    parser = argparse.ArgumentParser(description="Play an afwframes asset.")
        \\    parser.add_argument("--show-fps", action="store_true", help="Display the FPS counter.")
        \\    parser.add_argument("--audio", nargs="?", const="", default=None, help="Audio file to play with video.")
        \\    args = parser.parse_args()
        \\
        \\    asset_path = Path(__file__).resolve().parent / "{s}"
        \\    with AfwFrames(asset_path) as frames:
        \\        is_video = frames.frame_count > 1
        \\
        \\        app = afw.App(
        \\            target_fps=frames.fps if is_video and frames.fps > 0 else 60.0,
        \\            show_fps=args.show_fps,
        \\        )
        \\
        \\        audio_player = None
        \\        audio_target = None
        \\        if args.audio is not None:
        \\            if args.audio != "":
        \\                audio_target = args.audio
        \\            else:
        \\                base = asset_path.stem
        \\                for ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mkv", ".webm"):
        \\                    candidate = asset_path.parent / f"{{base}}{{ext}}"
        \\                    if candidate.exists():
        \\                        audio_target = str(candidate)
        \\                        break
        \\
        \\            if audio_target:
        \\                audio_player = afw.AudioPlayer()
        \\                audio_player.play(audio_target, loop=is_video)
        \\
        \\        state = {{"index": 0}}
        \\
        \\        @app.on_update
        \\        def update(dt):
        \\            if is_video:
        \\                state["index"] = (state["index"] + 1) % frames.frame_count
        \\            else:
        \\                app.stop() if app.frame_count > 1 else None
        \\
        \\        @app.on_render
        \\        def render(canvas):
        \\            frames.draw_frame(canvas, state["index"])
        \\
        \\        @app.on_key
        \\        def handle_key(ev):
        \\            if ev.key == afw.Key.ESCAPE or ev.is_char("q") or ev.key == afw.Key.CTRL_C:
        \\                app.stop()
        \\
        \\        try:
        \\            app.run()
        \\        finally:
        \\            if audio_player is not None:
        \\                audio_player.stop()
        \\
        \\
        \\if __name__ == "__main__":
        \\    main()
        \\
    , .{ asset_filename, module_hint, loader_base, module_hint, module_hint, module_hint, asset_filename });
    defer gpa.free(script);

    var f = try dir.createFile(io, loader_path, .{});
    defer f.close(io);
    var buf: [8192]u8 = undefined;
    var w = f.writer(io, &buf);
    try w.interface.writeAll(script);
    try w.interface.flush();
}

fn loaderPathFor(gpa: std.mem.Allocator, output_path: []const u8) ![]const u8 {
    const suffix = ".afwframes";
    const base = if (std.mem.endsWith(u8, output_path, suffix))
        output_path[0 .. output_path.len - suffix.len]
    else
        output_path;
    return std.fmt.allocPrint(gpa, "{s}.py", .{base});
}

fn audioStemPathFor(gpa: std.mem.Allocator, output_path: []const u8) ![]const u8 {
    const base = if (std.mem.endsWith(u8, output_path, ".afwframes"))
        output_path[0 .. output_path.len - ".afwframes".len]
    else
        output_path;
    return std.fmt.allocPrint(gpa, "{s}.mp3", .{base});
}

/// Best-effort audio track extraction next to the asset (stem.mp3).
/// Silently ignored if ffmpeg fails or produces an empty track.
fn extractAudioIfPresent(gpa: std.mem.Allocator, io: Io, dir: Io.Dir, input: []const u8, output_afwframes: []const u8, quiet: bool) void {
    const audio_out = audioStemPathFor(gpa, output_afwframes) catch return;
    defer gpa.free(audio_out);

    const result = std.process.run(gpa, io, .{
        .argv = &.{ "ffmpeg", "-y", "-v", "error", "-i", input, "-vn", "-q:a", "2", audio_out },
    }) catch return;
    switch (result.term) {
        .exited => |code| if (code != 0) return,
        else => return,
    }
    const st = dir.statFile(io, audio_out, .{}) catch return;
    if (st.size <= 1024) {
        dir.deleteFile(io, audio_out) catch {};
        return;
    }
    if (!quiet) eprint(io, "extracted audio track -> {s}\n", .{audio_out});
}

// ---------------------------------------------------------------------------
// decode-one-image helper (via ffmpeg)
// ---------------------------------------------------------------------------

fn decodeStillImage(gpa: std.mem.Allocator, io: Io, path: []const u8) !RgbBuffer {
    const result = std.process.run(gpa, io, .{
        .argv = &.{ "ffmpeg", "-v", "error", "-i", path, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "ppm", "pipe:1" },
    }) catch |err| switch (err) {
        error.FileNotFound => return FfmpegNotFound,
        else => return err,
    };
    switch (result.term) {
        .exited => |code| if (code != 0) return error.DecodeFailed,
        else => return error.DecodeFailed,
    }

    // Parse the single PPM frame out of the collected stdout.
    var stream = std.Io.Reader.fixed(result.stdout);
    const m0 = stream.takeByte() catch return error.DecodeFailed;
    const m1 = stream.takeByte() catch return error.DecodeFailed;
    if (m0 != 'P' or m1 != '6') return error.DecodeFailed;
    const width = try readPpmUint(&stream);
    const height = try readPpmUint(&stream);
    const maxval = try readPpmUint(&stream);
    if (maxval != 255) return error.DecodeFailed;

    const count = @as(usize, width) * @as(usize, height) * 3;
    if (result.stdout.len - stream.seek < count) return error.DecodeFailed;
    const data = try gpa.dupe(u8, result.stdout[stream.seek .. stream.seek + count]);
    return .{ .width = width, .height = height, .data = data };
}

// ---------------------------------------------------------------------------
// conversion drivers
// ---------------------------------------------------------------------------

const Context = struct {
    gpa: std.mem.Allocator,
    io: Io,
    cwd: Io.Dir,
    args: Args,
};

fn convertImage(ctx: *Context) !void {
    const img = try decodeStillImage(ctx.gpa, ctx.io, ctx.args.input);
    defer ctx.gpa.free(img.data);

    const rows = ctx.args.rows orelse deriveRows(ctx.args.cols, img.width, img.height);

    const cells_len = @as(usize, ctx.args.cols) * rows * 6;
    const cells = try ctx.gpa.alloc(u8, cells_len);
    defer ctx.gpa.free(cells);
    convertFrame(img.data, img.width, img.height, ctx.args.cols, rows, ctx.args.fit, ctx.args.pad_color, cells);

    var f = try ctx.cwd.createFile(ctx.io, ctx.args.output, .{});
    defer f.close(ctx.io);
    var wbuf: [1 << 16]u8 = undefined;
    var w = f.writer(ctx.io, &wbuf);
    try writeHeader(&w.interface, ctx.args.cols, rows, 1, 0.0);
    try w.interface.writeAll(cells);
    try w.interface.flush();

    if (ctx.args.emit_loader) {
        const asset_name = std.fs.path.basename(ctx.args.output);
        try writeLoaderScript(ctx.gpa, ctx.io, ctx.cwd, ctx.args.output, asset_name, ctx.args.module_hint);
        if (!ctx.args.quiet) eprint(ctx.io, "wrote loader script -> {s}.py\n", .{trimSuffix(ctx.args.output, ".afwframes")});
    }
    if (!ctx.args.quiet) {
        eprint(ctx.io, "converted {s} -> {s} ({d}x{d} cells)\n", .{ ctx.args.input, ctx.args.output, ctx.args.cols, rows });
    }
}

fn trimSuffix(s: []const u8, suffix: []const u8) []const u8 {
    return if (std.mem.endsWith(u8, s, suffix)) s[0 .. s.len - suffix.len] else s;
}

/// Writes the finished asset (header + frames), then the loader script.
fn writeAsset(ctx: *Context, frames: []const []const u8, cols: u32, rows: u32, fps: f32) !void {
    var f = try ctx.cwd.createFile(ctx.io, ctx.args.output, .{});
    defer f.close(ctx.io);
    var wbuf: [1 << 20]u8 = undefined;
    var w = f.writer(ctx.io, &wbuf);
    try writeHeader(&w.interface, cols, rows, @intCast(frames.len), fps);
    for (frames) |frame| {
        try w.interface.writeAll(frame);
    }
    try w.interface.flush();

    if (!ctx.args.emit_loader) return;
    const asset_name = std.fs.path.basename(ctx.args.output);
    try writeLoaderScript(ctx.gpa, ctx.io, ctx.cwd, ctx.args.output, asset_name, ctx.args.module_hint);
    if (!ctx.args.quiet) eprint(ctx.io, "wrote loader script -> {s}.py\n", .{trimSuffix(ctx.args.output, ".afwframes")});
}

fn convertVideoSequential(ctx: *Context, probed: ProbeResult) !void {
    const rows = ctx.args.rows orelse deriveRows(ctx.args.cols, probed.width, probed.height);

    var effective_fps = probed.fps;
    if (ctx.args.max_fps) |cap| {
        if (cap > 0 and cap < effective_fps) effective_fps = cap;
    }

    if (!ctx.args.quiet) {
        eprint(ctx.io, "extracting frames from {s} ({d}x{d} source, {d:.2} fps target) ...\n", .{ ctx.args.input, probed.width, probed.height, effective_fps });
    }

    var source = try VideoSource.spawnSource(ctx.gpa, ctx.io, ctx.args.input, probed, .{ .max_fps = ctx.args.max_fps });
    defer source.destroy(ctx.io, ctx.gpa);

    // Stream frames to a temp file, then assemble the real asset with
    // the now-known frame count patched into the header (ffprobe's
    // frame-count estimate isn't trustworthy).
    const tmp_path = try std.fmt.allocPrint(ctx.gpa, "{s}.partial", .{ctx.args.output});

    var tmp_file = ctx.cwd.createFile(ctx.io, tmp_path, .{}) catch |err| {
        return err;
    };
    defer tmp_file.close(ctx.io);
    var tw_buf: [1 << 20]u8 = undefined;
    var tw = tmp_file.writer(ctx.io, &tw_buf);

    const cells_len = @as(usize, ctx.args.cols) * rows * 6;
    const cells = try ctx.gpa.alloc(u8, cells_len);
    defer ctx.gpa.free(cells);

    var frame_count: u32 = 0;
    while (true) {
        if (ctx.args.max_frames) |limit| {
            if (frame_count >= limit) break;
        }
        const raw = (try source.nextFrame(ctx.io, ctx.gpa)) orelse break;
        defer ctx.gpa.free(raw.data);
        convertFrame(raw.data, raw.width, raw.height, ctx.args.cols, rows, ctx.args.fit, ctx.args.pad_color, cells);
        try tw.interface.writeAll(cells);
        frame_count += 1;
        if (!ctx.args.quiet and frame_count % 30 == 0) {
            eprint(ctx.io, "  ... {d} frames converted\n", .{frame_count});
        }
    }
    try tw.interface.flush();

    if (frame_count == 0) {
        ctx.cwd.deleteFile(ctx.io, tmp_path) catch {};
        return error.ZeroFrames;
    }

    // Assemble: final header followed by the buffered frame data.
    {
        var out_file = try ctx.cwd.createFile(ctx.io, ctx.args.output, .{});
        defer out_file.close(ctx.io);
        var ow_buf: [1 << 20]u8 = undefined;
        var ow = out_file.writer(ctx.io, &ow_buf);
        try writeHeader(&ow.interface, ctx.args.cols, rows, frame_count, effective_fps);

        var tmp_reader_file = try ctx.cwd.openFile(ctx.io, tmp_path, .{});
        defer tmp_reader_file.close(ctx.io);
        const tr_buf = try ctx.gpa.alloc(u8, 1 << 20);
        defer ctx.gpa.free(tr_buf);
        var tr = tmp_reader_file.reader(ctx.io, tr_buf);
        var chunk: [65536]u8 = undefined;
        while (true) {
            const n = tr.interface.readSliceShort(&chunk) catch break;
            if (n == 0) break;
            try ow.interface.writeAll(chunk[0..n]);
        }
        try ow.interface.flush();
    }
    ctx.cwd.deleteFile(ctx.io, tmp_path) catch {};

    if (!ctx.args.quiet) {
        eprint(ctx.io, "converted {s} -> {s} ({d} frames, {d}x{d} cells, {d:.2} fps)\n", .{ ctx.args.input, ctx.args.output, frame_count, ctx.args.cols, rows, probed.fps });
    }

    extractAudioIfPresent(ctx.gpa, ctx.io, ctx.cwd, ctx.args.input, ctx.args.output, ctx.args.quiet);
    if (ctx.args.emit_loader) {
        const asset_name = std.fs.path.basename(ctx.args.output);
        try writeLoaderScript(ctx.gpa, ctx.io, ctx.cwd, ctx.args.output, asset_name, ctx.args.module_hint);
        if (!ctx.args.quiet) eprint(ctx.io, "wrote loader script -> {s}.py\n", .{trimSuffix(ctx.args.output, ".afwframes")});
    }
}

// ---------------------------------------------------------------------------
// parallel file-mode conversion
// ---------------------------------------------------------------------------

const SegmentResult = struct {
    frames: std.ArrayList([]u8) = .empty,
    err: bool = false,
    count: usize = 0,
};

const SegmentCtx = struct {
    parent: *Context,
    probed: ProbeResult,
    rows: u32,
    start: f64,
    duration: f64,
    seg_idx: usize,
    result: *SegmentResult,
};

fn segmentWorker(sctx: *SegmentCtx) void {
    const ctx = sctx.parent;
    var source = VideoSource.spawnSource(ctx.gpa, ctx.io, ctx.args.input, sctx.probed, .{
        .max_fps = ctx.args.max_fps,
        .range = .{ .start = sctx.start, .duration = sctx.duration },
    }) catch {
        sctx.result.err = true;
        return;
    };
    defer source.destroy(ctx.io, ctx.gpa);

    const cells_len = @as(usize, ctx.args.cols) * sctx.rows * 6;

    while (true) {
        const raw = source.nextFrame(ctx.io, ctx.gpa) catch {
            sctx.result.err = true;
            return;
        } orelse break;
        const cells = ctx.gpa.alloc(u8, cells_len) catch {
            ctx.gpa.free(raw.data);
            sctx.result.err = true;
            return;
        };
        convertFrame(raw.data, raw.width, raw.height, ctx.args.cols, sctx.rows, ctx.args.fit, ctx.args.pad_color, cells);
        ctx.gpa.free(raw.data);
        sctx.result.frames.append(ctx.gpa, cells) catch {
            sctx.result.err = true;
            return;
        };
    }
    sctx.result.count = sctx.result.frames.items.len;
}

fn convertVideoParallel(ctx: *Context, probed: ProbeResult, duration: f64) !void {
    const rows = ctx.args.rows orelse deriveRows(ctx.args.cols, probed.width, probed.height);

    var effective_fps = probed.fps;
    if (ctx.args.max_fps) |cap| {
        if (cap > 0 and cap < probed.fps) effective_fps = cap;
    }

    // One segment per ~1s of video, capped at the job count: keeps each
    // ffmpeg worker doing enough real work to be worth its own process.
    const duration_secs: usize = @intFromFloat(@max(duration, 1.0));
    const segment_count: usize = @min(ctx.args.jobs, duration_secs);
    const segment_dur = duration / @as(f64, @floatFromInt(segment_count));

    if (!ctx.args.quiet) {
        eprint(ctx.io, "extracting frames from {s} ({d}x{d} source, {d:.2} fps target, {d} parallel segments) ...\n", .{ ctx.args.input, probed.width, probed.height, effective_fps, segment_count });
    }

    const results = try ctx.gpa.alloc(SegmentResult, segment_count);
    defer ctx.gpa.free(results);
    for (results) |*r| r.* = .{};

    // Contexts must live on the heap: every worker keeps its context
    // pointer until it is joined, long after this loop's stack frames
    // are gone.
    const sctxs = try ctx.gpa.alloc(SegmentCtx, segment_count);
    defer ctx.gpa.free(sctxs);

    const threads = try ctx.gpa.alloc(std.Thread, segment_count);
    defer ctx.gpa.free(threads);
    var spawned: usize = 0;
    defer {
        for (threads[0..spawned]) |t| t.join();
    }

    for (0..segment_count) |seg_idx| {
        const start = segment_dur * @as(f64, @floatFromInt(seg_idx));
        const dur = if (seg_idx == segment_count - 1) duration - start else segment_dur;
        sctxs[seg_idx] = .{
            .parent = ctx,
            .probed = probed,
            .rows = rows,
            .start = start,
            .duration = dur,
            .seg_idx = seg_idx,
            .result = &results[seg_idx],
        };
        threads[spawned] = std.Thread.spawn(.{}, segmentWorker, .{&sctxs[seg_idx]}) catch {
            results[seg_idx].err = true;
            continue;
        };
        spawned += 1;
    }

    // Every worker owns one result slot, so completion order does not
    // matter: frames stay in segment order by construction.
    for (threads[0..spawned]) |t| t.join();
    spawned = 0; // already joined; the deferred cleanup must not rejoin

    var any_err = false;
    for (results, 0..) |*r, seg_idx| {
        if (!ctx.args.quiet) {
            eprint(ctx.io, "  ... segment {d}: {d} frames{s}\n", .{ seg_idx, r.count, if (r.err) " (FAILED)" else "" });
        }
        any_err = any_err or r.err;
    }
    if (any_err) return error.SegmentFailed;

    var all_frames: std.ArrayList([]u8) = .empty;
    defer {
        for (all_frames.items) |frame| ctx.gpa.free(frame);
        all_frames.deinit(ctx.gpa);
    }
    for (results) |*r| {
        for (r.frames.items) |frame| {
            try all_frames.append(ctx.gpa, frame);
        }
        r.frames.deinit(ctx.gpa);
    }

    if (ctx.args.max_frames) |limit| {
        const keep = @min(@as(usize, limit), all_frames.items.len);
        for (all_frames.items[keep..]) |frame| ctx.gpa.free(frame);
        all_frames.shrinkRetainingCapacity(keep);
    }

    if (all_frames.items.len == 0) return error.ZeroFrames;

    try writeAsset(ctx, all_frames.items, ctx.args.cols, rows, effective_fps);

    if (!ctx.args.quiet) {
        eprint(ctx.io, "converted {s} -> {s} ({d} frames, {d}x{d} cells, {d:.2} fps, {d} jobs)\n", .{ ctx.args.input, ctx.args.output, all_frames.items.len, ctx.args.cols, rows, effective_fps, segment_count });
    }
}

// ---------------------------------------------------------------------------
// streaming mode
// ---------------------------------------------------------------------------

fn streamVideo(ctx: *Context, probed: ProbeResult) !void {
    const rows = ctx.args.rows orelse deriveRows(ctx.args.cols, probed.width, probed.height);

    if (!ctx.args.quiet) {
        eprint(ctx.io, "streaming {s} ({d}x{d} source -> {d}x{d} cells, {d:.2} fps) ...\n", .{ ctx.args.input, probed.width, probed.height, ctx.args.cols, rows, probed.fps });
    }

    var source = try VideoSource.spawnSource(ctx.gpa, ctx.io, ctx.args.input, probed, .{ .max_fps = ctx.args.max_fps });
    defer source.destroy(ctx.io, ctx.gpa);

    var out_buf: [1 << 16]u8 = undefined;
    var out = Io.File.stdout().writer(ctx.io, &out_buf);

    // frame_count = 0 signals "unbounded live stream" to readers.
    try writeHeader(&out.interface, ctx.args.cols, rows, 0, probed.fps);
    try out.interface.flush(); // readers may block on the header alone

    const cells_len = @as(usize, ctx.args.cols) * rows * 6;
    const cells = try ctx.gpa.alloc(u8, cells_len);
    defer ctx.gpa.free(cells);

    var frame_count: u32 = 0;
    while (true) {
        if (ctx.args.max_frames) |limit| {
            if (frame_count >= limit) break;
        }
        const raw = (try source.nextFrame(ctx.io, ctx.gpa)) orelse break;
        defer ctx.gpa.free(raw.data);
        convertFrame(raw.data, raw.width, raw.height, ctx.args.cols, rows, ctx.args.fit, ctx.args.pad_color, cells);
        try out.interface.writeAll(cells);
        try out.interface.flush(); // the crux of real-time streaming
        frame_count += 1;
    }

    if (!ctx.args.quiet) {
        eprint(ctx.io, "stream ended after {d} frames\n", .{frame_count});
    }
}

// ---------------------------------------------------------------------------
// arg parsing + main
// ---------------------------------------------------------------------------

fn parseArgs(arena: std.mem.Allocator, iter_values: [][]const u8) !Args {
    var args = Args{
        .input = "",
        .output = "",
    };

    var i: usize = 0;
    while (i < iter_values.len) : (i += 1) {
        const arg = iter_values[i];
        const needsValue = struct {
            fn check(idx: usize, vals: [][]const u8) ![]const u8 {
                if (idx + 1 >= vals.len) return error.MissingValue;
                return vals[idx + 1];
            }
        }.check;

        if (std.mem.eql(u8, arg, "-o") or std.mem.eql(u8, arg, "--output")) {
            args.output = try needsValue(i, iter_values);
            i += 1;
        } else if (std.mem.eql(u8, arg, "--cols")) {
            const v = try needsValue(i, iter_values);
            args.cols = std.fmt.parseInt(u32, v, 10) catch return error.BadCols;
            i += 1;
        } else if (std.mem.eql(u8, arg, "--rows")) {
            const v = try needsValue(i, iter_values);
            args.rows = std.fmt.parseInt(u32, v, 10) catch return error.BadRows;
            i += 1;
        } else if (std.mem.eql(u8, arg, "--fit")) {
            const v = try needsValue(i, iter_values);
            i += 1;
            if (std.mem.eql(u8, v, "contain")) {
                args.fit = .contain;
            } else if (std.mem.eql(u8, v, "cover")) {
                args.fit = .cover;
            } else if (std.mem.eql(u8, v, "stretch")) {
                args.fit = .stretch;
            } else return error.BadFit;
        } else if (std.mem.eql(u8, arg, "--fps")) {
            const v = try needsValue(i, iter_values);
            args.max_fps = std.fmt.parseFloat(f32, v) catch return error.BadFps;
            i += 1;
        } else if (std.mem.eql(u8, arg, "--max-frames")) {
            const v = try needsValue(i, iter_values);
            args.max_frames = std.fmt.parseInt(u32, v, 10) catch return error.BadMaxFrames;
            i += 1;
        } else if (std.mem.eql(u8, arg, "--pad-color")) {
            const v = try needsValue(i, iter_values);
            args.pad_color = try parseHexColor(v);
            i += 1;
        } else if (std.mem.eql(u8, arg, "--no-loader")) {
            args.emit_loader = false;
        } else if (std.mem.eql(u8, arg, "--module")) {
            args.module_hint = try needsValue(i, iter_values);
            i += 1;
        } else if (std.mem.eql(u8, arg, "-q") or std.mem.eql(u8, arg, "--quiet")) {
            args.quiet = true;
        } else if (std.mem.eql(u8, arg, "--stream")) {
            args.stream = true;
        } else if (std.mem.eql(u8, arg, "-j") or std.mem.eql(u8, arg, "--jobs")) {
            const v = try needsValue(i, iter_values);
            args.jobs = std.fmt.parseInt(usize, v, 10) catch return error.BadJobs;
            i += 1;
        } else if (arg.len > 0 and arg[0] != '-' and args.input.len == 0) {
            args.input = try arena.dupe(u8, arg);
        } else {
            return error.UnrecognizedArgument;
        }
    }

    if (args.input.len == 0) return error.MissingInput;
    if (args.cols == 0) return error.BadCols;
    if (args.rows) |r| {
        if (r == 0) return error.BadRows;
    }
    if (args.jobs == 0) return error.BadJobs;

    if (args.output.len == 0) {
        args.output = try defaultOutput(arena, args.input);
    }
    return args;
}

pub fn main(init: std.process.Init) !u8 {
    const gpa = init.gpa;
    const io = init.io;
    const arena = init.arena.allocator();
    const cwd = Io.Dir.cwd();

    // ---- collect argv ----
    var argv_list: std.ArrayList([]const u8) = .empty;
    {
        var it = try init.minimal.args.iterateAllocator(gpa);
        defer it.deinit();
        var first = true;
        while (it.next()) |a| {
            if (first) {
                first = false;
                continue; // skip argv[0]
            }
            try argv_list.append(gpa, try arena.dupe(u8, a));
        }
    }
    const argv = argv_list.items;
    defer argv_list.deinit(gpa);

    if (argv.len == 0) {
        printUsage(io);
        return 1;
    }
    for (argv) |a| {
        if (std.mem.eql(u8, a, "-h") or std.mem.eql(u8, a, "--help")) {
            printUsage(io);
            return 0;
        }
    }

    const args = parseArgs(arena, argv) catch |err| {
        switch (err) {
            error.MissingInput => eprint(io, "afw_media: error: missing required <input> argument\n", .{}),
            error.BadCols => eprint(io, "afw_media: error: --cols must be a positive integer greater than 0\n", .{}),
            error.BadRows => eprint(io, "afw_media: error: --rows must be a positive integer greater than 0\n", .{}),
            error.BadFit => eprint(io, "afw_media: error: unknown --fit mode (expected contain/cover/stretch)\n", .{}),
            error.BadJobs => eprint(io, "afw_media: error: --jobs must be a positive integer greater than 0\n", .{}),
            error.InvalidHexColor => eprint(io, "afw_media: error: invalid hex color (expected RRGGBB)\n", .{}),
            error.MissingValue => eprint(io, "afw_media: error: option requires a value\n", .{}),
            error.UnrecognizedArgument => eprint(io, "afw_media: error: unrecognized argument\n", .{}),
            else => eprint(io, "afw_media: error: failed to parse arguments: {s}\n", .{@errorName(err)}),
        }
        return 1;
    };

    var ctx = Context{ .gpa = gpa, .io = io, .cwd = cwd, .args = args };

    // input exists?
    _ = cwd.statFile(io, args.input, .{}) catch {
        eprint(io, "afw_media: error: input file not found: {s}\n", .{args.input});
        return 1;
    };

    if (args.stream) {
        if (isImagePath(args.input)) {
            eprint(io, "afw_media: error: --stream only applies to video input\n", .{});
            return 1;
        }
        const probed = probe(gpa, io, args.input) catch |err| {
            reportProbeError(io, err);
            return 1;
        };
        streamVideo(&ctx, probed) catch |err| {
            reportConvertError(io, err);
            return 1;
        };
        return 0;
    }

    if (isImagePath(args.input)) {
        convertImage(&ctx) catch |err| {
            reportConvertError(io, err);
            return 1;
        };
        return 0;
    }

    const probed = probe(gpa, io, args.input) catch |err| {
        reportProbeError(io, err);
        return 1;
    };

    if (args.jobs > 1) {
        const duration = probeDuration(gpa, io, args.input) catch |err| {
            reportProbeError(io, err);
            return 1;
        };
        convertVideoParallel(&ctx, probed, duration) catch |err| {
            reportConvertError(io, err);
            return 1;
        };
    } else {
        convertVideoSequential(&ctx, probed) catch |err| {
            reportConvertError(io, err);
            return 1;
        };
    }
    return 0;
}

fn reportProbeError(io: Io, err: anyerror) void {
    switch (err) {
        FfmpegNotFound => eprint(io, "afw_media: error: ffmpeg/ffprobe was not found on PATH. afw_media needs them installed to read media files.\n", .{}),
        error.ProbeFailed => eprint(io, "afw_media: error: failed to probe media (ffprobe returned no stream information)\n", .{}),
        else => eprint(io, "afw_media: error: {s}\n", .{@errorName(err)}),
    }
}

fn reportConvertError(io: Io, err: anyerror) void {
    switch (err) {
        FfmpegNotFound => eprint(io, "afw_media: error: ffmpeg was not found on PATH.\n", .{}),
        error.ZeroFrames => eprint(io, "afw_media: error: produced zero decodable frames (unsupported codec, corrupt file, or --max-frames 0?)\n", .{}),
        error.FrameTruncated => eprint(io, "afw_media: error: ffmpeg's output was truncated mid-frame\n", .{}),
        error.BadPpmMagic => eprint(io, "afw_media: error: unexpected PPM magic bytes from ffmpeg\n", .{}),
        error.DecodeFailed => eprint(io, "afw_media: error: failed to decode image via ffmpeg\n", .{}),
        else => eprint(io, "afw_media: error: {s}\n", .{@errorName(err)}),
    }
}
