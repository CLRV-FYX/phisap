"""长条瞬移接力(algo/relay.py)回归测试 —— "前八个长条分两波, 第二波必断一个"(Chart_AT.json)。

判定线真的在瞬移(moveX/moveY 零过渡时间跳变), 长条的判定区跟着瞬移。单个手指必须在瞬移的那一
毫秒跳过去, 整条触控链路只要比音符早/晚几十毫秒(手动同步的误差、注入延迟抖动), 就有那么久不在
判定区里, 超过 UP_TOLERANCE(50ms) 长条就断。

判定模拟器实测, 修复前 algo3f 在 Chart_AT 上整体偏差±40ms内全中, ±60ms 起就断(第二波的瞬移最密,
最先断); 修复后开头8个长条在 -80~+100ms 内都不断。
"""
import io
import json
import math
import os
import random
import sys
import unittest
from collections import defaultdict
from unittest import mock

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import algo.algo2 as algo2  # noqa: E402
import algo.algo3 as algo3  # noqa: E402
import algo.algo3f as algo3f  # noqa: E402
import algo.relay as relay  # noqa: E402
from algo.algo_base import PAUSE_BUTTON_BOX, TouchAction, hold_point  # noqa: E402
from chart import Chart  # noqa: E402
from judge_sim import simulate  # noqa: E402
from note import NoteType  # noqa: E402
from tests.test_algo import TAIL, Timeline, chart_of, judge_offset, line_dict, note  # noqa: E402


def quiet():
    return Console(file=io.StringIO(), width=200)


def shifted(ans, d):
    """整条触控链路晚d毫秒(负数=提前)"""
    out = defaultdict(list)
    for ms, evs in ans.items():
        out[ms + d].extend(evs)
    return out


def solve_no_relay(solve, chart, mp=16):
    """同一个算法, 但关掉长条接力(算法2以外的入口没有relay参数, 直接换掉规划函数)"""
    with mock.patch.object(algo2, 'plan_hold_relays', lambda *a, **k: None):
        return solve(chart, quiet(), mp)


def hold_misses(chart, ans, seeds=3):
    return sum(simulate(chart, ans, fps=60, seed=s)['hold']['miss'] for s in range(seeds))


def teleport_line(notes, xs, y=0.3, t0=84.0, step=9.0):
    """水平判定线, 从t0起每step单位(bpm120: 140ms)瞬移一次, x坐标(屏幕宽度的比例)在xs里循环"""
    def seg(a, b, x):
        return {'startTime': a, 'endTime': b, 'start': x, 'end': x, 'start2': y, 'end2': y}
    move, t = [seg(-999999.0, t0, xs[0])], t0
    for k in range(1, 40):
        move.append(seg(t, t + step, xs[k % len(xs)]))
        t += step
    move.append(seg(t, TAIL, xs[0]))
    return line_dict(notes, move=move)


def teleport_chart(*extra_lines):
    # 长条从64单位(1.0s)按下, 持续96单位(1.5s); 第一次瞬移在按下300ms之后(太靠近按下时刻的话,
    # 手指整体提前几十毫秒会在上一个位置按下, 那是"头"的问题, 不是长条中途断)
    return chart_of(teleport_line([note(64, 0.0, 3, 96.0)], [0.2, 0.8, 0.35, 0.65]), *extra_lines)


def vertical_jump_chart():
    """判定线只在竖直方向(沿判定线垂直的方向)瞬移: 判定区不变, 不算瞬移"""
    def seg(a, b, y):
        return {'startTime': a, 'endTime': b, 'start': 0.5, 'end': 0.5, 'start2': y, 'end2': y}
    move, t = [seg(-999999.0, 84.0, 0.2)], 84.0
    for k in range(1, 30):
        move.append(seg(t, t + 9.0, 0.2 if k % 2 == 0 else 0.8))
        t += 9.0
    move.append(seg(t, TAIL, 0.2))
    return chart_of(line_dict([note(64, 0.0, 3, 96.0)], move=move))


