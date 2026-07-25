#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
srgi - headless CLI for Space Rangers HD image formats: *.gi / *.gai / *.hai.

Decodes the game's image containers to PNG (stdlib + zlib only, no Pillow) -
a command-line counterpart to the decode side of ResEditor 1.3.2 (GUI-only).
Format reverse-engineered against the real game files and cross-checked with
the OpenSR reference loaders (Ranger/GILoader.cpp, GAILoader.cpp,
HAILoader.cpp; include/OpenSR/libRangerQt.h).

  GI  (0x00006967 "gi")   single frame, 6 encodings (type 0..5)
  GAI (0x00696167 "gai")  animation: GI keyframes + type-5 delta frames + times
  HAI (0x04210420)        multi-resolution indexed animation (HUD 128/256/384)

Binary layout (little-endian):
  GIFrameHeader (64 B)  u32 signature, version, startX, startY, finishX, finishY,
                        rBitmask, gBitmask, bBitmask, aBitmask, type, layerCount,
                        unknown1..4
  GILayerHeader (32 B)  u32 seek, size, startX, startY, finishX, finishY, unk1, unk2
                        (seek is relative to the frame start; size = layer bytes)
  RLE layer preamble    16 B: i32 rleSize, u32 w, u32 h, then palSize in byte @+12
  GAIHeader (48 B)      u32 signature, version, startX, startY, finishX, finishY,
                        frameCount, haveBackground, waitSeek, waitSize, unk1, unk2
                        then frameCount x (u32 giSeek, u32 giSize), then wait block
  HAIHeader (52 B)      u32 signature, width, height, rowBytes, count, frameSize,
                        unk1..6, palSize; then count x (h*rowBytes indices + palette)

Frame types (GIFrameHeader.type):
  0  one layer, raw 16-bit R5G6B5 or 32-bit ARGB (by masks)
  1  one layer, 16-bit R5G6B5, RLE
  2  three layers: body (R5G6B5 RLE) + outline (R5G6B5 RLE) + alpha (6-bit RLE)
  3  two layers: indexed R5G6B5 (RLE) + indexed alpha/colour (RLE)
  4  two layers: raw 8-bit indices + RGBA palette
  5  delta frame of a GAI animation (additive overlay on the previous frame)

Colour is normalised to straight (non-premultiplied) RGBA8888 for output.

Commands:
  srgi info   <file>            dump header / layer table
  srgi decode <file> <out>      -> <out>.png (gi) or <out>/<stem>_NNN.png (gai/hai)
  srgi verify <file|dir>        self-consistency test (headers + byte consumption)
