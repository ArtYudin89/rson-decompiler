# SCR → RSON Decompiler (Space Rangers HD)

Декомпилятор бинарных скриптов **Space Rangers HD: A War Apart** (`*.scr`) обратно в
текстовый формат визуального редактора **RSON** (`*.rson`). Восстанавливает группы,
состояния, диалоги, переменные, код и тексты, пригодные для повторной компиляции
обратно в `.scr` через `RScript.exe`.

> Подробный технический контекст (формат бинарника, разбор по версиям, известные
> расхождения round-trip) — в [CONTEXT.md](CONTEXT.md).

## Состав

```
decompiler.py     ядро: парсер .scr -> структура -> .rson (~2100 строк)
run.py            CLI-раннер: батч по каталогу, --check (round-trip), Lang.dat->Lang.txt
_dlgwatch.py      авто-закрытие модальных окон RScript.exe при --check (Windows)
validate.py       сверка результата с эталонными .rson
CONTEXT.md        технический контекст формата и парсера
rsons/            эталонные .rson (ground truth для валидации)
Modding_Manual/   справка по формату/функциям скриптов SR HD
```

## Запуск

```bash
# Один файл -> .rson рядом:
python run.py path/to/Mod_Foo.scr

# Каталог (рекурсивно) -> отдельный каталог результатов:
python run.py "path/to/Mods" --out-dir decompile_result

# С контрольной пересборкой (RScript.exe) и сверкой с оригиналом:
python run.py "path/to/Mods" --out-dir decompile_result --check
```

Ключи: `--out-dir DIR`, `--check`, `--rscript EXE`, `--blockpar EXE`, `--timeout SEC`,
`-v {verbose,brief,errors}`, `--log FILE`. См. `python run.py -h`.

## Зависимости

- **Python 3.9+** (только стандартная библиотека; для `--check` на Windows — `pywin32`
  не нужен, используется ctypes в `_dlgwatch.py`).
- Для `--check` (round-trip компиляция) и конвертации `Lang.dat` нужны сторонние
  модинг-утилиты сообщества SR, **не включённые в репозиторий**:
  - `RScript.exe` (RScript 4.10f) — компилятор `.rson` -> `.scr`;
  - `BlockParEditor.exe` — конвертация `Lang.dat` -> `Lang.txt`.

  Положи их рядом (`RScript_4.10f/RScript.exe`, `BlockParEditor_1.9/BlockParEditor.exe`)
  или укажи путь флагами `--rscript` / `--blockpar`. Без них декомпиляция работает,
  пропускается только контрольная пересборка.

## Статус

Декомпилятор покрывает все встреченные варианты формата (`h0` = 6/7/8, kavscr / nt /
preglob, мульти-звёздные паки и т.д.). Парсинг: ERR=0 на тестовом наборе; round-trip
(`--check`) даёт побайтовый MATCH для большинства файлов, оставшиеся расхождения —
различия версий компилятора/заглушки (детали в [CONTEXT.md](CONTEXT.md)).