def track_of(chart):
    line = chart.judge_lines[0]
    nt = line.notes[0]
    ms = round(line.seconds(nt.time) * 1000)
    length = math.ceil(line.seconds(nt.hold) * 1000)
    path = tuple(hold_point(line, nt, ms + o) for o in range(1, length + 1))
    return relay.HoldTrack(1000, line, nt, ms, length, path, hold_point(line, nt, ms))


def new_downs(on, off):
    """打开接力后多出来的DOWN事件(接力触点)"""
    def downs(a):
        return [(ms, e.pointer, e.pos) for ms in sorted(a) for e in a[ms] if e.action == TouchAction.DOWN]
    base = downs(off)
    return [d for d in downs(on) if d not in base]


class FindTeleportsTest(unittest.TestCase):
    def test_strip_jumps_are_teleports(self):
        tele = relay.find_teleports(track_of(teleport_chart()))
        self.assertGreaterEqual(len(tele), 8)
        # 相邻瞬移间隔就是140.6ms一次; 第一次在按下之后约312ms
        times = [t for t, _, _ in tele]
        self.assertAlmostEqual(times[0] - 1000, 20 * 15.625, delta=3)
        self.assertTrue(all(abs(b - a - 140.6) < 3 for a, b in zip(times, times[1:])))

    def test_jumps_along_the_judge_line_normal_are_not(self):
        # 水平线在竖直方向来回瞬移几百像素: 沿判定线方向的投影不变, 判定区没动, 手指根本不用跟
        self.assertEqual(relay.find_teleports(track_of(vertical_jump_chart())), [])


class HelperPositionTest(unittest.TestCase):
    """接力触点是"按下再抬起"的一次点击: 不能落在暂停按钮上, 不能贴着屏幕边缘"""

    def setUp(self):
        self.line = teleport_chart().judge_lines[0]
        self.note = self.line.notes[0]

    def test_far_from_edges_is_unchanged(self):
        self.assertEqual(relay.helper_position(self.line, self.note, 1500, (700.0, 300.0)), (700.0, 300.0))

    def test_pushed_inside_along_the_perpendicular(self):
        # 判定线水平: 垂直方向就是y; 贴着上沿(y=1)的点往里挪, x(判定投影)不变
        x, y = relay.helper_position(self.line, self.note, 1500, (700.0, 1.0))
        self.assertEqual(x, 700.0)
        self.assertGreaterEqual(y, relay.EDGE_INSET)

    def test_never_on_pause_button(self):
        bx, by = PAUSE_BUTTON_BOX
        for pos in ((30.0, 1.0), (100.0, 100.0), (bx - 1, by - 1), (5.0, 700.0)):
            p = relay.helper_position(self.line, self.note, 1500, pos)
            self.assertIsNotNone(p, pos)
            self.assertFalse(p[0] < bx and p[1] < by, f'{pos} -> {p} 落在暂停按钮上')
            self.assertEqual(p[0], pos[0], '判定线方向的投影被改了')


class ClickGuardTest(unittest.TestCase):
    """按下(DOWN)在游戏里是"事件时间之后的那一帧"才生效(30fps一帧33ms), 判定线在这期间还会瞬移。

    随机谱面(seed=4, 30fps)曾因此被偷走一个tap: 接力触点的DOWN在判定线瞬移前15ms发出, 事件时间点上
    离tap的判定区很远, 可到了处理它的那一帧, 判定线已经瞬移到了接力触点的头上。"""

    def setUp(self):
        def seg(a, b, x):
            return {'startTime': a, 'endTime': b, 'start': x, 'end': x, 'start2': 0.5, 'end2': 0.5}
        # 判定线在 65单位(1015.6ms) 从 x=0.1(128px) 瞬移到 x=0.9(1152px); tap 在 77单位(1203ms)
        line = line_dict([note(77, 0.0, 1)], move=[seg(-999999.0, 65.0, 0.1), seg(65.0, TAIL, 0.9)])
        self.chart = chart_of(line)
        ln = self.chart.judge_lines[0]
        self.heads = [(round(ln.seconds(n.time) * 1000), ln, n) for n in ln.notes]

    def test_guard_sees_the_teleport_after_the_event(self):
        guard = relay._ClickGuard(self.heads)
        # 1000ms时判定区在128px, 点击位置1152px离得很远; 但1015ms之后判定区就跳到了1152px
        self.assertFalse(guard.safe((1152.0, 360.0), 1000))
        self.assertTrue(guard.safe((1152.0, 360.0), 700))      # 远在tap的±260ms之外(1203-260=943)

    def test_guard_only_looking_at_the_event_instant_would_miss_it(self):
        with mock.patch.object(relay, 'CLICK_SETTLE_MS', 0):
            self.assertTrue(relay._ClickGuard(self.heads).safe((1152.0, 360.0), 1000))


