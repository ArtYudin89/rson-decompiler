"""srblockpar — чтение и запись датников `.dat` Space Rangers HD своим кодом.

Без внешних зависимостей, python 3.7 и новее. Заменяет вызовы BlockParEditor.

    import srblockpar

    doc = srblockpar.read_dat("CFG/Main.dat")
    print(doc.tree.get("BV.BV"))

    doc.tree.add_param("Свой", "да")
    srblockpar.write_dat("CFG/Main.dat", doc.tree, seed=doc.seed)

Что умеет: обе рамки файла (с внешним заголовком и без), оба вида дерева
(датник BlockPar и дерево данных `CacheData.dat`), текстовый вид по правилам
игры и машинный вид без потерь.

Устройство формата описано в `docs/dat-format.md` рядом с этим пакетом.
Лицензия — GPL v3, см. `LICENSE`.
"""

from .api import (  # noqa: F401
    DatDocument,
    describe,
    dat_to_text,
    kind_for_name,
    read_any,
    read_dat,
    read_json,
    read_text,
    same_tree,
    text_to_dat,
    write_dat,
    write_text,
)
from .blockpar import (  # noqa: F401
    Block,
    BlockParError,
    Entry,
    FLAVOR_BLOCK,
    FLAVOR_DATA,
    KIND_BLOCK,
    KIND_STRING,
    KIND_TEXT,
    decode_text,
    diff,
    from_dict,
    lossy_values,
    parse_text,
    read_tree,
    render_text,
    text_bytes,
    write_tree,
)
from .datfile import (  # noqa: F401
    DEFAULT_SEED,
    DatError,
    KIND_BLOCK as CONTAINER_BLOCK,
    KIND_DATA as CONTAINER_DATA,
)
from .zl import ZLError  # noqa: F401

__version__ = "1.0.0"

__all__ = [
    "read_dat", "write_dat", "read_text", "write_text", "read_json", "read_any",
    "dat_to_text", "text_to_dat", "kind_for_name", "same_tree", "describe",
    "DatDocument", "Block", "Entry", "parse_text", "render_text", "decode_text",
    "text_bytes", "read_tree", "write_tree", "from_dict", "diff", "lossy_values",
    "BlockParError", "DatError", "ZLError", "DEFAULT_SEED",
    "CONTAINER_BLOCK", "CONTAINER_DATA",
    "KIND_TEXT", "KIND_STRING", "KIND_BLOCK", "FLAVOR_BLOCK", "FLAVOR_DATA",
    "__version__",
]
