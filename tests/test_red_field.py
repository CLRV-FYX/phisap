"""噪点红场: 几何模型, 以及 algored 把触点放在红场外。"""
import io
import math
import os
import unittest

from rich.console import Console

import algo.algored as algored
from algo.algo_base import TouchAction, chart_offset_ms, shift_plan
from algo.red_field import RedField
from chart import Chart
from tests.test_algo import Timeline, chart_of, judge_offset, line_dict, note


def quiet():
    return Console(file=io.StringIO(), width=200)


def _block(bl, tr, enable, disable, subtract=False, moves=None, scales=None, rots=None):
    cx, cy = (bl[0] + tr[0]) / 2, (bl[1] + tr[1]) / 2
    return {
        'bottomLeftPercentage': {'x': bl[0], 'y': bl[1]},
        'topRightPercentage': {'x': tr[0], 'y': tr[1]},
        'enableTime': enable, 'disableTime': disable,
        'appearTime': enable, 'disappearTime': disable,
        'isSubtract': subtract,
        'moveEvents': moves or [{'time': enable, 'endPosition': {'x': cx, 'y': cy},
                                 'easeTypeX': 0, 'easeTypeY': 0}],
        'scaleEvents': scales or [{'time': enable, 'anchor': {'x': cx, 'y': cy},
                                   'scale': {'x': 1.0, 'y': 1.0}, 'easeTypeX': 0, 'easeTypeY': 0}],
        'rotateEvents': rots or [{'time': enable, 'anchor': {'x': cx, 'y': cy},
                                  'rotation': 0.0, 'easeType': 0}],
    }


def field_of(*blocks):
    return RedField.from_chart(Chart(3, 0.0, [], list(blocks)))


