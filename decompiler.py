#!/usr/bin/env python3
"""Space Rangers SCR -> RSON decompiler. Usage: python3 decompiler.py file.scr [-o out.rson]"""
import struct, json, argparse, re, logging, sys, threading
from pathlib import Path


class ParseError(Exception):
    """Raised when the bytes of a .scr do not fit the layout the game reads."""

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
# Log levels used in this tool:
#   ERROR   – parse failures, file I/O errors
#   WARNING – partial/suspicious data (not yet used extensively)
#   INFO    – per-file summary (brief mode)
#   DEBUG   – per-section counts, detailed progress (verbose mode)

logger = logging.getLogger('decompiler')

def setup_logging(log_file=None, verbosity='brief'):
    """Configure logging.
    verbosity: 'verbose' | 'brief' | 'errors'
    log_file:  path to log file, or None for stderr only.
    """
    level_map = {'verbose': logging.DEBUG, 'brief': logging.INFO, 'errors': logging.ERROR}
    level = level_map.get(verbosity, logging.INFO)
    logger.setLevel(logging.DEBUG)   # capture everything; handlers filter

    fmt_verbose = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')
    fmt_brief   = logging.Formatter('%(levelname)-7s %(message)s')

    handlers = []

    # Console handler (stderr)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    ch.setFormatter(fmt_verbose if verbosity == 'verbose' else fmt_brief)
    handlers.append(ch)

    # File handler (optional)
    if log_file:
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setLevel(level)
        fh.setFormatter(fmt_verbose)
        handlers.append(fh)

    for h in handlers:
        logger.addHandler(h)


def load_lang(path):
    """Load Lang.txt -> dict of CT string keys to text.
    Supports block formats: 'Name ^{' or 'Name ~{' or 'Name {'
    """
    lang = {}
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        if raw[:2] == b'\xff\xfe':
            txt = raw[2:].decode('utf-16-le')
        elif raw[:2] == b'\xfe\xff':
            txt = raw[2:].decode('utf-16-be')
        else:
            txt = None
            for enc in ('utf-8-sig', 'cp1251', 'utf-8'):
                try:
                    txt = raw.decode(enc)
                    break
                except (UnicodeDecodeError, Exception):
                    continue
            if txt is None:
                return lang
    except Exception:
        return lang
    # Normalise block openers: '^{' and '~{' → '{'
    txt = txt.replace('^{', '{').replace('~{', '{')
    # Find outer Script { ... } block
    script_m = re.search(r'\bScript\s*\{(.*?)\n\}', txt, re.DOTALL)
    if script_m:
        inner = script_m.group(1)
        for section in re.finditer(r'(\w+)\s*\{(.*?)\n\s*\}', inner, re.DOTALL):
            sec = section.group(1)
            for kv in re.finditer(r'^\s*(\d+)=(.*)', section.group(2), re.MULTILINE):
                key = f'Script.{sec}.{kv.group(1)}'
                lang[key] = kv.group(2).strip().replace('<br>', '\r\n')
    return lang

# ---------------------------------------------------------------------------
# .scr layout: every field in the order the game loads it
# ---------------------------------------------------------------------------
# Nothing here is guessed or searched for. The order and sizes are those of the
# engine loader: TScript.LoadFromBuffer and, for the header, the template pass
# CompileScriptTemplateCondition (source/script/aScript.pas of the engine
# decompilation SpaceRangersHD_decomp); a variable is TVarEC.LoadFromBuffer
# (EC_Expression.pas), a string is TBufEC.ReadWideString (EC_Buf.pas): UTF-16LE
# up to a zero word, no length prefix. The full table is in docs/TECHNICAL.md.

