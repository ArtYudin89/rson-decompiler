# -*- coding: utf-8 -*-
"""Тесты srgi.py — gi/gai/hai ↔ PNG.

Энкодер и декодер проверяются друг против друга: PNG → gi → PNG обязан вернуть
те же пиксели, а заголовок — те же поля. Плюс инварианты формата, которые
дешевле закрепить тестом, чем ловить прогоном по корпусу: ARGB — lossless,
RGB565 — ровно 5/6/5 бит, GAI помнит тайминги покадрово.

Байт-в-байт против настоящего .gi проверяется, только если под reference/
что-нибудь лежит (в git его нет — копирайтный контент).
"""
import json
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import srgi  # noqa: E402


def _reference_images(limit=8):
    ref = os.path.join(ROOT, "reference")
    if not os.path.isdir(ref):
        return []
    found = []
    for dirpath, _dirnames, filenames in os.walk(ref):
        for name in filenames:
            if name.lower().endswith(".gi"):
                found.append(os.path.join(dirpath, name))
                if len(found) >= limit:
                    return found
    return found


class PngTests(unittest.TestCase):
    def test_png_round_trip_without_pillow(self):
        rgba = bytes(range(64))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "a.png")
            srgi.write_png(path, 4, 4, rgba)
            self.assertEqual(srgi.read_png(path), (4, 4, rgba))

    def test_reads_back_a_large_image(self):
        """Многострочный IDAT со своим фильтром на каждой строке."""
        w, h = 37, 19
        rgba = bytes((x * 7 + y * 3) % 256 for y in range(h) for x in range(w * 4))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "b.png")
            srgi.write_png(path, w, h, rgba)
            self.assertEqual(srgi.read_png(path), (w, h, rgba))


class EncodeDecodeTests(unittest.TestCase):
    RGBA = bytes([
        255, 0, 0, 255,   0, 255, 0, 255,   0, 0, 255, 255,
        255, 255, 255, 255, 0, 0, 0, 255,  20, 40, 60, 128,
    ])

    def test_argb_is_lossless(self):
        blob = srgi.encode_gi(3, 2, self.RGBA, fmt="argb")
        self.assertEqual(blob[:4], b"gi\0\0")
        cv = srgi.decode_gi(blob)
        self.assertEqual((cv.w, cv.h), (3, 2))
        self.assertEqual(bytes(cv.buf), self.RGBA)

    def test_rgb565_quantises_to_five_six_five(self):
        cv = srgi.decode_gi(srgi.encode_gi(3, 1, self.RGBA[:12], fmt="rgb565"))
        self.assertEqual(bytes(cv.buf),
                         b"\xf8\x00\x00\xff\x00\xfc\x00\xff\x00\x00\xf8\xff")

    def test_indexed_keeps_a_small_palette_exactly(self):
        """≤256 цветов — indexed обязан быть lossless, иначе он бессмыслен."""
        rgba = (b"\xff\x00\x00\xff" + b"\x00\xff\x00\xff") * 8
        cv = srgi.decode_gi(srgi.encode_gi(4, 4, rgba, fmt="indexed"))
        self.assertEqual(bytes(cv.buf), rgba)

    def test_header_survives_a_round_trip(self):
        blob = srgi.encode_gi(5, 3, bytes(5 * 3 * 4), fmt="argb")
        header = srgi.read_gi_header(blob, 0)
        self.assertEqual((header["fx"], header["fy"]), (5, 3))
        self.assertEqual(header["layerCount"], 1)

    def test_verify_accepts_what_encode_produced(self):
        """verify = ре-сериализация заголовка байт-в-байт + расход layer.size."""
        blob = srgi.encode_gi(6, 4, bytes(6 * 4 * 4), fmt="argb")
        self.assertEqual(srgi._verify_gi(blob), [])


