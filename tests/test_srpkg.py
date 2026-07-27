# -*- coding: utf-8 -*-
"""Тесты srpkg.py — упаковка/распаковка .pkg.

Главное, что здесь закрепляется, — правила байт-точной пересборки. Они
восстановлены диффингом всего стокового корпуса (9087 файлов, 0 исключений), а
не выведены из здравого смысла, и потому легко «упрощаются» при рефакторинге в
что-нибудь разумное на вид и неверное по факту. Полный прогон 19/19 DATA ловит
это только при наличии игры под рукой; эти тесты — сразу.
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import srpkg  # noqa: E402


def _item(data_type, size, offset, name="test.scr"):
    it = srpkg.Item()
    it.name = name.encode("cp1251")
    it.full_name = name.upper().encode("cp1251")
    it.data_type = data_type
    it.size = size
    it.size_in_arc = 0
    it.offset = offset
    return it


def _sample_tree(root):
    """Дерево, покрывающее обе ветки правила RAW/ZL02 и вложенность."""
    files = {
        os.path.join("ModuleInfo.txt"): b"CE\r\n" * 4,                    # <128 Б → RAW
        os.path.join("DATA", "Script", "Mod.scr"): b"script body\n" * 512,  # → ZL02
        os.path.join("DATA", "Images", "Map.png"): b"\x89PNG\r\n\x1a\n" * 64,  # .png → RAW
    }
    for name, data in files.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return files


class ReadTests(unittest.TestCase):
    def test_zl02_record(self):
        raw = "Космические рейнджеры\x00".encode("cp1251") * 8
        blob = srpkg.ZL02_SIG + struct.pack("<I", len(raw)) + zlib.compress(raw)
        record = struct.pack("<I", 0) + struct.pack("<I", len(blob)) + blob
        self.assertEqual(srpkg.extract_file(record, _item(srpkg.TYPE_ZL02, len(raw), 0)), raw)

    def test_multiple_chunks(self):
        """Чанк — не более 64 КиБ распакованных, большие файлы идут несколькими."""
        chunks = [b"A" * 65536, b"B" * 17]
        body = b""
        for chunk in chunks:
            blob = srpkg.ZL02_SIG + struct.pack("<I", len(chunk)) + zlib.compress(chunk)
            body += struct.pack("<I", len(blob)) + blob
        record = struct.pack("<I", 0) + body
        it = _item(srpkg.TYPE_ZL02, sum(map(len, chunks)), 0, "large.scr")
        self.assertEqual(srpkg.extract_file(record, it), b"".join(chunks))

    def test_raw_record(self):
        raw = b"\x89PNG\r\n\x1a\n"
        record = struct.pack("<I", len(raw)) + raw
        self.assertEqual(srpkg.extract_file(record, _item(srpkg.TYPE_RAW, len(raw), 0, "a.png")), raw)

    def test_truncated_chunk_is_an_error_not_silence(self):
        blob = b"XXXX" + struct.pack("<I", 10) + zlib.compress(b"x" * 10)
        record = struct.pack("<I", 0) + struct.pack("<I", len(blob)) + blob
        with self.assertRaises(ValueError):
            srpkg.extract_file(record, _item(srpkg.TYPE_ZL02, 10, 0))


class PackRulesTests(unittest.TestCase):
    def test_raw_vs_zl02_is_by_name_and_size(self):
        """Не «помогает ли zlib» — стоковый корпус с таким тестом расходится."""
        big = b"x" * 4096                       # сжимается прекрасно, но:
        self.assertEqual(srpkg._encode_payload("a.scr", big, True)[1], srpkg.TYPE_ZL02)
        self.assertEqual(srpkg._encode_payload("a.png", big, True)[1], srpkg.TYPE_RAW)
        self.assertEqual(srpkg._encode_payload("a.jpg", big, True)[1], srpkg.TYPE_RAW)
        self.assertEqual(srpkg._encode_payload("A.PNG", big, True)[1], srpkg.TYPE_RAW)
        self.assertEqual(srpkg._encode_payload("a.scr", b"x" * 127, True)[1], srpkg.TYPE_RAW)
        self.assertEqual(srpkg._encode_payload("a.scr", b"x" * 128, True)[1], srpkg.TYPE_ZL02)

    def test_chunks_are_capped_at_64k_uncompressed(self):
        payload, dtype = srpkg._encode_payload("a.scr", b"z" * (srpkg.CHUNK_RAW * 2 + 5), True)
        self.assertEqual(dtype, srpkg.TYPE_ZL02)
        sizes, pos = [], 0
        while pos < len(payload):
            packed = struct.unpack_from("<I", payload, pos)[0]
            sizes.append(struct.unpack_from("<I", payload, pos + 8)[0])
            pos += 4 + packed
        self.assertEqual(sizes, [srpkg.CHUNK_RAW, srpkg.CHUNK_RAW, 5])

    def test_full_name_upper_cases_cp1251_cyrillic(self):
        self.assertEqual(srpkg._cp1251_upper("копия".encode("cp1251")),
                         "КОПИЯ".encode("cp1251"))
        self.assertEqual(srpkg._cp1251_upper("ёж".encode("cp1251")),
                         "ЁЖ".encode("cp1251"))
        self.assertEqual(srpkg._cp1251_upper(b"file.scr"), b"FILE.SCR")

    def test_directories_come_before_files(self):
        with tempfile.TemporaryDirectory() as directory:
            src = os.path.join(directory, "src")
            os.makedirs(os.path.join(src, "zdir"))
            with open(os.path.join(src, "zdir", "inner.txt"), "wb") as f:
                f.write(b"inner")
            with open(os.path.join(src, "afile.txt"), "wb") as f:
                f.write(b"file")
            pkg = os.path.join(directory, "out.pkg")
            srpkg._pack_one(src, pkg, compress=True)
            _buf, root = srpkg.load_pkg(pkg)
            self.assertEqual(
                [(it.name.decode("cp1251"), it.data_type == srpkg.TYPE_DIR) for it in root],
                [("zdir", True), ("afile.txt", False)])


class RoundTripTests(unittest.TestCase):
    def test_pack_then_unpack_returns_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            src = os.path.join(directory, "src")
            os.makedirs(src)
            files = _sample_tree(src)
            pkg = os.path.join(directory, "mod.pkg")
            out = os.path.join(directory, "out")
            srpkg._pack_one(src, pkg, compress=True)
            srpkg._unpack_one(pkg, out, verbose=False)
            for name, data in files.items():
                with self.subTest(name=name):
                    with open(os.path.join(out, name), "rb") as f:
                        self.assertEqual(f.read(), data)

    def test_packing_is_deterministic(self):
        """На этом держится байт-точность: то же дерево → те же байты."""
        with tempfile.TemporaryDirectory() as directory:
            src = os.path.join(directory, "src")
            os.makedirs(src)
            _sample_tree(src)
            a, b = os.path.join(directory, "a.pkg"), os.path.join(directory, "b.pkg")
            srpkg._pack_one(src, a, compress=True)
            srpkg._pack_one(src, b, compress=True)
            with open(a, "rb") as fa, open(b, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_raw_mode_stores_everything_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            src = os.path.join(directory, "src")
            os.makedirs(src)
            with open(os.path.join(src, "big.scr"), "wb") as f:
                f.write(b"q" * 4096)
            pkg = os.path.join(directory, "raw.pkg")
            srpkg._pack_one(src, pkg, compress=False)
            _buf, root = srpkg.load_pkg(pkg)
            self.assertEqual([it.data_type for it in root], [srpkg.TYPE_RAW])


class UnsafePathTests(unittest.TestCase):
    """Имена приходят из архива, а архивы — чужие."""

    def test_parent_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in ("../escape.scr", "DATA/../../escape.scr", "../../../etc/passwd"):
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        srpkg.safe_destination(directory, path)

    @unittest.skipUnless(os.name == "nt", "буква диска — понятие Windows")
    def test_drive_letter_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                srpkg.safe_destination(directory, "C:/Windows/System32/evil.dll")

    def test_leading_slash_is_neutralised_rather_than_refused(self):
        """'/etc/passwd' — не побег: ведущий пустой сегмент отбрасывается, и путь
        становится относительным внутри каталога назначения."""
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(srpkg.safe_destination(directory, "/etc/passwd"),
                             os.path.join(directory, "etc", "passwd"))

    def test_ordinary_path_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = srpkg.safe_destination(directory, "DATA/Script/ok.scr")
            self.assertEqual(dest, os.path.join(directory, "DATA", "Script", "ok.scr"))

    def test_trailing_slash_of_a_directory_entry_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(srpkg.safe_destination(directory, "DATA/"),
                             os.path.join(directory, "DATA"))


if __name__ == "__main__":
    unittest.main()
