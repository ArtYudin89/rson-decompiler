# Конвертер ресурсов Space Rangers HD — правила для Claude

Четыре конвертера: **scr↔rson** (завершён, MATCH 315/321), **pkg** pack/unpack (byte-exact), **blockpar** dat↔txt, **gi** gi/gai/hai→png (декодер). Живой статус — в памяти `scr-decompiler-status`.

## Два клона — дисциплина синхронизации
- `c:\claude_sandbox\rson-decompiler\` (дефис) — git-клон public `ArtYudin89/rson-decompiler`: **код править здесь**, отсюда пушить.
- `c:\claude_sandbox\rson_decompiler\` (подчёркивание) — рабочая копия НЕ под git: здесь игра (`Space Rangers HD A War Apart\DATA\*.pkg`), моды, эталоны, прогоны. Правки кода синхронизировать сюда.

## Публичный репо — гигиена
- `/reference/`, `/ResEditor_*` — в .gitignore: рабочие эталоны, скрины и чужие бинарники (ResEditor — VB6, CLI нет принципиально) не коммитить.
- .scr модов в репо — юзер разрешил (моды публичны), но новый копирайтный контент — только с явного ОК.

## Команды
- Корпус: `python run.py "<Mods dir>" --out-dir decompile_result --check --timeout 10` (коллизии имён → `_dedupe_name`).
- pkg: `python run.py pkg list|unpack|pack|verify` (или `srpkg.py`). Батч по каталогу: `list|unpack|verify` рекурсивно по всем `*.pkg`; `unpack` кладёт каждый архив в `outdir/<rel>.pkg/` (пакет-директория), а `pack <dir> <outdir>` (выход БЕЗ `.pkg`) собирает обратно все `*.pkg/`-директории — round-trip `unpack↔pack`. Направление pack по выходу: `…/x.pkg` → один архив, каталог → батч. Byte-exact правило — см. «Незыблемое».
- gi: `python run.py gi info|decode|encode|verify` (или `srgi.py`) — gi/gai/hai↔PNG, только stdlib+zlib (свой мини-PNG, без Pillow). Все команды принимают каталог → рекурсивный батч по всем `*.gi/*.gai/*.hai` (decode зеркалит дерево в `out/`). `verify` = ре-сериализация заголовка байт-в-байт + инвариант расхода `GILayerHeader.size`. **Энкодер пишет все 6 типов кадра** через `--format`: `argb`(0)/`indexed`(4)/`delta`(5) lossless, `rgb565`(0)/`rle565`(1)/`rle-alpha`(2)/`rle-indexed`(3) с потерями. Гейт для lossy — НЕ байт-round-trip, а `decode(encode(x)) == quantise_gi(x, fmt)`. **GAI с `haveBackground=1` держит ключевой кадр в соседнем `<stem>.gi`** — decode подхватывает его сам (иначе дельты дают контур на прозрачном фоне); `--bg FILE.GI` переопределяет, `--no-bg` отключает, имя фона пишется в `<stem>.times.json`. **Тип кадра 6** (битовая дельта HD-анимаций, в OpenSR его нет) реверснут из `okgf.dll!OKGR_F6_DrawRGBA` и сверен эмуляцией DLL байт-в-байт — спека в `docs/TECHNICAL.md`.
- blockpar: `python run.py blockpar <src|dir> [--to-dat]` — обёртка над BlockParEditor CLI; **реимплемент формата запрещён юзером** (.dat недетерминирован по байтам — random seed; round-trip проверять ПО ТЕКСТУ).
- Headless-компиляция RScript: `run_rscript_watched` из `_dlgwatch.py` (модалки авто-OK; «зависание» rc=-1 = модалка; rc=217 на выходе безвреден).

## Незыблемое по форматам
- Канон вывода = интерактивный SCR-декомпил RScript (эталоны `*_recovery.rson` в корне). Пиксель-в-пиксель координаты НЕдостижимы (в .scr их нет) — воспроизводим структуру. Export*=false и LangDatOut='' в шапке НАМЕРЕННО (иначе редактор перезапишет Lang.dat).
- RScript ре-ключует CT-тексты → байт-идентичность только бинарным патчингом; `_patch_dialog_codes` реконсилит ПОДСТАНОВКИ, не вставки (вставки — UTF-16-редактированием бинаря).
- nt-формат (h0=8) хранит код читаемым UTF-16-LE; код-строки null-terminated во всех форматах; файлы НЕ выровнены глобально по 2 байта.
- pkg перепаковка byte-exact на всём стоковом корпусе (19/19 DATA) — правило ResEditor восстановлено диффингом (9087 файлов, 0 исключений): (1) RAW-vs-ZL02 по имени+размеру, НЕ по сжимаемости — `.jpg/.png` ИЛИ `size<128 Б` → RAW, иначе ZL02 (chunk 64KiB, zlib 9); некоторые `≥128 Б` идут ZL02 даже когда zlib их раздувает; (2) порядок в каталоге — сначала ВСЕ подпапки, потом файлы, каждая группа регистронезависимо (upper); (3) поле fullName апперкейсит и кириллицу cp1251 (`а-я`0xE0-FF→`А-Я`, ё0xB8→Ё0xA8), не только ASCII — важно для модов с русскими именами.
- Спека форматов (scr/pkg, план gi/gai/hai) — `docs/TECHNICAL.md`; эталоны: авторские `_dump.txt` рядом с .scr, `rsons/`.
