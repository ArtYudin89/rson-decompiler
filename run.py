#!/usr/bin/env python3
"""Space Rangers HD SCR→RSON decompiler runner.

Examples:
  # RSON рядом с бинарником:
  python run.py path/to/Mods

  # RSON в отдельный каталог (decompile_result/<ScriptName>/):
  python run.py path/to/Mods --out-dir decompile_result

  # С контрольной сборкой через RScript.exe:
  python run.py path/to/Mods --out-dir decompile_result --check

  # Один файл:
  python run.py path/to/Mod_Example.scr --out-dir decompile_result --check

  # BlockPar dat<->txt (обёртка над BlockParEditor.exe --cli --convert;
  # направление определяется по расширению source):
  python run.py blockpar path/to/Lang.dat              # -> Lang.txt рядом
  python run.py blockpar path/to/CacheData.txt out/CacheData.dat

  # Батч по каталогу (рекурсивно, результат всегда рядом с исходным):
  python run.py blockpar path/to/Mod            # все *.dat -> *.txt
  python run.py blockpar path/to/Mod --to-dat   # все *.txt -> *.dat

  # PKG-архивы ресурсов (замена pack/unpack из ResEditor):
  python run.py pkg list   DATA/english.pkg
  python run.py pkg unpack DATA/english.pkg out_dir/
  python run.py pkg pack   out_dir/ new.pkg          # zl02-сжатие как в оригинале
  python run.py pkg pack   out_dir/ new.pkg --raw    # без сжатия
"""
import argparse, json, re, shutil, struct, subprocess, sys, os, tempfile, time, logging
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))
import decompiler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_rscript(hint):
    if hint:
        p = Path(hint)
        return p if p.exists() else None
    for name in ['RScript_4.10f/RScript.exe', 'RScript_4.5f/RScript.exe']:
        p = HERE / name
        if p.exists():
            return p
    return None


def _find_blockpar(hint):
    if hint:
        p = Path(hint)
        return p if p.exists() else None
    for name in ['BlockParEditor_1.9/BlockParEditor.exe']:
        p = HERE / name
        if p.exists():
            return p
    return None


def _is_text_dialogs(path):
    """Return True if path is a dialogs file with actual text content.

    Skip files whose values look like CT key references (e.g. 'Script.Mod.6')
    because passing those to RScript may cause hangs.
    """
    try:
        raw = path.read_bytes()
        if raw[:2] == b'\xff\xfe':
            text = raw[2:].decode('utf-16-le')
        elif raw[:2] == b'\xfe\xff':
            text = raw[2:].decode('utf-16-be')
        else:
            text = raw.decode('utf-8', errors='replace')
        for line in text.split('\n'):
            line = line.strip()
            if '=' not in line:
                continue
            val = line.split('=', 1)[1].strip()
            if not val:
                continue
            return not re.match(r'^Script\.', val)
        return True
    except Exception:
        return False


def _convert_lang_dat(lang_dat, blockpar, out_txt):
    """Convert Lang.dat → Lang.txt using BlockParEditor. Returns True on success."""
    if blockpar is None or not lang_dat.exists():
        return False
    try:
        subprocess.run(
            [str(blockpar), '--cli', '--convert', str(lang_dat), str(out_txt)],
            capture_output=True, timeout=30,
        )
        return out_txt.exists()
    except Exception:
        return False


def _run_blockpar(blockpar, source, dest=None, timeout=60):
    """Convert a BlockPar file with BlockParEditor.exe --cli --convert.

    Direction is chosen by BlockParEditor from the source extension:
    .dat → .txt (decode) and .txt → .dat (encode). `dest` is optional; when
    omitted the tool writes next to `source` with the swapped extension.

    Like RScript, BlockParEditor is a Delphi GUI app: on bad input it pops a
    MODAL error box ('Error open dat', 'Runtime error 217' on exit, …) that
    would hang the process until timeout. We reuse the _dlgwatch pattern from
    _run_rscript to press OK and capture the real error text. Falls back to a
    plain timed subprocess when _dlgwatch is unavailable.

    Returns (ok: bool, message: str) — message is the output path on success
    or the captured error text on failure.
    """
    source = Path(source)
    if not source.exists():
        return False, f'source not found: {source}'
    if dest is None:
        out = source.with_suffix('.txt' if source.suffix.lower() == '.dat' else '.dat')
    else:
        out = Path(dest)
    args = [str(blockpar), '--cli', '--convert', str(source)]
    if dest is not None:
        args.append(str(out))

    start = time.time()
    err = ''
    try:
        import _dlgwatch
    except Exception:
        _dlgwatch = None
    try:
        if _dlgwatch is None:
            # No dialog watcher: a modal error box would hang until timeout.
            try:
                subprocess.run(args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=timeout)
            except subprocess.TimeoutExpired:
                err = 'timeout (modal dialog?)'
        else:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            deadline = time.time() + timeout
            seen, dialogs = set(), []
            while True:
                rc = proc.poll()
                for hwnd in _dlgwatch._find_dialogs(proc.pid):
                    txt = _dlgwatch._dialog_text(hwnd)
                    if txt not in seen:
                        seen.add(txt)
                        if 'Runtime error' not in txt:
                            dialogs.append(txt)
                    _dlgwatch._press_ok(hwnd)
                if rc is not None:
                    break
                if time.time() > deadline:
                    proc.kill(); proc.wait(5)
                    break
                time.sleep(0.15)
            if dialogs:
                err = '; '.join(dialogs)[:300]
    except Exception as e:
        return False, str(e)

    # Success = the target was (re)written by this run and no error dialog showed.
    # Comparing against the run's start time detects a fresh write even when the
    # target already existed (e.g. decoding over an old sibling .txt).
    if out.exists() and out.stat().st_mtime >= start - 2 and not err:
        return True, str(out)
    return False, err or f'no output written ({out.name})'


