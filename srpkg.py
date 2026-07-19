#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
srpkg - headless CLI for Space Rangers HD *.pkg resource archives.

A command-line replacement for the pack/unpack side of ResEditor 1.3.2
(the GUI-only tool). Format reverse-engineered against the real game files
and cross-checked with the OpenSR reference loader (Ranger/PKG.cpp, ZLib.cpp).

Formats supported: list / unpack / pack of .pkg (RAW and ZL02-zlib entries).
GI/GAI/HAI *image* decoding is out of scope (this only moves file bytes in/out
of the container, losslessly).

Binary layout (little-endian):
  file[0]              u32  offset of root dir header (always 4)
  dir header (12 B)    u32 zero1=170, u32 itemsCount, u32 zero2=158
  item (158 B)         u32 sizeInArc, u32 size,
                       char[63] fullName (UPPER), char[63] name,
                       u32 dataType, u32 dataType(mirror), u32 0, u32 0,
                       u32 offset, u32 0
  data block           u32 prefix(=sizeInArc-4), then payload:
                         dataType 1 (RAW):  `size` bytes
                         dataType 2 (ZL02): repeated [u32 packed][ "ZL02" u32 raw zlib ]
                                            with <=64 KiB uncompressed per chunk
                         dataType 3       : directory (offset -> its dir header)
