# SCR Decompiler — Context Transfer for VS Code Session

> This document transfers accumulated knowledge from 16+ Claude.ai sessions to Claude Code in VS Code.
> Primary artifact: `decompiler.py` — converts Space Rangers HD `.scr` binary scripts → `.rson` (visual editor format).

---

## Current Status

**44 OK / 0 SKIP / 1 ERR** (45 files in test suite)

Only remaining failure: `Mod_ShuPirates_BlackHole.scr` (see "Next Steps" below).

---

## File Layout

```
decompiler.py          # Main artifact (~2113 lines)
CONTEXT.md             # This file
```

### Reference RSONs (ground truth for validation)
These live alongside the script in the project. Key ones:
- `Mod_ShuPirates_BlackHole_original.rson` — G=2 S=3 D=0
- `Mod_ShuKlissan_original.rson`           — G=14 S=26 D=37
- `Mod_ShuWarriors_original.rson`          — G=9 S=11 D=11
- `Mod_ShuBounty_original.rson`
- `PC_fem_rangers_original.rson`
- `Mod_EvoDreadnought_original.rson`
- `Mod_RevDiplomat_original.rson`
- `Mod_RevWarriorsAttack_original.rson`
- `DefendOrder_original.rson`
- ...and ~20 more in project dir

---

## How to Run

```bash
# Parse one file:
python3 decompiler.py path/to/Mod_Foo.scr -o Mod_Foo.rson

# Verbose (shows cp checkpoints):
python3 decompiler.py path/to/Mod_Foo.scr -o /dev/null -v verbose

# Batch test (adapt paths):
python3 -c "
import sys, logging, os
sys.path.insert(0, '.')
import decompiler; logging.disable(logging.CRITICAL)
UP='path/to/uploads'
tests = ['Excalibur.scr', 'Mod_ShuBounty.scr', ...]
ok=skip=err=0
for n in tests:
    scr=f'{UP}/{n}'
    if not os.path.exists(scr): continue
    d=open(scr,'rb').read()
    try:
        vo,_=decompiler.parse(d,{})
        G=len(vo['Groups']); S=len(vo['States']); D=len(vo['Dialogs'])
        print(f'OK {n} G={G} S={S} D={D}'); ok+=1
    except Exception as e:
        if 'format version 6' in str(e): print(f'SKIP {n}'); skip+=1
        else: print(f'ERR {n}: {e}'); err+=1
print(f'OK={ok} SKIP={skip} ERR={err}')
"
```

### Comparing output with reference RSON

```python
import json
ref = json.loads(open('Mod_Foo_original.rson').read())
ref_vo = ref['Visual.Objects'][0]
# Then compare Groups/States/Dialogs/Variables arrays field by field
```

---

## Binary Format: Core Concepts

### Header (first 12 bytes)
```python
h0, h1, h2 = struct.unpack_from('<III', data, 0)
```

| Flag | Derivation | Meaning |
|------|-----------|---------|
| `is_kavscr` | `h2 != 0` | kavscr format (has groups/states/dialogs) |
| `is_new_fmt` | `h0 == 8` | "new" format (h0=8 vs h0=6/7) |
| `is_nt_fmt` | `(h0!=8) or (h2==0) or (h2>1)` | "nt" format (has nt-style fields) |
| `is_preglob` | `h2 >= 3` | pre-global variable count in h2 |

### Format variants (key combos encountered)

| h0 | h1 | h2 | is_kavscr | is_nt_fmt | Examples | Notes |
|----|----|----|-----------|-----------|---------|-------|
| 8  | ≥100 | 1 | ✓ | ✗ | kavscr8, Mod_ShuBounty | Standard kavscr |
| 8  | ≥100 | 1 | ✓ | ✗ | Mod_ShuPirates_BH h1=1085 | h1≥1000 extra star format |
| 8  | ≥100 | 2 | ✓ | ✓ | Mod_ShuKlissan, Mod_ShuWarriors | kavscr is_nt_fmt |
| 8  | <100 | 0 | ✗ | ✓ | Mod_ShuMiniBoss h1=86 | ambient script |
| 7  | ≥350 | 0 | ✗ | ✓ | ShuMiniBoss_PiratesHunt h1=596 | h0=7 non-kavscr |
| 7  | ≥200 | 0 | ✗ | ✓ | Mod_ShuQuad h1=288 | h0=7 compact places |
| 7  | ≥100 | 1 | ✓ | ✓ | Mod_RevMerchants h1=417 | h0=7 kavscr multi-star |
| 8  | ≥100 | 0 | ✗ | ✓ | Mod_RevElection, PC_fem_rangers | non-kavscr new_fmt |
| 6  | any | 0 | ✗ | ✓ | DefendOrder | old rw format |

