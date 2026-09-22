"""Дерево датника BlockPar: чтение и запись двоичного и текстового вида.

Два родственных дерева:

* **block** — то, что игра зовёт BlockPar (`Main.dat`, `Lang.dat`): у каждого
  блока есть признак «держать записи упорядоченными» и у каждой записи —
  пометки группы одноимённых;
* **data** — дерево данных (`CacheData.dat`): то же самое без признака и
  пометок.

Порядок записей сохраняется как есть — он значим: игра при слиянии модов
ищет одноимённые блоки по счёту вхождений.
"""

import struct
from collections import OrderedDict

KIND_TEXT = 0
KIND_STRING = 1
KIND_BLOCK = 2

KIND_NAMES = {KIND_TEXT: "text", KIND_STRING: "param", KIND_BLOCK: "block"}

FLAVOR_BLOCK = "block"
FLAVOR_DATA = "data"


class BlockParError(ValueError):
    """Дерево не разбирается."""


class Entry(object):
    __slots__ = ("kind", "name", "value", "block", "group_index", "group_count", "comment")

    def __init__(self, kind, name="", value=None, block=None, group_index=0, group_count=0,
                 comment=""):
        self.kind = kind
        self.name = name
        self.value = value
        self.block = block
        self.group_index = group_index
        self.group_count = group_count
        self.comment = comment

    def __repr__(self):
        if self.kind == KIND_BLOCK:
            return "<блок %s: %d записей>" % (self.name, len(self.block.entries))
        return "<%s %s=%r>" % (KIND_NAMES.get(self.kind, "?"), self.name, self.value)


class Block(object):
    def __init__(self, sorted_index=True, entries=None):
        self.sorted_index = sorted_index
        self.entries = list(entries or ())

    # -- сборка ------------------------------------------------------------

    def add_param(self, name, value, comment=""):
        entry = Entry(KIND_STRING, name, value=value, comment=comment)
        self.entries.append(entry)
        return entry

    def add_block(self, name, sorted_index=True, comment=""):
        child = Block(sorted_index)
        self.entries.append(Entry(KIND_BLOCK, name, block=child, comment=comment))
        return child

    # -- выборка -----------------------------------------------------------

    def params(self):
        return [e for e in self.entries if e.kind == KIND_STRING]

    def blocks(self):
        return [e for e in self.entries if e.kind == KIND_BLOCK]

    def get(self, path, default=None):
        """Значение по пути вида `Graph.Cursor.Image`; берётся первое совпадение."""
        node = self
        parts = path.split(".")
        for step in parts[:-1]:
            found = None
            for entry in node.entries:
                if entry.kind == KIND_BLOCK and entry.name == step:
                    found = entry.block
                    break
            if found is None:
                return default
            node = found
        for entry in node.entries:
            if entry.kind == KIND_STRING and entry.name == parts[-1]:
                return entry.value
        return default

    def walk(self, prefix=""):
        """Перебор листьев: (путь, значение). Одноимённые получают номер в скобках."""
        counts = {}
        for entry in self.entries:
            if entry.kind == KIND_TEXT:
                continue
            seen = counts.get(entry.name, 0)
            counts[entry.name] = seen + 1
            name = entry.name if seen == 0 else "%s(%d)" % (entry.name, seen)
            path = name if not prefix else prefix + "." + name
            if entry.kind == KIND_STRING:
                yield path, entry.value
            else:
                for item in entry.block.walk(path):
                    yield item

    def to_dict(self):
        """Вид для машинного вывода: без потерь, повторы имён сохраняются порядком.

        Пометки группы у упорядоченных блоков выписываются как есть: они не
        выводятся из имён (проверено на датниках игры), а без них обратная
        сборка не даст тот же файл побайтово.
        """
        out = []
        for entry in self.entries:
            if entry.kind == KIND_STRING:
                item = OrderedDict((("kind", "param"), ("name", entry.name),
                                    ("value", entry.value)))
            elif entry.kind == KIND_BLOCK:
                item = OrderedDict((("kind", "block"), ("name", entry.name),
                                    ("sorted", entry.block.sorted_index),
                                    ("entries", entry.block.to_dict())))
            else:
                item = OrderedDict((("kind", "text"), ("comment", entry.comment)))
            if self.sorted_index and entry.kind != KIND_TEXT:
                item["group"] = [entry.group_index, entry.group_count]
            if entry.comment and entry.kind != KIND_TEXT:
                item["comment"] = entry.comment
            out.append(item)
        return out

    def __eq__(self, other):
        if not isinstance(other, Block):
            return NotImplemented
        if self.sorted_index != other.sorted_index or len(self.entries) != len(other.entries):
            return False
        for left, right in zip(self.entries, other.entries):
            if left.kind != right.kind or left.name != right.name:
                return False
            if left.kind == KIND_STRING and left.value != right.value:
                return False
            if left.kind == KIND_BLOCK and left.block != right.block:
                return False
        return True

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __repr__(self):
        return "<Block записей=%d %s>" % (
            len(self.entries), "упорядоченный" if self.sorted_index else "как есть")


