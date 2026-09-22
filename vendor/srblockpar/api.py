"""Удобный слой над разбором датников: файл на входе — дерево на выходе.

Здесь же правило выбора вида дерева по имени файла: `CacheData.dat` — дерево
данных, всё остальное — датник BlockPar. Проверено на 1539 датниках игры и
модов: 415 файлов с именем CacheData все до одного оказались деревом данных,
и ни одного исключения в другую сторону.
"""

import json
import os
from collections import OrderedDict

from . import blockpar, datfile

FLAVORS = {
    datfile.KIND_BLOCK: blockpar.FLAVOR_BLOCK,
    datfile.KIND_DATA: blockpar.FLAVOR_DATA,
}

CACHE_NAME = "cachedata"


class DatDocument(object):
    """Прочитанный датник: дерево плюс то, как был сложен файл."""

    def __init__(self, tree, container, path=None):
        self.tree = tree
        self.container = container
        self.path = path

    @property
    def kind(self):
        return self.container.kind

    @property
    def seed(self):
        return self.container.seed

    @property
    def outer(self):
        return self.container.outer


def kind_for_name(path):
    """Какое дерево ожидается в файле с таким именем."""
    name = os.path.splitext(os.path.basename(str(path)))[0].lower()
    return datfile.KIND_DATA if name == CACHE_NAME else datfile.KIND_BLOCK


def read_dat(path):
    """Прочитать .dat. Вид дерева определяется по подошедшему ключу, не по имени."""
    with open(path, "rb") as handle:
        raw = handle.read()
    container = datfile.unpack(raw)
    tree = blockpar.read_tree(container.payload, FLAVORS[container.kind])
    return DatDocument(tree, container, path)


def write_dat(path, tree, kind=None, seed=datfile.DEFAULT_SEED, outer=True):
    """Собрать .dat. Вид дерева по умолчанию — по имени файла."""
    kind = kind or kind_for_name(path)
    payload = blockpar.write_tree(tree, FLAVORS[kind])
    data = datfile.pack(payload, kind, seed, outer)
    folder = os.path.dirname(str(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)


def read_text(path):
    """Прочитать текстовый вид датника (UTF-16 с меткой или однобайтовый)."""
    with open(path, "rb") as handle:
        return blockpar.parse_text(blockpar.decode_text(handle.read()))


def write_text(path, tree, ansi=False):
    """Записать текстовый вид. Возвращает (размер, кодировка).

    `ansi=True` — однобайтовый cp1251, как писал BlockParEditor 1.9. Если в
    дереве есть знаки вне cp1251, тихо терять их нельзя: такой файл пишется
    в UTF-16, и кодировка возвращается настоящая.
    """
    encoding = "utf-16"
    if ansi:
        try:
            data = blockpar.text_bytes(tree, "cp1251", "strict")
            encoding = "cp1251"
        except UnicodeEncodeError:
            data = blockpar.text_bytes(tree, "utf-16")
    else:
        data = blockpar.text_bytes(tree, "utf-16")
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data), encoding


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    entries = document["entries"] if isinstance(document, dict) else document
    sorted_index = True
    if isinstance(document, dict):
        container = document.get("container") or {}
        sorted_index = container.get("kind", datfile.KIND_BLOCK) == datfile.KIND_BLOCK
    return blockpar.from_dict(entries, sorted_index)


def read_any(path):
    """Дерево из любого нашего вида: .dat, .txt или .json."""
    lowered = str(path).lower()
    if lowered.endswith(".txt"):
        return read_text(path)
    if lowered.endswith(".json"):
        return read_json(path)
    return read_dat(path).tree


def dat_to_text(source, target=None, ansi=False):
    """Разобрать .dat в .txt. Возвращает (путь, список теряемых значений)."""
    document = read_dat(source)
    target = target or os.path.splitext(str(source))[0] + ".txt"
    write_text(target, document.tree, ansi)
    return target, blockpar.lossy_values(document.tree)


def text_to_dat(source, target=None, kind=None, seed=datfile.DEFAULT_SEED, outer=True):
    """Собрать .dat из .txt. Вид дерева — по имени целевого файла."""
    target = target or os.path.splitext(str(source))[0] + ".dat"
    tree = read_text(source)
    write_dat(target, tree, kind, seed, outer)
    return target


def same_tree(left, right):
    """Одно ли дерево в двух файлах (любого вида)."""
    return list(read_any(left).walk()) == list(read_any(right).walk())


def describe(path):
    """Короткая справка о файле — для журналов и отчётов."""
    document = read_dat(path)
    report = OrderedDict((("path", str(path)),))
    report.update(document.container.describe())
    report["entries"] = len(document.tree.entries)
    report["build_version"] = document.tree.get("BV.BV")
    return report