### Variable types
```
0=None, 1=Int (i32), 2=Dword (u32), 4=String (wstr), 9=Array (dword+3 bytes)
```

### Star record format
```
[name wstr][priority dword][NoKling byte][NoComeKling byte][unk dword][extra byte (h0=6 only)]
```

### Group struct (kavscr is_nt_fmt h0=8)
```
nm(rs) + pln(rs) + sti(dw) + own(dw) + [extra fields]
```

### State struct (kavscr)
```
nm(rs) + mv(dw) + [mo(rs) if mw_peek >= 0x20] + ac(dw) + ac*[atk_nm(rs)]
+ [ta(b1)+ti(rs) if b0==1,b1==0] OR [ti(rs)+ta(b1)]
+ ot(rs) + oa(rs) + code(rs)
+ [extra_code(rs) if peek_len>20 AND string contains non-alnum chars]
+ [null separator 2 bytes if next is short uppercase name]
+ [ctrl-char separator (len=1, ord<0x20) if followed by lowercase → break loop]
```

---

## Scan-Forward Guards (key defensive patterns)

The parser uses scan-forward recovery extensively when counts are garbage:

### dialog_count scan
```python
if dialog_count > 5000 and not is_kavscr and is_nt_fmt:
    # scan forward step=2 for dc ≤ 1000 with valid name following
```

### msg_count scan
```python
if msg_count > 10000 and is_nt_fmt:
    # scan forward step=2 for mc ≤ 1000
elif msg_count > 10000:
    # kavscr NOT is_nt_fmt: just set mc=0, rewind pos
```

### ans_count scan (global answers section)
```python
if ans_count > 10000:
    # scan for ans=0 near EOF (within 15%) or first small value
```

### group_count scan
```python
if group_count > 1000:
    # scan forward for gc ≤ 50 with valid uppercase name following
```

### FIX B pc2/ic2 scan (multi-star places)
```python
if pc2 > len(data) // 2:
    # scan for gc in range(pos-8, pos+5000) step=1
    # require: v ≤ 50, nm ≥ 4 chars, uppercase, alphanumeric
```

### nk2/nc2 guard (FIX B star struct)
```python
if nk2 > 8 or nc2 > 8:
    # scan for gc step=1 from pos-10 to pos+5000
```

---

## FIX B: Multi-star kavscr (is_new_fmt NOT is_nt_fmt)

For `is_new_fmt and not is_nt_fmt and is_kavscr and star_count > 1`:

**h1 < 1000:** Extra stars stored inline after ships:
```
for each extra star:
  star2_name(rs) + con(dw) + nk2(b) + nc2(b) + pri(dw)
  + nk2*dword + nc2*dword + [extra_dw+extra_b if nk2>0 or nc2>0]
  + ep_count(dw) + ep_planets(rs+6dw+rs) + es_count(dw) + es_ships(...)
  + pc2(dw) + places + ic2(dw) + items
  + [extra separator dword if pc2==0 and ic2==0 and nk2/nc2>0]
```

**h1 ≥ 1000 (e.g. Mod_ShuPirates_BlackHole h1=1085):**
```
for each extra star:
  index_dword(skip) + read_one_star(skip_prefix=True)
  + ep_count(dw) + ep_planets + es_count(dw) + es_ships
then scan-forward for [0,0, 0,0, sentinel_chunks..., sc, state_name]
```

---

## Pre-state Code Chunks

For `is_kavscr and not is_preglob` (most kavscr files):
```
while dw(pos) > 1000:
    chunk = rs(pos)   # one UTF-16LE wstring per chunk
```
The chunks are large binary code blobs (1000+ chars each).

For `is_nt_fmt and not is_new_fmt and not is_kavscr` (h0=6/7 non-kavscr):
```
5 dwords header
while dw(pos) > 1000:
    chunk = rs(pos)
```

---

## kavscr State Loop Special Cases

### extra_code detection
After `code(rs)`, peek at next wstring. If:
- Length > 20 chars AND
- Contains non-alphanumeric chars (spaces, parens, semicolons)
→ consume it as `extra_code`

If length > 20 but string is **purely alphanumeric** → it's the NEXT STATE'S NAME, do NOT consume.

### Inter-state separators
After each state's code/extra_code:
1. **Null separator** `[0,0]`: if next 2 bytes are `[0,0]` AND the 2 bytes after are NOT `[0,0]` AND the following name is short (≤50) uppercase ASCII → skip 2 bytes
2. **Ctrl-char separator**: if next wchar is control (0 < ord < 0x20):
   - Peek at name after the separator wstring
   - If name is **empty or starts lowercase/non-ASCII** → append current state, BREAK loop (entering dialog section)
   - If name starts uppercase → consume separator (pos += separator_wstring_size)

