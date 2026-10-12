# -*- coding: utf-8 -*-
"""Тесты чтения .scr: decompiler.read_scr_layout и разбор parse() поверх него.

Порядок и размеры полей взяты из загрузчика движка (TScript.LoadFromBuffer в
декомпиляции SpaceRangersHD_decomp), а не подобраны по файлам. Прежний разбор
угадывал раскладку эвристиками и на части модов читал со сдвигом на байт
(«иероглифы» в коде), падал или путал Init/Turn/DialogBegin. Здесь файлы
собираются в самом тесте — так в публичный репозиторий не попадают чужие .scr,
а каждое поле имеет известное значение.
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import decompiler  # noqa: E402

decompiler.logger.disabled = True


class Scr:
    """Минимальный писатель .scr в порядке загрузчика игры."""

    def __init__(self, version=8):
        self.version = version
        self.b = bytearray()

    def u8(self, v):
        self.b += struct.pack('<B', v)
        return self

    def u32(self, v):
        self.b += struct.pack('<I', v & 0xFFFFFFFF)
        return self

    def f32(self, v):
        self.b += struct.pack('<f', v)
        return self

    def s(self, text):
        self.b += text.encode('utf-16-le') + b'\0\0'
        return self

    def vars(self, items):
        self.u32(len(items))
        for name, kind, value in items:
            self.s(name).u8(kind)
            if kind in (1, 2):
                self.u32(value)
            elif kind == 3:
                self.b += struct.pack('<d', value)
            elif kind == 4:
                self.s(value)
            elif kind == 9:
                self.vars(value)
        return self


def build(version=8, globals_=(), global_code='', locals_=(), stars=None, places=(),
          items=(), groups=(), relations=(), init='', turn='', dialog_begin='',
          states=(), dialogs=(), messages=(), answers=()):
    w = Scr(version)
    w.u32(version).u32(0)
    w.vars(list(globals_)).s(global_code)
    struct.pack_into('<I', w.b, 4, len(w.b))
    w.vars(list(locals_))
    w.u32(0)                                   # constellations
    stars = stars if stars is not None else [{'name': 'Star0'}]
    w.u32(len(stars))
    for st in stars:
        w.s(st['name']).u32(st.get('constellation', -1))
        if version < 7:
            w.u8(0)
        w.u8(st.get('no_kling', 0)).u8(st.get('no_come_kling', 0))
        w.u32(len(st.get('links', ())))
        for other, dmin, dmax, hole in st.get('links', ()):
            w.u32(other)
            if version < 7:
                w.u32(0)
            w.u32(dmin).u32(dmax)
            if version < 7:
                w.u32(0).u32(0).u32(0)
            w.u8(hole)
        w.u32(len(st.get('planets', ())))
        for name, dialog in st.get('planets', ()):
            w.s(name).u32(1).u32(2).u32(3).u32(4).u32(10).u32(90).s(dialog)
        w.u32(len(st.get('ships', ())))
        for ship in st.get('ships', ()):
            w.u32(ship.get('count', 1)).u32(62).u32(126).u8(ship.get('player', 0))
            for v in (100, 10000, 0, 0, 0):
                w.u32(v)
            if version < 7:
                w.u32(0).u32(0)
            for v in (0, 100, 0, 100, 0, 100):
                w.u32(v)
            if version < 7:
                w.u32(0).u32(0)
            w.f32(ship.get('strength_min', 0.0)).f32(ship.get('strength_max', 0.0))
            w.s(ship.get('ruins', ''))
    w.u32(len(places))
    for p in places:
        w.s(p['name']).s(p.get('star', '')).u32(p['type'])
        t = p['type']
        if t == 0:
            w.f32(p.get('angle', 0.0)).f32(p.get('dist', 0.0)).u32(p.get('radius', 0))
        elif t in (1, 4):
            w.s(p['obj']).u32(p.get('radius', 0))
        elif t == 2:
            w.s(p['obj'])
        elif t in (3, 5):
            w.s(p['obj']).f32(p.get('dist', 0.0)).u32(p.get('radius', 0)).f32(p.get('angle', 0.0))
        elif t == 6:
            w.s(p['obj']).s(p['obj2']).u32(p.get('radius', 0))
    w.u32(len(items))
    for name, place in items:
        w.s(name).s(place)
        for v in (3, 5, 10, 2, 0, 0):
            w.u32(v)
        w.s('')
    w.u32(len(groups))
    for g in groups:
        w.s(g['name']).s(g.get('planet', '')).u32(g.get('state', 0)).u32(62).u32(126)
        for v in (1, 2, 100, 10000, 0, 0, 0):
            w.u32(v)
        if version < 7:
            w.u32(0)
        w.u8(g.get('add_player', 0))
        if version < 7:
            w.u32(0).u32(0).u32(0).u32(0)
        for v in g.get('status', (0, 100, 0, 100, 0, 100)):
            w.u32(v)
        w.u32(g.get('dist_search', 10000))
        w.s(g.get('dialog', '')).f32(g.get('strength_min', 0.0)).f32(g.get('strength_max', 0.0))
        w.s(g.get('ruins', ''))
    w.u32(len(relations))
    for g1, g2 in relations:
        w.u32(g1).u32(g2).u32(5).u32(5).f32(0.0).f32(1000.0)
    w.s(init).s(turn)
    if version > 5:
        w.s(dialog_begin)
    w.u32(len(states))
    for st in states:
        w.s(st['name']).u32(st.get('move', 0))
        if st.get('move', 0) not in (0, 5):
            w.s(st.get('target', ''))
        w.u32(len(st.get('attack', ())))
        for name in st.get('attack', ()):
            w.s(name)
        w.s(st.get('take_item', '')).u8(st.get('take_all', 0))
        w.s(st.get('on_talk', '')).s(st.get('on_act', '')).s(st.get('ether', ''))
        w.s(st.get('code', ''))
    w.u32(len(dialogs))
    for name, code in dialogs:
        w.s(name).s(code)
    w.u32(len(messages))
    for name, code in messages:
        w.s(name).s(code)
    w.u32(len(answers))
    for name, code, turn_code in answers:
        w.s(name).s(code).s(turn_code)
    return bytes(w.b)


def ops_by_type(vo):
    out = {}
    for op in vo['Operations']:
        out.setdefault(op['Code.Type'], []).append(op['Code'])
    return out


class LayoutTest(unittest.TestCase):

    def test_every_field_in_place(self):
        data = build(
            globals_=[('gFlag', 1, 7)], global_code='GRun();',
            locals_=[('vText', 4, 'hi'), ('vList', 9, [('', 1, 5), ('', 0, None)]),
                     ('vReal', 3, 1.5)],
            stars=[{'name': 'Home', 'planets': [('Earth', 'EarthTalk')],
                    'ships': [{'count': 2, 'strength_min': 1.25, 'ruins': 'Base'}]},
                   {'name': 'Far', 'links': [(0, 5, 40, 1)]}],
            places=[{'name': 'P0', 'star': 'Home', 'type': 0, 'angle': 90.0, 'dist': 0.25, 'radius': 300},
                    {'name': 'P1', 'star': 'Home', 'type': 1, 'obj': 'Earth', 'radius': 75},
                    {'name': 'P2', 'star': 'Home', 'type': 2, 'obj': 'Earth'},
                    {'name': 'P3', 'star': 'Home', 'type': 3, 'obj': 'Far', 'dist': 0.5, 'radius': 50, 'angle': 45.0},
                    {'name': 'P6', 'star': 'Home', 'type': 6, 'obj': 'vX', 'obj2': 'vY', 'radius': 150}],
            items=[('Box', 'P0')],
            groups=[{'name': 'Guards', 'planet': 'Earth', 'state': 1, 'add_player': 1,
                     'dialog': 'GuardTalk', 'strength_min': 0.5}],
            init='int vI;', turn='TurnCode();', dialog_begin='BeginCode();',
            states=[{'name': 'Idle', 'move': 0, 'attack': ['Guards'], 'take_all': 1},
                    {'name': 'Follow', 'move': 2, 'target': 'Earth', 'on_talk': 'GuardTalk',
                     'on_act': '[t_OnStep]Act();', 'code': 'StateTurn();'}],
            dialogs=[('GuardTalk', 'DChange(0);')],
            messages=[('', 'DText(CT("Script.T.0"));')],
            answers=[('bye', "DAnswer('bye~'+CT(\"Script.T.1\"));", 'DialogEnd();')])
        lay = decompiler.read_scr_layout(data)
        self.assertEqual(lay['version'], 8)
        self.assertEqual(lay['globals'], [('gFlag', 1, 7)])
        self.assertEqual(lay['global_code'], 'GRun();')
        self.assertEqual([v[0] for v in lay['locals']], ['vText', 'vList', 'vReal'])
        self.assertEqual(lay['locals'][1][2], [('', 1, 5), ('', 0, None)])
        home, far = lay['stars']
        self.assertEqual(home['planets'][0]['dialog'], 'EarthTalk')
        self.assertEqual(home['ships'][0]['ruins'], 'Base')
        self.assertEqual(far['links'], [(0, 5, 40, 1)])
        self.assertEqual([p['type'] for p in lay['places']], [0, 1, 2, 3, 6])
        self.assertEqual(lay['places'][2]['obj'], 'Earth')
        self.assertEqual(lay['places'][4]['obj2'], 'vY')
        self.assertEqual(lay['groups'][0]['add_player'], 1)
        self.assertEqual(lay['groups'][0]['dialog'], 'GuardTalk')
        self.assertEqual((lay['init_code'], lay['turn_code'], lay['dialog_begin_code']),
                         ('int vI;', 'TurnCode();', 'BeginCode();'))
        idle, follow = lay['states']
        self.assertEqual((idle['target'], idle['attack'], idle['take_all']), ('', ['Guards'], 1))
        self.assertEqual((follow['target'], follow['on_talk'], follow['on_act'], follow['code']),
                         ('Earth', 'GuardTalk', '[t_OnStep]Act();', 'StateTurn();'))
        self.assertEqual(lay['answers'][0][2], 'DialogEnd();')

        vo, links = decompiler.parse(data, {})
        ops = ops_by_type(vo)
        self.assertEqual(ops['Init'], [['int vI;']])
        self.assertIn(['TurnCode();'], ops['Turn'])
        self.assertEqual(ops['DialogBegin'], [['BeginCode();']])
        self.assertEqual([v['Name'] for v in vo['Variables']], ['gFlag', 'vText', 'vList', 'vReal'])
        self.assertEqual([v['Global'] for v in vo['Variables']], [True, False, False, False])
        self.assertEqual(vo['Ships'][0]['StrengthMin'], '1.25')
        self.assertEqual(vo['Groups'][0]['StrengthMin'], '0.5')
        self.assertTrue(vo['Groups'][0]['AddPlayer'])
        self.assertEqual(vo['Groups'][0]['Dialog'], vo['Dialogs'][0]['#'])
        follow_rson = vo['States'][1]
        self.assertEqual((follow_rson['OnTalk'], follow_rson['OnActCode']),
                         ('GuardTalk', '[t_OnStep]Act();'))
        self.assertEqual(follow_rson['MoveObj'], vo['Planets'][0]['#'])
        self.assertEqual(vo['States'][0]['Attack.Items'], [{'Target.0': vo['Groups'][0]['#']}])
        star_links = [l for l in links if l['Type'] == 'TStarLink']
        self.assertEqual(star_links, [{'Type': 'TStarLink', 'Begin': vo['Stars'][1]['#'],
                                       'End': vo['Stars'][0]['#'], 'Nom': 0, 'Arrow': False,
                                       'DistMin': 5, 'DistMax': 40, 'Hole': True}])
        places = {p['Name']: p for p in vo['Places']}
        self.assertEqual((places['P0']['Angle'], places['P0']['Dist'], places['P0']['Radius']),
                         ('90', '0.25', 300))
        self.assertEqual((places['P3']['Angle'], places['P3']['Dist'], places['P3']['Obj1']),
                         ('45', '0.5', vo['Stars'][1]['#']))

    def test_relations_do_not_shift_the_code_slots(self):
        # Отношения групп лежат между группами и тремя слотами кода. Прежний
        # разбор считал первые два слова после групп пустыми «заглушками» и с
        # записью отношения сдвигал подписи: ход становился Init, начало
        # диалога — Turn (PC_final, bountyhunter, ShuQuest).
        data = build(groups=[{'name': 'A'}, {'name': 'B'}], relations=[(0, 1), (1, 0)],
                     turn='TurnOnly();', dialog_begin='DlgBegin();',
                     states=[{'name': 'S'}])
        vo, links = decompiler.parse(data, {})
        ops = ops_by_type(vo)
        self.assertNotIn('Init', ops)
        self.assertEqual(ops['DialogBegin'], [['DlgBegin();']])
        self.assertIn(['TurnOnly();'], ops['Turn'])
        # Отношения выводятся так, как их хранит RSON автора: TGroupLink.
        a, b = (g['#'] for g in vo['Groups'])
        self.assertEqual([l for l in links if l['Type'] == 'TGroupLink'], [
            {'Type': 'TGroupLink', 'Begin': a, 'End': b, 'Nom': 0, 'Arrow': True,
             'Rel1': 5, 'Rel2': 5, 'WarWeightMin': '0', 'WarWeightMax': '1000'},
            {'Type': 'TGroupLink', 'Begin': b, 'End': a, 'Nom': 1, 'Arrow': True,
             'Rel1': 5, 'Rel2': 5, 'WarWeightMin': '0', 'WarWeightMax': '1000'}])

    def test_state_fields_by_move_type(self):
        # Цель хода есть только у типов, кроме 0 (стоять) и 5 (обычный ИИ); у
        # целевого типа строка цели бывает и пустой. Ошибка в длине любого поля
        # сдвигала всё дальнейшее на 1–2 байта — «иероглифы» в коде.
        states = [{'name': 'A', 'move': 5, 'take_all': 1},
                  {'name': 'B', 'move': 3, 'target': '', 'attack': []},
                  {'name': 'C', 'move': 2, 'target': 'Home', 'take_item': 'Box', 'take_all': 1,
                   'on_act': 'Act();'},
                  {'name': 'D', 'move': 0, 'code': 'Last();'}]
        data = build(items=[('Box', 'Star0')], states=states,
                     dialogs=[('Talk', '')], messages=[('', 'DText("x");')])
        vo, _ = decompiler.parse(data, {})
        self.assertEqual([s['Name'] for s in vo['States']], ['A', 'B', 'C', 'D'])
        c = vo['States'][2]
        self.assertEqual((c['TakeItem'], c['TakeAllItem'], c['OnTalk'], c['OnActCode']),
                         (vo['Items'][0]['#'], True, '', 'Act();'))
        self.assertEqual(vo['States'][1]['MoveObj'], -1)
        self.assertEqual([d['Name'] for d in vo['Dialogs']], ['Talk'])
        self.assertEqual(len(vo['DialogMessages']), 1)

    def test_array_init(self):
        # RScript пишет массив из Init: число — столько пустых элементов,
        # '[0,5,7]' — элементы Int с этими значениями (так у ShuWarriors).
        data = build(locals_=[('aEmpty', 9, []), ('aSized', 9, [('', 0, None)] * 3),
                              ('aInts', 9, [('', 1, 0), ('', 1, 5), ('', 1, 0xFFFFFFFF)]),
                              ('aMixed', 9, [('', 1, 5), ('', 0, None)])])
        vo, _ = decompiler.parse(data, {})
        self.assertEqual({v['Name']: v['Init'] for v in vo['Variables']},
                         {'aEmpty': '0', 'aSized': '3', 'aInts': '[0,5,-1]', 'aMixed': '2'})

    def test_version_7_group_has_no_old_fields(self):
        # Версия 7 читается так же, как 8; лишние поля записей — только у < 7.
        group = {'name': 'G', 'add_player': 1, 'status': (5, 6, 7, 8, 9, 10), 'dist_search': 1234}
        for version in (6, 7, 8):
            lay = decompiler.read_scr_layout(build(version=version, groups=[group],
                                                   stars=[{'name': 'S', 'ships': [{}]}]))
            g = lay['groups'][0]
            self.assertEqual((g['add_player'], g['status'], g['dist_search']),
                             (1, [5, 6, 7, 8, 9, 10], 1234), version)

    def test_versions_5_and_6_are_read_but_not_decompiled(self):
        for version in (5, 6):
            data = build(version=version, turn='T();')
            self.assertEqual(decompiler.read_scr_layout(data)['turn_code'], 'T();')
            with self.assertRaisesRegex(decompiler.ParseError, 'format version %d — skipped' % version):
                decompiler.parse(data, {})

    def test_bad_files_fail_loudly(self):
        data = build(states=[{'name': 'S', 'code': 'x();'}])
        with self.assertRaisesRegex(decompiler.ParseError, 'run past the end|no terminator'):
            decompiler.read_scr_layout(data[:-3])
        with self.assertRaisesRegex(decompiler.ParseError, 'bytes left after the last answer'):
            decompiler.read_scr_layout(data + b'\0\0')
        with self.assertRaisesRegex(decompiler.ParseError, 'format version 9'):
            decompiler.read_scr_layout(struct.pack('<I', 9) + data[4:])
        moved = bytearray(data)
        struct.pack_into('<I', moved, 4, struct.unpack_from('<I', data, 4)[0] + 2)
        with self.assertRaisesRegex(decompiler.ParseError, 'body offset'):
            decompiler.read_scr_layout(bytes(moved))
        huge = bytearray(build())
        body = struct.unpack_from('<I', huge, 4)[0]
        struct.pack_into('<I', huge, body, 0x7FFFFFF0)       # number of local vars
        with self.assertRaisesRegex(decompiler.ParseError, 'Impossible count'):
            decompiler.read_scr_layout(bytes(huge))


if __name__ == '__main__':
    unittest.main()