"""
import argparse
import binascii
import json
import os
import struct
import sys
import zlib

GI_SIG = 0x00006967
GAI_SIG = 0x00696167
HAI_SIG = 0x04210420
ZL01_SIG = 0x31304C5A
ZL02_SIG = 0x32304C5A

FRAME_HDR = 64           # GIFrameHeader, 16 u32
LAYER_HDR = 32           # GILayerHeader, 8 u32
GAI_HDR = 48             # GAIHeader, 12 u32
HAI_HDR = 52             # HAIHeader, 13 u32
HAI_FRAME_TIME = 50      # ms per HAI frame (constant, per OpenSR)

_RGB565_LUT = None       # lazy 65536 -> b"RGBA" lookup


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _lut():
    """RGBA bytes for every 16-bit R5G6B5 value (top-justified, alpha 255)."""
    global _RGB565_LUT
    if _RGB565_LUT is None:
        L = [b""] * 65536
        for c in range(65536):
            L[c] = bytes((((c >> 11) & 0x1F) << 3,
                          ((c >> 5) & 0x3F) << 2,
                          (c & 0x1F) << 3, 255))
        _RGB565_LUT = L
    return _RGB565_LUT


def unpack_zl(blob):
    """Single-shot ZL01/ZL02 blob (as OpenSR unpackZL): sig, u32 rawSize, zlib."""
    sig = _u32(blob, 0)
    if sig not in (ZL01_SIG, ZL02_SIG):
        raise ValueError("not a ZL01/ZL02 blob")
    raw = zlib.decompress(blob[8:])
    want = _u32(blob, 4)
    if len(raw) != want:
        raise ValueError("ZL raw size mismatch: %d != %d" % (len(raw), want))
    return raw


# --------------------------------------------------------------------------- canvas

class Canvas:
    __slots__ = ("w", "h", "buf")

    def __init__(self, w, h):
        self.w = w if w > 0 else 0
        self.h = h if h > 0 else 0
        self.buf = bytearray(self.w * self.h * 4)

    def copy(self):
        c = Canvas.__new__(Canvas)
        c.w, c.h, c.buf = self.w, self.h, bytearray(self.buf)
        return c


# ---------------------------------------------------------------- decode primitives
# Every primitive draws into `cv` starting at pixel (x, y), reading from `buf`
# at absolute offset `pos`, and returns the position just past the bytes it
# consumed - the caller checks that against GILayerHeader.size.

def blit_r5g6b5(cv, x, y, w, h, buf, pos):
    """type 0 (R5G6B5 masks): raw 16-bit scanlines, w*h*2 bytes."""
    L = _lut()
    cw, ch, cb = cv.w, cv.h, cv.buf
    for row in range(h):
        yy = y + row
        if 0 <= yy < ch and 0 <= x and x + w <= cw:
            cols = struct.unpack_from("<%dH" % w, buf, pos)
            o = (yy * cw + x) * 4
            cb[o:o + w * 4] = b"".join([L[c] for c in cols])
        pos += w * 2
    return pos


def blit_argb(cv, x, y, w, h, buf, pos):
    """type 0 (ARGB masks): raw 32-bit BGRA scanlines -> RGBA."""
    cw, ch, cb = cv.w, cv.h, cv.buf
    for row in range(h):
        yy = y + row
        for col in range(w):
            b, g, r, a = buf[pos], buf[pos + 1], buf[pos + 2], buf[pos + 3]
            pos += 4
            xx = x + col
            if 0 <= xx < cw and 0 <= yy < ch:
                o = (yy * cw + xx) * 4
                cb[o] = r; cb[o + 1] = g; cb[o + 2] = b; cb[o + 3] = a
    return pos


def draw_r5g6b5(cv, x, y, buf, pos):
    """types 1/2: RLE 16-bit R5G6B5. Opaque colour (alpha 255)."""
    L = _lut()
    cw, ch, cb = cv.w, cv.h, cv.buf
    size = struct.unpack_from("<i", buf, pos)[0]
    pos += 16
    line, col = y, x
    while size > 0:
        b = buf[pos]; pos += 1; size -= 1
        if b == 0 or b == 0x80:                     # new scanline
            line += 1; col = x
        elif b > 0x80:                              # literal run of cnt pixels
            cnt = b & 0x7F
            size -= cnt * 2
            cols = struct.unpack_from("<%dH" % cnt, buf, pos)
            pos += cnt * 2
            if 0 <= line < ch and 0 <= col and col + cnt <= cw:
                o = (line * cw + col) * 4
                cb[o:o + cnt * 4] = b"".join([L[c] for c in cols])
            col += cnt
        else:                                       # skip b pixels
            col += b
    return pos


def draw_a6(cv, x, y, buf, pos):
    """type 2 layer 2: RLE 6-bit alpha, written to the alpha channel only."""
    cw, ch, cb = cv.w, cv.h, cv.buf
    size = struct.unpack_from("<i", buf, pos)[0]
    pos += 16
    line, col = y, x
    while size > 0:
        b = buf[pos]; pos += 1; size -= 1
        if b == 0 or b == 0x80:
            line += 1; col = x
        elif b > 0x80:
            cnt = b & 0x7F
            size -= cnt
            for _ in range(cnt):
                a = buf[pos]; pos += 1
                if 0 <= line < ch and 0 <= col < cw:
                    cb[(line * cw + col) * 4 + 3] = (4 * (63 - a)) & 0xFF
                col += 1
        else:
            col += b
    return pos


def draw_rgbi(cv, x, y, buf, pos):
    """type 3 layer 0: RLE indices into an R5G6B5 palette. Opaque colour."""
    L = _lut()
    cw, ch, cb = cv.w, cv.h, cv.buf
    size = struct.unpack_from("<i", buf, pos)[0]
    palsize = buf[pos + 12] or 256
    pos += 16
    pal = list(struct.unpack_from("<%dH" % palsize, buf, pos))
    pos += palsize * 2
    line, col = y, x
    while size > 0:
        b = buf[pos]; pos += 1; size -= 1
        if b == 0 or b == 0x80:
            line += 1; col = x
        elif b > 0x80:
            cnt = b & 0x7F
            size -= cnt
            idx = buf[pos:pos + cnt]
            pos += cnt
            if 0 <= line < ch and 0 <= col and col + cnt <= cw:
                o = (line * cw + col) * 4
                cb[o:o + cnt * 4] = b"".join([L[pal[i]] for i in idx])
            col += cnt
        else:
            col += b
    return pos


def draw_ai(cv, x, y, buf, pos):
    """type 3 layer 1: RLE indices into a colour+alpha palette. Full RGBA."""
    cw, ch, cb = cv.w, cv.h, cv.buf
    size = struct.unpack_from("<i", buf, pos)[0]
    palsize = buf[pos + 12] or 256
    pos += 16
    pal = []
    for _ in range(palsize):
        color, araw = struct.unpack_from("<HH", buf, pos)
        pos += 4
        alpha = (4 * (63 - (araw & 0xFF))) & 0xFF
        pal.append(bytes((((color >> 11) & 0x1F) << 3,
                          ((color >> 5) & 0x3F) << 2,
                          (color & 0x1F) << 3, alpha)))
    line, col = y, x
    while size > 0:
        b = buf[pos]; pos += 1; size -= 1
        if b == 0 or b == 0x80:
            line += 1; col = x
        elif b > 0x80:
            cnt = b & 0x7F
            size -= cnt
            idx = buf[pos:pos + cnt]
            pos += cnt
            if 0 <= line < ch and 0 <= col and col + cnt <= cw:
                o = (line * cw + col) * 4
                cb[o:o + cnt * 4] = b"".join([pal[i] for i in idx])
            col += cnt
        else:
            col += b
    return pos


def decode_indexed(cv, x, y, w, h, buf, ipos, ppos, psize):
    """type 4: raw 8-bit indices + RGBA palette (palette stored r,g,b,a)."""
    cw, ch, cb = cv.w, cv.h, cv.buf
    npal = psize // 4
    pal = [buf[ppos + 4 * i:ppos + 4 * i + 4] for i in range(npal)]
    for row in range(h):
        yy = y + row
        idx = buf[ipos:ipos + w]
        ipos += w
        if 0 <= yy < ch and 0 <= x and x + w <= cw:
            o = (yy * cw + x) * 4
            cb[o:o + w * 4] = b"".join([pal[i] if i < npal else b"\0\0\0\0"
                                        for i in idx])
    return ipos


# type 5 delta: OpenSR stores channels in memory order B,G,R,A (0..3); map each
# delta channel onto the matching RGBA byte.
_DELTA_CH = (2, 1, 0, 3)


def decode_delta(cv, x, y, buf, pos):
    """type 5: additive delta frame overlaid on the previous (copied) frame."""
    cw, ch, cb = cv.w, cv.h, cv.buf
    b12, b13 = buf[pos + 12], buf[pos + 13]
    shlca = [8 - (b12 & 15), 8 - (b12 >> 4), 8 - (b13 & 15), 8 - (b13 >> 4)]
    skip = _u32(buf, pos + 8) << 2
    pos += 16 + skip
    line, col, channel = y, x, 0
    shlc = shlca[0]
    while True:
        b = buf[pos]; pos += 1
        if b & 0x80:
            cnt = (b & 0x0F) + 1
            op = (b >> 4) & 7
            sign = -1 if op >= 4 else 1
            sub = op & 3
            if sub == 0:
                per, mask, sh = 8, 1, 1
            elif sub == 1:
                per, mask, sh = 4, 3, 2
            elif sub == 2:
                per, mask, sh = 2, 15, 4
            else:
                per, mask, sh = 1, 0, 0          # whole-byte delta
            while cnt > 0:
                take = cnt if cnt < per else per
                bb = buf[pos]; pos += 1
                for _ in range(take):
                    if sub == 3:
                        d = bb + 1
                    else:
                        d = (bb & mask) + 1
                        bb >>= sh
                    if 0 <= col < cw and 0 <= line < ch:
                        o = (line * cw + col) * 4 + _DELTA_CH[channel]
                        cb[o] = (cb[o] + sign * (d << shlc)) & 0xFF
                    col += 1
                cnt -= take
        elif b == 0:                             # next channel / next scanline
            channel += 1
            if channel >= 4:
                channel = 0
                line += 1
            col = x
            shlc = shlca[channel]
        elif b == 0x3F:                          # long skip
            col += struct.unpack_from("<H", buf, pos)[0]
            pos += 2
        elif (b & 0xC0) == 0:                    # short skip (1..0x3e)
            col += b
        elif b == 0x40:                          # terminator
            break
    return pos


# --------------------------------------------------------------------------- headers

def read_gi_header(buf, off):
    (sig, ver, sx, sy, fx, fy, rM, gM, bM, aM, typ, lc,
     u1, u2, u3, u4) = struct.unpack_from("<16I", buf, off)
    return dict(sig=sig, ver=ver, sx=sx, sy=sy, fx=fx, fy=fy, rM=rM, gM=gM,
                bM=bM, aM=aM, type=typ, layerCount=lc, u=(u1, u2, u3, u4))


def read_layers(buf, off, count):
    return [list(struct.unpack_from("<8I", buf, off + FRAME_HDR + LAYER_HDR * i))
            for i in range(count)]


# ------------------------------------------------------------------------- gi frame

def decode_gi_frame(buf, off, animation=False, background=None, anim_box=None):
    """Decode one GIFrameHeader at `off` -> Canvas (RGBA)."""
    h = read_gi_header(buf, off)
    layers = read_layers(buf, off, h["layerCount"])
    if animation:
        ax0, ay0, ax1, ay1 = anim_box
        W, H, bx, by = ax1 - ax0, ay1 - ay0, ax0, ay0
    else:
        W, H, bx, by = h["fx"] - h["sx"], h["fy"] - h["sy"], h["sx"], h["sy"]
    for L in layers:                                    # normalise layer coords
        L[2] -= bx; L[3] -= by; L[4] -= bx; L[5] -= by

    typ = h["type"]
    if typ == 5 and background is not None:
        cv = background.copy()
    else:
        cv = Canvas(W, H)

    def seek(L):
        return off + L[0]

    if typ == 0:
        L = layers[0]
        if L[1]:
            w, ht = L[4] - L[2], L[5] - L[3]
            if (h["aM"], h["rM"], h["gM"], h["bM"]) == (0xFF000000, 0xFF0000, 0xFF00, 0xFF):
                blit_argb(cv, L[2], L[3], w, ht, buf, seek(L))
            else:
                blit_r5g6b5(cv, L[2], L[3], w, ht, buf, seek(L))
    elif typ == 1:
        L = layers[0]
        if L[1]:
            draw_r5g6b5(cv, L[2], L[3], buf, seek(L))
    elif typ == 2:
        for li, fn in ((0, draw_r5g6b5), (1, draw_r5g6b5), (2, draw_a6)):
            L = layers[li]
            if L[1]:
                fn(cv, L[2], L[3], buf, seek(L))
    elif typ == 3:
        if layers[0][1]:
            draw_rgbi(cv, layers[0][2], layers[0][3], buf, seek(layers[0]))
        if layers[1][1]:
            draw_ai(cv, layers[1][2], layers[1][3], buf, seek(layers[1]))
    elif typ == 4:
        L0, L1 = layers[0], layers[1]
        if L0[1] and L1[1]:
            decode_indexed(cv, L0[2], L0[3], L0[4] - L0[2], L0[5] - L0[3],
                           buf, seek(L0), seek(L1), L1[1])
    elif typ == 5:
        L = layers[0]
        if L[1]:
            decode_delta(cv, L[2], L[3], buf, seek(L))
    else:
        raise ValueError("unknown GI frame type %d" % typ)
    return cv


def decode_gi(buf):
    if _u32(buf, 0) != GI_SIG:
        raise ValueError("not a GI frame")
    return decode_gi_frame(buf, 0)


# ------------------------------------------------------------------------------ gai

def load_gai_times(buf, frame_count, wait_seek, wait_size):
    times = [0] * frame_count
    if not wait_size:
        return times
    block_count = _u32(buf, wait_seek)
    p = wait_seek + 4 + block_count * 8 + 2
    for _ in range(block_count):
        bfc = _u32(buf, p)
        for j in range(bfc):
            frame = _u32(buf, p + 4 + j * 8)
            time = _u32(buf, p + 4 + j * 8 + 4)
            if frame < frame_count:
                times[frame] = time
        p += bfc * 8 + 4 + 2
    return times


def decode_gai(buf):
    (sig, ver, sx, sy, fx, fy, frame_count, have_bg,
     wait_seek, wait_size, u1, u2) = struct.unpack_from("<12I", buf, 0)
    if sig != GAI_SIG:
        raise ValueError("not a GAI file")
    anim_box = (sx, sy, fx, fy)
    times = load_gai_times(buf, frame_count, wait_seek, wait_size)
    frames = []
    bg = None
    for i in range(frame_count):
        gi_seek, gi_size = struct.unpack_from("<2I", buf, GAI_HDR + i * 8)
        if gi_seek and gi_size:
            fsig = _u32(buf, gi_seek)
            if fsig in (ZL01_SIG, ZL02_SIG):
                fbuf = unpack_zl(buf[gi_seek:gi_seek + gi_size])
                cv = decode_gi_frame(fbuf, 0, True, bg, anim_box)
            else:
                cv = decode_gi_frame(buf, gi_seek, True, bg, anim_box)
        else:
            cv = bg.copy() if bg is not None else Canvas(fx - sx, fy - sy)
        frames.append(cv)
        bg = cv
    return frames, times, bool(have_bg)


# ------------------------------------------------------------------------------ hai

def decode_hai(buf):
    (sig, w, h, row_bytes, count, frame_size,
     u1, u2, u3, u4, u5, u6, pal_size) = struct.unpack_from("<13I", buf, 0)
    if sig != HAI_SIG:
        raise ValueError("not a HAI file")
    frames = []
    npal = pal_size // 4
    for i in range(count):
        base = HAI_HDR + i * (h * row_bytes + pal_size)
        ppos = base + h * row_bytes
        pal = [buf[ppos + 4 * k:ppos + 4 * k + 4] for k in range(npal)]
        cv = Canvas(w, h)
        cb = cv.buf
        for row in range(h):
            idx = buf[base + row * row_bytes:base + row * row_bytes + w]
            o = (row * w) * 4
            cb[o:o + w * 4] = b"".join([pal[k] if k < npal else b"\0\0\0\0"
                                        for k in idx])
        frames.append(cv)
    times = [HAI_FRAME_TIME] * count
    return frames, times


# --------------------------------------------------------------------------- output

def write_png(path, w, h, rgba):
    if w <= 0 or h <= 0:
        w, h, rgba = max(w, 1), max(h, 1), bytes(max(w, 1) * max(h, 1) * 4)

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data +
                struct.pack(">I", binascii.crc32(typ + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                              # filter: none
        raw += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8-bit RGBA
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def load_any(path):
    """Read a file, transparently unwrapping a whole-file ZL01/ZL02 wrapper."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 4 and _u32(data, 0) in (ZL01_SIG, ZL02_SIG):
        data = unpack_zl(data)
    return data


