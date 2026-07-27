# -*- coding: utf-8 -*-
"""Тесты srgi.py — gi/gai/hai ↔ PNG.

Энкодер и декодер проверяются друг против друга: PNG → gi → PNG обязан вернуть
те же пиксели, а заголовок — те же поля. Плюс инварианты формата, которые
дешевле закрепить тестом, чем ловить прогоном по корпусу: ARGB — lossless,
RGB565 — ровно 5/6/5 бит, GAI помнит тайминги покадрово.

Байт-в-байт против настоящего .gi проверяется, только если под reference/
что-нибудь лежит (в git его нет — копирайтный контент).
"""
import os
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
