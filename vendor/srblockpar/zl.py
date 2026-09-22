"""Обёртка ZL01/ZL02: подпись, длина распакованного, дальше поток zlib.

Так игра хранит сжатые куски — и в датниках, и в ресурсах. Длина записана
отдельно, чтобы распаковщик сразу знал, сколько выделять.
"""

import struct
import zlib

SIGNATURES = (b"ZL01", b"ZL02")
DEFAULT_SIGNATURE = b"ZL01"
HEADER_SIZE = 8

# Потоки игры начинаются с 78 DA — сжатие на максимальном уровне.
DEFAULT_LEVEL = 9


class ZLError(ValueError):
    """Не обёртка ZL либо повреждённый поток."""


def looks_like(data):
    return len(data) >= HEADER_SIZE and data[:4] in SIGNATURES


def unwrap(data):
    """Распаковать обёртку. Возвращает (данные, подпись)."""
    if not looks_like(data):
        raise ZLError("нет подписи ZL01/ZL02")
    signature = data[:4]
    declared = struct.unpack_from("<I", data, 4)[0]
    try:
        body = zlib.decompress(data[HEADER_SIZE:])
    except zlib.error as error:
        raise ZLError("поток zlib не распаковался: %s" % error)
    if len(body) != declared:
        raise ZLError("длина не сошлась: в заголовке %d, распаковалось %d" % (declared, len(body)))
    return body, signature


def wrap(data, signature=DEFAULT_SIGNATURE, level=DEFAULT_LEVEL):
    if signature not in SIGNATURES:
        raise ZLError("незнакомая подпись %r" % signature)
    return signature + struct.pack("<I", len(data)) + zlib.compress(data, level)
