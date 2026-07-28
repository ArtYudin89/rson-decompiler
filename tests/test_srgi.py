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