"""
import argparse
import os
import struct
import sys
import zlib

ITEM_SIZE = 158
DIR_HDR_SIZE = 12
DIR_ZERO1 = 170          # 12 + 158, constant in every real dir header
DIR_ZERO2 = 158          # item size, constant
NAME_FIELD = 63
CHUNK_RAW = 0x10000      # 64 KiB uncompressed per ZL02 chunk (matches originals)
ZL02_SIG = b"ZL02"

TYPE_RAW = 1
TYPE_ZL02 = 2
TYPE_DIR = 3

NAME_ENC = "cp1251"      # SR uses cp1251/cp866; resource names are ASCII in practice


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


# --------------------------------------------------------------------------- read

class Item:
    __slots__ = ("name", "full_name", "data_type", "size", "size_in_arc",
                 "offset", "children")

    def __init__(self):
        self.children = []


def _read_dir(buf, dir_off):
    count = _u32(buf, dir_off + 4)
    items = []
    for i in range(count):
        b = dir_off + DIR_HDR_SIZE + ITEM_SIZE * i
        it = Item()
        it.size_in_arc = _u32(buf, b + 0)
        it.size = _u32(buf, b + 4)
        it.full_name = buf[b + 8:b + 8 + NAME_FIELD].split(b"\x00")[0]
        it.name = buf[b + 71:b + 71 + NAME_FIELD].split(b"\x00")[0]
        it.data_type = _u32(buf, b + 134)
        it.offset = _u32(buf, b + 150)
        if it.data_type == TYPE_DIR:
            it.children = _read_dir(buf, it.offset)
        items.append(it)
    return items


def load_pkg(path):
    with open(path, "rb") as f:
        buf = f.read()
    root_off = _u32(buf, 0)
    return buf, _read_dir(buf, root_off)


def extract_file(buf, it):
    """Return the decompressed bytes of a file item."""
    if it.data_type == TYPE_RAW:
        start = it.offset + 4
        return buf[start:start + it.size]
    if it.data_type == TYPE_ZL02:
        out = bytearray()
        p = it.offset + 4
        while len(out) < it.size:
            packed = _u32(buf, p)
            p += 4
            blob = buf[p:p + packed]
            p += packed
            if blob[:4] != ZL02_SIG:
                raise ValueError("bad ZL02 signature in %r" % it.name)
            out += zlib.decompress(blob[8:])
        if len(out) != it.size:
            raise ValueError("size mismatch on %r: %d != %d"
                             % (it.name, len(out), it.size))
        return bytes(out)
    raise ValueError("cannot extract data_type=%d (%r)" % (it.data_type, it.name))


# --------------------------------------------------------------------------- list

def _walk(items, prefix=""):
    for it in items:
        name = it.name.decode(NAME_ENC, "replace")
        if it.data_type == TYPE_DIR:
            yield prefix + name + "/", it, True
            yield from _walk(it.children, prefix + name + "/")
        else:
            yield prefix + name, it, False


def cmd_list(args):
    _, root = load_pkg(args.pkg)
    nfiles = 0
    total = 0
    for path, it, is_dir in _walk(root):
        if is_dir:
            print(path)
        else:
            tag = {TYPE_RAW: "raw ", TYPE_ZL02: "zl02"}.get(it.data_type, "?   ")
            print("  %s %10d  %s" % (tag, it.size, path))
            nfiles += 1
            total += it.size
    print("--- %d files, %d bytes uncompressed" % (nfiles, total), file=sys.stderr)


# ------------------------------------------------------------------------- unpack

def cmd_unpack(args):
    buf, root = load_pkg(args.pkg)
    out_root = args.outdir
    os.makedirs(out_root, exist_ok=True)
    n = 0
    for path, it, is_dir in _walk(root):
        dest = os.path.join(out_root, *path.split("/"))
        if is_dir:
            os.makedirs(dest.rstrip(os.sep), exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "wb") as f:
                f.write(extract_file(buf, it))
            n += 1
            if args.verbose:
                print(path)
    print("unpacked %d files -> %s" % (n, out_root), file=sys.stderr)


# --------------------------------------------------------------------------- pack

class Node:
    __slots__ = ("name", "is_dir", "children", "data", "payload",
                 "data_type", "off")

    def __init__(self, name, is_dir):
        self.name = name
        self.is_dir = is_dir
        self.children = []
        self.data = None       # raw file bytes (files)
        self.payload = None    # encoded data block payload (files)
        self.data_type = TYPE_DIR if is_dir else TYPE_RAW
        self.off = 0


def _build_tree(src_dir):
    root = Node("", True)

    def rec(node, path):
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                child = Node(entry, True)
                node.children.append(child)
                rec(child, full)
            elif os.path.isfile(full):
                child = Node(entry, False)
                with open(full, "rb") as f:
                    child.data = f.read()
                node.children.append(child)
    rec(root, src_dir)
    return root


def _encode_payload(data, compress):
    """Return the payload bytes that follow the 4-byte prefix, and data_type."""
    if not compress:
        return data, TYPE_RAW
    out = bytearray()
    for i in range(0, max(len(data), 1), CHUNK_RAW):
        chunk = data[i:i + CHUNK_RAW]
        z = zlib.compress(chunk, 9)
        blob = ZL02_SIG + struct.pack("<I", len(chunk)) + z
        out += struct.pack("<I", len(blob)) + blob
        if not data:            # zero-length file: emit one empty chunk then stop
            break
    return bytes(out), TYPE_ZL02


def _encode_names(name):
    nb = name.encode(NAME_ENC, "replace")
    if len(nb) >= NAME_FIELD:
        raise ValueError("name too long (>%d bytes): %r" % (NAME_FIELD - 1, name))
    full = bytes(c - 32 if 0x61 <= c <= 0x7A else c for c in nb)  # ASCII upper
    return (full.ljust(NAME_FIELD, b"\x00"), nb.ljust(NAME_FIELD, b"\x00"))


def _layout(node, cur, compress):
    """Depth-first: reserve dir header + item table, then each child block."""
    node.off = cur
    cur += DIR_HDR_SIZE + ITEM_SIZE * len(node.children)
    for child in node.children:
        if child.is_dir:
            cur = _layout(child, cur, compress)
        else:
            child.payload, child.data_type = _encode_payload(child.data, compress)
            child.off = cur
            cur += 4 + len(child.payload)   # 4-byte prefix + payload
    return cur


def _emit(node, buf):
    struct.pack_into("<III", buf, node.off, DIR_ZERO1, len(node.children), DIR_ZERO2)
    for i, child in enumerate(node.children):
        b = node.off + DIR_HDR_SIZE + ITEM_SIZE * i
        full, nm = _encode_names(child.name)
        if child.is_dir:
            size_in_arc = size = 0
        else:
            size = len(child.data)
            size_in_arc = 4 + len(child.payload)
        struct.pack_into("<II", buf, b + 0, size_in_arc, size)
        struct.pack_into("<%ds" % NAME_FIELD, buf, b + 8, full)
        struct.pack_into("<%ds" % NAME_FIELD, buf, b + 71, nm)
        struct.pack_into("<IIIIII", buf, b + 134,
                         child.data_type, child.data_type, 0, 0, child.off, 0)
        if child.is_dir:
            _emit(child, buf)
        else:
            struct.pack_into("<I", buf, child.off, size_in_arc - 4)  # prefix
            buf[child.off + 4:child.off + 4 + len(child.payload)] = child.payload


def cmd_pack(args):
    compress = not args.raw
    root = _build_tree(args.srcdir)
    total = _layout(root, 4, compress)      # root header at offset 4
    buf = bytearray(total)
    struct.pack_into("<I", buf, 0, 4)       # pointer to root dir header
    _emit(root, buf)
    with open(args.pkg, "wb") as f:
        f.write(buf)
    print("packed -> %s (%d bytes, %s)"
          % (args.pkg, len(buf), "raw" if args.raw else "zl02"), file=sys.stderr)


# ------------------------------------------------------------------------- verify

def cmd_verify(args):
    """Round-trip self-test: unpack pkg -> repack -> unpack, compare all bytes."""
    import tempfile
    import shutil
    buf, root = load_pkg(args.pkg)
    orig = {p: extract_file(buf, it) for p, it, d in _walk(root) if not d}
    tmp = tempfile.mkdtemp(prefix="srpkg_")
    try:
        srcdir = os.path.join(tmp, "src")
        for path, data in orig.items():
            dest = os.path.join(srcdir, *path.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        for mode in ("zl02", "raw"):
            pkg2 = os.path.join(tmp, "rt_%s.pkg" % mode)
            root_n = _build_tree(srcdir)
            total = _layout(root_n, 4, mode == "zl02")
            b2 = bytearray(total)
            struct.pack_into("<I", b2, 0, 4)
            _emit(root_n, b2)
            with open(pkg2, "wb") as f:
                f.write(b2)
            buf2, r2 = load_pkg(pkg2)
            got = {p: extract_file(buf2, it) for p, it, d in _walk(r2) if not d}
            if got != orig:
                miss = set(orig) ^ set(got)
                bad = [p for p in orig if p in got and orig[p] != got[p]]
                print("FAIL (%s): missing/extra=%r content-diff=%r"
                      % (mode, list(miss)[:5], bad[:5]))
                return 1
            print("OK  %-4s round-trip: %d files, %d -> %d bytes"
                  % (mode, len(orig), len(buf), len(b2)))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="srpkg",
                                 description="Space Rangers HD .pkg pack/unpack CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list archive contents")
    p.add_argument("pkg")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("unpack", help="extract archive to a directory")
    p.add_argument("pkg")
    p.add_argument("outdir")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("pack", help="build archive from a directory")
    p.add_argument("srcdir")
    p.add_argument("pkg")
    p.add_argument("--raw", action="store_true",
                   help="store uncompressed (default: ZL02/zlib)")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("verify", help="round-trip self-test on an existing pkg")
    p.add_argument("pkg")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
