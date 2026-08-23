const std = @import("std");

const UPPER = "\xe2\x96\x80";
const FULL = "\xe2\x96\x88";

fn putDec(buf: [*]u8, cap: usize, pos: usize, val: u32) usize {
    var tmp: [10]u8 = undefined;
    var n: usize = 0;
    var v = val;
    if (v == 0) {
        if (pos < cap) buf[pos] = '0';
        return pos + 1;
    }
    while (v > 0) {
        tmp[n] = '0' + @as(u8, @intCast(v % 10));
        n += 1;
        v /= 10;
    }
    var i: usize = 0;
    while (i < n) : (i += 1) {
        if (pos + i < cap) buf[pos + i] = tmp[n - 1 - i];
    }
    return pos + n;
}

fn put(buf: [*]u8, cap: usize, pos: usize, data: []const u8) usize {
    if (pos + data.len <= cap) {
        @memcpy(buf[pos .. pos + data.len], data);
    }
    return pos + data.len;
}

fn putByte(buf: [*]u8, cap: usize, pos: usize, b: u8) usize {
    if (pos < cap) buf[pos] = b;
    return pos + 1;
}

fn changed6(rgb: [*]const u8, pv: [*]const u8, i: usize) bool {
    return rgb[i] != pv[i] or rgb[i + 1] != pv[i + 1] or rgb[i + 2] != pv[i + 2] or
        rgb[i + 3] != pv[i + 3] or rgb[i + 4] != pv[i + 4] or rgb[i + 5] != pv[i + 5];
}

export fn afw_render_frame(
    rgb: [*]const u8,
    prev: ?[*]const u8,
    cols: c_int,
    rows: c_int,
    out: [*]u8,
    out_cap: usize,
) usize {
    if (cols <= 0 or rows <= 0) return 0;
    const c: usize = @intCast(cols);
    const r: usize = @intCast(rows);
    const has_prev = prev != null;
    const pv: [*]const u8 = prev orelse rgb;

    var pos: usize = 0;
    var cur_x: c_int = -1;
    var cur_y: c_int = -1;
    var last_full: bool = false;
    var last_fg: [3]u8 = .{ 0, 0, 0 };
    var last_bg: [3]u8 = .{ 0, 0, 0 };
    var last_has_bg: bool = false;
    var first_style: bool = true;

    var y: usize = 0;
    while (y < r) : (y += 1) {
        var x: usize = 0;
        while (x < c) {
            const i = (y * c + x) * 6;
            if (has_prev and !changed6(rgb, pv, i)) {
                x += 1;
                continue;
            }

            const tr = rgb[i];
            const tg = rgb[i + 1];
            const tb = rgb[i + 2];
            const br = rgb[i + 3];
            const bg = rgb[i + 4];
            const bb = rgb[i + 5];
            const full = (tr == br) and (tg == bg) and (tb == bb);

            var xend: usize = x;
            while (xend < c) {
                const j = (y * c + xend) * 6;
                if (has_prev and !changed6(rgb, pv, j)) break;
                const tr2 = rgb[j];
                const tg2 = rgb[j + 1];
                const tb2 = rgb[j + 2];
                const br2 = rgb[j + 3];
                const bg2 = rgb[j + 4];
                const bb2 = rgb[j + 5];
                const full2 = (tr2 == br2) and (tg2 == bg2) and (tb2 == bb2);
                if (full2 != full) break;
                if (tr2 != tr or tg2 != tg or tb2 != tb) break;
                if (!full and (br2 != br or bg2 != bg or bb2 != bb)) break;
                xend += 1;
            }

            if (cur_y != @as(c_int, @intCast(y)) or cur_x != @as(c_int, @intCast(x))) {
                pos = put(out, out_cap, pos, "\x1b[");
                pos = putDec(out, out_cap, pos, @intCast(y + 1));
                pos = putByte(out, out_cap, pos, ';');
                pos = putDec(out, out_cap, pos, @intCast(x + 1));
                pos = putByte(out, out_cap, pos, 'H');
            }

            const has_bg = !full;
            const style_changed = first_style or full != last_full or
                tr != last_fg[0] or tg != last_fg[1] or tb != last_fg[2] or
                has_bg != last_has_bg or
                (has_bg and (br != last_bg[0] or bg != last_bg[1] or bb != last_bg[2]));

            if (style_changed) {
                pos = put(out, out_cap, pos, "\x1b[0;38;2;");
                pos = putDec(out, out_cap, pos, tr);
                pos = putByte(out, out_cap, pos, ';');
                pos = putDec(out, out_cap, pos, tg);
                pos = putByte(out, out_cap, pos, ';');
                pos = putDec(out, out_cap, pos, tb);
                if (!full) {
                    pos = put(out, out_cap, pos, ";48;2;");
                    pos = putDec(out, out_cap, pos, br);
                    pos = putByte(out, out_cap, pos, ';');
                    pos = putDec(out, out_cap, pos, bg);
                    pos = putByte(out, out_cap, pos, ';');
                    pos = putDec(out, out_cap, pos, bb);
                }
                pos = putByte(out, out_cap, pos, 'm');
                last_full = full;
                last_fg = .{ tr, tg, tb };
                last_bg = .{ br, bg, bb };
                last_has_bg = has_bg;
                first_style = false;
            }

            var gx = x;
            const glyph: []const u8 = if (full) FULL else UPPER;
            while (gx < xend) : (gx += 1) {
                pos = put(out, out_cap, pos, glyph);
            }

            cur_x = @as(c_int, @intCast(xend));
            cur_y = @as(c_int, @intCast(y));
            x = xend;
        }
    }
    return @min(pos, out_cap);
}