# ------------------------------------------------------------------------------ cli

def cmd_info(args):
    if os.path.isdir(args.file):
        files = sorted(_iter_images(args.file))
        if not files:
            print("no .gi/.gai/.hai files under %s" % args.file, file=sys.stderr)
            return 1
        rc = 0
        for path in files:
            print("== %s ==" % os.path.relpath(path, args.file))
            try:
                rc |= _info_one(path)
            except Exception as e:                   # noqa: BLE001 - report, don't crash
                rc = 1
                print("    error: %s" % e, file=sys.stderr)
        return rc
    return _info_one(args.file)


def _info_one(path):
    data = load_any(path)
    sig = _u32(data, 0)
    if sig == GI_SIG:
        h = read_gi_header(data, 0)
        print("GI  version=%d box=(%d,%d)-(%d,%d) type=%d layers=%d"
              % (h["ver"], h["sx"], h["sy"], h["fx"], h["fy"], h["type"], h["layerCount"]))
        print("    masks R=%08x G=%08x B=%08x A=%08x" % (h["rM"], h["gM"], h["bM"], h["aM"]))
        for i, L in enumerate(read_layers(data, 0, h["layerCount"])):
            print("    layer[%d] seek=%d size=%d box=(%d,%d)-(%d,%d)"
                  % (i, L[0], L[1], L[2], L[3], L[4], L[5]))
    elif sig == GAI_SIG:
        (s, ver, sx, sy, fx, fy, fc, hbg, ws, wsz, u1, u2) = struct.unpack_from("<12I", data, 0)
        print("GAI version=%d box=(%d,%d)-(%d,%d) frames=%d haveBackground=%d"
              % (ver, sx, sy, fx, fy, fc, hbg))
        _, times, _ = decode_gai(data)
        print("    frame times (ms): %s%s" % (times[:12], " ..." if len(times) > 12 else ""))
    elif sig == HAI_SIG:
        (s, w, h, rb, cnt, fsz, u1, u2, u3, u4, u5, u6, psz) = struct.unpack_from("<13I", data, 0)
        print("HAI %dx%d rowBytes=%d frames=%d frameSize=%d palSize=%d"
              % (w, h, rb, cnt, fsz, psz))
    else:
        print("unknown signature %08x" % sig, file=sys.stderr)
        return 1
    return 0