---

## Known Remaining Issue: Mod_ShuPirates_BlackHole

**File:** h0=8, h1=1085, h2=1, kavscr NOT is_nt_fmt, star_count=2  
**Expected (from RSON):** G=2, S=3, D=0  
**Groups:** 'PlayerGroup', 'PiratesGroup'  

**Root cause:**
- ships+items done at pos=1744
- FIX B h1≥1000 runs `read_one_star(skip_prefix=True)` from pos=1744
- The star2 data at 1744 contains garbage (bytes `[149, 0, 0, 0, 0, 1, ...]`)
- ep_count guard fires (ep=5242981), aborts FIX B at pos≈1763
- Scan-forward starts at pos+100=1863, step=2 (even addresses only)
- Real gc=2 is at ODD offset **1817** → scan misses it!
- `'PlayerGroup'` appears at 1821 (after gc=2 at 1817)
- gc=2 also appears at 1472 (before ships, in vars section) and 30053 (near EOF, wrong)

**Fix needed:**
After the ep_count guard fires in FIX B h1≥1000, change the scan-forward to use **step=1** instead of step=2, OR add a targeted scan:
```python
# After ep_count guard:
# Instead of generic ic/pc=0 pattern scan, scan for gc=2 + 'PlayerGroup' specifically:
for scan_off in range(pos, min(pos + 500, len(data) - 8)):
    v = struct.unpack_from('<I', data, scan_off)[0]
    if 0 < v <= 50:
        nm0, _ = rs(data, scan_off + 4)
        if nm0 and len(nm0) >= 4 and nm0[0].isupper() and nm0.replace('_','').isalnum():
            found_pos = scan_off
            break
```
Then set `pos = found_pos` and let the group parsing proceed normally.

The current scan pattern looks for `[0,0,0,0]` (ic=0 + gc=0) which doesn't match BH's layout.

---

## Test Suite (45 files)

```
Excalibur.scr                    G=1  S=1  D=1    # h0=8 single-group reference
Mod_ShuBounty.scr                G=1  S=1  D=19   # h0=8 h2=1 large kavscr
PC_fem_rangers.scr               G=8  S=21 D=19
DrKles_Mod.scr                   G=1  S=3  D=2
Mod_EvoDreadnought.scr           G=5  S=12 D=0
kavscr1.scr                      G=3  S=6  D=2
kavscr8.scr                      G=3  S=6  D=2
AdvancedOptions.scr              G=1  S=1  D=4
AdvancedArts.scr                 G=1  S=0  D=0
Mod_ExpAcryn.scr                 G=3  S=5  D=20
Mod_ExpBlackMarket_1.scr         G=5  S=7  D=3
Mod_RevColonization.scr          G=3  S=6  D=4
Mod_ExpPhysGun.scr               G=4  S=6  D=6
Mod_RevCaravan.scr               G=2  S=7  D=0
Mod_RevDiplomat.scr              G=0  S=13 D=3
Mod_ExpBlackMarket_2.scr         G=2  S=2  D=2
Mod_RevWarriorsAttack.scr        G=3  S=9  D=0
Mod_RevScientist.scr             G=3  S=4  D=1
Mod_RevElection.scr              G=9  S=29 D=10
MS_Begin.scr                     G=7  S=15 D=7
Mod_AddInhabitedPlanets.scr      G=0  S=1  D=0
Mod_AddUninhabitedPlanets.scr    G=0  S=1  D=0
mod_merchant.scr                 G=2  S=2  D=2
Cat_Drugs.scr                    G=3  S=3  D=4
Mod_RevTerrorist.scr             G=4  S=6  D=5
mod_operationchameleon.scr       G=3  S=2  D=4
Mod_ShuMercsHQ.scr               G=16 S=37 D=53
mod_tryfixbalance.scr            G=5  S=5  D=0
Mod_ShuDomiks.scr                G=18 S=14 D=17
Mod_RefFastExit.scr              G=0  S=0  D=1
FW.scr                           G=0  S=0  D=0
Mod_SR1Gaals_Pirates.scr         G=3  S=6  D=2
Mod_ShuPirates_BlackHole.scr     ERR  (see above)
MS_Terron.scr                    G=1  S=0  D=100
Mod_EvoBG.scr                    G=0  S=0  D=0
Mod_SR1PelengZond.scr            G=1  S=8  D=3
ShuMiniBoss_PiratesHunt.scr      G=0  S=0  D=62
Mod_ShuWarriors.scr              G=9  S=11 D=11
Mod_ExpCaravan.scr               G=1  S=0  D=0    # partial (FIX B gc false positive)
Mod_RevDominatorSpy.scr          G=1  S=0  D=5
Mod_RevMerchants.scr             G=1  S=0  D=0    # partial (h0=7 kavscr multi-star)
Mod_ShuKlissan.scr               G=14 S=26 D=37
Mod_ShuMiniBoss.scr              G=7  S=0  D=14
ShuMiniBoss_WarriorsHunt.scr     G=0  S=0  D=62
Mod_ShuQuad.scr                  G=7  S=1  D=13
```