class RelayPlanTest(unittest.TestCase):
    def test_wider_sync_window_than_single_finger(self):
        # 没有接力: 整体晚80ms, 瞬移后手指80ms之后才到, 超过50ms容忍, 长条断
        chart = teleport_chart()
        plain = algo2.solve(chart, quiet(), 16, relay=False)
        self.assertGreater(hold_misses(chart, shifted(plain, 80)), 0, '没有接力时应该断(测试失去意义)')
        self.assertGreater(hold_misses(chart, shifted(plain, -90)), 0)
        for name, solve in (('algo2', algo2.solve), ('algo3', algo3.solve), ('algo3f', algo3f.solve)):
            ans = solve(chart, quiet(), 16)
            for d in (-80, 0, 80, 100):
                with self.subTest(algo=name, shift=d):
                    self.assertEqual(hold_misses(chart, shifted(ans, d)), 0)

    def test_helpers_come_from_the_free_pool(self):
        chart = teleport_chart()
        for mp in (16, 10):
            with self.subTest(max_pointers=mp):
                con = quiet()
                ans = algo3f.solve(chart, con, mp)
                self.assertTrue(all(ans[ms] for ms in ans), '规划里不该有空批次')
                pids = {e.pointer for evs in ans.values() for e in evs}
                tl = Timeline(ans)
                self.assertEqual(tl.errors, [])
                self.assertLessEqual(tl.max_active, mp)
                self.assertLessEqual(len(pids), mp)
                self.assertIn('长条瞬移接力', con.file.getvalue())

    def test_plan_unchanged_when_no_pointer_is_free(self):
        # 池子里只有长条自己的触点: 接力凑不齐, 整簇放弃, 和关掉接力完全一样
        chart = teleport_chart()
        con = quiet()
        on = algo2.solve(chart, con, 1)
        off = algo2.solve(chart, quiet(), 1, relay=False)
        self.assertEqual(dict(on), dict(off))
        self.assertIn('触点不够', con.file.getvalue())

    def test_helpers_at_safe_positions(self):
        chart = teleport_chart()
        on = algo2.solve(chart, quiet(), 16)
        off = algo2.solve(chart, quiet(), 16, relay=False)
        helpers = new_downs(on, off)
        self.assertGreaterEqual(len(helpers), 8)
        bx, by = PAUSE_BUTTON_BOX
        for _, _, (x, y) in helpers:
            self.assertGreaterEqual(min(x, 1280 - x, y, 720 - y), relay.EDGE_INSET - 1e-6)
            self.assertFalse(x < bx and y < by)

    def test_helper_is_static_and_short_lived(self):
        chart = teleport_chart()
        on = algo2.solve(chart, quiet(), 16)
        off = algo2.solve(chart, quiet(), 16, relay=False)
        for ms, pid, pos in new_downs(on, off):
            events = [(t, e) for t in sorted(on) for e in on[t] if e.pointer == pid and t >= ms]
            self.assertEqual(events[0][1].action, TouchAction.DOWN)
            ups = [t for t, e in events if e.action == TouchAction.UP]
            self.assertTrue(ups)
            self.assertLess(ups[0] - ms, 250, '接力触点不该占着触点太久')
            moves = [t for t, e in events if e.action == TouchAction.MOVE and t < ups[0]]
            self.assertEqual(moves, [], '接力触点按下之后应该一动不动')

    def test_helper_never_steals_a_tap(self):
        """接力触点按下是一次点击: 它按下的位置/时间在某个tap的判定范围内, 就会把那个tap偷走(变Good/Bad)"""
        # 另一条竖直不动的判定线, 在瞬移的位置(x=0.8, 即1024px)附近放tap: 每个瞬移点前后都有
        taps = [note(t, 0.0, 1) for t in range(90, 200, 3)]
        still = line_dict(taps, move=[{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.8, 'end': 0.8,
                                       'start2': 0.3, 'end2': 0.3}])
        chart = teleport_chart(still)
        con = quiet()
        on = algo3f.solve(chart, con, 16)
        off = solve_no_relay(algo3f.solve, chart)
        for seed in range(3):
            st = simulate(chart, on, fps=60, seed=seed)
            self.assertEqual({k: v for k, v in st['tap'].items() if k != 'perfect' and v}, {})
            self.assertEqual({k: v for k, v in st['hold'].items() if k != 'perfect' and v}, {})
        # 独立地再检查一遍: 没有一个接力触点落在任何tap的判定范围(±260ms, 宽±181px)内
        line = chart.judge_lines[1]
        tap_times = [round(line.seconds(n.time) * 1000) for n in line.notes]
        helpers = new_downs(on, off)
        self.assertTrue(helpers, '这张谱面本来就该有一部分瞬移能放接力触点')
        for ms, pid, pos in helpers:
            for t in tap_times:
                if abs(t - ms) <= relay.CLICK_GUARD_MS:
                    self.assertGreater(abs(judge_offset(line, ms, pos)), 151.2 + relay.CLICK_MARGIN - 1e-6,
                                       f'接力触点@{ms}ms{pos}会误触{t}ms的tap')

    def test_guard_is_what_prevents_the_steal(self):
        # 同一张谱面把误触检查关掉, 接力触点就会落进tap的判定范围(证明上一个测试确实在测这件事)
        taps = [note(t, 0.0, 1) for t in range(90, 200, 3)]
        still = line_dict(taps, move=[{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.8, 'end': 0.8,
                                       'start2': 0.3, 'end2': 0.3}])
        chart = teleport_chart(still)
        with mock.patch.object(relay._ClickGuard, 'safe', lambda self, *a, **k: True):
            unguarded = algo2.solve(chart, quiet(), 16)
        guarded = algo2.solve(chart, quiet(), 16)
        off = algo2.solve(chart, quiet(), 16, relay=False)
        self.assertGreater(len(new_downs(unguarded, off)), len(new_downs(guarded, off)))