def _decode_frames(data):
    """Return (frames, times|None). Single GI -> one frame, no times."""
    sig = _u32(data, 0)
    if sig == GI_SIG:
        return [decode_gi(data)], None
    if sig == GAI_SIG:
        frames, times, _ = decode_gai(data)
        return frames, times
    if sig == HAI_SIG:
        return decode_hai(data)
    raise ValueError("unknown signature %08x" % sig)


def _write_frames(out_dir, stem, frames, times):
    """Write decoded frames into out_dir (always treated as a directory).

    Single image -> out_dir/stem.png; animation -> out_dir/stem_NNN.png plus a
    stem.times.json sidecar. Returns (w, h, nframes).
    """
    os.makedirs(out_dir, exist_ok=True)
    if times is None:                              # single image
        cv = frames[0]
        write_png(os.path.join(out_dir, stem + ".png"), cv.w, cv.h, cv.buf)
        return cv.w, cv.h, 1
    width = height = 0
    for i, cv in enumerate(frames):
        write_png(os.path.join(out_dir, "%s_%03d.png" % (stem, i)), cv.w, cv.h, cv.buf)
        width, height = cv.w, cv.h
    with open(os.path.join(out_dir, stem + ".times.json"), "w") as f:
        json.dump({"frames": len(frames), "size": [width, height], "times_ms": times},
                  f, indent=2)
    return width, height, len(frames)