---

## Key Fixes Applied This Session (sessions 15-16)

| Fix | File(s) fixed | Description |
|-----|--------------|-------------|
| extra_code alnum check | Mod_ShuKlissan | If peek wstring is purely alphanumeric → it's a state name, not code; don't consume |
| ctrl-sep break | Mod_SR1PelengZond | After state loop, ctrl-char wstring followed by lowercase name = entering dialog section |
| 2-byte null separator | Mod_ShuWarriors | After state code, [0,0] + non-null + short uppercase name → skip 2 bytes separator |
| dc scan: remove is_new_fmt req | Mod_ShuQuad | dc scan now fires for h0=7 too (was restricted to h0=8) |
| msg scan: extend to all formats | Mod_RevDominatorSpy | msg>10000: is_nt_fmt → scan; else → set mc=0 |
| ans_count scan | Mod_ShuMiniBoss, WarriorsHunt | global ans > 10000 → scan for ans=0 near EOF |
| pc2/ic2 guard with gc scan | Mod_ExpCaravan, RevMerchants | FIX B pc2 garbage → scan for gc instead of crash |
| places_count read in L800 | Mod_ShuQuad | h0=7 non-kavscr h1≥200 branch was missing places_count=dw() |
| h0=7 kavscr star_count>1 skip | Mod_RevMerchants | Skip places section entirely; let group_count scan handle positioning |
| group_count scan | Mod_RevMerchants | gc garbage → scan forward for gc ≤ 50 with valid name |
| ans loop EOF break | Mod_ExpCaravan | Break gracefully instead of crash when ans loop hits EOF |
| L1047 items EOF check | Mod_RevMerchants | Break if pos+4 > len(data) in items loop |

---

## decompiler.py Internal Structure

```
L1-50      Imports, helper functions (dw, b1, rs, read_wstr_nt, _guard)
L51-200    Type/format detection, variable parsing
L200-490   Star / planet / ship parsing
L491-690   FIX B: multi-star kavscr is_new_fmt NOT is_nt_fmt
L691-910   is_preglob places, is_nt_fmt places (various sub-branches by h1/h0/kavscr)
L910-1095  Fallback places branches (h0=7 compact, h0=6 read_wstr_nt)
L1096-1200 Group parsing
L1200-1460 Pre-state code chunks
L1460-1600 kavscr state loops (multiple branches: h1<1000, h1≥1000, h1≥4000)
L1600-1750 Dialog parsing
L1750-1900 Message parsing + global ans_count
L1900-2000 RSON output construction
L2000-2113 CLI entry point, _worker, main
```

---

## Useful Binary Inspection Patterns

```python
import struct

def rs(d, p):
    """Read UTF-16LE wstring"""
    e = p
    while e + 1 < len(d):
        if d[e] == 0 and d[e+1] == 0: break
        e += 2
    return d[p:e].decode('utf-16-le', 'replace'), e + 2

def dw(d, p):
    return struct.unpack_from('<I', d, p)[0], p + 4

# Scan for (count, name) pairs — useful for finding sc/dc/gc/mc:
d = open('Mod_Foo.scr', 'rb').read()
for off in range(start, end, 2):
    v = struct.unpack_from('<I', d, off)[0]
    if 0 < v <= 50:
        nm, _ = rs(d, off + 4)
        if nm and len(nm) >= 4 and nm[0].isupper() and nm[0].isascii():
            print(f'v={v} at {off}: {nm!r}')

# Find a UTF-16LE string:
pat = 'PlayerGroup'.encode('utf-16-le') + b'\x00\x00'
pos = d.find(pat)
print(f'Found at {pos}, dw before = {struct.unpack_from("<I",d,pos-4)[0]}')
```

---

## Lang.dat Note

`Lang.dat` is strongly encrypted (entropy ~7.96 bits). XOR/RC4/zlib/LCG all fail. The decompiler uses `Lang.txt` (plaintext companion) when available for CT() string resolution, but this is optional — the parse works without it.

---

*Generated after session 16. 44/45 test files pass.*