class AnimationTests(unittest.TestCase):
    def _frames(self, count=3, w=4, h=4):
        return [(w, h, bytes([(i * 40 + j) % 256 for j in range(w * h * 4)]))
                for i in range(count)]

    def test_gai_keeps_frames_and_their_timings(self):
        frames = self._frames()
        times = [100, 250, 40]
        blob = srgi.encode_gai(frames, times)
        decoded, decoded_times = srgi.decode_gai(blob)[:2]
        self.assertEqual(len(decoded), len(frames))
        self.assertEqual(decoded_times, times)
        for i, cv in enumerate(decoded):
            with self.subTest(frame=i):
                self.assertEqual(bytes(cv.buf), frames[i][2])

    def test_hai_round_trip(self):
        frames = self._frames(count=2)
        decoded = srgi.decode_hai(srgi.encode_hai(frames))[0]
        self.assertEqual(len(decoded), 2)
        self.assertEqual(bytes(decoded[0].buf), frames[0][2])


def _sprite(w=11, h=9):
    """Спрайт как в игре: непрозрачное ядро, полупрозрачная кайма, пустые поля."""
    px = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * 4
            if 2 <= x < w - 2 and 2 <= y < h - 2:          # ядро
                px[o:o + 4] = bytes(((x * 23) % 256, (y * 37) % 256, 90, 255))
            elif 1 <= x < w - 1 and 1 <= y < h - 1:        # кайма
                px[o:o + 4] = bytes((200, 100, (x * 11) % 256, 40 + 8 * x))
    return w, h, bytes(px)


