"""Validation: parse all SCR files and compare G/S/D against reference RSONs."""
import sys, os, glob, json, re, logging
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
logging.disable(logging.CRITICAL)

import decompiler

def ref_counts(rson_path):
    """Extract G/S/D counts from reference RSON."""
    txt = open(rson_path, encoding='utf-8-sig', errors='replace').read()
    # Format 1: "Groups.Count": N (compact decompiler output)
    if '"Groups.Count"' in txt:
        def get(key):
            m = re.search(r'"' + key + r'"\s*:\s*(\d+)', txt)
            return int(m.group(1)) if m else 0
        return get('Groups.Count'), get('States.Count'), get('Dialogs.Count')
    # Format 2: "Type": "TGroup" / "TState" / "TDialog" (visual editor format)
    g = len(re.findall(r'"Type"\s*:\s*"TGroup"', txt))
    s = len(re.findall(r'"Type"\s*:\s*"TState"', txt))
    d = len(re.findall(r'"Type"\s*:\s*"TDialog"', txt))
    return g, s, d

# Find all SCR files (exclude round-trip temp output)
scrs = sorted(f for f in glob.glob('**/*.scr', recursive=True)
              if '_roundtrip_tmp' not in f.replace('\\', '/')
              and not f.replace('\\', '/').startswith('decompile_result/'))
# Build reference map: basename (no ext) -> rson path
# Two-pass: non-recovery first, then recovery overrides (both stripped of _recovery suffix).
rson_files = glob.glob('rsons/**/*.rson', recursive=True)
ref_map = {}
for r in rson_files:
    name = os.path.splitext(os.path.basename(r))[0]
    if not name.endswith('_recovery'):
        ref_map[name] = r
for r in rson_files:
    name = os.path.splitext(os.path.basename(r))[0]
    if name.endswith('_recovery'):
        stripped = name[:-len('_recovery')]
        ref_map[stripped] = r  # recovery preferred over non-recovery

ok = skip = err = 0
match = mismatch = 0
mismatches = []
errors = []

for scr in scrs:
    name = os.path.splitext(os.path.basename(scr))[0]
    data = open(scr, 'rb').read()
    try:
        vo, _ = decompiler.parse(data, {})
        g = len(vo['Groups']); s = len(vo['States']); d = len(vo['Dialogs'])
        ok += 1
    except decompiler.ParseError as e:
        msg = str(e)
        if 'format version 6' in msg:
            skip += 1
            continue
        err += 1
        errors.append((scr, str(e)))
        continue
    except Exception as e:
        err += 1
        errors.append((scr, str(e)))
        continue

    # Prefer _recovery.rson next to the SCR over the standard rsons/ reference.
    scr_dir = os.path.dirname(scr)
    recovery_path = os.path.join(scr_dir, name + '_recovery.rson')
    if os.path.exists(recovery_path):
        ref_path = recovery_path
    elif name in ref_map:
        ref_path = ref_map[name]
    else:
        ref_path = None

    if ref_path:
        rg, rs_, rd = ref_counts(ref_path)
        if (g, s, d) == (rg, rs_, rd):
            match += 1
        else:
            mismatch += 1
            mismatches.append((name, g, s, d, rg, rs_, rd))

print(f"SCR: OK={ok} SKIP={skip} ERR={err}  |  REF: MATCH={match} MISMATCH={mismatch}")
if errors:
    print(f"\nErrors ({len(errors)}):")
    for scr, msg in errors:
        print(f"  {os.path.basename(scr)}: {msg[:80]}")
if mismatches:
    print(f"\nMismatches ({len(mismatches)}):")
    # Group by h0 version
    h7 = [(n,g,s,d,rg,rs_,rd) for (n,g,s,d,rg,rs_,rd) in mismatches if True]
    for n,g,s,d,rg,rs_,rd in sorted(mismatches, key=lambda x: x[0]):
        print(f"  {n}: G:{g}vs{rg} S:{s}vs{rs_} D:{d}vs{rd}")