class _ScrReader:
    """Cursor over the bytes of a .scr; reading past the end is a ParseError."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def _unpack(self, fmt, size, what):
        if self.pos + size > len(self.data):
            raise ParseError(f"{what}: {size} bytes at offset {self.pos} run past the end "
                             f"of the file ({len(self.data)} bytes)")
        value = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def u8(self, what):
        return self._unpack('<B', 1, what)

    def u32(self, what):
        return self._unpack('<I', 4, what)

    def i32(self, what):
        return self._unpack('<i', 4, what)

    def f64(self, what):
        return self._unpack('<d', 8, what)

    def wstr(self, what):
        d, p = self.data, self.pos
        while p + 1 < len(d):
            if d[p] == 0 and d[p + 1] == 0:
                text = d[self.pos:p].decode('utf-16-le', 'replace')
                self.pos = p + 2
                return text
            p += 2
        raise ParseError(f"{what}: the string at offset {self.pos} has no terminator")

    def count(self, what):
        n = self.i32(what)
        if n < 0 or n > len(self.data) - self.pos:   # every record takes a byte or more
            raise ParseError(f"Impossible count {n} for {what!r} at offset {self.pos - 4} "
                             f"(file {len(self.data)} bytes)")
        return n


def _read_vars(r, what):
    """i32 count, then per variable: wstr name, u8 kind, value by kind."""
    out = []
    for _ in range(r.count(what + ' count')):
        name = r.wstr(what + ' name')
        kind = r.u8(what + ' kind')
        if kind in (1, 2):
            value = r.u32(what + ' value')
        elif kind == 3:
            value = r.f64(what + ' value')
        elif kind == 4:
            value = r.wstr(what + ' value')
        elif kind == 9:
            value = _read_vars(r, what + ' element')
        elif kind in (0, 5, 6, 7, 8, 10):
            value = None
        else:
            raise ParseError(f"{what} {name!r}: unknown kind {kind} at offset {r.pos - 1}")
        out.append((name, kind, value))
    return out


def read_scr_layout(data):
    """Read every field of a .scr the way the game does.

    Returns plain dicts and lists named after the RSON fields where they have
    one; raises ParseError when the bytes do not fit (a version the game does
    not load, a record past the end, bytes left over after the last answer)."""
    r = _ScrReader(data)
    version = r.u32('version')
    if not 5 <= version <= 8:
        raise ParseError(f"format version {version} — the game loads versions 5 to 8")
    lay = {'version': version, 'body_offset': r.u32('body offset')}
    lay['globals'] = _read_vars(r, 'global var')
    lay['global_code'] = r.wstr('global code')
    # LoadFromBuffer jumps to the low word of the offset; RScript puts the body
    # right after the global code, and a file where the two differ is one the
    # game itself reads wrong.
    if lay['body_offset'] & 0xFFFF != r.pos:
        raise ParseError(f"script body offset {lay['body_offset']} does not match the end "
                         f"of the global code ({r.pos})")
    lay['locals'] = _read_vars(r, 'var')
    lay['constellations'] = r.u32('constellation count')

    stars = []
    for _ in range(r.count('star count')):
        star = {'name': r.wstr('star name'), 'constellation': r.i32('star constellation')}
        if version < 7:
            r.u8('star flag (version < 7)')
        star['no_kling'] = r.u8('star NoKling')
        star['no_come_kling'] = r.u8('star NoComeKling')
        links = []
        for _ in range(r.count('star link count')):
            other = r.i32('star link')
            if version < 7:
                r.i32('star link field (version < 7)')
            dist_min, dist_max = r.u32('star link DistMin'), r.u32('star link DistMax')
            if version < 7:
                for _ in range(3):
                    r.i32('star link field (version < 7)')
            links.append((other, dist_min, dist_max, r.u8('star link Hole')))
        star['links'] = links
        star['planets'] = [{
            'name': r.wstr('planet name'), 'race': r.u32('planet Race'),
            'owner': r.u32('planet Owner'), 'economy': r.u32('planet Economy'),
            'government': r.u32('planet Goverment'), 'range_min': r.u32('planet RangeMin'),
            'range_max': r.u32('planet RangeMax'), 'dialog': r.wstr('planet Dialog'),
        } for _ in range(r.count('planet count'))]
        ships = []
        for _ in range(r.count('ship count')):
            ship = {'count': r.u32('ship Count'), 'owner': r.u32('ship Owner'),
                    'type': r.u32('ship Ship.Type'), 'player': r.u8('ship Player'),
                    'speed_min': r.u32('ship SpeedMin'), 'speed_max': r.u32('ship SpeedMax'),
                    'weapon': r.u32('ship Weapon'), 'cargo_hook': r.u32('ship CargoHook'),
                    'empty_space': r.u32('ship EmptySpace')}
            if version < 7:
                r.i32('ship field (version < 7)'); r.i32('ship field (version < 7)')
            ship['status'] = [r.u32('ship status') for _ in range(6)]
            if version < 7:
                r.i32('ship field (version < 7)'); r.i32('ship field (version < 7)')
            ship['strength_min'] = r.u32('ship StrengthMin')   # float32 bits
            ship['strength_max'] = r.u32('ship StrengthMax')
            ship['ruins'] = r.wstr('ship Ruins')
            ships.append(ship)
        star['ships'] = ships
        stars.append(star)
    lay['stars'] = stars

    places = []
    for _ in range(r.count('place count')):
        place = {'name': r.wstr('place name'), 'star': r.wstr('place star'),
                 'type': r.i32('place type'), 'obj': '', 'obj2': '',
                 'angle': 0, 'dist': None, 'radius': 0}     # angle and dist: float32 bits
        kind = place['type']
        if kind == 0:
            place['angle'] = r.u32('place Angle')
            place['dist'] = r.u32('place Dist')
            place['radius'] = r.u32('place Radius')
        elif kind in (1, 4):
            place['obj'] = r.wstr('place object')
            place['radius'] = r.u32('place Radius')
        elif kind == 2:
            place['obj'] = r.wstr('place object')
        elif kind in (3, 5):
            place['obj'] = r.wstr('place object')
            place['dist'] = r.u32('place Dist')
            place['radius'] = r.u32('place Radius')
            place['angle'] = r.u32('place Angle')
        elif kind == 6:
            place['obj'] = r.wstr('place object')
            place['obj2'] = r.wstr('place object 2')
            place['radius'] = r.u32('place Radius')
        else:
            raise ParseError(f"place {place['name']!r}: unknown type {kind} at offset {r.pos - 4}")
        places.append(place)
    lay['places'] = places

    lay['items'] = [{
        'name': r.wstr('item name'), 'place': r.wstr('item place'),
        'class': r.u32('item Class'), 'type': r.u32('item Item.Type'),
        'size': r.u32('item Size'), 'level': r.u32('item Level'),
        'radius': r.u32('item Radius'), 'owner': r.u32('item Owner'),
        'useless': r.wstr('item Useless'),
    } for _ in range(r.count('item count'))]

    groups = []
    for _ in range(r.count('group count')):
        group = {'name': r.wstr('group name'), 'planet': r.wstr('group planet'),
                 'state': r.i32('group state'), 'owner': r.u32('group Owner'),
                 'type': r.u32('group Group.Type'), 'count_min': r.u32('group CntShipMin'),
                 'count_max': r.u32('group CntShipMax'), 'speed_min': r.u32('group SpeedMin'),
                 'speed_max': r.u32('group SpeedMax'), 'weapon': r.u32('group Weapon'),
                 'cargo_hook': r.u32('group CargoHook'), 'empty_space': r.u32('group EmptySpace')}
        if version < 7:
            r.i32('group field (version < 7)')
        group['add_player'] = r.u8('group AddPlayer')
        if version < 7:
            for _ in range(4):
                r.i32('group field (version < 7)')
        group['status'] = [r.u32('group status') for _ in range(6)]
        group['dist_search'] = r.u32('group DistSearch')
        group['dialog'] = r.wstr('group Dialog')
        group['strength_min'] = r.u32('group StrengthMin')   # float32 bits
        group['strength_max'] = r.u32('group StrengthMax')
        group['ruins'] = r.wstr('group Ruins')
        groups.append(group)
    lay['groups'] = groups

    # Group1, Group2, relation 1→2, relation 2→1 (i32), combat balance min/max (f32)
    lay['relations'] = [tuple(r.u32('group relation') for _ in range(6))
                        for _ in range(r.count('group relation count'))]

    lay['init_code'] = r.wstr('init code')
    lay['turn_code'] = r.wstr('turn code')
    lay['dialog_begin_code'] = r.wstr('dialog begin code') if version > 5 else ''

    states = []
    for _ in range(r.count('state count')):
        state = {'name': r.wstr('state name'), 'move': r.u32('state Move')}
        # Idle (0) and the normal AI (5) have no move target
        state['target'] = r.wstr('state MoveObj') if state['move'] not in (0, 5) else ''
        state['attack'] = [r.wstr('state attack target')
                           for _ in range(r.count('state attack count'))]
        state['take_item'] = r.wstr('state TakeItem')
        state['take_all'] = r.u8('state TakeAllItem')
        state['on_talk'] = r.wstr('state OnTalk')
        state['on_act'] = r.wstr('state OnActCode')
        state['ether_code'] = r.wstr('state ether code')
        state['code'] = r.wstr('state turn code')
        states.append(state)
    lay['states'] = states

    lay['dialogs'] = [(r.wstr('dialog name'), r.wstr('dialog code'))
                      for _ in range(r.count('dialog count'))]
    lay['messages'] = [(r.wstr('message name'), r.wstr('message code'))
                       for _ in range(r.count('message count'))]
    lay['answers'] = [(r.wstr('answer name'), r.wstr('answer code'), r.wstr('answer turn code'))
                      for _ in range(r.count('answer count'))]
    if r.pos != len(data):
        raise ParseError(f"{len(data) - r.pos} bytes left after the last answer "
                         f"(offset {r.pos} of {len(data)})")
    return lay


def _is_valid_id(name):
    """Return True if name is a valid game script identifier (ASCII alnum + underscore)."""
    return bool(name) and all(c.isascii() and (c.isalnum() or c == '_') for c in name)

def _fmt_place_float(raw_dword):
    """Format a value stored as float32 bits in a dword as a string.

    Choose the shortest decimal that re-encodes to the SAME float32 bits, so
    RScript reconstructs the original dword exactly (e.g. -0.9 vs the stored
    -0.90000003 = 0xbf666663)."""
    bits = raw_dword & 0xFFFFFFFF
    f = struct.unpack('<f', struct.pack('<I', bits))[0]
    if f == 0.0 or f == int(f):
        return str(int(round(f)))
    for prec in range(1, 10):
        s = f'{f:.{prec}f}'
        if struct.unpack('<f', struct.pack('<f', float(s)))[0] == f:
            s = s.rstrip('0')
            return s if s[-1] != '.' else s + '0'
    s = f'{f:.6f}'.rstrip('0')
    return s if s[-1] != '.' else s + '0'

TYPE_NAMES = {1:'Int', 2:'Dword', 3:'Float', 4:'Str'   , 5:'Bool', 9:'Array'}

def code_lines(s, strip_indent=True):
    # RScript adds one CT() wrapper during compilation; strip it back for round-trip
    s = re.sub(r'CT\s*\(CT\s*\((["\'][^"\']+["\'])\)\)', r'CT(\1)', s)
    lines = s.replace('\r\n','\n').replace('\r','\n').split('\n')
    while lines and lines[-1]=='': lines.pop()
    if strip_indent and lines:
        non_empty = [l for l in lines if l.strip()]
        if non_empty:
            min_i = min(len(l)-len(l.lstrip()) for l in non_empty)
            if min_i > 0:
                lines = [l[min_i:] for l in lines]
            else:
                # min_i == 0: strip secondary minimum (common indent of indented lines)
                indented = [l for l in non_empty if l.startswith(' ')]
                if indented:
                    sec = min(len(l)-len(l.lstrip()) for l in indented)
                    lines = [l[sec:] if l.startswith(' ' * sec) else l for l in lines]
    return lines

def escape_code_line(s):
    """Escape a raw code line for RScript: its parser maps double-backslash
    to one backslash and backslash-quote to a quote, so a binary
    backslash-quote must be written as three backslashes plus quote
    (verified against author-made rsons). This holds for both single (')
    and double (") quotes: a binary `\\"name\\"` must round-trip through
    three backslashes."""
    BS = chr(92); Q = chr(39); DQ = chr(34)
    s = s.replace(BS, BS + BS)
    s = s.replace(BS + BS + Q, BS + BS + BS + Q)
    s = s.replace(BS + BS + DQ, BS + BS + BS + DQ)
    return s

def resolve_ct_in_lines(lines, lang):
    """Replace CT("key") with quoted text from lang dict in code lines."""
    if not lang:
        return lines
    def ct_sub(m):
        key = m.group(1)
        text = lang.get(key)
        if text is not None:
            return f'"{text}"'
        return m.group(0)
    return [re.sub(r'CT\s*\(["\']([^"\']+)["\']\)', ct_sub, line) for line in lines]


def process_msg_code(code):
    """Extract CT ref, verbatim DText code, and Turn lines from dialog message code."""
    ct_ref, raw_turn, orig_dtext = '', [], ''
    raw_lines = code.replace('\r\n','\n').replace('\r','\n').split('\n')
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            # Preserve blank lines inside turn code (round-trip), but only
            # after some content was collected (skip leading blanks).
            if raw_turn or orig_dtext:
                raw_turn.append('')
            continue
        if re.match(r'DText\s*\(', stripped):
            m = re.search(r'CT\s*\(["\']([^"\']+)["\']\)', stripped)
            if m: ct_ref = m.group(1)
            # Find end of DText(...) call by balancing parentheses
            depth = 0
            end_idx = 0
            for ci, ch in enumerate(stripped):
                if ch == '(': depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        end_idx = ci + 1
                        break
            if not orig_dtext:
                orig_dtext = stripped[:end_idx]
            # Skip optional semicolon and spaces after DText(...)
            rest = stripped[end_idx:].lstrip('; \t')
            if rest:
                raw_turn.append(rest)
        else:
            raw_turn.append(line.rstrip())
    # Re-join and normalize indent via code_lines
    turn_lines = code_lines('\n'.join(raw_turn)) if raw_turn else []
    return ct_ref, turn_lines, orig_dtext

def _fmt_strength(bits):
    """StrengthMin/StrengthMax of ships and groups: float32 bits as RSON text."""
    return '0' if bits == 0 else _fmt_place_float(bits)


def parse(data, lang=None):
    if lang is None: lang = {}
    if len(data) >= 4 and struct.unpack_from('<I', data, 0)[0] in (5, 6):
        # The game still loads these, and read_scr_layout reads them; RScript
        # writes version 8 only, so their RSON could never compile back to them.
        raise ParseError(f"format version {struct.unpack_from('<I', data, 0)[0]} — skipped")
    lay = read_scr_layout(data)
    # h2 = number of global variables (the u32 at offset 8): RScript marks that
    # many leading variables Global.
    h2 = len(lay['globals'])

    def var_value(kind, value):
        if kind == 9:
            # An array's Init is its length (RScript makes that many empty
            # elements) or, when every element is an Int, the list '[0,5,7]'.
            if value and all(k == 1 for _, k, _ in value):
                return '[%s]' % ','.join(str(v - (1 << 32) if v >= 1 << 31 else v)
                                         for _, _, v in value)
            return len(value)
        if kind in (1, 2, 3, 4):
            return value
        return 0

    var_decls = [(n, t, var_value(t, v)) for n, t, v in lay['globals'] + lay['locals']]
    global_code = lay['global_code']
    const_count = lay['constellations']

    stars, planets, ships = [], [], []
    for st in lay['stars']:
        # Constellation -1 (none) shows as Priority 0; RScript sorts stars by
        # Priority, and equal ones are resolved through the star-name
        # variables below (their value is the star's index).
        pri = st['constellation']
        stars.append({'Name': st['name'], 'Priority': 0 if pri == -1 else pri,
                      'NoKling': bool(st['no_kling']),
                      'NoComeKling': bool(st['no_come_kling']),
                      '_links': st['links']})
        for p in st['planets']:
            planets.append({'Name': p['name'], 'Race': p['race'], 'Owner': p['owner'],
                            'Economy': p['economy'], 'Goverment': p['government'],
                            'RangeMin': p['range_min'], 'RangeMax': p['range_max'],
                            'Dialog': -1 if p['dialog'] == '' else p['dialog'],
                            '+Star': st['name']})
        for s in st['ships']:
            v = s['status']
            ships.append({'Name': '', 'Count': s['count'], 'Owner': s['owner'],
                          'Ship.Type': s['type'], 'Player': bool(s['player']),
                          'SpeedMin': s['speed_min'], 'SpeedMax': s['speed_max'],
                          'Weapon': s['weapon'], 'CargoHook': s['cargo_hook'],
                          'EmptySpace': s['empty_space'],
                          'StatusTraderMin': v[0], 'StatusTraderMax': v[1],
                          'StatusWarriorMin': v[2], 'StatusWarriorMax': v[3],
                          'StatusPirateMin': v[4], 'StatusPirateMax': v[5],
                          'StrengthMin': _fmt_strength(s['strength_min']),
                          'StrengthMax': _fmt_strength(s['strength_max']),
                          'Ruins': s['ruins'], '+Star': st['name']})

    places = []
    for p in lay['places']:
        # Types without a stored distance keep RScript's default 0.5.
        places.append({'name': p['name'], 'ref': p['obj'], 'ref2': p['obj2'],
                       'ptype': p['type'], 'angle': p['angle'], 'radius': p['radius'],
                       'dist_raw': 0x3F000000 if p['dist'] is None else p['dist'],
                       'star_ref': p['star']})

    items = [{'Type': 'TItem', 'Name': it['name'], 'Class': it['class'],
              'Item.Type': it['type'], 'Size': it['size'], 'Level': it['level'],
              'Radius': it['radius'], 'Owner': it['owner'], 'Useless': it['useless'],
              '+Place': it['place']} for it in lay['items']]

    groups = []
    for g in lay['groups']:
        v = g['status']
        groups.append({'Name': g['name'], 'Owner': g['owner'], 'Group.Type': g['type'],
                       'CntShipMin': g['count_min'], 'CntShipMax': g['count_max'],
                       'SpeedMin': g['speed_min'], 'SpeedMax': g['speed_max'],
                       'Weapon': g['weapon'], 'CargoHook': g['cargo_hook'],
                       'EmptySpace': g['empty_space'], 'AddPlayer': bool(g['add_player']),
                       'StatusTraderMin': v[0], 'StatusTraderMax': v[1],
                       'StatusWarriorMin': v[2], 'StatusWarriorMax': v[3],
                       'StatusPirateMin': v[4], 'StatusPirateMax': v[5],
                       'DistSearch': g['dist_search'],
                       'Dialog': -1 if g['dialog'] == '' else g['dialog'],
                       'StrengthMin': _fmt_strength(g['strength_min']),
                       'StrengthMax': _fmt_strength(g['strength_max']),
                       'Ruins': g['ruins'], '+Planet': g['planet'], '+State': g['state']})
    relations = lay['relations']

    # The three code slots before the states, by position: Init, Turn, DialogBegin.
    dialog_begin_chunks = [lay['init_code'], lay['turn_code'], lay['dialog_begin_code']]

    states = []
    for s in lay['states']:
        states.append({'Name': s['name'], 'Move': s['move'],
                       'MoveObj': s['target'] if s['target'] else -1,
                       'Attack.Count': len(s['attack']), 'Attack.Items': s['attack'],
                       'TakeItem': -1 if s['take_item'] == '' else s['take_item'],
                       'TakeAllItem': bool(s['take_all']),
                       'OnTalk': s['on_talk'], 'OnActCode': s['on_act'],
                       'EType': 0, 'EUnique': '', 'EMsg': '',
                       '_code': s['code']})

    dialogs, turn_ops = [], []
    for nm, cd in lay['dialogs']:
        dialogs.append({'Name': nm,
                        'Move':0,'MoveObj':-1,'Attack.Count':0,'TakeItem':-1,
                        'TakeAllItem':False,'OnTalk':'','OnActCode':'',
                        'EType':0,'EUnique':'','EMsg':'',
                        '_has_turn_dlg': bool(cd)})
        if cd: turn_ops.append(cd)

    # Dialog messages: name + code (contains DText + Turn)
    msgs = []
    msg_ct_refs = []
    msg_turn_codes = []
    msg_orig_dtexts = []
    for nm, cd in lay['messages']:
        ct_ref, t_lines, orig_dtext = process_msg_code(cd)
        msgs.append({'Name':nm})
        msg_ct_refs.append(ct_ref)
        msg_turn_codes.append(t_lines)
        msg_orig_dtexts.append(orig_dtext)

    # Dialog answers: name, DAnswer code, turn code
    answers = []
    ans_ct_refs = []
    ans_turn_codes = []
    ans_orig_dacodes = []
    for nm, da_code, turn in lay['answers']:
        m_name = re.match(r"DAnswer\s*\(\s*'([^~']*?)~", da_code)
        act = m_name.group(1) if m_name else nm
        m_ct = re.search(r'CT\s*\(["\']([^"\']+)["\']\)', da_code)
        ct_ref = m_ct.group(1) if m_ct else ''
        t_lines = code_lines(turn) if turn.strip() else []
        # Extract verbatim DAnswer call (balance parens)
        da_stripped = da_code.strip().split('\n')[0].strip()
        orig_da = ''
        if re.match(r'DAnswer\s*\(', da_stripped):
            dep, ei = 0, 0
            for ci, ch in enumerate(da_stripped):
                if ch == '(': dep += 1
                elif ch == ')':
                    dep -= 1
                    if dep == 0: ei = ci + 1; break
            orig_da = da_stripped[:ei]
        answers.append({'Name': act, '_has_turn': bool(t_lines)})
        ans_ct_refs.append(ct_ref)
        ans_turn_codes.append(t_lines)
        ans_orig_dacodes.append(orig_da)
    logger.debug('    [cp] read %d vars, %d stars, %d groups, %d states, %d dialogs, '
                 '%d messages, %d answers', len(var_decls), len(stars), len(groups),
                 len(states), len(dialogs), len(msgs), len(answers))

    # ── Build RSON ────────────────────────────────────────────────────────────

    obj_names = (set(s['Name'] for s in stars) |
                 set(p['Name'] for p in planets) |
                 set(s['Name'] for s in ships) |
                 set(it['Name'] for it in items) |
                 set(g['Name'] for g in groups) |
                 set(d['Name'] for d in dialogs) |
                 set(pl['name'] for pl in places))
    real_vars = [(n,t,v) for n,t,v in var_decls if n not in obj_names and _is_valid_id(n)]
    def fmt_init(t, v):
        if t == 0: return ''
        if t == 3:  # Double — 3 decimal places like game engine
            return f'{float(v):.3f}'
        return str(v)
    # h2 is the number of pre-global vars stored in the header block; RScript
    # writes h2 = count of Global-flagged vars, so mark exactly the first h2
    # emitted variables to round-trip the header.
    variables = [{'Type':'TVar','Name':n,'Pos.x':i*90,'Pos.y':0,'Parent':-1,'#':i,
                  'Var.Type':('None' if t==0 else TYPE_NAMES.get(t,'Int')),
                  'Init': fmt_init(t, v),
                  'Global': (i < h2)}
                 for i,(n,t,v) in enumerate(real_vars)]

    # Operations: Global, pre-state code chunks, state/dialog/msg/ans turn codes
    # RScript rejects an empty Global op (syntax error 'sme=2 line=!;') and
    # crashes with EListError when the Global op is absent — emit a lone
    # comment for scripts whose global slot is empty; the
    # binary patcher removes the extra '  //' bytes afterwards.
    _gc_lines = code_lines(global_code)
    if not _gc_lines:
        _gc_lines = ['//']
    ops = [{'Name':'','Code.Type':'Global','Code':_gc_lines}]
    # Pre-state code chunks: slot position determines type (0=Init, 1=Turn, 2=DialogBegin).
    # Use absolute index in dialog_begin_chunks, not count of non-empty chunks, so that
    # code in the Init slot (idx 0) is not mis-labelled as Turn when states are present.
    pre_type_map = {0:'Init', 1:'Turn', 2:'DialogBegin'}
    for idx, chunk in enumerate(dialog_begin_chunks):
        if not chunk.strip():
            continue  # skip empty chunks
        ops.append({'Name':'','Code.Type':pre_type_map[idx],'Code':code_lines(chunk)})
    for s in states:             # state turn codes
        code_s = s.get('_code', '')
        if code_s:
            ops.append({'Name':'','Code.Type':'Turn','Code':code_lines(code_s),'_pos_y_type':'state'})
    for c in turn_ops:           # dialog codes
        ops.append({'Name':'','Code.Type':'Turn','Code':code_lines(c),'_pos_y_type':'dlg'})
    for t_lines in msg_turn_codes:
        if t_lines: ops.append({'Name':'','Code.Type':'Turn','Code':t_lines,'_pos_y_type':'msg'})
    for t_lines in ans_turn_codes:
        if t_lines: ops.append({'Name':'','Code.Type':'Turn','Code':t_lines,'_pos_y_type':'ans'})

    # ── Position & ID assignment ──────────────────────────────────────────────
    # Helper: cell width approximation (proportional font, ceil to multiple of 30)
    def cell_w(name, px_per_char=14, min_w=90):
        w = max(min_w, len(name) * px_per_char)
        return ((w + 29) // 30) * 30

    def layout_horizontal(objs, y_start, width_fn, row_gap=25, max_x=1800):
        x, y = 0, y_start
        rows_used = [y_start]
        for k, obj in enumerate(objs):
            obj['Pos.x'] = x
            obj['Pos.y'] = y
            x += width_fn(obj)
            if x >= max_x and k < len(objs) - 1:
                x = 0
                y += row_gap
                if y not in rows_used:
                    rows_used.append(y)
        return rows_used

    # Variables: horizontal flow, row height=25 (editor decompile: min cell 60)
    MAX_X = 1600
    base = len(variables)
    vx, vy = 0, 0
    for v in variables:
        v['Pos.x'] = vx
        v['Pos.y'] = vy
        vx += cell_w(v['Name'], min_w=60)
        if vx >= MAX_X:
            vx = 0; vy += 25
    var_bottom = vy + 25 if variables else 0

    # Vertical column below the variables: Ship, Star, Places-row, Planet, Item
    # share one sequence with 45px spacing; an empty section takes no slot.
    # Mirrors the editor's own SCR-decompile (verified against its output).
    _cy = var_bottom + 20
    Y_SHIP  = _cy; _cy += 45 if ships   else 0
    Y_STAR  = _cy; _cy += 45 if stars   else 0
    Y_PLACE = _cy; _cy += 45 if places  else 0
    Y_PLANET= _cy; _cy += 45 if planets else 0
    Y_ITEM  = _cy; _cy += 45 if items   else 0
    Y_GROUP = _cy

    # RScript requires unique star priorities ('Star priority. First unique').
    # When the star records carry duplicates, take them from the variable
    # RScript declares for each star: its value is the star's index.
    if len(stars) > 1:
        _pris = [s.get('Priority', 0) for s in stars]
        if len(set(_pris)) < len(_pris):
            _reg = {}
            for _vn, _vt, _vv in var_decls:
                if _vn not in _reg and isinstance(_vv, int) and _vv:
                    _reg[_vn] = _vv
            for s in stars:
                _rv = _reg.get(s.get('Name'))
                if _rv:
                    s['Priority'] = _rv
    for i, s in enumerate(stars):
        s.update({'Type':'TStar','Pos.x':i*90,'Pos.y':Y_STAR,'Parent':-1,'#':base+i,'Constellation':const_count})
    base += len(stars)
    for i, s in enumerate(ships):
        s.update({'Type':'TStarShip','Pos.x':i*90,'Pos.y':Y_SHIP,'Parent':-1,'#':base+i})
    base += len(ships)
    for i, p in enumerate(planets):
        p.update({'Type':'TPlanet','Pos.x':i*90,'Pos.y':Y_PLANET,'Parent':-1,'#':base+i})
    base += len(planets)
    for i, pl in enumerate(places):
        # Keep the original star ref verbatim (may be empty) — defaulting to
        # stars[0] breaks round-trip for places without a star link.
        _pl_star = pl.pop('star_ref', '')
        _pl_angle = pl.pop('angle', 0)
        if isinstance(_pl_angle, int) and _pl_angle > 1000000:
            # Stored as float32 bits (e.g. 1119092736 = 90.0)
            _pl_angle = _fmt_place_float(_pl_angle)
        else:
            _pl_angle = str(_pl_angle)
        pl.update({'Type':'TPlace',
                   'Name': pl.pop('name', ''),
                   'Pos.x': pl.pop('posx', i*90),
                   'Pos.y': pl.pop('posy', Y_PLACE),
                   'Parent':-1,'#':base+i,
                   'Place.Type': pl.pop('ptype', 0),
                   'Angle': _pl_angle,
                   'Dist': _fmt_place_float(pl.pop('dist_raw', 0)),
                   'Radius': pl.pop('radius', 0),
                   'Obj1': pl.pop('ref', '') or -1,
                   'Obj2': pl.pop('ref2', '') or -1,
                   '+Star': _pl_star})
    base += len(places)
    for i, it in enumerate(items):
        it.update({'Type':'TItem','Pos.x':i*90,'Pos.y':Y_ITEM,'Parent':-1,'#':base+i})
    base += len(items)

    # Groups: compact grid, wrap by width; remember each group's position so its
    # +State target can be parked directly underneath (as the editor lays it out).
    GRID_MAX_X = 450
    GROUP_ROW = 60
    _gx = _gy = 0
    _group_pos = []
    for i, g in enumerate(groups):
        _w = cell_w(g.get('Name', ''))
        if _gx > 0 and _gx + _w > GRID_MAX_X:
            _gx = 0; _gy += GROUP_ROW
        g.update({'Type':'TGroup','Pos.x':_gx,'Pos.y':Y_GROUP + _gy,'Parent':-1,'#':base+i})
        _group_pos.append((_gx, Y_GROUP + _gy))
        _gx += _w
    _groups_bottom = Y_GROUP + _gy + GROUP_ROW
    base += len(groups)

    # Pre-state ops get IDs before state
    op_base = base
    n_pre_ops = 1 + sum(1 for c in dialog_begin_chunks if c)  # Global + non-empty pre-state chunks
    base += n_pre_ops

    # State index -> owning group position (group['+State'] is an index into states).
    _state_owner = {}
    for _gi, g in enumerate(groups):
        _si = g.get('+State', -1)
        if isinstance(_si, int) and 0 <= _si < len(states) and _si not in _state_owner:
            _state_owner[_si] = _group_pos[_gi]
    # Owned states sit under their group (x, y+25); orphans flow in rows below the grid.
    Y_STATE_ORPHAN = _groups_bottom + 25
    _ox = _oy = 0
    _state_bottom = Y_STATE_ORPHAN
    for i, s in enumerate(states):
        if i in _state_owner:
            _gx0, _gy0 = _state_owner[i]
            sx, sy = _gx0, _gy0 + 25
        else:
            _w = cell_w(s.get('Name', ''))
            if _ox > 0 and _ox + _w > GRID_MAX_X:
                _ox = 0; _oy += 25
            sx, sy = _ox, Y_STATE_ORPHAN + _oy
            _ox += _w
        s.update({'Type':'TState','Pos.x':sx,'Pos.y':sy,'Parent':-1,'#':base+i})
        if sy > _state_bottom:
            _state_bottom = sy
    Y_STATE = _state_bottom
    base += len(states)

    # Layout dialogs, msgs, answers horizontally with wrapping; empty sections
    # don't consume vertical space.
    Y_DLG = Y_STATE + (100 if dialogs else 45)

    dlg_rows = layout_horizontal(dialogs, Y_DLG, lambda d: cell_w(d['Name'], min_w=120))
    dlg_bottom = max(dlg_rows) + 25 if dialogs else Y_DLG
    Y_MSG = dlg_bottom + (45 if msgs else 0)

    msg_rows = layout_horizontal(msgs, Y_MSG, lambda m: 150)
    msg_bottom = max(msg_rows) + 25 if msgs else Y_MSG
    Y_ANS = msg_bottom + (45 if answers else 0)

    ans_rows = layout_horizontal(answers, Y_ANS, lambda a: 120)
    ans_bottom = max(ans_rows) + 25 if answers else Y_ANS
    Y_OPS_PRE = ans_bottom + (45 if (dialogs or msgs or answers) else 0)

    # Assign # to dialogs
    for i, d in enumerate(dialogs):
        d_clean = {'Name': d['Name']}
        d_clean.update({'Type':'TDialog','Pos.x':d['Pos.x'],'Pos.y':d['Pos.y'],
                        'Parent':-1,'#':base+i})
        if d.get('_has_turn_dlg'):
            d_clean['_has_turn_dlg'] = True
        dialogs[i] = d_clean
    base += len(dialogs)

    # Assign # to msgs
    for i, m in enumerate(msgs):
        msg_text = lang.get(msg_ct_refs[i], '') if i < len(msg_ct_refs) else ''
        m.update({'Type':'TDialogMsg','Parent':-1,'#':base+i,
                  'Msg':msg_text,'DMsg.Num':str(i)})
        orig_dtext = msg_orig_dtexts[i] if i < len(msg_orig_dtexts) else ''
        if orig_dtext:
            m['_orig_dtext'] = orig_dtext
    base += len(msgs)

    # Assign # to answers
    for i, a in enumerate(answers):
        ans_text = lang.get(ans_ct_refs[i], '') if i < len(ans_ct_refs) else ''
        a.update({'Type':'TDialogAnswer','Parent':-1,'#':base+i,
                  'Msg':ans_text,'AMsg.Num':str(i)})
        orig_da = ans_orig_dacodes[i] if i < len(ans_orig_dacodes) else ''
        if orig_da:
            a['_orig_danswer'] = orig_da
    base += len(answers)

    # Assign IDs and positions to ops
    op_ids = list(range(op_base, op_base+n_pre_ops)) + list(range(base, base+len(ops)-n_pre_ops))

    pre_type_x = {'Global': 0, 'Init': 60, 'Turn': 120, 'DialogBegin': 180}
    pre_x_counter = 0
    dlg_ti = msg_ti = ans_ti = state_ti = 0

    for i, op in enumerate(ops):
        typ = op.pop('_pos_y_type', '')
        op['_pos_y_type_saved'] = typ
        op.pop('_pos_y', None)
        op['#'] = op_ids[i]
        op['Type'] = 'Top'
        op['Parent'] = -1
        ct = op['Code.Type']

        if not typ:
            # Pre-state op
            px = pre_type_x.get(ct, pre_x_counter * 60)
            pre_x_counter += 1
            op['Pos.x'] = px
            op['Pos.y'] = Y_OPS_PRE
        elif typ == 'state':
            # Turn-код состояния — вплотную слева от своего TState (как в
            # интерактивном декомпиле редактора: x-20, тот же y). Итерация
            # зеркалит порядок создания опов: по states с непустым _code.
            while state_ti < len(states) and not states[state_ti].get('_code', ''):
                state_ti += 1
            if state_ti < len(states):
                par = states[state_ti]
                op['Pos.x'] = par['Pos.x'] - 20
                op['Pos.y'] = par['Pos.y']
                state_ti += 1
            else:
                op['Pos.x'] = 0; op['Pos.y'] = Y_OPS_PRE
        elif typ == 'dlg':
            if dlg_ti < len(dialogs):
                par = dialogs[dlg_ti]
                op['Pos.x'] = par['Pos.x'] - 20
                op['Pos.y'] = par['Pos.y']
                dlg_ti += 1
            else:
                op['Pos.x'] = 0; op['Pos.y'] = Y_OPS_PRE
        elif typ == 'msg':
            if msg_ti < len(msgs):
                par = msgs[msg_ti]
                op['Pos.x'] = par['Pos.x'] - 20
                op['Pos.y'] = par['Pos.y']
                msg_ti += 1
            else:
                op['Pos.x'] = 0; op['Pos.y'] = Y_OPS_PRE
        elif typ == 'ans':
            while ans_ti < len(answers) and not answers[ans_ti].get('_has_turn'):
                ans_ti += 1
            if ans_ti < len(answers):
                par = answers[ans_ti]
                op['Pos.x'] = par['Pos.x'] - 20
                op['Pos.y'] = par['Pos.y']
                ans_ti += 1
            else:
                op['Pos.x'] = 0; op['Pos.y'] = Y_OPS_PRE

    # Ensure no two objects share the same (Pos.x, Pos.y) — offset duplicates by 80 on x
    all_objs = (variables + stars + ships + planets + places + items + groups + states + dialogs +
                msgs + answers + ops)
    seen_pos = {}
    for obj in all_objs:
        key = (obj.get('Pos.x', 0), obj.get('Pos.y', 0))
        while key in seen_pos:
            obj['Pos.x'] = obj.get('Pos.x', 0) + 80
            key = (obj.get('Pos.x', 0), obj.get('Pos.y', 0))
        seen_pos[key] = obj

    # Resolve place Obj1 from ref name (star/planet/ship/group name → '#')
    _ref_to_id = {}
    for _obj in stars + ships + planets + groups + items:
        _n = _obj.get('Name', '')
        if _n: _ref_to_id[_n] = _obj['#']
    for pl in places:
        _ref = pl.pop('ref', '')
        if _ref and _ref in _ref_to_id:
            pl['Obj1'] = _ref_to_id[_ref]

    vo = {'ConstellationCount':const_count,'DialogAnswers':answers,
          'DialogMessages':msgs,'Dialogs':dialogs,'Groups':groups,'Items':items,
          'Operations':ops,'Places':places,'Planets':planets,'Ships':ships,
          'Stars':stars,'States':states,'Variables':variables}

    # Resolve state MoveObj/TakeItem/Attack.Items names -> global # indices.
    # On a name clash the section listed last wins: places in scripts without
    # global variables, items in scripts with them (as it always was).
    if h2 == 0:
        _sections = ('Stars', 'Planets', 'Ships', 'Groups', 'Items', 'Places')
    else:
        _sections = ('Stars', 'Planets', 'Ships', 'Groups', 'Places', 'Items')
    name_to_id = {}
    for section in _sections:
        for obj in vo.get(section, []):
            n = obj.get('Name','')
            if n: name_to_id[n] = obj['#']
    for st in states:
        mo = st.get('MoveObj')
        if isinstance(mo, str) and mo:
            st['MoveObj'] = name_to_id.get(mo, -1)
        ti = st.get('TakeItem')
        if isinstance(ti, str) and ti:
            st['TakeItem'] = name_to_id.get(ti, -1)
        ai = st.get('Attack.Items', [])
        if ai and isinstance(ai[0], str):
            resolved = {}
            for idx, nm in enumerate(ai):
                resolved[f'Target.{idx}'] = name_to_id.get(nm, -1)
            st['Attack.Items'] = [resolved]

    # Build Visual.Links
    def find_id(name, section):
        for obj in vo.get(section, []):
            if obj.get('Name') == name: return obj.get('#')
        return None

    links = []
    # State -> their Turn ops (kavscr/nt_fmt per-state inline codes)
    state_turn_ops = [op for op in ops if op['Code.Type']=='Turn' and op.get('_pos_y_type_saved')=='state']
    _state_turn_iter = iter(state_turn_ops)
    for s in states:
        if s.get('_code', '').strip():
            _t_op = next(_state_turn_iter, None)
            if _t_op is not None:
                links.append({'Type':'TGraphLink','Begin':s['#'],'End':_t_op['#'],'Nom':0,'Arrow':True})
    # Dialog/Msg/Answer -> their Turn ops
    dlg_turn_ops = [op for op in ops if op['Code.Type']=='Turn' and op.get('_pos_y_type_saved')=='dlg']
    # Turn ops exist only for dialogs with non-empty code — pair via an
    # iterator gated by the per-dialog flag (same fix as messages).
    _dlg_turn_iter = iter(dlg_turn_ops)
    for i,d in enumerate(dialogs):
        d_id = d.get('#')
        if d.get('_has_turn_dlg'):
            t_op = next(_dlg_turn_iter, None)
            if t_op:
                links.append({'Type':'TGraphLink','Begin':d_id,'End':t_op['#'],'Nom':0,'Arrow':True})
    # Turn ops exist only for messages with non-empty turn code, so walk the
    # ops with an iterator gated by each message's code presence (msgs and
    # msg_turn_codes are index-parallel).
    msg_turn_ops = [op for op in ops if op['Code.Type']=='Turn' and op.get('_pos_y_type_saved')=='msg']
    msg_turn_iter = iter(msg_turn_ops)
    for i,m in enumerate(msgs):
        if i < len(msg_turn_codes) and msg_turn_codes[i]:
            t_op = next(msg_turn_iter, None)
            if t_op:
                links.append({'Type':'TGraphLink','Begin':m['#'],'End':t_op['#'],'Nom':0,'Arrow':True})
    ans_turn_ops = [op for op in ops if op['Code.Type']=='Turn' and op.get('_pos_y_type_saved')=='ans']
    ans_turn_iter = iter(ans_turn_ops)
    for a in answers:
        if a.get('_has_turn'):
            t_op = next(ans_turn_iter, None)
            if t_op:
                links.append({'Type':'TGraphLink','Begin':a['#'],'End':t_op['#'],'Nom':0,'Arrow':True})
    # Ship -> Star, Planet -> Star, Group -> Planet, Group -> State
    for ship in ships:
        star_id = find_id(ship.get('+Star',''), 'Stars')
        if star_id is not None:
            links.append({'Type':'TGraphLink','Begin':ship['#'],'End':star_id,'Nom':0,'Arrow':True})
    for planet in planets:
        star_id = find_id(planet.get('+Star',''), 'Stars')
        if star_id is not None:
            links.append({'Type':'TGraphLink','Begin':planet['#'],'End':star_id,'Nom':0,'Arrow':True})
    for item in items:
        # Item links to its Place. The +Place ref may name a Place, Group,
        # Planet, Ship or Star (items may sit directly on a planet) — resolve
        # through all of them, not just Places/Groups.
        _ipl = item.get('+Place', '')
        place_id = None
        for _isec in ('Places', 'Groups', 'Planets', 'Ships', 'Stars'):
            place_id = find_id(_ipl, _isec)
            if place_id is not None:
                break
        if place_id is not None:
            links.append({'Type':'TGraphLink','Begin':item['#'],'End':place_id,'Nom':0,'Arrow':True})
    for pl in places:
        # Place links to its star (not to Obj1)
        star_id = find_id(pl.get('+Star',''), 'Stars')
        if star_id is not None:
            links.append({'Type':'TGraphLink','Begin':pl['#'],'End':star_id,'Nom':0,'Arrow':True})
    # TStarLink: a star's distance condition to an earlier star of the script
    for s in stars:
        for _other, _dmin, _dmax, _hole in s['_links']:
            if 0 <= _other < len(stars):
                links.append({'Type':'TStarLink',
                              'Begin': s['#'],
                              'End':   stars[_other]['#'],
                              'Nom':   0,
                              'Arrow': False,
                              'DistMin': _dmin,
                              'DistMax': _dmax,
                              'Hole':   bool(_hole)})
    for group in groups:
        planet_id = find_id(group.get('+Planet',''), 'Planets')
        if planet_id is not None:
            links.append({'Type':'TGraphLink','Begin':group['#'],'End':planet_id,'Nom':0,'Arrow':True})
        state_idx = group.get('+State', -1)
        if state_idx >= 0 and state_idx < len(states):
            links.append({'Type':'TGraphLink','Begin':group['#'],'End':states[state_idx]['#'],'Nom':0,'Arrow':True})
    # TGroupLink: relations between groups (Rel1 = first to second, Rel2 back)
    # and the allowed strength balance; Nom numbers the links of one pair.
    _pair_seen = {}
    for _g1, _g2, _r12, _r21, _wmin, _wmax in relations:
        if not (0 <= _g1 < len(groups) and 0 <= _g2 < len(groups)):
            continue
        _pair = frozenset((_g1, _g2))
        links.append({'Type':'TGroupLink','Begin':groups[_g1]['#'],'End':groups[_g2]['#'],
                      'Nom':_pair_seen.get(_pair, 0),'Arrow':True,'Rel1':_r12,'Rel2':_r21,
                      'WarWeightMin':_fmt_place_float(_wmin),
                      'WarWeightMax':_fmt_place_float(_wmax)})
        _pair_seen[_pair] = _pair_seen.get(_pair, 0) + 1
    # kavscr multi-star: group Dialog stored as wstring name -> resolve to id
    for group in groups:
        dlg_val = group.get('Dialog')
        if isinstance(dlg_val, str) and dlg_val:
            dlg_id = find_id(dlg_val, 'Dialogs')
            group['Dialog'] = dlg_id if dlg_id is not None else -1
        elif dlg_val is None or dlg_val == 0:
            group['Dialog'] = -1

    # Place Obj1 holds an object NAME after parse — resolve to its global #
    # (planet/group/item/ship); RScript errors out on unresolved type-1 places.
    for _plc in places:
        for _ofield in ('Obj1', 'Obj2'):
            _ov = _plc.get(_ofield)
            if isinstance(_ov, str) and _ov:
                _oid = None
                # type-6 places reference variables (e.g. QuestPlaceX/Y); include
                # Variables in the search.
                for _sec in ('Planets', 'Groups', 'Items', 'Ships', 'Stars', 'Variables'):
                    _oid = find_id(_ov, _sec)
                    if _oid is not None:
                        break
                _plc[_ofield] = _oid if _oid is not None else -1

    # Planet Dialog may be a wstring name — resolve to the
    # dialog's global # (RScript EInvalidCast on string here).
    for _pl in planets:
        _pdlg = _pl.get('Dialog')
        if isinstance(_pdlg, str) and _pdlg:
            _pid = find_id(_pdlg, 'Dialogs')
            _pl['Dialog'] = _pid if _pid is not None else -1

    for op in ops:
        op.pop('_pos_y_type_saved', None)
        if 'Code' in op:
            op['Code'] = [escape_code_line(l) for l in op['Code']]
        # NOTE: CT refs in Operations code are kept verbatim (no resolve_ct_in_lines).
        # RScript re-keys all texts on compile; literal text would get new sequential
        # keys, breaking round-trip. Verbatim CT refs keep the original key visible
        # for the binary patcher to restore.
    for a in answers:
        a.pop('_has_turn', None)
    for d in dialogs:
        d.pop('_has_turn_dlg', None)
    for s in states:
        s.pop('_code', None)
    for star in stars:
        star.pop('_links', None)

    # Канон редактора: порядок ключей узла Type, Name, Pos.x, Pos.y, Parent, #,
    # затем полезная нагрузка; пустой Attack.Items редактор не пишет вовсе
    # (сверено с авторскими rson и выводом интерактивного декомпила RScript).
    _HEAD = ('Type', 'Name', 'Pos.x', 'Pos.y', 'Parent', '#')
    for _kind, _kind_list in vo.items():
        if not isinstance(_kind_list, list):
            continue
        # У звёзд Constellation идёт сразу после служебной шапки.
        _head = _HEAD + ('Constellation',) if _kind == 'Stars' else _HEAD
        for _i, _n in enumerate(_kind_list):
            if not isinstance(_n, dict):
                continue
            if 'Attack.Items' in _n and not _n['Attack.Items']:
                _n.pop('Attack.Items')
            _kind_list[_i] = {
                **{k: _n[k] for k in _head if k in _n},
                **{k: v for k, v in _n.items() if k not in _head},
            }
    return vo, links


PARSE_TIMEOUT = 10   # seconds per file; increase for very large mods

def decompile(scr_path, out_path=None, lang_path=None):
    import gc
    # Use a mutable container for data so we can zero it out on timeout,
    # releasing the large byte allocation even while the worker thread is still running.
    _data = [open(scr_path, 'rb').read()]
    logger.info('[~] Starting  %s (%d bytes)', Path(scr_path).name, len(_data[0]))
    result = [None]; error = [None]
    def _worker():
        try:
            data = _data[0]          # local ref; _data[0] may be cleared on timeout
            if data is None: return  # already cancelled
            lang = load_lang(lang_path) if lang_path else {}
            result[0] = parse(data, lang)
        except Exception as e:
            import traceback
            error[0] = (e, traceback.format_exc())
        finally:
            # Always release our local copy so the bytes object can be GC'd
            data = None  # noqa: F841
    t = threading.Thread(target=_worker, daemon=True)
    t.start(); t.join(PARSE_TIMEOUT)
    if t.is_alive():
        logger.error('[!] TIMEOUT %s (>%ds) — skipping', Path(scr_path).name, PARSE_TIMEOUT)
        _data[0] = None   # zero out the data ref in the shared container → bytes freed ASAP
        gc.collect()
        return
    if error[0]:
        e, tb = error[0]
        m_ver = re.match(r'format version (\d+) — skipped', str(e))
        if m_ver:
            logger.info('[~] Skipped   %s — format version %s (not supported)',
                        Path(scr_path).name, m_ver.group(1))
        else:
            logger.error('[!] FAILED %s: %s\n%s', scr_path, e, tb)
        _data[0] = None; gc.collect()
        return
    vo, links = result[0]
    _data[0] = None; result[0] = None  # free binary data before building JSON
    if not out_path:
        out_path = str(Path(scr_path).with_suffix('.rson'))
    script_name = Path(scr_path).stem
    # The binary's internal script name (used in CT refs like 'Script.<name>.N')
    # may differ from the filename (e.g. Mod_Foo_.scr may contain
    # 'Script.Mod_Foo.'). RScript names new CT keys after ScriptName,
    # so use the internal one for round-trip fidelity.
    try:
        raw = Path(scr_path).read_bytes()
        m_sn = re.search(br'"\x00S\x00c\x00r\x00i\x00p\x00t\x00\.\x00((?:[\w]\x00)+)\.\x00',
                         raw)
        if m_sn:
            internal = m_sn.group(1).decode('utf-16-le')
            if internal and internal != script_name:
                logger.info('    internal ScriptName %r (file stem %r)', internal, script_name)
                script_name = internal
    except Exception:
        pass
    # Полная шапка редактора (как в авторских сохранениях). ExportLang* и
    # LangDat*/MainDat*/CacheDat* оставляем пустыми/False: непустой LangDatOut
    # с ExportLangDat=true заставил бы редактор ПЕРЕЗАПИСАТЬ Lang.dat мода
    # при компиляции.
    # Информационная плашка — как её ставит интерактивный SCR-декомпил самого
    # редактора (текст правил преобразования тот же).
    banner = {
        'Type': 'TGraphRectText',
        'Rect.Left': 632, 'Rect.Top': -268, 'Rect.Right': 1339, 'Rect.Bottom': -80,
        'FStyle': 0, 'FColor': 10710818, 'BStyle': 0, 'BColor': 14474460,
        'BSize': 1, 'BCoef': '0.300000011920929',
        'AlignX': 0, 'AlignY': 0, 'AlignRect': False,
        'Text': (f'SCR Decompiling {script_name}\r\n\r\n'
                 'ChangeState(...) -> Link to StateName\r\n'
                 'DChange(...) -> Link to DMsg.Num\r\n'
                 'DAdd(...) -> Link to AMsg.Num'),
        'Color': 16777215, 'Font': 'Segoe UI', 'FontSize': 10,
        'fsBold': True, 'fsItalic': False, 'fsUnderline': False,
    }
    rson = {
        'FileID': 573785173,
        'FileVersion': 8,
        'ViewPos.x': -75,
        'ViewPos.y': -186,
        'ScriptName': script_name,
        'ScriptFileOut': f'D:\\{script_name}.scr',
        'ScriptTextOut': f'D:\\{script_name}.txt',
        'LangDatIn': '',
        'LangDatOut': '',
        'MainDatIn': '',
        'MainDatOut': '',
        'CacheDatIn': '',
        'CacheDatOut': '',
        'ExportLangTxt': False,
        'ExportLangDat': False,
        'Visual.Objects': [vo],
        'Visual.Links': links,
        'BlockPar.EC.Total.Strings': 0,
        'BlockPar.EC': [],
        'Rect.Text': [banner],
    }
    json.dump(rson, open(out_path,'w',encoding='utf-8'), ensure_ascii=False, indent=4)
    logger.info('[+] Done      %s -> %s', Path(scr_path).name, out_path)
    for k in ('Variables','Operations','Stars','Planets','Ships','Groups','States','Dialogs','DialogMessages','DialogAnswers'):
        logger.debug('    %-20s: %d', k, len(vo[k]))
    del vo, links, rson; gc.collect()

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Space Rangers SCR decompiler')
    ap.add_argument('scr', help='.scr file or directory for batch mode')
    ap.add_argument('-o','--output', default=None)
    ap.add_argument('-l','--lang', default=None, help='Lang.txt or Lang.dat')
    ap.add_argument('--log', default=None, metavar='FILE',
                    help='Write log to FILE (default: stderr only)')
    ap.add_argument('-v','--verbosity', default='brief',
                    choices=['verbose','brief','errors'],
                    help='Log verbosity: verbose=all details, brief=per-file summary (default), errors=failures only')
    a = ap.parse_args()

    setup_logging(log_file=a.log, verbosity=a.verbosity)

    scr_path = Path(a.scr)

    if scr_path.is_dir():
        # Catalog mode: recursively find all .scr files under the given directory.
        # For each .scr file, the lang file is resolved by walking UP the directory tree
        # from the .scr file and checking each ancestor's CFG/Rus/Lang.txt (and .dat).
        # This covers the layout:
        #   <catalog>/<mod>/data/scripts/*.scr
        #   <catalog>/<mod>/CFG/Rus/Lang.txt
        # as well as any other nesting depth.

        def find_lang_for_scr(scr_file: Path):
            # Walk up from scr_file's parent toward catalog root,
            # at each level check <dir>/CFG/Rus/Lang.txt and <dir>/CFG/Rus/Lang.dat
            d = scr_file.parent
            while True:
                for ext in ['Lang.txt', 'Lang.dat']:
                    candidate = d / 'CFG' / 'Rus' / ext
                    if candidate.exists():
                        return str(candidate)
                # Also check plain Lang.txt/Lang.dat right in this dir (fallback)
                for ext in ['Lang.txt', 'Lang.dat']:
                    candidate = d / ext
                    if candidate.exists():
                        return str(candidate)
                if d == scr_path or d == d.parent:
                    break
                d = d.parent
            return None

        scr_files = sorted(scr_path.rglob('*.scr'))
        if not scr_files:
            logger.error('[!] No .scr files found under %s', scr_path)
            sys.exit(1)

        # Determine output directory: -o sets a flat output folder; without -o, .rson goes beside .scr
        out_dir = Path(a.output) if a.output else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        ok, fail = 0, 0
        for f in scr_files:
            file_lang = a.lang or find_lang_for_scr(f)
            if out_dir:
                out = str(out_dir / f.with_suffix('.rson').name)
            else:
                out = str(f.with_suffix('.rson'))
            try:
                decompile(str(f), out, file_lang)
                ok += 1
            except Exception as e:
                logger.error('[!] FAILED %s: %s', f.name, e)
                fail += 1
        logger.info('[+] Done: %d OK, %d failed', ok, fail)
    else:
        # Single file mode
        lang = a.lang
        if not lang:
            for candidate in [scr_path.parent/'Lang.txt', scr_path.parent/'Lang.dat']:
                if candidate.exists(): lang = str(candidate); break
        # If -o is an existing directory, put the .rson inside it
        out = a.output
        if out and Path(out).is_dir():
            out = str(Path(out) / scr_path.with_suffix('.rson').name)
        decompile(str(scr_path), out, lang)