def _decode_dir(args):
    """Batch-decode every gi/gai/hai under args.file, mirroring the tree into args.out."""
    root = args.file
    files = sorted(_iter_images(root))
    if not files:
        print("no .gi/.gai/.hai files under %s" % root, file=sys.stderr)
        return 1
    ok = fail = 0
    for path in files:
        rel = os.path.relpath(path, root)
        stem = os.path.splitext(os.path.basename(path))[0]
        reldir = os.path.dirname(rel)
        out_dir = os.path.join(args.out, reldir) if reldir else args.out
        try:
            frames, times = _decode_frames(load_any(path))
            w, h, n = _write_frames(out_dir, stem, frames, times)
            ok += 1
            what = ("%d frames %dx%d" % (n, w, h)) if times is not None else ("%dx%d" % (w, h))
            print("  OK    %s (%s)" % (rel, what))
        except Exception as e:                       # noqa: BLE001 - report, don't crash
            fail += 1
            print("  FAIL  %s: %s" % (rel, e))
    print("--- %d files: %d OK, %d FAIL -> %s" % (len(files), ok, fail, args.out),
          file=sys.stderr)
    return 1 if fail else 0


def cmd_decode(args):
    if os.path.isdir(args.file):
        return _decode_dir(args)
    data = load_any(args.file)
    frames, times = _decode_frames(data)
    stem = os.path.splitext(os.path.basename(args.file))[0]
    if times is None:                              # single image
        out = args.out
        if os.path.isdir(out) or out.endswith(("/", "\\")):
            os.makedirs(out, exist_ok=True)
            out = os.path.join(out, stem + ".png")
        elif not out.lower().endswith(".png"):
            out += ".png"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        cv = frames[0]
        write_png(out, cv.w, cv.h, cv.buf)
        print("decoded -> %s (%dx%d)" % (out, cv.w, cv.h), file=sys.stderr)
    else:                                          # animation -> directory
        width, height, n = _write_frames(args.out, stem, frames, times)
        print("decoded %d frames -> %s/%s_NNN.png (%dx%d)"
              % (n, args.out, stem, width, height), file=sys.stderr)
    return 0