# -- двоичный вид ---------------------------------------------------------


class _Cursor(object):
    def __init__(self, data):
        self.data = data
        self.position = 0

    def need(self, length):
        if self.position + length > len(self.data):
            raise BlockParError("дерево обрывается на смещении %d" % self.position)

    def byte(self):
        self.need(1)
        value = self.data[self.position]
        self.position += 1
        return value

    def uint32(self):
        self.need(4)
        value = struct.unpack_from("<I", self.data, self.position)[0]
        self.position += 4
        return value

    def int32(self):
        self.need(4)
        value = struct.unpack_from("<i", self.data, self.position)[0]
        self.position += 4
        return value

    def wide_string(self):
        data = self.data
        end = self.position
        while True:
            if end + 2 > len(data):
                raise BlockParError("строка не закрыта на смещении %d" % self.position)
            if data[end] == 0 and data[end + 1] == 0:
                break
            end += 2
        text = data[self.position:end].decode("utf-16-le", "replace")
        self.position = end + 2
        return text


def _read_tree(cursor, flavor):
    if flavor == FLAVOR_BLOCK:
        block = Block(sorted_index=bool(cursor.byte()))
    else:
        block = Block(sorted_index=False)
    count = cursor.int32()
    if count < 0:
        raise BlockParError("отрицательное число записей: %d" % count)
    for _ in range(count):
        group_index = group_count = 0
        if flavor == FLAVOR_BLOCK and block.sorted_index:
            group_index = cursor.int32()
            group_count = cursor.int32()
        kind = cursor.byte()
        name = cursor.wide_string()
        entry = Entry(kind, name, group_index=group_index, group_count=group_count)
        if kind == KIND_STRING:
            entry.value = cursor.wide_string()
        elif kind == KIND_BLOCK:
            entry.block = _read_tree(cursor, flavor)
        elif kind != KIND_TEXT:
            raise BlockParError("незнакомый вид записи %d у «%s»" % (kind, name))
        block.entries.append(entry)
    return block


def read_tree(data, flavor=FLAVOR_BLOCK):
    """Разобрать распакованное дерево из байтов."""
    cursor = _Cursor(data)
    block = _read_tree(cursor, flavor)
    if cursor.position != len(data):
        raise BlockParError("после дерева осталось %d лишних байт" % (len(data) - cursor.position))
    return block


def _wide(text):
    return (text or "").encode("utf-16-le") + b"\0\0"


def _write_tree(block, flavor, out):
    if flavor == FLAVOR_BLOCK:
        out.append(b"\x01" if block.sorted_index else b"\x00")
    out.append(struct.pack("<i", len(block.entries)))
    for entry in block.entries:
        if flavor == FLAVOR_BLOCK and block.sorted_index:
            out.append(struct.pack("<ii", entry.group_index, entry.group_count))
        out.append(struct.pack("<B", entry.kind))
        out.append(_wide(entry.name))
        if entry.kind == KIND_STRING:
            out.append(_wide(entry.value))
        elif entry.kind == KIND_BLOCK:
            _write_tree(entry.block, flavor, out)


def write_tree(block, flavor=FLAVOR_BLOCK):
    """Собрать байты дерева обратно."""
    out = []
    _write_tree(block, flavor, out)
    return b"".join(out)