class RedFieldModelTest(unittest.TestCase):
    def test_y_up_percentage(self):
        """比例坐标 y 向上。屏幕底部(像素 y 接近 720)才落进 y=0~0.2 的矩形。"""
        f = field_of(_block((0, 0), (1, 0.2), 0, 10))
        self.assertTrue(f.contains(640, 700, 1.0))
        self.assertFalse(f.contains(640, 10, 1.0))

    def test_only_enable_window_blocks_clicks(self):
        """挡点击的只有 enableTime <= t < disableTime。

        appear 期间噪点可能已经画出来, 但原生判定不挡点击。enable==disable
        的块整段都不挡。把淡入算进死区, 垂线上明明有解也会被跳过。
        """
        raw = _block((0.2, 0.2), (0.8, 0.8), 5, 6)
        raw['appearTime'] = 0
        raw['disappearTime'] = 20
        f = field_of(raw)
        self.assertFalse(f.contains(640, 360, 1.0), '淡入期间不挡点击')
        self.assertTrue(f.contains(640, 360, 5.0))
        self.assertTrue(f.contains(640, 360, 5.999))
        self.assertFalse(f.contains(640, 360, 6.0), 'disable 是开区间')
        self.assertFalse(f.contains(640, 360, 8.0), 'enable 结束之后不再挡点击')
        point = _block((0.2, 0.2), (0.8, 0.8), 2, 2)
        point['appearTime'] = 0
        point['disappearTime'] = 4
        self.assertFalse(field_of(point).contains(640, 360, 1.0))
        self.assertFalse(field_of(point).contains(640, 360, 2.0))

    def test_interval_matches_contains(self):
        """垂直线上 s=0 落在红区间里, 必须和 contains 一致, 包括转过的矩形。"""
        b = _block((0.2, 0.42), (0.8, 0.58), 0, 10, rots=[
            {'time': 0, 'anchor': {'x': 0.5, 'y': 0.5}, 'rotation': 35.0, 'easeType': 0},
        ])
        f = field_of(b)
        for x in range(40, 1280, 80):
            for y in range(40, 720, 80):
                ivs = f.safe_intervals(x, y, 0.0, 1.0, 1.0)
                # 安全区间不含 0, 才说明这个点本身在规划边距下的红场里或出了屏幕/暂停键。
                # 这里只核对几何: 用 0 边距的 contains, 和 intersect 的符号。
                inside = f.contains(x, y, 1.0, margin_px=0.0)
                slot = f.blocks[0].intersect_s(x, y, 0.0, 1.0, 1.0, 0.0)
                on_line = slot is not None and slot[0] <= 0.0 <= slot[1]
                self.assertEqual(on_line, inside, (x, y, slot, inside))
                if inside:
                    self.assertFalse(any(a <= 0.0 <= b for a, b in ivs), (x, y, ivs))

    def test_subtract_xor_cancels_overlapping_red(self):
        """普通块取并集, subtract 按奇偶, 两者再异或。不是后写覆盖。

        铺满屏幕的普通块和同样大的 subtract 重叠, 屏幕不再是红场。
        只有 subtract、没有普通块的地方仍然挡点击。小块叠在 subtract 上时,
        重叠处互相抵消, 小块外面的 subtract 还在。
        """
        full = _block((-1, -1), (2, 2), 0, 10, subtract=False)
        hole = _block((-1, -1), (2, 2), 0, 10, subtract=True)
        small = _block((0.4, 0.4), (0.6, 0.6), 0, 10, subtract=False)
        self.assertFalse(field_of(full, hole).contains(640, 360, 1.0))
        f = field_of(hole, small)
        self.assertFalse(f.contains(640, 360, 1.0), '重叠处互相抵消')
        self.assertTrue(f.contains(100, 100, 1.0), '单独的 subtract 也挡点击')
        left = _block((0.0, 0.4), (0.3, 0.6), 0, 10)
        right = _block((0.7, 0.4), (1.0, 0.6), 0, 10)
        union = field_of(left, right)
        self.assertTrue(union.contains(100, 360, 1.0))
        self.assertTrue(union.contains(1100, 360, 1.0))
        self.assertFalse(union.contains(640, 360, 1.0))

    def test_ease_13_holds_until_the_next_keyframe(self):
        """ease 13 进度恒为 0。中途不能按 RPE 的缓动提前挪走。"""
        b = _block((0.4, 0.4), (0.6, 0.6), 0, 10, moves=[
            {'time': 0, 'endPosition': {'x': 0.5, 'y': 0.5}, 'easeTypeX': 13, 'easeTypeY': 13},
            {'time': 2, 'endPosition': {'x': 0.1, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
        ])
        f = field_of(b)
        self.assertTrue(f.contains(640, 360, 1.0), 'ease 13 停在起点')
        self.assertFalse(f.contains(0.1 * 1280, 360, 1.0))
        self.assertTrue(f.contains(0.1 * 1280, 360, 2.0))
        self.assertFalse(f.contains(640, 360, 2.0))

    def test_move_translates_center(self):
        """endPosition 是矩形中心。挪到屏幕左侧后, 原来的中心不再是红场。"""
        b = _block((0.4, 0.4), (0.6, 0.6), 0, 10, moves=[
            {'time': 0, 'endPosition': {'x': 0.5, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
            {'time': 2, 'endPosition': {'x': 0.1, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
        ])
        f = field_of(b)
        self.assertTrue(f.contains(640, 360, 0.0))
        self.assertFalse(f.contains(640, 360, 2.0))
        self.assertTrue(f.contains(0.1 * 1280, 360, 2.0))

    def test_rotation_is_ccw(self):
        """90 度把横条转成竖条: 中心正上方进红场, 正右方出去。"""
        b = _block((0.2, 0.48), (0.8, 0.52), 0, 10, rots=[
            {'time': 0, 'anchor': {'x': 0.5, 'y': 0.5}, 'rotation': 90.0, 'easeType': 0},
        ])
        f = field_of(b)
        self.assertTrue(f.contains(640, 360 - 80, 1.0), '正上方应在转过的竖条里')
        self.assertFalse(f.contains(640 + 200, 360, 1.0), '正右方不应再被横条盖住')

    def test_chart_at_fullscreen_subtract_cancels_red(self):
        """Chart_AT 在 64.2s 有铺满屏幕的普通块, 后面紧跟一块同样大的 subtract。
        这一秒还有音符, 所以屏幕中心不能是红场。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Chart_AT.json')
        if not os.path.exists(path):
            self.skipTest('没有 Chart_AT.json')
        import json
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        chart = Chart.from_dict(data)
        f = RedField.from_chart(chart)
        self.assertFalse(f.contains(640, 360, 64.2))
        # 70s 的四块红场还在(subtract 铺满屏幕的那块更早, 盖不住后面的块)
        self.assertTrue(any(b.covers(70) and not b.subtract for b in f.blocks))
        # 以前被判成「整条垂线没有缝」的时刻, 原生几何下 ±40ms 内都有红场外的点。
        from note import NoteType
        from algo.algo_base import hold_point, note_state, recalc_pos
        wanted = {27.847, 75.446, 76.188, 102.967, 103.069, 128.243}
        seen = set()
        for line in chart.judge_lines:
            for note in line.notes_above + line.notes_below:
                note_ms = round(line.seconds(note.time) * 1000)
                key = round(note_ms / 1000, 3)
                if key not in wanted or key in seen:
                    continue
                seen.add(key)
                slot = None
                for dt in range(-40, 41, 4):
                    raw, sa, ca = note_state(line, note, note_ms + dt)
                    raw = hold_point(line, note, note_ms + dt) if note.type == NoteType.HOLD else recalc_pos(raw, sa, ca)
                    hit = f.vertical_slot(raw[0], raw[1], sa, ca, (note_ms + dt) / 1000.0)
                    if hit is None:
                        continue
                    if f.contains(hit[0], hit[1], (note_ms + dt) / 1000.0, 0.0):
                        continue
                    slot = hit
                    break
                self.assertIsNotNone(slot, key)
        self.assertEqual(seen, wanted)


class AlgoredTest(unittest.TestCase):
    def _tap_chart(self, blocks, x=0.0, t=32.0):
        ch = chart_of(line_dict([note(t, x)]))
        ch.block_areas = list(blocks)
        return ch

    def test_moving_field_opens_a_gap_later(self):
        """判定时刻整条垂线都在红场里, 但红场马上滑走。要等到让开再按, 不能当成没位置。"""
        raw = _block((0.35, -0.2), (0.65, 1.2), 0.0, 2.0, moves=[
            {'time': 0.0, 'endPosition': {'x': 0.5, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
            {'time': 0.50, 'endPosition': {'x': 0.5, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
            {'time': 0.58, 'endPosition': {'x': -0.8, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
        ])
        ch = self._tap_chart([raw])
        ans = algored.solve(ch, quiet(), 16)
        line = ch.judge_lines[0]
        ms = round(line.seconds(32.0) * 1000)
        field = RedField.from_chart(ch)
        self.assertTrue(field.contains(640, 360, ms / 1000.0), '测试前提: 判定时刻中心在红场里')
        downs = [(t, e) for t, evs in ans.items() for e in evs if e.action == TouchAction.DOWN]
        self.assertTrue(downs, '红场会让开, 应该按下')
        t, e = min(downs, key=lambda it: abs(it[0] - ms))
        self.assertLess(abs(t - ms), 80, '要落在 Good 窗口里')
        self.assertFalse(field.contains(e.pos[0], e.pos[1], t / 1000.0), e.pos)
        self.assertLess(abs(judge_offset(line, t, e.pos)), 5.0)

    def test_appear_only_noise_does_not_block_the_tap(self):
        """enable==disable 的块只是淡入淡出, 不挡点击。音符可以点在原位置。"""
        raw = _block((0.3, 0.35), (0.7, 0.65), 2.0, 2.0, moves=[
            {'time': 0.0, 'endPosition': {'x': 0.5, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
            {'time': 1.5, 'endPosition': {'x': 0.5, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
            {'time': 2.0, 'endPosition': {'x': -1.5, 'y': 0.5}, 'easeTypeX': 0, 'easeTypeY': 0},
        ])
        raw['appearTime'] = 0.0
        raw['disappearTime'] = 2.0
        ch = self._tap_chart([raw])
        ans = algored.solve(ch, quiet(), 16)
        line = ch.judge_lines[0]
        ms = round(line.seconds(32.0) * 1000)  # 0.5s, 矩形还画在中心, 但 enable 是一个点
        self.assertLess(ms / 1000.0, 1.0)
        fingers = Timeline(ans).at(ms)
        self.assertTrue(fingers, '不挡点击的淡入不能把音符跳过')
        field = RedField.from_chart(ch)
        self.assertFalse(field.contains(640, 360, ms / 1000.0), '测试前提: 这一刻不挡点击')
        for pos in fingers.values():
            self.assertFalse(field.contains(pos[0], pos[1], ms / 1000.0), pos)
            self.assertLess(abs(judge_offset(line, ms, pos)), 5.0)

    def test_tap_slides_out_along_perpendicular(self):
        """红场盖住判定点时, 触点沿垂直方向挪出去, 投影不变, 并且不在红场里。"""
        # 水平判定线在屏幕中央, 红场盖住中央一条横带
        ch = self._tap_chart([_block((0.3, 0.35), (0.7, 0.65), 0, 10)])
        ans = algored.solve(ch, quiet(), 16)
        tl = Timeline(ans)
        self.assertFalse(tl.errors, tl.errors)
        line = ch.judge_lines[0]
        ms = round(line.seconds(32.0) * 1000)
        fingers = tl.at(ms)
        self.assertTrue(fingers, '判定时刻应该有手指按下')
        field = RedField.from_chart(ch)
        # 横带在比例 y 0.35~0.65, 屏幕 y(向下) 是 252~468
        for pos in fingers.values():
            self.assertFalse(field.contains(pos[0], pos[1], ms / 1000.0), pos)
            self.assertLess(abs(judge_offset(line, ms, pos)), 5.0)
            gap = 252 - pos[1] if pos[1] < 252 else pos[1] - 468 if pos[1] > 468 else 0
            self.assertGreater(gap, 20.0, pos)

    def test_unavoidable_note_is_not_pressed(self):
        """整条垂直线都在红场里时不按下。点进红场会出大问题, 不能先按下再抬。"""
        ch = self._tap_chart([_block((-1, -1), (2, 2), 0, 10)])
        ans = algored.solve(ch, quiet(), 16)
        downs = [e for evs in ans.values() for e in evs if e.action == TouchAction.DOWN]
        self.assertFalse(downs, '整屏红场时不能发出按下')
        tl = Timeline(ans)
        ms = round(ch.judge_lines[0].seconds(32.0) * 1000)
        self.assertFalse(tl.at(ms + 5))

    def test_no_red_field_matches_algo2(self):
        import algo.algo2 as algo2
        ch = chart_of(line_dict([note(32.0, 0.0), note(40.0, 1.0, n_type=2)]))
        a = algored.solve(ch, quiet(), 16)
        b = algo2.solve(ch, quiet(), 16)

        def flat(ans):
            return [(ms, e.action, e.pointer, tuple(round(v, 3) for v in e.pos))
                    for ms in sorted(ans) for e in ans[ms]]

        self.assertEqual(flat(a), flat(b))

    def test_lift_checks_milliseconds_without_events(self):
        """没有新事件的毫秒也要查。红场会自己移到还按着的手指上。

        以前写成了 `ms not in down`, 而 down 的键是触点 id, 于是这种毫秒被直接跳过。
        """
        from collections import defaultdict
        from algo.algored import _lift_red
        from algo.algo_base import VirtualTouchEvent
        ch = chart_of(line_dict([note(0.0, 0.0)]))
        ch.block_areas = [_block((-1, -1), (2, 2), 0.003, 0.004)]
        events = defaultdict(list)
        events[0].append(VirtualTouchEvent((640.0, 360.0), TouchAction.DOWN, 1000))
        events[5].append(VirtualTouchEvent((640.0, 360.0), TouchAction.UP, 1000))
        self.assertGreater(_lift_red(events, RedField.from_chart(ch)), 0)
        ups = [ms for ms, evs in events.items() if any(e.action == TouchAction.UP and e.pointer == 1000 for e in evs)]
        self.assertTrue(ups)
        self.assertLess(min(ups), 5, '必须在红场盖住的那一毫秒抬起, 不能拖到原来的松手时刻')

    def test_field_moving_onto_a_finger_lifts_it(self):
        """红场自己移到还按着的手指上时, 不能等下一次 MOVE, 要立刻抬起。"""
        ch = chart_of(line_dict([note(0.0, 0.0, n_type=3, hold=80.0)]))
        ch.block_areas = [_block((-1, -1), (2, 2), 0.4, 2.0)]
        ans = algored.solve(ch, quiet(), 16)
        tl = Timeline(ans)
        self.assertFalse(tl.errors, tl.errors)
        self.assertTrue(tl.at(100), '红场出现前长条应该按着')
        self.assertFalse(tl.at(500), '整屏红场盖住之后手指必须已经抬起')

    def test_hold_stays_outside_a_patch(self):
        """长条的判定点在红场里, 但垂直方向还有空位: 整段都要在红场外, 并且不用立刻抬起。"""
        ch = chart_of(line_dict([note(32.0, 0.0, n_type=3, hold=16.0)]))  # 16 个时间单位
        ch.block_areas = [_block((0.3, 0.4), (0.7, 0.6), 0, 10)]
        ans = algored.solve(ch, quiet(), 16)
        field = RedField.from_chart(ch)
        tl = Timeline(ans)
        self.assertFalse(tl.errors, tl.errors)
        line = ch.judge_lines[0]
        ms0 = round(line.seconds(32.0) * 1000)
        self.assertTrue(tl.at(ms0 + 30), '躲得开的长条不应该被立刻抬起')
        ys = []
        for dt in range(0, 40, 5):
            for pos in tl.at(ms0 + dt).values():
                self.assertFalse(field.contains(pos[0], pos[1], (ms0 + dt) / 1000.0), (dt, pos))
                ys.append(pos[1])
        self.assertTrue(ys)
        # 这块红场是比例 y 0.4~0.6, 屏幕 y 288~432。整段都要停在同一侧, 不能扫过红场。
        self.assertTrue(all(y < 288 for y in ys) or all(y > 432 for y in ys), ys)

    def test_hold_prepresses_before_red_comes_out_of_the_middle(self):
        """红区从中间略偏两侧冒出来时, 不能等它盖住再换手。

        长条已经按在判定点上。红场赶到之前, 要先在垂线的空位上按住不放,
        然后再松开原来的触点。两边同时松、或旧手指留在红区里, 长条都会断。
        """
        ch = chart_of(line_dict([note(32.0, 0.0, n_type=3, hold=64.0)]))
        # 0.9s 起中间一条横带挡点击, 略偏上。垂线上下都还有空位, 但中间已经过不去。
        ch.block_areas = [_block((0.25, 0.42), (0.75, 0.62), 0.90, 1.20)]
        ans = algored.solve(ch, quiet(), 16)
        tl = Timeline(ans)
        self.assertFalse(tl.errors, tl.errors)
        field = RedField.from_chart(ch)
        line = ch.judge_lines[0]
        hold = line.notes_above[0]
        ms0 = round(line.seconds(hold.time) * 1000)
        end = ms0 + math.ceil(line.seconds(hold.hold) * 1000)
        red_at = 900
        self.assertTrue(field.contains(640, 360, red_at / 1000.0), '测试前提: 红场盖住判定点')
        self.assertFalse(field.contains(640, 200, red_at / 1000.0))
        self.assertFalse(field.contains(640, 520, red_at / 1000.0))

        def outside(ms):
            return [pos for pos in tl.at(ms).values()
                    if abs(judge_offset(line, ms, pos)) < 8 and not field.contains(pos[0], pos[1], ms / 1000.0)]

        # 红场出现前至少 40ms, 空位上已经有一只按着的手指。原来的触点这时还可以留在中间。
        early = outside(red_at - 40)
        self.assertTrue(early, '红场赶到之前就要按在垂线空位上')
        self.assertTrue(any(pos[1] < 250 or pos[1] > 440 for pos in early), early)
        # 原来的中间触点要在红场盖住之前松开, 不能留到红区里。
        self.assertFalse(any(abs(pos[1] - 360) < 40 for pos in tl.at(red_at - 16).values()),
                         tl.at(red_at - 16))
        # 新旧重叠: 新的已经按在空位上, 原来的还没松。
        overlap = [ms for ms in range(red_at - 80, red_at) if len(tl.at(ms)) >= 2 and outside(ms)]
        self.assertTrue(overlap, '新触点按住之后才能松开原来的')
        # 同一毫秒按下又松开, 游戏先处理抬起, 长条会断。至少重叠一帧以上。
        self.assertGreaterEqual(max(overlap) - min(overlap), 32, overlap)
        self.assertLess(max(ms for ms, evs in ans.items()
                            if any(e.action == TouchAction.UP for e in evs) and ms < red_at), red_at)
        # 红场出现之后, 判定点上不能还留着手指, 空位上的那只要继续按。
        for ms in range(red_at, red_at + 80, 10):
            fingers = tl.at(ms)
            for pos in fingers.values():
                self.assertFalse(field.contains(pos[0], pos[1], ms / 1000.0), (ms, pos))
            self.assertTrue(outside(ms), (ms, fingers))
        # 整段长条都有手指在判定带里, 不能断。
        gap = 0
        worst = 0
        for ms in range(ms0, end + 1, 4):
            if outside(ms):
                worst = max(worst, gap)
                gap = 0
            else:
                gap += 4
        worst = max(worst, gap)
        self.assertLess(worst, 40, '长条中间断开超过一帧')

    def test_prepress_click_does_not_steal_or_bad_a_tap(self):
        """换手的新按下不能落进旁边 tap 的判定窗。

        红场从中间赶来时仍然要先按住空位、再松开原来的。旁边的 tap 和长条在同一条
        判定线上, 换垂线偏移躲不开, 新按下只能放到它的判定窗外面: ±80ms 会抢走,
        再早到 180ms 是 Bad。长条头仍要留在 ±40ms 里。
        """
        from algo.algo_base import JUDGE_HALF_WIDTH, note_state
        ch = chart_of(line_dict([
            note(32.0, 0.0, n_type=3, hold=64.0),
            note(54.4, 1.0),
        ]))
        ch.block_areas = [_block((0.25, 0.42), (0.75, 0.62), 0.90, 1.20)]
        ans = algored.solve(ch, quiet(), 16)
        tl = Timeline(ans)
        self.assertFalse(tl.errors, tl.errors)
        line = ch.judge_lines[0]
        hold, tap = line.notes_above
        ms0 = round(line.seconds(hold.time) * 1000)
        tap_ms = round(line.seconds(tap.time) * 1000)
        end = ms0 + math.ceil(line.seconds(hold.hold) * 1000)
        red_at = 900
        self.assertLess(abs(tap_ms - 850), 30, tap_ms)
        field = RedField.from_chart(ch)

        downs = [(t, e.pos) for t, evs in ans.items() for e in evs if e.action == TouchAction.DOWN]
        self.assertGreaterEqual(len(downs), 2, '红场从中间来, 要先按住新的再松原来的')
        head = [t for t, _ in downs if abs(t - ms0) <= 40]
        self.assertTrue(head, '长条头要留在 Perfect ±40ms')
        tap_clicks = [t for t, pos in downs if abs(t - tap_ms) <= 40 and abs(judge_offset(line, t, pos) - tap.x * 72) < 20]
        self.assertTrue(tap_clicks, '旁边的 tap 还要由它自己的点击打中')

        def hits_tap(t, pos):
            raw, sa, ca = note_state(line, tap, t)
            along = (pos[0] - raw[0]) * ca + (pos[1] - raw[1]) * sa
            dt = tap_ms - t
            if abs(along) > JUDGE_HALF_WIDTH:
                return False
            return -180 <= dt <= 180

        stolen = [(t, pos) for t, pos in downs if hits_tap(t, pos) and abs(t - tap_ms) > 40]
        self.assertFalse(stolen, f'换手按下打进了 tap 的判定窗: {stolen}')

        def outside(ms):
            return [pos for pos in tl.at(ms).values()
                    if abs(judge_offset(line, ms, pos)) < 8 and not field.contains(pos[0], pos[1], ms / 1000.0)]

        self.assertTrue(outside(red_at - 40), '红场赶到之前空位上要有手指')
        overlap = [ms for ms in range(red_at - 80, red_at) if len(tl.at(ms)) >= 2 and outside(ms)]
        self.assertTrue(overlap, '新触点按住之后才能松开原来的')
        self.assertGreaterEqual(max(overlap) - min(overlap), 32, overlap)
        gap = worst = 0
        for ms in range(ms0, end + 1, 4):
            if outside(ms):
                worst = max(worst, gap)
                gap = 0
            else:
                gap += 4
        self.assertLess(max(worst, gap), 40, '躲开 tap 之后长条不能断')

    def test_short_hold_is_not_skipped_to_dodge_a_later_tap(self):
        """躲开旁边的音符不能把按下挪到长条结束之后, 否则这条长条会被跳过。"""
        ch = chart_of(line_dict([
            note(32.0, 0.0, n_type=3, hold=1.0),
            note(34.2, 0.0),
        ]))
        ans = algored.solve(ch, quiet(), 16)
        line = ch.judge_lines[0]
        hold = line.notes_above[0]
        ms0 = round(line.seconds(hold.time) * 1000)
        downs = [(t, e.pos) for t, evs in ans.items() for e in evs if e.action == TouchAction.DOWN]
        self.assertTrue(any(abs(t - ms0) <= 40 and abs(judge_offset(line, t, pos)) < 8 for t, pos in downs),
                        '很短的长条也要按下, 不能为了躲开后面的 tap 跳过')


    def test_hold_does_not_camp_on_the_red_lip(self):
        """空位比手指宽时, 不能贴着红边跟着挪。

        Chart_AT 84208 那条长条, 缝宽几百像素, 旧规划却一直坐在离红边 28px 的唇上。
        红场一快或注入晚一帧, 手指就进噪区, 同一处必断。
        """
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Chart_AT.json')
        if not os.path.isfile(path):
            self.skipTest('没有 Chart_AT.json')
        import json
        from note import NoteType
        from algo.algo_base import hold_point, note_state
        from algo.algored import _normal, _note_guard, _plan_hold
        with open(path, encoding='utf-8-sig') as f:
            chart = Chart.from_dict(json.load(f))
        field = RedField.from_chart(chart)
        occupied = _note_guard(chart)
        lip_ms = 0
        for li in (1, 2):
            line = chart.judge_lines[li]
            note = next(n for n in line.notes_above + line.notes_below
                        if n.type == NoteType.HOLD and abs(round(line.seconds(n.time) * 1000) - 84208) < 5)
            ms0 = round(line.seconds(note.time) * 1000)
            end = ms0 + math.ceil(line.seconds(note.hold) * 1000)
            guard = lambda t, pos, _n=note: occupied(t, pos, _n)
            planned = _plan_hold(field, line, note, ms0, end, occupied=guard)
            self.assertIsNotNone(planned, li)
            segments, _pre = planned
            self.assertGreaterEqual(len(segments), 1)
            for st, hd, pts in segments:
                for k, p in enumerate([hd, *pts]):
                    t = st + k
                    if t > end or t % 8:
                        continue
                    raw = hold_point(line, note, t)
                    _, sa, ca = note_state(line, note, t)
                    nx, ny = _normal(sa, ca)
                    s = (p[0] - raw[0]) * nx + (p[1] - raw[1]) * ny
                    ivs = field.safe_intervals(raw[0], raw[1], sa, ca, t / 1000.0)
                    home = next(((a, b) for a, b in ivs if a - 2 <= s <= b + 2), None)
                    if home is None:
                        continue
                    edge = min(s - home[0], home[1] - s)
                    wider = any(b - a > (home[1] - home[0]) + 80 for a, b in ivs)
                    if edge <= 32 and (wider or home[1] - home[0] > 120):
                        lip_ms += 8
        self.assertLess(lip_ms, 80, '还在贴着红边坐, 宽空位不用')


class OffsetPlanTest(unittest.TestCase):
    def test_shift_matches_offset(self):
        ch = chart_of(line_dict([note(0.0, 0.0)]))
        ch.offset = 0.25
        self.assertEqual(chart_offset_ms(ch), 250)
        moved = shift_plan({1000: ['a'], 2000: ['b']}, ch)
        self.assertEqual(sorted(moved), [1250, 2250])
        plain = chart_of(line_dict([note(0.0, 0.0)]))
        same = {1: ['a']}
        self.assertEqual(chart_offset_ms(plain), 0)
        self.assertIs(shift_plan(same, plain), same)


if __name__ == '__main__':
    unittest.main()