def _verify_gi(data):
    """Return list of problems ([] == ok) for a single GI frame."""
    problems = []
    h = read_gi_header(data, 0)
    lc = h["layerCount"]
    layers = read_layers(data, 0, lc)
    # 1) header re-serialises byte-exact (lossless structural parse)
    hdr_len = FRAME_HDR + LAYER_HDR * lc
    repacked = struct.pack("<16I", h["sig"], h["ver"], h["sx"], h["sy"], h["fx"],
                           h["fy"], h["rM"], h["gM"], h["bM"], h["aM"], h["type"],
                           lc, *h["u"])
    for L in layers:
        repacked += struct.pack("<8I", *L)
    if repacked != data[0:hdr_len]:
        problems.append("header re-serialisation differs")
    # 2) each layer's decoder consumes exactly GILayerHeader.size bytes
    typ = h["type"]
    prim = {0: None, 1: [draw_r5g6b5], 2: [draw_r5g6b5, draw_r5g6b5, draw_a6],
            3: [draw_rgbi, draw_ai]}.get(typ)
    cv = decode_gi_frame(data, 0)                  # raises on structural error
    norm = read_layers(data, 0, lc)
    bx, by = h["sx"], h["sy"]
    for L in norm:
        L[2] -= bx; L[3] -= by; L[4] -= bx; L[5] -= by
    if typ in (1, 2, 3):
        for li, fn in enumerate(prim):
            L = layers[li]
            if not L[1]:
                continue
            end = fn(Canvas(cv.w, cv.h), norm[li][2], norm[li][3], data, L[0])
            got = end - L[0]
            if got != L[1]:
                problems.append("layer %d consumed %d != size %d" % (li, got, L[1]))
    elif typ == 0:
        L = layers[0]
        if L[1]:
            w, ht = norm[0][4] - norm[0][2], norm[0][5] - norm[0][3]
            bpp = 4 if h["aM"] == 0xFF000000 else 2
            if w * ht * bpp != L[1]:
                problems.append("type0 raw %d != size %d" % (w * ht * bpp, L[1]))
    elif typ == 4:
        L0, L1 = layers[0], layers[1]
        w, ht = norm[0][4] - norm[0][2], norm[0][5] - norm[0][3]
        if L0[1] and w * ht != L0[1]:
            problems.append("type4 indices %d != size %d" % (w * ht, L0[1]))
    elif typ == 5:
        L = layers[0]
        if L[1]:
            end = decode_delta(cv.copy(), norm[0][2], norm[0][3], data, L[0])
            if not (L[0] <= end <= L[0] + L[1]):
                problems.append("type5 delta overran layer size")
    if cv.w != h["fx"] - h["sx"] or cv.h != h["fy"] - h["sy"]:
        problems.append("canvas size mismatch")
    return problems