def _find_lang_files(scr_file, stop_at):
    """Walk up from scr_file toward stop_at looking for Lang.dat and Lang.txt.

    Checks <dir>/CFG/Rus/Lang.dat|txt first, then <dir>/Lang.dat|txt directly.
    Returns (lang_dat_path_or_None, lang_txt_path_or_None).
    """
    lang_dat = lang_txt = None
    d = scr_file.parent
    while True:
        for subdir in (d / 'CFG' / 'Rus', d):
            if lang_dat is None:
                c = subdir / 'Lang.dat'
                if c.exists():
                    lang_dat = c
            if lang_txt is None:
                c = subdir / 'Lang.txt'
                if c.exists():
                    lang_txt = c
        if d == stop_at or d == d.parent:
            break
        d = d.parent
    return lang_dat, lang_txt


def _run_rscript(rscript, rson_path, out_scr, lang_txt=None, timeout=30):
    """Run RScript.exe --cli.

    The third parameter (dialogs file) is WRITTEN BY RScript: it receives the
    'newkey=text' table of all CT keys RScript assigned during compilation
    (input content is ignored — verified with a 500-offset key test).
    Returns (rc, error_message_or_empty, writeback_dict).
    writeback_dict maps int key -> text (with RScript's <br> escaping kept).
    """
    tmp_dialogs = out_scr.with_suffix('.dialogs.txt')
    tmp_dialogs.write_bytes(b'')
    rc, err = 0, ''
    try:
        # RScript shows MODAL dialogs: a harmless 'Runtime error 217' on exit
        # (the reason every run used to "hang" until timeout) and real error
        # boxes (syntax errors, Group-Planet, EInvalidCast...). _dlgwatch
        # presses OK and captures their text.
        import _dlgwatch
        proc = subprocess.Popen(
            [str(rscript), '--cli', '-b', '-f', str(rson_path), str(out_scr), str(tmp_dialogs)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout
        dialogs = []
        seen = set()
        while True:
            rc = proc.poll()
            for hwnd in _dlgwatch._find_dialogs(proc.pid):
                txt = _dlgwatch._dialog_text(hwnd)
                if txt not in seen:
                    seen.add(txt)
                    if 'Runtime error 217' not in txt:
                        dialogs.append(txt)
                _dlgwatch._press_ok(hwnd)
            if rc is not None:
                break
            if time.time() > deadline:
                proc.kill()
                proc.wait(5)
                rc = -1
                break
            time.sleep(0.2)
        if dialogs:
            err = '; '.join(dialogs)[:300]
    except Exception as e:
        rc, err = -2, str(e)
    writeback = {}
    try:
        raw = tmp_dialogs.read_bytes()
        if raw[:2] == b'\xff\xfe':
            text = raw[2:].decode('utf-16-le')
        else:
            text = raw.decode('utf-8', errors='replace')
        for line in text.split('\r\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                if k.strip().isdigit():
                    writeback[int(k.strip())] = v
    except Exception:
        pass
    finally:
        if tmp_dialogs.exists():
            tmp_dialogs.unlink()
    return rc, err, writeback


# ---------------------------------------------------------------------------
# Binary post-processor: restore original dialog slot numbers and variable names
# ---------------------------------------------------------------------------

def _find_ref_spans(data, script_name):
    """Find CT-reference spans in an SCR binary, in occurrence order.

    Returns list of (start, end, key, kind) byte spans. Each span covers the
    outermost CT(...) call (including a CT(CT(..)) double wrap) or the whole
    DText(...)/DAnswer(...) call when the ref is inside one.
    kind is 'dtext', 'danswer' or 'ct'.

    The file is NOT globally 2-byte aligned (sections shift alignment), so all
    decoding is done in local windows anchored at the needle match.
    """
    needle = f'"Script.{script_name}.'.encode('utf-16-le')
    call_heads = ('DText(', 'DAnswer(')
    spans = []
    pos = 0
    while True:
        idx = data.find(needle, pos)
        if idx < 0:
            break
        wstart = max(0, idx - 400)
        head = data[wstart:idx].decode('utf-16-le', errors='replace')
        tail = data[idx:idx + 200].decode('utf-16-le', errors='replace')
        m = re.match(r'"Script\.\w+\.(\d+)"', tail)
        if not m:
            pos = idx + 2
            continue
        key = int(m.group(1))

        def call_end(start_bytes):
            full = data[start_bytes:start_bytes + 4000].decode('utf-16-le', errors='replace')
            dep = 0
            for i, ch in enumerate(full):
                if ch == '(':
                    dep += 1
                elif ch == ')':
                    dep -= 1
                    if dep == 0:
                        return start_bytes + (i + 1) * 2
                elif ch == chr(0):
                    break
            return -1

        # Enclosing DText(/DAnswer( call: must start before the ref (within
        # the same null-terminated string) and close after it.
        kind = 'ct'
        start = end = None
        for h in call_heads:
            p = head.rfind(h)
            if p < 0 or chr(0) in head[p:]:
                continue
            s = wstart + p * 2
            e = call_end(s)
            if e > idx:
                kind = 'dtext' if h == 'DText(' else 'danswer'
                start, end = s, e
                break
        if start is None:
            # outermost CT( / CT(CT( directly before the ref
            m_ct = re.search(r'(CT\(CT\(|CT\()$', head)
            if not m_ct:
                pos = idx + 2
                continue
            start = wstart + m_ct.start() * 2
            end = call_end(start)
            if end <= idx:
                pos = idx + 2
                continue
        spans.append((start, end, key, kind))
        pos = max(end, idx + 2)
    return spans


def _patch_dialog_codes(comp_path, rson_data, orig_path=None, writeback=None):
    """Align the compiled SCR with the original binary by CT-call occurrence.

    RScript re-keys every CT text sequentially in its own processing order,
    double-wraps pass-through CT(...) calls, CT-wraps string literals (adding
    Format(CT(k),"<0>",0) for texts with placeholders), and drops variables in
    DText/DAnswer args. None of this is controllable from the RSON side, so we
    restore the original bytes span-by-span:

      * comp CT(CT(...)) double / DText / DAnswer  -> paired 1:1 with the
        original's span of the same kind, replaced with the original bytes;
      * comp single CT(...) outside dialog calls   -> an RScript-generated wrap
        of a literal that the original kept plain; restored from the writeback
        dialogs table ('newkey=text') that RScript wrote during compilation.

    Strings in nt-format binaries are null-terminated (no length prefixes), so
    size-changing replacements are safe. Only applies to nt-format binaries.
    Bails out (returns False, file untouched) on any alignment mismatch.
    """
    comp = comp_path.read_bytes()
    if len(comp) < 12:
        return False
    h0, h1, h2 = struct.unpack_from('<III', comp)
    # nt binaries use null-terminated strings (size-changing edits are free);
    # kavscr (h2 in 1..2) uses length-prefixed strings, so every in-string
    # edit must also fix the enclosing length prefix (_fix_prefix below).
    nt = (h0 == 7) or (h2 == 0) or (h2 >= 3)
    script_name = rson_data.get('ScriptName', '')
    if not script_name or orig_path is None or not orig_path.exists():
        return False
    orig = orig_path.read_bytes()
    writeback = writeback or {}
    disk = comp  # bytes currently on disk

    def _finish(buf):
        refined = _unwrap_literal_formats(bytes(buf), orig, nt=nt)
        if refined != disk:
            comp_path.write_bytes(refined)
            return True
        return False

    # Pre-pass: answers with empty text compile to the short DAnswer('name')
    # form (no CT ref), while the original used DAnswer('name~'+CT(...)).
    # Restore the verbatim original call so span alignment below can pair up.
    comp_ba = bytearray(comp)
    changed = False
    cursors = {}  # short form bytes -> next search offset (per name group)
    for obj in rson_data.get('Visual.Objects', []):
        for a in obj.get('DialogAnswers', []):
            if a.get('Msg'):
                # Non-empty text compiles to the full form — nothing to restore
                # (and a global find() would corrupt another answer's call).
                continue
            od = a.get('_orig_danswer', '')
            m = re.match(r"DAnswer\('([^~']*)~'\+", od)
            m_short = re.fullmatch(r"DAnswer\('([^~']*)'\);?", od)
            if m:
                short = f"DAnswer('{m.group(1)}')".encode('utf-16-le')
                repl = od.encode('utf-16-le')
            elif re.match(r"DAnswer\(\s*(?:Format\()?CT\(", od):
                # Nameless answer: empty text compiles to DAnswer('')
                short = "DAnswer('')".encode('utf-16-le')
                repl = od.encode('utf-16-le')
            elif m_short:
                # The original call IS the short form — nothing to change, but
                # this occurrence must be consumed so the next same-named
                # answer doesn't grab it.
                short = f"DAnswer('{m_short.group(1)}')".encode('utf-16-le')
                repl = None
            else:
                continue
            idx = bytes(comp_ba).find(short, cursors.get(short, 0))
            if idx < 0:
                continue
            if repl is not None:
                comp_ba[idx:idx + len(short)] = repl
                changed = True
                cursors[short] = idx + len(repl)
            else:
                cursors[short] = idx + len(short)
    if changed:
        comp = bytes(comp_ba)

    o_spans = _find_ref_spans(orig, script_name)
    c_spans = _find_ref_spans(comp, script_name)
    if not c_spans:
        return _finish(comp)

    double_ct = 'CT(CT('.encode('utf-16-le')
    fmt_head = 'Format('.encode('utf-16-le')

    patches = []  # (cstart, cend, replacement_bytes)
    oi = 0
    for cs, ce, ckey, ckind in c_spans:
        if ckind == 'ct' and comp[cs:cs + len(double_ct)] != double_ct:
            # RScript-generated CT wrap of a literal -> restore quoted literal.
            text = writeback.get(ckey)
            if text is None:
                return _finish(comp)
            text = text.replace('<br>', chr(13) + chr(10))
            if comp[cs - len(fmt_head):cs] == fmt_head:
                # Format(CT(k),"<0>",0,...) wrapper for placeholder texts
                tail = comp[ce:ce + 400].decode('utf-16-le', errors='replace')
                m_tail = re.match(r'(,"<\d+>",\d+)*\)', tail)
                if m_tail:
                    cs, ce = cs - len(fmt_head), ce + m_tail.end() * 2
            patches.append((cs, ce, f'"{text}"'.encode('utf-16-le')))
            continue
        if oi >= len(o_spans) or o_spans[oi][3] != ckind:
            return _finish(comp)
        os_, oe = o_spans[oi][0], o_spans[oi][1]
        if ckind == 'danswer':
            # Safety: answer names must agree, otherwise the pairing is off
            def _aname(b):
                m2 = re.match(r"DAnswer\('([^~']*)[~']",
                              b.decode('utf-16-le', errors='replace'))
                return m2.group(1) if m2 else ''
            if _aname(comp[cs:cs + 200]) != _aname(orig[os_:os_ + 200]):
                return _finish(comp)
        patches.append((cs, ce, orig[os_:oe]))
        oi += 1
    if oi != len(o_spans):
        return _finish(comp)

    out = bytearray(comp)
    for cs, ce, repl in sorted(patches, key=lambda p: -p[0]):
        out[cs:ce] = repl
    return _finish(out)


def _fix_len_prefix(buf, cs, ce, delta_chars):
    """Adjust the length prefix of the wstring enclosing [cs, ce).

    Length-prefixed wstrings store a u32 char count followed by UTF-16 data.
    Scan backward from cs for a plausible prefix: u32 L at p such that the
    string [p+4, p+4+2L) covers the edited span. Returns True on success.
    """
    for p in range(cs - 4, max(-1, cs - 200000), -1):
        L = struct.unpack_from('<I', buf, p)[0]
        if L == 0 or L > 2_000_000:
            continue
        end = p + 4 + 2 * L
        if p + 4 <= cs and end >= ce and end <= len(buf):
            struct.pack_into('<I', buf, p, L + delta_chars)
            return True
    return False


def _unwrap_literal_formats(comp, orig, nt=True):
    """Diff-driven refinement: unwrap Format(<literal>,"<0>",0,...) wrappers.

    RScript 4.10f wraps quoted literals containing <i> placeholders in
    Format(lit,"<0>",0,...) even when no CT is involved, so these sites carry
    no Script ref and are invisible to span alignment. At each first-diff
    position, if comp has such a wrapper where orig has the bare literal
    (verified byte-for-byte), unwrap it and continue.
    """
    fmt_head = 'Format('.encode('utf-16-le')
    out = bytearray(comp)
    # h0 is the writing-compiler's format version: RScript 4.10f always emits
    # 8 while old originals carry 7. Pure metadata — restore and let the
    # content bytes be judged on their own.
    if (len(out) >= 4 and len(orig) >= 4
            and orig[:4] == b'\x07\x00\x00\x00' and out[:4] == b'\x08\x00\x00\x00'):
        out[:4] = orig[:4]
    for _ in range(10000):  # hard stop against non-advancing loops
        n = min(len(out), len(orig))
        # Skip the header: h1 (var-section offset) legitimately differs until
        # body edits below settle; it is restored by the final h1 check.
        d = next((i for i in range(12, n) if out[i] != orig[i]), n)
        if d >= n:
            break
        # Zero-padding width differs between compiler versions (extra/missing
        # zero record fields). Equalize the zero run when that resyncs the
        # streams over a 128-byte window.
        if out[d] == 0 or orig[d] == 0:
            zc = zo = 0
            while d + zc < len(out) and out[d + zc] == 0:
                zc += 1
            while d + zo < len(orig) and orig[d + zo] == 0:
                zo += 1
            if zc != zo and bytes(out[d + zc:d + zc + 128]) == bytes(orig[d + zo:d + zo + 128]):
                if zc > zo:
                    del out[d:d + (zc - zo)]
                else:
                    out[d:d] = b'\x00' * (zo - zc)
                continue
        # Whitespace/padding skew: extra '  ' empty-op chunks, indent shifts
        # from re-indented code, or padding fields. Delete from comp (or copy
        # from orig) a short run of spaces/zeros when that resyncs the streams.
        # The 16-byte window allows chains of per-line indent fixes.
        ws_fixed = False
        WS = (0x00, 0x20, 0x0d, 0x0a, 0x2f, 0x3b)  # NUL space CR LF '/' ';' (dummy '//' global slot)
        for k in range(1, 64):
            if (all(b in WS for b in out[d:d + k])
                    and bytes(out[d + k:d + k + 6]) == bytes(orig[d:d + 6])):
                del out[d:d + k]
                ws_fixed = True
                break
            if (all(b in WS for b in orig[d:d + k])
                    and bytes(out[d:d + 6]) == bytes(orig[d + k:d + k + 6])):
                out[d:d] = orig[d:d + k]
                ws_fixed = True
                break
        if ws_fixed:
            continue
        # v7 record extra fields: old-format originals store extra dwords in
        # ship/group records (e.g. [1, A, 0, A, 0] or [A, A]) that v8 output
        # omits. Re-insert the original block when it is composed only of
        # 0/1/A/B dwords (no novel data) and the streams resync after it.
        a_val = bytes(out[d:d + 4])
        b_val = bytes(out[d + 4:d + 8])
        allowed = {b'\x00\x00\x00\x00', b'\x01\x00\x00\x00', a_val, b_val}
        inserted = False
        for k in range(4, 41, 4):
            block = bytes(orig[d:d + k])
            if len(block) < k or any(block[i:i + 4] not in allowed
                                     for i in range(0, k, 4)):
                break
            if bytes(orig[d + k:d + k + 24]) == bytes(out[d:d + 24]):
                out[d:d] = block
                inserted = True
                break
        if inserted:
            continue
        # Float ULP drift: RScript parses decimal Dist values to a float that
        # may differ from the original by 1 ulp. Restore when the dword looks
        # like a float (plausible exponent byte) and the rest resyncs.
        # The high byte of a float32 is the sign+exponent; for the small
        # magnitudes used by place Dist (~1e-2..1e2) it is one of 0x3x/0x4x
        # (positive) or 0xbx/0xcx (negative).
        if (abs(out[d] - orig[d]) == 1
                and bytes(out[d + 1:d + 4]) == bytes(orig[d + 1:d + 4])
                and ((0x34 <= orig[d + 3] <= 0x4c)
                     or (0xb4 <= orig[d + 3] <= 0xcc))
                and bytes(out[d + 4:d + 20]) == bytes(orig[d + 4:d + 20])):
            out[d] = orig[d]
            continue
        # Escaped quotes: RScript drops the backslash from \\' escapes inside
        # string literals when recompiling. Re-insert from the original.
        bs = '\\'.encode('utf-16-le')
        if (bytes(orig[d:d + 2]) == bs
                and bytes(out[d:d + 14]) == bytes(orig[d + 2:d + 16])):
            out[d:d] = bs
            continue
        # Negative-value clamp: RScript writes -1 (0xFFFFFFFF) where the
        # original had another negative dword (e.g. Priority -2). Restore the
        # original value when the following bytes resync.
        if (bytes(out[d:d + 4]) == b'\xff\xff\xff\xff'
                and len(orig) >= d + 4 and orig[d + 3] == 0xff
                and bytes(out[d + 4:d + 20]) == bytes(orig[d + 4:d + 20])):
            out[d:d + 4] = orig[d:d + 4]
            continue
        # Placeholder renumbering: RScript rewrites "<N>" arg names (e.g.
        # "<1>" -> "<0>"). Restore the original digit when both sides have a
        # digit inside <...> at the same position.
        lt = '<'.encode('utf-16-le')
        gt = '>'.encode('utf-16-le')
        if (bytes(out[d - 2:d]) == lt and bytes(orig[d - 2:d]) == lt
                and out[d + 1] == 0 and orig[d + 1] == 0
                and chr(out[d]).isdigit() and chr(orig[d]).isdigit()):
            # Extend over multi-digit numbers (e.g. orig "<10>" vs comp "<0>")
            eo = d
            while eo + 1 < len(orig) and orig[eo + 1] == 0 and chr(orig[eo]).isdigit():
                eo += 2
            ec = d
            while ec + 1 < len(out) and out[ec + 1] == 0 and chr(out[ec]).isdigit():
                ec += 2
            if bytes(orig[eo:eo + 2]) == gt and bytes(out[ec:ec + 2]) == gt:
                out[d:ec] = orig[d:eo]
                continue
        # Generic structural skew (last resort before Format rules): the old
        # compiler emits extra record fields (e.g. star NoKling data arrays)
        # that RScript 4.10f omits, and vice versa. Insert the original bytes
        # (or drop compiled ones) when a strict 24-byte resync confirms it.
        gen_fixed = False
        for k in range(1, 41):
            if bytes(orig[d + k:d + k + 24]) == bytes(out[d:d + 24]):
                out[d:d] = orig[d:d + k]
                gen_fixed = True
                break
            if bytes(out[d + k:d + k + 24]) == bytes(orig[d:d + 24]):
                del out[d:d + k]
                gen_fixed = True
                break
        if gen_fixed:
            continue
        # The wrapper must start at or just before the diff: comp '...Format('
        # vs orig '...<lit>'. Since bytes before d are equal, the Format( head
        # lies within [d - len(fmt_head), d].
        win_lo = max(0, d - len(fmt_head))
        idx = bytes(out[win_lo:d + len(fmt_head) + 2]).rfind(fmt_head)
        if idx < 0:
            break
        fs = win_lo + idx
        window = bytes(out[fs:fs + 2000]).decode('utf-16-le', errors='replace')
        # Wrapped literal: Format(<lit>,"<0>",0,...) -> <lit>
        m = re.match(r'Format\((\'[^\']*\'|"[^"]*")((?:,"<\d+>",\d+)+)\)', window)
        lit = None
        if m:
            lit = m.group(1)
        else:
            # Normalized placeholder: Format("<0>","<0>",N) -> "<N>"
            m = re.match(r'Format\("<0>","<0>",(\d+)\)', window)
            if m:
                lit = f'"<{m.group(1)}>"'
        if lit is None:
            break
        lit_b = lit.encode('utf-16-le')
        if bytes(orig[fs:fs + len(lit_b)]) != lit_b:
            break
        out[fs:fs + len(m.group(0)) * 2] = lit_b
    # Body-level edits before the var section change its offset (h1). When
    # everything except h1 now matches the original, the original h1 is by
    # definition the correct one.
    if (len(out) == len(orig) and bytes(out[:4]) == bytes(orig[:4])
            and bytes(out[8:]) == bytes(orig[8:])):
        out[4:8] = orig[4:8]
    return bytes(out)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def _dedupe_name(scr, out_dir, name):
    """Qualify the output dir name when another mod's same-named script
    already occupies out_dir/<name>/ (e.g. six different PC_final.scr)."""
    if out_dir is None:
        return name
    marker = out_dir / name / (name + '.scr')
    if marker.exists():
        try:
            if marker.read_bytes() != scr.read_bytes():
                parts = scr.parts
                # .../<ModFolder>/DATA/Script/<x>.scr -> ModFolder
                tag = parts[-4] if len(parts) >= 4 else 'alt'
                return f'{name}@{tag}'
        except OSError:
            pass
    return name


def process_scr(scr, base_dir, out_dir, rscript, blockpar, check, timeout):
    """Decompile one SCR file. Returns a short status string."""
    name = _dedupe_name(scr, out_dir, scr.stem)

    # --- Step 2.1: Find lang files and resolve a text-format Lang.txt for decompilation ---
    lang_dat, lang_txt = _find_lang_files(scr, base_dir)

    # Decompiler needs text-format Lang.txt (load_lang cannot parse binary Lang.dat).
    # If only Lang.dat exists, decode it with BlockParEditor before decompiling.
    _temp_lang = None   # temp file path; cleaned up after decompilation

    if lang_txt:
        lang_for_decomp = str(lang_txt)
    elif lang_dat and blockpar:
        if out_dir is not None:
            # Decode directly into the output dir (avoids a second conversion later)
            mod_dir = out_dir / name
            mod_dir.mkdir(parents=True, exist_ok=True)
            _dest_lang_txt = mod_dir / 'Lang.txt'
            if _convert_lang_dat(lang_dat, blockpar, _dest_lang_txt):
                lang_for_decomp = str(_dest_lang_txt)
            else:
                lang_for_decomp = None
        else:
            # Alongside mode: convert to a temp file, delete after decompilation
            fd, _tmp = tempfile.mkstemp(suffix='.txt', prefix='_lang_')
            os.close(fd)
            _temp_lang = Path(_tmp)
            if _convert_lang_dat(lang_dat, blockpar, _temp_lang):
                lang_for_decomp = str(_temp_lang)
            else:
                try: _temp_lang.unlink()
                except OSError: pass
                _temp_lang = None
                lang_for_decomp = None
    else:
        lang_for_decomp = None

    # --- Determine output paths ---
    if out_dir is None:
        # Alongside mode: RSON next to .scr, no file copying
        rson_out  = scr.with_suffix('.rson')
        check_out = scr.parent / (name + '_check.scr') if check else None
        copy_mode = False
        lang_txt_for_check = lang_txt
    else:
        # Separate mode: out_dir/<ScriptName>/
        mod_dir = out_dir / name
        mod_dir.mkdir(parents=True, exist_ok=True)
        rson_out  = mod_dir / (name + '.rson')
        check_out = mod_dir / (name + '_check.scr') if check else None
        copy_mode = True
        lang_txt_for_check = None  # will be set after copying/converting

    # --- Step 2.2: Decompile (lang_for_decomp is now always text-format or None) ---
    try:
        decompiler.decompile(str(scr), str(rson_out), lang_for_decomp)
    finally:
        if _temp_lang is not None:
            try: _temp_lang.unlink()
            except OSError: pass

    if not rson_out.exists():
        return 'SKIP'   # unsupported format

    # --- Copy / convert lang files to output dir ---
    if copy_mode:
        shutil.copy2(scr, mod_dir / (name + '.scr'))

        out_lang_txt = None

        if lang_dat:
            shutil.copy2(lang_dat, mod_dir / 'Lang.dat')

        if lang_txt:
            out_lang_txt = mod_dir / 'Lang.txt'
            shutil.copy2(lang_txt, out_lang_txt)
        else:
            # Lang.txt may already have been decoded into mod_dir before decompilation
            _decoded = mod_dir / 'Lang.txt'
            if _decoded.exists():
                out_lang_txt = _decoded

        lang_txt_for_check = out_lang_txt

    # --- Round-trip check ---
    # Note: RScript ignores the dialogs-file *input* (verified experimentally);
    # it only writes its own key table there, which the patcher consumes.
    if check:
        if rscript is None:
            return 'OK (check skipped: RScript.exe not found)'
        # Remove stale check output to avoid false results from a previous run
        if check_out and check_out.exists():
            check_out.unlink()
        rc, err, writeback = _run_rscript(rscript, rson_out, check_out,
                                          timeout=timeout)
        # Check output regardless of rc — RScript may write before timeout/error
        if check_out and check_out.exists():
            # Align compiled binary with the original (CT keys, literals, args)
            try:
                rson_data = json.loads(rson_out.read_text(encoding='utf-8'))
                _patch_dialog_codes(check_out, rson_data, orig_path=scr,
                                    writeback=writeback)
            except Exception:
                pass
            orig = scr.read_bytes()
            comp = check_out.read_bytes()
            if orig == comp:
                return 'OK check=MATCH'
            for i, (a, b) in enumerate(zip(orig, comp)):
                if a != b:
                    return f'OK check=MISMATCH@{i}'
            return f'OK check=len_diff {len(orig)}vs{len(comp)}'
        if rc == 217:
            return 'OK check=NO_OUTPUT'
        return f'OK check=rc{rc}' + (f' ({err})' if err else '')

    return 'OK'


# ---------------------------------------------------------------------------
# `blockpar` subcommand: dat<->txt via BlockParEditor.exe
# ---------------------------------------------------------------------------

def cmd_blockpar(argv):
    """Handle `python run.py blockpar <source> [dest]`.

    source may be a single .dat/.txt file or a directory. For a directory the
    conversion is a recursive batch (like the main runner's rglob('*.scr')):
    every *.dat → *.txt sibling by default, or every *.txt → *.dat with
    --to-dat. The result is always written next to the source file.
    """
    ap = argparse.ArgumentParser(
        prog='run.py blockpar',
        description='Конвертация BlockPar dat<->txt через BlockParEditor.exe '
                    '(--cli --convert). Одиночный файл: направление по расширению '
                    '(.dat→.txt, .txt→.dat). Каталог: рекурсивный батч, результат '
                    'всегда рядом с исходным файлом.')
    ap.add_argument('source',
                    help='Файл .dat/.txt (CacheData/Main/Lang…) ИЛИ каталог для '
                         'рекурсивного батча')
    ap.add_argument('dest', nargs='?', default=None,
                    help='Путь результата — только для одиночного файла '
                         '(по умолчанию рядом с source со сменой расширения)')
    ap.add_argument('--to-dat', action='store_true',
                    help='Батч по каталогу: конвертировать все *.txt → *.dat '
                         '(по умолчанию все *.dat → *.txt)')
    ap.add_argument('--blockpar', default=None, metavar='EXE',
                    help='Путь к BlockParEditor.exe (авто-поиск если не задан)')
    ap.add_argument('--timeout', type=int, default=60, metavar='SEC',
                    help='Таймаут на конвертацию одного файла (по умолчанию 60)')
    a = ap.parse_args(argv)

    blockpar = _find_blockpar(a.blockpar)
    if blockpar is None:
        print('ERROR: BlockParEditor.exe не найден. Укажите путь через --blockpar '
              'или положите BlockParEditor_1.9/ рядом с run.py.', file=sys.stderr)
        sys.exit(1)

    src = Path(a.source)

    # --- Directory: recursive batch, result always next to each source file ---
    if src.is_dir():
        if a.dest is not None:
            print('ERROR: для каталога аргумент dest не поддерживается — каждый '
                  'файл кладётся рядом с исходным.', file=sys.stderr)
            sys.exit(1)
        pattern = '*.txt' if a.to_dat else '*.dat'
        files = sorted(src.rglob(pattern))
        if not files:
            print(f'ERROR: файлы {pattern} не найдены в {src} (рекурсивно)',
                  file=sys.stderr)
            sys.exit(1)
        ok = fail = 0
        for f in files:
            good, msg = _run_blockpar(blockpar, f, None, a.timeout)
            try:
                rel = f.relative_to(src)
            except ValueError:
                rel = f
            if good:
                ok += 1
                print(f'  OK    {rel} -> {Path(msg).name}')
            else:
                fail += 1
                print(f'  FAIL  {rel}: {msg}')
        print()
        print(f'Итого: OK={ok}  FAIL={fail}  '
              f'({pattern} → {"dat" if a.to_dat else "txt"}, рекурсивно, рядом с исходным)')
        sys.exit(2 if fail else 0)

    # --- Single file: direction by extension ---
    if not src.exists():
        print(f'ERROR: {src} не найден', file=sys.stderr)
        sys.exit(1)
    ok, msg = _run_blockpar(blockpar, src, a.dest, a.timeout)
    if ok:
        print(f'OK: {src.name} -> {msg}')
    else:
        print(f'FAIL: {src}: {msg}', file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description='Space Rangers HD SCR→RSON decompiler runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('dir',
                    help='Каталог со скриптами (рекурсивный поиск .scr) или один .scr файл')
    ap.add_argument('--out-dir', default=None, metavar='DIR',
                    help='Отдельный каталог для результатов. Структура: DIR/<ScriptName>/'
                         '<scr+rson+Lang.dat+Lang.txt+check.scr>. '
                         'Если не задан — RSON сохраняется рядом с .scr (alongside mode).')
    ap.add_argument('--check', action='store_true',
                    help='Контрольная сборка: RSON → RScript.exe → .scr, сравнить с оригиналом')
    ap.add_argument('--rscript', default=None, metavar='EXE',
                    help='Путь к RScript.exe (авто-поиск в директории скрипта если не задан)')
    ap.add_argument('--blockpar', default=None, metavar='EXE',
                    help='Путь к BlockParEditor.exe для конвертации Lang.dat→Lang.txt '
                         '(авто-поиск в директории скрипта если не задан)')
    ap.add_argument('--timeout', type=int, default=30, metavar='SEC',
                    help='Таймаут RScript.exe на файл (по умолчанию 30)')
    ap.add_argument('-v', '--verbosity', default='brief',
                    choices=['verbose', 'brief', 'errors'],
                    help='Уровень лога: verbose/brief/errors (по умолчанию brief)')
    ap.add_argument('--log', default=None, metavar='FILE',
                    help='Записать лог в файл')
    a = ap.parse_args()

    decompiler.setup_logging(log_file=a.log, verbosity=a.verbosity)

    src = Path(a.dir)
    out_dir = Path(a.out_dir) if a.out_dir else None
    rscript  = _find_rscript(a.rscript)  if a.check else None
    blockpar = _find_blockpar(a.blockpar)

    if a.check and rscript is None:
        print('WARN: --check задан, но RScript.exe не найден. Проверка будет пропущена.')
    if blockpar is None:
        print('INFO: BlockParEditor.exe не найден — Lang.dat не будет конвертирован в Lang.txt автоматически.')

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    # --- Collect SCR files ---
    if src.is_file():
        scr_files = [src]
        base_dir = Path(src.anchor)  # search all the way up to drive root
    elif src.is_dir():
        scr_files = sorted(src.rglob('*.scr'))
        base_dir = src
    else:
        print(f'ERROR: {src} не найден', file=sys.stderr)
        sys.exit(1)

    if not scr_files:
        print(f'ERROR: .scr файлы не найдены в {src}', file=sys.stderr)
        sys.exit(1)

    # --- Process ---
    ok = skip = fail = 0
    check_match = check_mismatch = check_fail = 0

    for scr in scr_files:
        try:
            status = process_scr(scr, base_dir, out_dir, rscript, blockpar, a.check, a.timeout)
        except Exception as e:
            status = f'ERR: {e}'

        try:
            rel = scr.relative_to(base_dir)
        except ValueError:
            rel = scr
        if status == 'SKIP':
            skip += 1
            print(f'  SKIP  {rel}')
        elif status.startswith('ERR'):
            fail += 1
            print(f'  FAIL  {rel}  {status}')
        else:
            ok += 1
            marker = 'OK   '
            if 'check=MATCH' in status:
                check_match += 1
            elif 'check=MISMATCH' in status:
                check_mismatch += 1
                marker = 'WARN '
            elif 'check=' in status and 'check=MATCH' not in status:
                check_fail += 1
                marker = 'WARN '
            print(f'  {marker} {rel}  [{status}]')

    # --- Summary ---
    print()
    print(f'Итого: OK={ok}  SKIP={skip}  FAIL={fail}', end='')
    if a.check:
        print(f'  |  Проверка: MATCH={check_match}  MISMATCH={check_mismatch}  FAIL={check_fail}', end='')
    print()

    if out_dir:
        print(f'Результаты: {out_dir.resolve()}')
    else:
        print('Режим: RSON рядом с .scr (alongside)')


if __name__ == '__main__':
    # Subcommands: `blockpar` (dat<->txt), `pkg` (.pkg pack/unpack);
    # everything else is the SCR→RSON runner.
    if len(sys.argv) > 1 and sys.argv[1] == 'blockpar':
        cmd_blockpar(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'pkg':
        import srpkg
        sys.exit(srpkg.main(sys.argv[2:]))
    else:
        main()
