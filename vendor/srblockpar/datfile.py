"""Оболочка файла .dat: контрольные суммы, поточный шифр, обёртка ZL.

Слои, снаружи внутрь:

1. необязательный внешний заголовок — 8 байт: длина остатка (поXOR-енная
   двумя ключами сумм) и внешняя контрольная сумма. Игра без него работает,
   только помечает у себя «контрольная сумма ресурса не сошлась»;
   BlockParEditor заголовок не пишет вовсе;
2. 8 байт: контрольная сумма расшифрованного и начальное число потока,
   поXOR-енное ключом;
3. поточный шифр — XOR с младшими байтами генератора 16807 по модулю 2^31-1;
4. обёртка ZL01 и внутри неё сжатое дерево.

Ключей два: для датников BlockPar (Main.dat, Lang.dat) и для данных
(CacheData.dat). Какой подошёл — видно по контрольной сумме, и это же
говорит, какое внутри дерево.
"""

import struct
import zlib
from collections import OrderedDict

from . import zl

BLOCK_SEED_KEY = 0xB1E8C689
RESOURCE_SEED_KEY = 0xEA8F3F37
CRC_KEY1 = 0x7DB6C99D
CRC_KEY2 = 0xC83FCBF3

KIND_BLOCK = "block"
KIND_DATA = "data"

SEED_KEYS = OrderedDict((
    (KIND_BLOCK, BLOCK_SEED_KEY),
    (KIND_DATA, RESOURCE_SEED_KEY),
))

# Начальное число по умолчанию при записи: любое годится, берём постоянное,
# чтобы одинаковое дерево давало одинаковый файл.
DEFAULT_SEED = 0x1F2E3D4C

MODULUS = 0x7FFFFFFF
MULTIPLIER = 16807
QUOTIENT = 127773
REMAINDER = 2836


class DatError(ValueError):
    """Файл не разбирается как .dat игры."""


def keystream(seed, size):
    """Байты гаммы. Генератор тот же, что в игре, с усечением деления к нулю."""
    out = bytearray(size)
    state = seed
    for i in range(size):
        quotient = int(state / QUOTIENT)
        state = MULTIPLIER * (state - quotient * QUOTIENT) - REMAINDER * quotient
        if state <= 0:
            state += MODULUS
        out[i] = (state - 1) & 0xFF
    return bytes(out)


def apply_cipher(data, seed):
    """Наложить гамму. Действие обратно самому себе."""
    if not data:
        return b""
    gamma = keystream(seed, len(data))
    mixed = int.from_bytes(data, "little") ^ int.from_bytes(gamma, "little")
    return mixed.to_bytes(len(data), "little")


def _signed(value):
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def outer_checksum(body):
    """Внешняя сумма: считается по телу файла в два приёма, как в игре."""
    first = (zlib.crc32(body) ^ CRC_KEY1) & 0xFFFFFFFF
    return (zlib.crc32(struct.pack("<I", first) + body) ^ CRC_KEY2) & 0xFFFFFFFF


def has_outer_header(raw):
    if len(raw) < 16:
        return False
    declared = struct.unpack_from("<I", raw, 0)[0] ^ (CRC_KEY1 ^ CRC_KEY2)
    return declared == len(raw) - 8


class DatFile(object):
    """Разобранная оболочка: что внутри и как файл был сложен."""

    def __init__(self, payload, kind, seed, outer, checksum_ok, signature):
        self.payload = payload          # байты дерева, уже распакованные
        self.kind = kind                # block или data
        self.seed = seed                # начальное число потока
        self.outer = outer              # был ли внешний заголовок
        self.checksum_ok = checksum_ok  # сошлась ли внешняя сумма
        self.signature = signature      # подпись обёртки ZL

    def describe(self):
        return OrderedDict((
            ("kind", self.kind),
            ("outer_header", self.outer),
            ("outer_checksum_ok", self.checksum_ok),
            ("seed", self.seed),
            ("zl_signature", self.signature.decode("ascii")),
            ("payload_size", len(self.payload)),
        ))


def _try_frame(raw, base, seed_key):
    if len(raw) < base + 12:
        return None
    stored_crc, seed_field = struct.unpack_from("<II", raw, base)
    seed = _signed(seed_field ^ seed_key)
    # Сначала расшифровываем четыре байта и смотрим на подпись ZL: это
    # отсеивает чужие файлы с расширением .dat, не расшифровывая их целиком.
    if apply_cipher(raw[base + 8:base + 12], seed) not in zl.SIGNATURES:
        return None
    plain = apply_cipher(raw[base + 8:], seed)
    if zlib.crc32(plain) & 0xFFFFFFFF != stored_crc:
        return None
    return plain, seed


def unpack(raw):
    """Снять оболочку. Перебирает обе рамки и оба ключа, опора — сумма."""
    if len(raw) < 16:
        raise DatError("файл слишком мал для .dat")
    outer = has_outer_header(raw)
    bases = (8, 0) if outer else (0, 8)
    for base in bases:
        for kind, seed_key in SEED_KEYS.items():
            found = _try_frame(raw, base, seed_key)
            if found is None:
                continue
            plain, seed = found
            body, signature = zl.unwrap(plain)
            checksum_ok = False
            if base == 8:
                checksum_ok = outer_checksum(raw[8:]) == struct.unpack_from("<I", raw, 4)[0]
            return DatFile(body, kind, seed, base == 8, checksum_ok, signature)
    raise DatError("ни одна из рамок не сошлась по контрольной сумме — это не датник игры")


def pack(payload, kind=KIND_BLOCK, seed=DEFAULT_SEED, outer=True, signature=zl.DEFAULT_SIGNATURE):
    """Сложить файл обратно. По умолчанию с внешним заголовком — как у игры."""
    if kind not in SEED_KEYS:
        raise DatError("незнакомый вид датника: %s" % kind)
    blob = zl.wrap(payload, signature)
    crc = zlib.crc32(blob) & 0xFFFFFFFF
    body = struct.pack("<II", crc, (seed ^ SEED_KEYS[kind]) & 0xFFFFFFFF) + apply_cipher(blob, seed)
    if not outer:
        return body
    head = struct.pack("<II", (len(body) ^ (CRC_KEY1 ^ CRC_KEY2)) & 0xFFFFFFFF, outer_checksum(body))
    return head + body