def random_teleport_line(rng, notes):
    """随机瞬移(也可能跑出屏幕)+随机旋转的判定线"""
    move, t = [], -999999.0
    while t < 400.0:
        end = 0.0 if t < 0 else t + rng.choice([0.5, 4.0, 9.0])
        end = rng.choice([20.0, 40.0, 70.0]) if t < 0 else end
        x, y = rng.uniform(-0.2, 1.2), rng.uniform(-0.2, 1.2)
        move.append({'startTime': t, 'endTime': end, 'start': x, 'end': rng.choice([x, rng.uniform(0, 1)]),
                     'start2': y, 'end2': y})
        t = end
    move.append({'startTime': t, 'endTime': TAIL, 'start': 0.5, 'end': 0.5, 'start2': 0.5, 'end2': 0.5})
    rotate, t = [], -999999.0
    while t < 400.0:
        end = rng.choice([30.0, 60.0, 120.0]) if t < 0 else t + rng.choice([30.0, 60.0, 120.0])
        a = rng.choice([0.0, 0.0, 15.0, -40.0, 90.0])
        rotate.append({'startTime': t, 'endTime': end, 'start': a, 'end': a})
        t = end
    rotate.append({'startTime': t, 'endTime': TAIL, 'start': 0.0, 'end': 0.0})
    return line_dict(notes, move=move, rotate=rotate)