def _verify_file(path):
    data = load_any(path)
    sig = _u32(data, 0)
    if sig == GI_SIG:
        return _verify_gi(data)
    if sig == GAI_SIG:
        problems = []
        (s, ver, sx, sy, fx, fy, fc, hbg, ws, wsz, u1, u2) = struct.unpack_from("<12I", data, 0)
        frames, times, _ = decode_gai(data)         # raises on structural error
        if len(frames) != fc or len(times) != fc:
            problems.append("frame/time count mismatch")
        return problems
    if sig == HAI_SIG:
        problems = []
        (s, w, h, rb, cnt, fsz, u1, u2, u3, u4, u5, u6, psz) = struct.unpack_from("<13I", data, 0)
        if fsz != h * rb + psz:
            problems.append("frameSize %d != h*rowBytes+palSize %d" % (fsz, h * rb + psz))
        need = HAI_HDR + cnt * fsz
        if need > len(data):
            problems.append("declared frames overrun file (need %d > %d)" % (need, len(data)))
        # need < len(data) is NOT an error: some ship HAI append higher-resolution
        # copies of the animation after the primary block; decode_hai reads only the
        # `count` primary frames the header describes, so the tail is simply ignored.
        frames, _ = decode_hai(data)
        if len(frames) != cnt:
            problems.append("decoded %d != count %d" % (len(frames), cnt))
        return problems
    return ["unknown signature %08x" % sig]