class RleEncoderTests(unittest.TestCase):
    """type-1/2/3 — lossy по построению: цвет уходит в R5G6B5, альфа в 6 бит.

    Поэтому гейт другой, чем у argb: не `decode(encode(x)) == x`, а
    `decode(encode(x)) == quantise_gi(x)` — энкодер обязан только квантовать и
    ничего больше не терять. Плюс инвариант расхода слоя из `gi verify`.
    """
    RLE_FORMATS = ("rle565", "rle-alpha", "rle-indexed")

    def test_round_trip_lands_on_the_quantised_image(self):
        w, h, rgba = _sprite()
        for fmt in self.RLE_FORMATS:
            with self.subTest(fmt=fmt):
                blob = srgi.encode_gi(w, h, rgba, fmt=fmt)
                cv = srgi.decode_gi(blob)
                self.assertEqual((cv.w, cv.h), (w, h))
                self.assertEqual(bytes(cv.buf), srgi.quantise_gi(w, h, rgba, fmt))

    def test_layer_budget_is_consumed_exactly(self):
        w, h, rgba = _sprite()
        for fmt in self.RLE_FORMATS:
            with self.subTest(fmt=fmt):
                self.assertEqual(srgi._verify_gi(srgi.encode_gi(w, h, rgba, fmt=fmt)), [])

    def test_frames_look_like_the_games_own(self):
        """Тип кадра, число слоёв и маски — ровно те, что во всём стоке."""
        w, h, rgba = _sprite()
        for fmt, typ, layers in (("rle565", 1, 1), ("rle-alpha", 2, 3),
                                 ("rle-indexed", 3, 2)):
            with self.subTest(fmt=fmt):
                header = srgi.read_gi_header(srgi.encode_gi(w, h, rgba, fmt=fmt), 0)
                self.assertEqual(header["type"], typ)
                self.assertEqual(header["layerCount"], layers)
                self.assertEqual((header["rM"], header["gM"], header["bM"], header["aM"]),
                                 (0xF800, 0x7E0, 0x1F, 0x0))

    def test_opaque_pixels_keep_all_255(self):
        """6-битная альфа упирается в 252 — непрозрачное держит цветной слой."""
        rgba = b"\x40\x80\xc0\xff" * 4
        for fmt in ("rle-alpha", "rle-indexed"):
            with self.subTest(fmt=fmt):
                cv = srgi.decode_gi(srgi.encode_gi(2, 2, rgba, fmt=fmt))
                self.assertEqual(bytes(cv.buf)[3::4], b"\xff" * 4)

    def test_partial_alpha_snaps_to_a_multiple_of_four(self):
        rgba = b"\x00\x00\x00\xc9" + b"\x00\x00\x00\xc8" + b"\x00\x00\x00\x03"
        cv = srgi.decode_gi(srgi.encode_gi(3, 1, rgba, fmt="rle-alpha"))
        self.assertEqual(bytes(cv.buf)[3::4], bytes((200, 200, 0)))

    def test_layer_box_is_cropped_to_the_drawn_pixels(self):
        """Пустые поля не должны стоить ни опкодов, ни строк."""
        w, h = 40, 30
        px = bytearray(w * h * 4)
        for y in range(12, 15):
            for x in range(20, 26):
                px[(y * w + x) * 4:(y * w + x) * 4 + 4] = b"\x10\x20\x30\xff"
        blob = srgi.encode_gi(w, h, bytes(px), fmt="rle-alpha")
        box = srgi.read_layers(blob, 0, 3)[0][2:6]
        self.assertEqual(tuple(box), (20, 12, 26, 15))

    def test_a_blank_image_produces_empty_layers(self):
        blob = srgi.encode_gi(4, 3, bytes(4 * 3 * 4), fmt="rle-alpha")
        self.assertEqual([L[1] for L in srgi.read_layers(blob, 0, 3)], [0, 0, 0])
        self.assertEqual(bytes(srgi.decode_gi(blob).buf), bytes(4 * 3 * 4))

    def test_long_runs_split_across_opcodes(self):
        """Пробег литералов и пропусков >0x7F не влезает в один опкод."""
        w = 400
        px = bytearray(w * 4)
        for x in range(300, 400):
            px[x * 4:x * 4 + 4] = b"\xff\xff\xff\xff"
        blob = srgi.encode_gi(w, 1, bytes(px), fmt="rle565")
        self.assertEqual(srgi._verify_gi(blob), [])
        self.assertEqual(bytes(srgi.decode_gi(blob).buf),
                         srgi.quantise_gi(w, 1, bytes(px), "rle565"))

    def test_rle_indexed_refuses_an_oversized_palette(self):
        """Цвета обязаны остаться различимыми и ПОСЛЕ квантования в 5/6/5."""
        n = 300
        rgba = b"".join([bytes(((i % 32) * 8, (i // 32) * 4, 0, 255))
                         for i in range(n)])
        with self.assertRaises(ValueError):
            srgi.encode_gi(n, 1, rgba, fmt="rle-indexed")

    def test_delta_is_not_a_standalone_gi(self):
        with self.assertRaises(ValueError):
            srgi.encode_gi(2, 2, bytes(16), fmt="delta")


class DeltaFrameTests(unittest.TestCase):
    """type-5: поканальные аддитивные разницы с предыдущим кадром.

    Сдвиги shlc пишем нулевыми (0x88/0x88 — так же, как в большинстве стоковых
    кадров), поэтому дельта восстанавливает все 8 бит канала и вся анимация
    остаётся lossless — здесь гейт снова строгий, байт-в-байт.
    """
    def _frames(self, n=4, w=12, h=10):
        base = bytearray(w * h * 4)
        for i in range(len(base)):
            base[i] = (i * 13) % 256
        out = [bytes(base)]
        for k in range(1, n):
            nxt = bytearray(out[-1])
            for y in range(3, 7):                          # шевелим только кусок
                for x in range(4, 9):
                    o = (y * w + x) * 4
                    nxt[o] = (nxt[o] + 7 * k) % 256
                    nxt[o + 3] = (nxt[o + 3] + 250) % 256   # заворот через 0
            out.append(bytes(nxt))
        return [(w, h, f) for f in out]

    def test_delta_animation_is_lossless(self):
        frames = self._frames()
        times = [40] * len(frames)
        decoded = srgi.decode_gai(srgi.encode_gai(frames, times, "delta"))[0]
        for i, cv in enumerate(decoded):
            with self.subTest(frame=i):
                self.assertEqual(bytes(cv.buf), frames[i][2])

    def test_delta_beats_keyframes_on_size(self):
        frames = self._frames(n=8)
        times = [40] * len(frames)
        delta = len(srgi.encode_gai(frames, times, "delta"))
        keys = len(srgi.encode_gai(frames, times, "argb"))
        self.assertLess(delta, keys)

    def test_frames_after_the_first_are_type_five(self):
        frames = self._frames()
        blob = srgi.encode_gai(frames, [40] * len(frames), "delta")
        types = []
        for i in range(len(frames)):
            seek, size = struct.unpack_from("<2I", blob, srgi.GAI_HDR + i * 8)
            types.append(srgi.read_gi_header(blob, seek)["type"])
        self.assertEqual(types, [0] + [5] * (len(frames) - 1))

    def test_delta_layer_carries_a_row_seek_table(self):
        """Преамбула: размер потока, w, h слоя, shlc-нибблы, затем u32 на строку."""
        frames = self._frames()
        blob = srgi.encode_gai(frames, [40] * len(frames), "delta")
        seek, _size = struct.unpack_from("<2I", blob, srgi.GAI_HDR + 8)
        layer = srgi.read_layers(blob, seek, 1)[0]
        base = seek + layer[0]
        stream, lw, lh = struct.unpack_from("<3I", blob, base)
        self.assertEqual((lw, lh), (layer[4] - layer[2], layer[5] - layer[3]))
        self.assertEqual(blob[base + 12:base + 14], b"\x88\x88")
        table = struct.unpack_from("<%dI" % lh, blob, base + 16)
        self.assertEqual(table[0], 16 + 4 * lh)
        self.assertEqual(list(table), sorted(table))
        self.assertEqual(layer[1], 16 + 4 * lh + stream)

    def test_only_the_changed_box_is_stored(self):
        w, h, first = _sprite(20, 16)
        second = bytearray(first)
        for x in range(6, 9):
            o = (5 * w + x) * 4
            second[o] = (second[o] + 30) % 256
        blob = srgi.encode_gai([(w, h, first), (w, h, bytes(second))], [40, 40], "delta")
        seek, _size = struct.unpack_from("<2I", blob, srgi.GAI_HDR + 8)
        box = srgi.read_layers(blob, seek, 1)[0][2:6]
        self.assertEqual(tuple(box), (6, 5, 9, 6))

    def test_an_unchanged_frame_costs_an_empty_layer(self):
        w, h, px = _sprite()
        blob = srgi.encode_gai([(w, h, px), (w, h, px)], [40, 40], "delta")
        seek, _size = struct.unpack_from("<2I", blob, srgi.GAI_HDR + 8)
        self.assertEqual(srgi.read_layers(blob, seek, 1)[0][1], 0)
        self.assertEqual(bytes(srgi.decode_gai(blob)[0][1].buf), px)

    def test_every_delta_magnitude_round_trips(self):
        """Все четыре ширины опкода (1/2/4/8 бит) и оба знака сразу."""
        n = 256
        first = bytes(bytearray(n * 4))
        second = bytes(bytearray([i % 256 for i in range(n * 4)]))
        third = bytes(bytearray([(255 - i) % 256 for i in range(n * 4)]))
        frames = [(n, 1, first), (n, 1, second), (n, 1, third)]
        decoded = srgi.decode_gai(srgi.encode_gai(frames, [40] * 3, "delta"))[0]
        for i, cv in enumerate(decoded):
            with self.subTest(frame=i):
                self.assertEqual(bytes(cv.buf), frames[i][2])

    def test_delta_frames_consume_their_layer_exactly(self):
        frames = self._frames()
        blob = srgi.encode_gai(frames, [40] * len(frames), "delta")
        for i in range(len(frames)):
            seek, size = struct.unpack_from("<2I", blob, srgi.GAI_HDR + i * 8)
            with self.subTest(frame=i):
                self.assertEqual(srgi._verify_gi(blob[seek:seek + size]), [])


class EncodeCliTests(unittest.TestCase):
    """CLI: --format доводит новые типы до дерева, self-check ловит расхождение."""

    def test_tree_rebuild_in_a_native_format(self):
        w, h, rgba = _sprite()
        with tempfile.TemporaryDirectory() as directory:
            src, out = os.path.join(directory, "src"), os.path.join(directory, "out")
            os.makedirs(src)
            srgi.write_png(os.path.join(src, "pic.png"), w, h, rgba)
            self.assertEqual(srgi.main(["encode", src, out, "--format", "rle-alpha"]), 0)
            blob = srgi.load_any(os.path.join(out, "pic.gi"))
            self.assertEqual(srgi.read_gi_header(blob, 0)["type"], 2)

    def test_delta_tree_keeps_single_images_lossless(self):
        """Одиночный PNG дельтой быть не может — он падает обратно в argb."""
        w, h, rgba = _sprite()
        with tempfile.TemporaryDirectory() as directory:
            src, out = os.path.join(directory, "src"), os.path.join(directory, "out")
            os.makedirs(src)
            srgi.write_png(os.path.join(src, "pic.png"), w, h, rgba)
            self.assertEqual(srgi.main(["encode", src, out, "--format", "delta"]), 0)
            cv = srgi.decode_gi(srgi.load_any(os.path.join(out, "pic.gi")))
            self.assertEqual(bytes(cv.buf), rgba)


class BackgroundTests(unittest.TestCase):
    """haveBackground=1: кадры-дельты строятся на ключевом кадре из соседнего .gi.

    Пустые кадры (giSeek=giSize=0) — вырожденный случай той же механики: без фона
    получается прозрачный холст, с фоном — копия ключевого кадра.
    """
    W, H = 3, 2

    def _empty_gai(self, frames=2, have_bg=1):
        head = struct.pack("<12I", srgi.GAI_SIG, 1, 0, 0, self.W, self.H,
                           frames, have_bg, 0, 0, 0, 0)
        return head + struct.pack("<%dI" % (frames * 2), *([0] * frames * 2))

    def _key_frame(self):
        rgba = bytes([(i * 11) % 256 for i in range(self.W * self.H * 4)])
        return rgba, srgi.encode_gi(self.W, self.H, rgba, fmt="argb")

    def test_frames_fall_back_to_a_transparent_canvas_without_one(self):
        decoded = srgi.decode_gai(self._empty_gai())[0]
        self.assertEqual([bytes(cv.buf) for cv in decoded],
                         [bytes(self.W * self.H * 4)] * 2)

    def test_frames_start_from_the_external_key_frame(self):
        rgba, blob = self._key_frame()
        bg = srgi.decode_background(blob, (0, 0, self.W, self.H))
        decoded = srgi.decode_gai(self._empty_gai(), bg)[0]
        self.assertEqual([bytes(cv.buf) for cv in decoded], [rgba] * 2)

    def test_a_mismatched_background_is_refused(self):
        bg = srgi.decode_background(srgi.encode_gi(4, 4, bytes(64), "argb"),
                                    (0, 0, 4, 4))
        with self.assertRaises(ValueError):
            srgi.decode_gai(self._empty_gai(), bg)

    def test_background_must_be_a_gi(self):
        with self.assertRaises(ValueError):
            srgi.decode_background(self._empty_gai(), (0, 0, self.W, self.H))

    def test_sibling_lookup_ignores_case(self):
        with tempfile.TemporaryDirectory() as directory:
            gai = os.path.join(directory, "2Gaal2Anim0.gai")
            open(gai, "wb").close()
            gi = os.path.join(directory, "2Gaal2Anim0.GI")
            open(gi, "wb").close()
            self.assertEqual(srgi.find_background(gai), gi)

    def test_sibling_lookup_returns_none_when_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            gai = os.path.join(directory, "lonely.gai")
            open(gai, "wb").close()
            self.assertIsNone(srgi.find_background(gai))


class _BitWriter:
    """Inverse of srgi._Bits: fields go in MSB-first, bytes fill LSB-first."""

    def __init__(self):
        self.data = bytearray()
        self.bit = 0

    def write(self, value, n):
        while n:
            if self.bit == 0:
                self.data.append(0)
            take = min(8 - self.bit, n)
            part = (value >> (n - take)) & ((1 << take) - 1)
            self.data[-1] |= part << self.bit
            self.bit = (self.bit + take) % 8
            n -= take
        return self


class Type6Tests(unittest.TestCase):
    """type 6 — битовый delta-формат HD-анимаций (порт okgf.dll!OKGR_F6_DrawRGBA).

    Опкоды проверяются поштучно: дельта канала, очистка пикселей, переход на
    строку ниже, конец блока. Плюс инвариант расхода — поток обязан кончиться
    ровно на границе слоя (на стоковом корпусе это выполняется байт-в-байт).
    """
    WIDTHS = 0x65, 0x55                              # 5,6,5,5 бит = R5G6B5A5

    def _stream(self, blocks, body):
        head = bytes(self.WIDTHS) + struct.pack("<H", blocks)
        return head + bytes(body.data)

    def _canvas(self, w=4, h=2, fill=0x80):
        cv = srgi.Canvas(w, h)
        cv.buf[:] = bytes([fill]) * (w * h * 4)
        return cv

    def test_bit_fields_round_trip(self):
        bw = _BitWriter().write(1000, 10).write(5, 3).write(2, 2).write(1, 1)
        br = srgi._Bits(bytes(bw.data), 0)
        self.assertEqual([br.read(10), br.read(3), br.read(2), br.read(1)],
                         [1000, 5, 2, 1])

    def test_delta_scales_by_the_channel_width(self):
        """(v+1) << shift, канал 0 потока = B (порядок буфера B,G,R,A)."""
        bw = _BitWriter()
        bw.write(0, 10).write(0, 10)                 # x=0, y=0
        bw.write(3, 3).write(0, 2).write(0, 1)       # width=3, chan=0 (B), плюс
        bw.write(1, 1).write(1, 3)                   # дельта: v=1 -> (1+1)<<3 = 16
        bw.write(0, 1).write(0, 3)                   # конец сегмента
        bw.write(0, 3).write(1, 2)                   # конец блока
        cv = self._canvas()
        srgi.decode_type6(cv, 0, 0, self._stream(1, bw), 0)
        self.assertEqual(bytes(cv.buf[:4]), bytes((0x80, 0x80, 0x90, 0x80)))

    def test_sign_bit_subtracts(self):
        bw = _BitWriter()
        bw.write(0, 10).write(0, 10)
        bw.write(3, 3).write(2, 2).write(1, 1)       # chan=2 (R), минус
        bw.write(1, 1).write(1, 3)                   # -(1+1)<<3 = -16
        bw.write(0, 1).write(0, 3)
        bw.write(0, 3).write(1, 2)
        cv = self._canvas()
        srgi.decode_type6(cv, 0, 0, self._stream(1, bw), 0)
        self.assertEqual(bytes(cv.buf[:4]), bytes((0x70, 0x80, 0x80, 0x80)))

    def test_skip_run_leaves_pixels_alone(self):
        bw = _BitWriter()
        bw.write(0, 10).write(0, 10)
        bw.write(3, 3).write(0, 2).write(0, 1)
        bw.write(0, 1).write(2, 3)                   # пропустить 2 пикселя
        bw.write(1, 1).write(0, 3)                   # дельта в третий: (0+1)<<3
        bw.write(0, 1).write(0, 3)
        bw.write(0, 3).write(1, 2)
        cv = self._canvas()
        srgi.decode_type6(cv, 0, 0, self._stream(1, bw), 0)
        self.assertEqual(bytes(cv.buf[:8]), bytes([0x80] * 8))       # два пропущены
        self.assertEqual(bytes(cv.buf[8:12]), bytes((0x80, 0x80, 0x88, 0x80)))

    def test_clear_run_zeroes_whole_pixels(self):
        bw = _BitWriter()
        bw.write(0, 10).write(0, 10)
        bw.write(0, 3).write(2, 2)                   # width=0, chan=2 -> clear/skip
        bw.write(1, 1)                               # очистить пиксель 0
        bw.write(0, 1).write(1, 3)                   # пропустить 1
        bw.write(1, 1)                               # очистить пиксель 2
        bw.write(0, 1).write(0, 3)                   # конец
        bw.write(0, 3).write(1, 2)
        cv = self._canvas()
        srgi.decode_type6(cv, 0, 0, self._stream(1, bw), 0)
        self.assertEqual(bytes(cv.buf[:12]),
                         bytes((0, 0, 0, 0, 0x80, 0x80, 0x80, 0x80, 0, 0, 0, 0)))

    def test_row_command_steps_down_a_scanline(self):
        bw = _BitWriter()
        bw.write(0, 10).write(0, 10)
        bw.write(0, 3).write(0, 2)                   # на строку ниже
        bw.write(3, 3).write(0, 2).write(0, 1)
        bw.write(1, 1).write(0, 3)                   # дельта в (0,1)
        bw.write(0, 1).write(0, 3)
        bw.write(0, 3).write(1, 2)
        cv = self._canvas()
        srgi.decode_type6(cv, 0, 0, self._stream(1, bw), 0)
        self.assertEqual(bytes(cv.buf[:4]), bytes([0x80] * 4))       # строка 0 цела
        self.assertEqual(cv.buf[4 * 4 + 2], 0x88)                    # (0,1).B += 8

    def test_writes_outside_the_canvas_are_clipped(self):
        bw = _BitWriter()
        bw.write(3, 10).write(0, 10)                 # x=3 при ширине 4
        bw.write(3, 3).write(0, 2).write(0, 1)
        bw.write(1, 1).write(0, 3)                   # пиксель 3 — последний в строке
        bw.write(1, 1).write(0, 3)                   # пиксель 4 — уже за краем
        bw.write(0, 1).write(0, 3)
        bw.write(0, 3).write(1, 2)
        cv = self._canvas(w=4, h=2)
        srgi.decode_type6(cv, 0, 0, self._stream(1, bw), 0)
        self.assertEqual(cv.buf[3 * 4 + 2], 0x88)
        self.assertEqual(bytes(cv.buf[16:20]), bytes([0x80] * 4))    # строка 1 цела

    def test_offset_layer_origin_is_honoured(self):
        bw = _BitWriter()
        bw.write(1, 10).write(0, 10)
        bw.write(3, 3).write(0, 2).write(0, 1)
        bw.write(1, 1).write(0, 3)
        bw.write(0, 1).write(0, 3)
        bw.write(0, 3).write(1, 2)
        cv = self._canvas(w=4, h=2)
        srgi.decode_type6(cv, 1, 1, self._stream(1, bw), 0)          # origin (1,1)
        self.assertEqual(cv.buf[(4 + 2) * 4 + 2], 0x88)              # -> (2,1)

    def test_stream_ends_on_the_layer_boundary(self):
        bw = _BitWriter()
        bw.write(0, 10).write(0, 10)
        bw.write(0, 3).write(1, 2)                   # пустой блок
        blob = self._stream(1, bw)
        cv = self._canvas()
        self.assertEqual(srgi.decode_type6(cv, 0, 0, blob, 0), len(blob))


class ClippingTests(unittest.TestCase):
    """Слой может свисать за холст (фоновый .gi обычно больше бокса анимации) —
    видимая часть обязана рисоваться, а не отбрасываться целиком вместе с раном."""

    def test_clip_run_edges(self):
        self.assertEqual(srgi._clip_run(0, 4, 4), (0, 4))     # ровно по холсту
        self.assertEqual(srgi._clip_run(-2, 4, 4), (2, 2))    # свисает слева
        self.assertEqual(srgi._clip_run(3, 4, 4), (0, 1))     # свисает справа
        self.assertEqual(srgi._clip_run(-9, 4, 4), (9, 0))    # целиком слева
        self.assertEqual(srgi._clip_run(7, 4, 4), (0, 0))     # целиком справа

    def test_raw_scanline_is_clipped_not_dropped(self):
        cv = srgi.Canvas(2, 1)
        srgi.blit_r5g6b5(cv, -1, 0, 3, 1, struct.pack("<3H", 0xF800, 0x07E0, 0x001F), 0)
        self.assertEqual(bytes(cv.buf), b"\x00\xfc\x00\xff\x00\x00\xf8\xff")

    def test_background_larger_than_the_animation_box_still_lands(self):
        """Ровно случай FormShip2/2CenterNormal: .gi 334x249, бокс gai (24,17)-(307,232)."""
        rgba = bytes([(i * 5 + 3) % 256 for i in range(4 * 2 * 4)])
        blob = srgi.encode_gi(4, 2, rgba, fmt="argb")
        cv = srgi.decode_background(blob, (1, 0, 3, 2))
        self.assertEqual((cv.w, cv.h), (2, 2))
        expected = b"".join(rgba[(row * 4 + col) * 4:(row * 4 + col) * 4 + 4]
                            for row in range(2) for col in (1, 2))
        self.assertEqual(bytes(cv.buf), expected)


class DecodeCliTests(unittest.TestCase):
    """CLI: автоподхват соседнего .gi, --no-bg, и что sidecar это фиксирует."""

    def _animation(self, directory, have_bg=1):
        w, h = 3, 2
        rgba = bytes([(i * 7 + 1) % 256 for i in range(w * h * 4)])
        head = struct.pack("<12I", srgi.GAI_SIG, 1, 0, 0, w, h, 2, have_bg, 0, 0, 0, 0)
        with open(os.path.join(directory, "anim.gai"), "wb") as f:
            f.write(head + struct.pack("<4I", 0, 0, 0, 0))
        with open(os.path.join(directory, "anim.gi"), "wb") as f:
            f.write(srgi.encode_gi(w, h, rgba, fmt="argb"))
        return rgba

    def _decode(self, src, out, *extra):
        self.assertEqual(srgi.main(["decode", src, out] + list(extra)), 0)
        with open(os.path.join(out, "anim.times.json")) as f:
            return json.load(f), srgi.read_png(os.path.join(out, "anim_000.png"))

    def test_decode_picks_up_the_sibling_gi(self):
        with tempfile.TemporaryDirectory() as directory:
            rgba = self._animation(directory)
            out = os.path.join(directory, "out")
            meta, (_w, _h, got) = self._decode(os.path.join(directory, "anim.gai"), out)
            self.assertEqual(meta.get("background"), "anim.gi")
            self.assertEqual(got, rgba)

    def test_no_bg_leaves_the_deltas_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            self._animation(directory)
            out = os.path.join(directory, "out")
            meta, (w, h, got) = self._decode(os.path.join(directory, "anim.gai"),
                                             out, "--no-bg")
            self.assertNotIn("background", meta)
            self.assertEqual(got, bytes(w * h * 4))

    def test_havebackground_zero_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            self._animation(directory, have_bg=0)
            out = os.path.join(directory, "out")
            meta, (w, h, got) = self._decode(os.path.join(directory, "anim.gai"), out)
            self.assertNotIn("background", meta)
            self.assertEqual(got, bytes(w * h * 4))

    def test_explicit_bg_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            self._animation(directory)
            other = bytes([(i * 3 + 9) % 256 for i in range(3 * 2 * 4)])
            alt = os.path.join(directory, "other.gi")
            with open(alt, "wb") as f:
                f.write(srgi.encode_gi(3, 2, other, fmt="argb"))
            out = os.path.join(directory, "out")
            meta, (_w, _h, got) = self._decode(os.path.join(directory, "anim.gai"),
                                               out, "--bg", alt)
            self.assertEqual(meta.get("background"), "other.gi")
            self.assertEqual(got, other)

    def test_bg_is_rejected_for_a_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            self._animation(directory)
            out = os.path.join(directory, "out")
            self.assertEqual(srgi.main(["decode", directory, out, "--bg",
                                        os.path.join(directory, "anim.gi")]), 1)

    def test_batch_picks_up_backgrounds_too(self):
        with tempfile.TemporaryDirectory() as directory:
            rgba = self._animation(directory)
            out = os.path.join(directory, "out")
            self.assertEqual(srgi.main(["decode", directory, out]), 0)
            with open(os.path.join(out, "anim.times.json")) as f:
                self.assertEqual(json.load(f).get("background"), "anim.gi")
            self.assertEqual(srgi.read_png(os.path.join(out, "anim_001.png"))[2], rgba)


class ReferenceCorpusTests(unittest.TestCase):
    @unittest.skipUnless(_reference_images(), "нет .gi под reference/")
    def test_reference_images_verify_and_decode(self):
        for path in _reference_images():
            with self.subTest(image=os.path.basename(path)):
                self.assertEqual(srgi._verify_file(path), [])
                cv = srgi.decode_gi(srgi.load_any(path))
                self.assertGreater(cv.w * cv.h, 0)


if __name__ == "__main__":
    unittest.main()