def random_notes(rng, count):
    out = []
    for _ in range(count):
        k = rng.choice([1, 1, 2, 3, 3, 4])
        out.append(note(rng.randrange(40, 380), rng.uniform(-6, 6), k, rng.choice([8.0, 24.0, 64.0]) if k == 3 else 0.0))
    return out


class RandomChartsTest(unittest.TestCase):
    def charts(self, n=3):
        for seed in range(n):
            rng = random.Random(1000 + seed)
            yield seed, chart_of(*(random_teleport_line(rng, random_notes(rng, 40)) for _ in range(3)))

    def test_plans_stay_valid(self):
        for seed, chart in self.charts():
            for name, solve, mp in (('algo2', algo2.solve, 16), ('algo3', algo3.solve, 16),
                                    ('algo3f', algo3f.solve, 16), ('algo3f', algo3f.solve, 10)):
                with self.subTest(seed=seed, algo=name, max_pointers=mp):
                    ans = solve(chart, quiet(), mp)
                    tl = Timeline(ans)
                    self.assertEqual(tl.errors[:3], [])
                    self.assertLessEqual(tl.max_active, mp)
                    for evs in ans.values():
                        for e in evs:
                            self.assertTrue(0 <= e.pos[0] < 1280 and 0 <= e.pos[1] < 720, e)

    def test_relay_never_hurts_when_in_sync(self):
        # 完全同步(没有时间误差)时, 打开接力只会让结果更好或一样, 不会更差
        for seed, chart in self.charts(2):
            on = algo3f.solve(chart, quiet(), 16)
            off = solve_no_relay(algo3f.solve, chart)
            for fps in (60, 30):
                with self.subTest(seed=seed, fps=fps):
                    bad = lambda a: sum(v for k in ('tap', 'drag', 'hold', 'flick')
                                        for r, v in simulate(chart, a, fps=fps, seed=seed)[k].items() if r != 'perfect')
                    self.assertLessEqual(bad(on), bad(off))


def chart_at_first_seconds(seconds=10.0):
    path = os.path.join(ROOT, 'Chart_AT.json')
    if not os.path.isfile(path):
        raise unittest.SkipTest('Chart_AT.json 不在工作区')
    with io.open(path, encoding='utf-8-sig') as f:
        data = json.load(f)
    for line in data['judgeLineList']:
        k = 1.875 / line['bpm']
        for key in ('notesAbove', 'notesBelow'):
            line[key] = [n for n in line[key] if n['time'] * k < seconds]
    return Chart.from_dict(data)


class ChartATTest(unittest.TestCase):
    """Chart_AT.json 开头的8个长条(2波x4个, 判定线每个节拍瞬移一次)"""

    @classmethod
    def setUpClass(cls):
        cls.chart = chart_at_first_seconds()
        cls.holds = [n for line in cls.chart.judge_lines for n in line.notes if n.type == NoteType.HOLD]

    def test_has_eight_teleporting_holds(self):
        self.assertEqual(len(self.holds), 8)

    def test_all_eight_survive_sync_errors(self):
        ans = algo3f.solve(self.chart, quiet(), 16)
        for d in (-80, -60, 0, 60, 80, 90):
            with self.subTest(shift=d):
                self.assertEqual(hold_misses(self.chart, shifted(ans, d), seeds=2), 0)

    def test_without_relay_the_second_wave_breaks(self):
        ans = solve_no_relay(algo3f.solve, self.chart)
        self.assertEqual(hold_misses(self.chart, ans, seeds=2), 0)         # 完全同步时没有问题
        broke = [d for d in (-60, 60) if hold_misses(self.chart, shifted(ans, d), seeds=2) > 0]
        self.assertTrue(broke, '修复前整体偏差±60ms时应该断长条(测试失去意义)')

    def test_stays_within_budget(self):
        for mp in (16, 10):
            with self.subTest(max_pointers=mp):
                ans = algo3f.solve(self.chart, quiet(), mp)
                tl = Timeline(ans)
                self.assertEqual(tl.errors, [])
                self.assertLessEqual(tl.max_active, mp)


if __name__ == '__main__':
    unittest.main()