def _iter_images(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.lower().endswith((".gi", ".gai", ".hai")):
                yield os.path.join(dirpath, n)


def cmd_verify(args):
    files = list(_iter_images(args.path))
    if args.limit:
        files = files[:args.limit]
    ok = fail = 0
    by_kind = {}
    for path in files:
        try:
            problems = _verify_file(path)
        except Exception as e:                       # noqa: BLE001 - report, don't crash
            problems = ["exception: %s" % e]
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        rec = by_kind.setdefault(ext, [0, 0])
        if problems:
            fail += 1; rec[1] += 1
            print("FAIL %s" % path)
            for p in problems[:4]:
                print("       %s" % p)
        else:
            ok += 1; rec[0] += 1
            if args.verbose:
                print("ok   %s" % path)
    summary = "  ".join("%s ok=%d fail=%d" % (k, v[0], v[1]) for k, v in sorted(by_kind.items()))
    print("--- %d files: %d OK, %d FAIL   [%s]" % (len(files), ok, fail, summary),
          file=sys.stderr)
    return 1 if fail else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="srgi",
                                 description="Space Rangers HD gi/gai/hai -> PNG decoder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="dump header / layer table")
    p.add_argument("file", help="gi/gai/hai file, or a directory to scan recursively")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("decode", help="decode to PNG (gi) or a directory of PNGs (gai/hai)")
    p.add_argument("file", help="gi/gai/hai file, or a directory to batch-decode recursively")
    p.add_argument("out", help="output PNG (single gi) or output directory (dir/animation)")
    p.set_defaults(func=cmd_decode)

    p = sub.add_parser("verify", help="self-consistency test on a file or directory")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=0, help="cap number of files scanned")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