def from_dict(entries, sorted_index=True):
    """Собрать дерево из машинного вида (`Block.to_dict`)."""
    block = Block(sorted_index)
    for item in entries:
        kind = item.get("kind")
        group = item.get("group") or (0, 0)
        if kind == "param":
            entry = Entry(KIND_STRING, item.get("name", ""), value=item.get("value", ""),
                          comment=item.get("comment", ""))
        elif kind == "block":
            child = from_dict(item.get("entries", []), item.get("sorted", True))
            entry = Entry(KIND_BLOCK, item.get("name", ""), block=child,
                          comment=item.get("comment", ""))
        elif kind == "text":
            entry = Entry(KIND_TEXT, comment=item.get("comment", ""))
        else:
            raise BlockParError("незнакомый вид записи: %r" % (kind,))
        entry.group_index, entry.group_count = group[0], group[1]
        block.entries.append(entry)
    return block


# -- текстовый вид --------------------------------------------------------

COMMENT_MARK = "//"
INDENT = "    "


def _split_comment(line):
    index = line.find(COMMENT_MARK)
    if index < 0:
        return line, ""
    return line[:index], line[index:]


def _parse_into(block, lines, cursor, initial=""):
    pending = initial.strip()
    while True:
        if not pending:
            if cursor[0] >= len(lines):
                return
            pending = lines[cursor[0]].strip()
            cursor[0] += 1
        text, comment = _split_comment(pending)
        text = text.strip()
        pending = ""
        if "{" in text:
            head, rest = text.split("{", 1)
            name = head.strip()
            if not name:
                raise BlockParError("блок без имени: «%s»" % text)
            sorted_index = True
            if name.endswith("^"):
                name = name[:-1].strip()
            elif name.endswith("~"):
                sorted_index = False
                name = name[:-1].strip()
            if "=" in name:
                name = name.split("=", 1)[0].strip()
            child = block.add_block(name, sorted_index, comment)
            _parse_into(child, lines, cursor, rest)
        elif "}" in text:
            return
        elif "=" in text:
            name, value = text.split("=", 1)
            block.add_param(name.strip(), value, comment)
        elif comment:
            block.entries.append(Entry(KIND_TEXT, comment=comment))


def parse_text(text):
    """Разобрать текстовый вид датника по правилам игры."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    root = Block(sorted_index=True)
    _parse_into(root, lines, [0])
    return root


def decode_text(data):
    """Текст датника из байтов: UTF-16 с меткой порядка или однобайтовый cp1251."""
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    return data.decode("cp1251", "replace")


def render_text(block, level=0):
    """Текстовый вид: отступ в четыре пробела, блоки помечены ^ или ~."""
    out = []
    pad = INDENT * level
    for entry in block.entries:
        if entry.kind == KIND_TEXT:
            out.append(entry.comment)
        elif entry.kind == KIND_STRING:
            out.append("%s%s=%s%s" % (pad, entry.name, entry.value or "", entry.comment))
        else:
            mark = "^" if entry.block.sorted_index else "~"
            out.append("%s%s %s{" % (pad, entry.name, mark))
            out.extend(render_text(entry.block, level + 1))
            out.append("%s}%s" % (pad, entry.comment))
    return out


def text_bytes(block, encoding="utf-16", errors="replace"):
    """Текст в байтах: как у игры — UTF-16LE с меткой порядка и переводом CRLF.

    С `encoding="cp1251"` выходит однобайтовый вид, как писал BlockParEditor 1.9;
    `errors="strict"` тогда даёт UnicodeEncodeError на знаках вне этой кодировки
    вместо тихой замены.
    """
    body = "\r\n".join(render_text(block)) + "\r\n"
    if encoding == "utf-16":
        return b"\xff\xfe" + body.encode("utf-16-le")
    return body.encode(encoding, errors)


def lossy_values(block, prefix=""):
    """Значения, которые текстовый вид потеряет: игра режет всё после `//`."""
    out = []
    for path, value in block.walk(prefix):
        if value and COMMENT_MARK in value:
            out.append((path, value))
    return out


# -- сравнение ------------------------------------------------------------


def diff(left, right):
    """Расхождения двух деревьев: (путь, что было, что стало)."""
    left_items = OrderedDict(left.walk())
    right_items = OrderedDict(right.walk())
    changes = []
    for path, value in left_items.items():
        if path not in right_items:
            changes.append((path, value, None))
        elif right_items[path] != value:
            changes.append((path, value, right_items[path]))
    for path, value in right_items.items():
        if path not in left_items:
            changes.append((path, None, value))
    return changes
