"""flick很密("红键雨")的回归测试 —— Rrhar'il AT 这类"大量红键会漏"的谱面。

Rrhar'il AT 23.4~24.0秒: 每79ms一组, 同一毫秒4个红键, 连续8组共32个。一个滑键触点划一个flick要占用
约100ms, 4个滑键触点接得住第一组, 接不住79ms之后的下一组, 只能交替用两批共8个手指。
(Phigros中一根手指一次滑动只能判定一个flick, 扫屏触点每次单程最多接一个, 补不了这个缺口。)

判定模拟器用 --strict-flick/--phigros 规则实测: 修复前 algo3f 在 Rrhar'il AT 上16触点漏12个、10触点漏14个;
修复后 16触点全中、10触点只漏1个。仓库里没有这张谱面, 这里用合成的红键雨(同样的时间结构)代替。
"""
import io
import os
import random
import sys
import unittest

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import algo.algo2 as algo2  # noqa: E402
import algo.algo3 as algo3  # noqa: E402
import algo.algo3f as algo3f  # noqa: E402
from algo.algo_base import FLICK_RADIUS, TouchAction, flick_path  # noqa: E402
from judge_sim import simulate  # noqa: E402
from tests.test_algo import TAIL, Timeline, chart_of, line_dict, note  # noqa: E402
from tests.test_algo3 import drag_flick_chart, non_perfect  # noqa: E402

ALGOS = (('algo3f', algo3f.solve), ('algo3', algo3.solve))
RAIN_X = (-6.36, -3.18, 3.18, 6.36)     # 4个红键分布在判定线的4个位置


def quiet():
    return Console(file=io.StringIO(), width=200)


def rain_chart(groups=8, spacing=5.0, t0=128.0, extra_lines=()):
    """每spacing单位(bpm120: 5单位=78ms)一组, 同一时刻4个红键, 连续groups组"""
    notes = [note(t0 + g * spacing, x, 4) for g in range(groups) for x in RAIN_X]
    rain = line_dict(notes, rotate=[{'startTime': -999999.0, 'endTime': TAIL, 'start': -5.0, 'end': -5.0}])
    return chart_of(rain, *extra_lines)


def flick_pids(ans):
    return sorted({e.pointer for evs in ans.values() for e in evs if e.pointer >= algo3.FLICK_FINGER_BASE})


class RainTest(unittest.TestCase):
    def test_rain_is_caught_under_strict_rules(self):
        chart = rain_chart()
        for name, solve in ALGOS:
            for mp in (16, 10):
                ans = solve(chart, quiet(), mp)
                for fps in (60, 30):
                    for seed in range(2):
                        for rule in ({'strict_flick': True}, {'phigros': True}):
                            with self.subTest(algo=name, max_pointers=mp, fps=fps, seed=seed, rule=tuple(rule)):
                                self.assertEqual(non_perfect(simulate(chart, ans, fps=fps, seed=seed, **rule)), {})

    def test_default_four_fingers_are_not_enough(self):
        # 测试的前提: 4个手指(不靠推迟/短划)接不住, 8个才够 —— 否则这个测试什么都证明不了
        chart = rain_chart()
        self.assertGreater(algo3.plan_flick_fingers(chart, 4, relax=False)[1], 0)
        self.assertEqual(algo3.plan_flick_fingers(chart, 8, relax=False)[1], 0)
        self.assertEqual(algo3.fingers_for(chart, 4, 8), 8)
        self.assertEqual(algo3.fingers_for(chart, 4, 6), 6)     # 上限不够就用满上限

    def test_fingers_grow_into_unused_tap_hold_slots(self):
        chart = rain_chart()
        con = quiet()
        ans = algo3f.solve(chart, con, 16)
        self.assertEqual(len(flick_pids(ans)), 8)
        self.assertIn('滑键触点由4个增加到8个', con.file.getvalue())
        self.assertNotIn('无法执行', con.file.getvalue())

    def test_fingers_stay_default_when_flicks_are_sparse(self):
        con = quiet()
        ans = algo3f.solve(drag_flick_chart(), con, 16)
        self.assertEqual(len(flick_pids(ans)), 4)
        self.assertNotIn('增加到', con.file.getvalue())

    def test_budget_is_never_exceeded(self):
        chart = rain_chart()
        for name, solve in ALGOS:
            for mp in (16, 10):
                with self.subTest(algo=name, max_pointers=mp):
                    ans = solve(chart, quiet(), mp)
                    tl = Timeline(ans)
                    self.assertEqual(tl.errors, [])
                    self.assertLessEqual(tl.max_active, mp)
                    self.assertLessEqual(len({e.pointer for evs in ans.values() for e in evs}), mp)

    def test_flick_fingers_press_once_and_never_judge_by_fresh_press(self):
        chart = rain_chart()
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                ans = solve(chart, quiet(), 16)
                downs = {}
                for ms, evs in ans.items():
                    for e in evs:
                        if e.action == TouchAction.DOWN:
                            downs.setdefault(e.pointer, []).append(ms)
                fingers = flick_pids(ans)
                self.assertGreater(len(fingers), 4)
                for pid in fingers:
                    self.assertEqual(len(downs[pid]), 1, f'滑键触点{pid}被重新按下过')
                st = simulate(chart, ans, seed=0, phigros=True)
                for n in st['notes']:
                    pid, t = n.by
                    self.assertGreater(t * 1000 - max(d for d in downs[pid] if d <= t * 1000), 60)


class TapHoldNotSqueezedTest(unittest.TestCase):
    def test_tap_hold_keep_their_pointers(self):
        # 红键雨的同时有5个长条同时按着: tap/hold池至少要留5个, 滑键触点不能把它们挤掉
        holds = line_dict([note(100.0, x, 3, 64.0) for x in (-6.0, -3.0, 0.0, 3.0, 6.0)] +
                          [note(t, -7.0 + (t % 14), 1) for t in range(180, 260, 7)])
        chart = rain_chart(extra_lines=(holds,))
        demand = algo3.tap_hold_demand(chart)
        self.assertGreaterEqual(demand, 5)
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                con = quiet()
                ans = solve(chart, con, 16)
                self.assertNotIn('无法执行', con.file.getvalue())
                self.assertEqual(non_perfect(simulate(chart, ans, seed=0, phigros=True)), {})

    def test_never_grows_past_what_tap_hold_needs(self):
        # 10触点: 池子只剩tap/hold需要的峰值, 绝不能再往下挤(挤了就是丢音符)
        holds = line_dict([note(100.0, x, 3, 64.0) for x in (-6.0, -2.0, 2.0, 6.0)])
        chart = rain_chart(extra_lines=(holds,))
        con = quiet()
        ans = algo3f.solve(chart, con, 10)
        self.assertNotIn('无法执行', con.file.getvalue())
        pool = {e.pointer for evs in ans.values() for e in evs if e.pointer < 2000}
        self.assertGreaterEqual(len(pool), algo3.tap_hold_demand(chart))
        self.assertEqual(Timeline(ans).errors, [])


class DemandTest(unittest.TestCase):
    def test_tap_hold_demand_is_what_the_allocator_needs(self):
        """tap_hold_demand 是分配器"不丢音符"的最少触点数(不多不少)"""
        for seed in range(12):
            rng = random.Random(seed)
            lines = []
            for _ in range(rng.choice([1, 2, 3])):
                notes = []
                for _ in range(rng.choice([20, 60, 150])):
                    k = rng.choice([1, 1, 1, 3])
                    notes.append(note(rng.randrange(0, rng.choice([400, 1600, 3200])), rng.uniform(-8, 8), k,
                                      rng.choice([2.0, 8.0, 32.0]) if k == 3 else 0.0))
                lines.append(line_dict(notes))
            chart = chart_of(*lines)
            d = algo3.tap_hold_demand(chart)
            with self.subTest(seed=seed, demand=d):
                st = {}
                algo2.solve(chart, quiet(), d, stats=st, relay=False)
                self.assertEqual(st['dropped'], 0)
                self.assertEqual(st['pool_peak'], d)
                if d > 1:
                    st = {}
                    algo2.solve(chart, quiet(), d - 1, stats=st, relay=False)
                    self.assertGreater(st['dropped'], 0)

    def test_empty_chart(self):
        self.assertEqual(algo3.tap_hold_demand(chart_of(line_dict([note(100.0, 0.0, 4)]))), 0)


class RelaxTest(unittest.TestCase):
    """手指数用满仍排不下的flick: 先推迟, 再短划"""

    def test_relax_assigns_more_than_plain(self):
        chart = rain_chart()
        plain = algo3.plan_flick_fingers(chart, 4, relax=False)[1]
        relaxed = algo3.plan_flick_fingers(chart, 4, relax=True)[1]
        self.assertGreater(plain, 0)
        self.assertLess(relaxed, plain)

    def test_no_relax_when_a_finger_is_free(self):
        # 稀疏的flick: 每个都有空闲手指, 推迟/短划一个都不该用到, 事件和 relax=False 完全一样
        chart = drag_flick_chart()
        con = quiet()
        on, _ = algo3.plan_flick_fingers(chart, 4, con, relax=True)
        off, _ = algo3.plan_flick_fingers(chart, 4, relax=False)
        self.assertEqual(dict(on), dict(off))
        self.assertNotIn('快速短划', con.file.getvalue())

    def test_squeezed_swipe_is_still_a_fast_200px_swipe(self):
        chart = rain_chart()
        line = chart.judge_lines[0]
        nt = line.notes[0]
        ms = round(line.seconds(nt.time) * 1000)
        path = flick_path(line, nt, ms, algo3.SQUEEZE_START, algo3.SQUEEZE_END, FLICK_RADIUS)
        self.assertEqual(len(path), algo3.SQUEEZE_END - algo3.SQUEEZE_START + 1)
        self.assertAlmostEqual(abs(path[-1][1] - path[0][1]), 2 * FLICK_RADIUS, delta=25)
        steps = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(path, path[1:])]
        self.assertGreater(min(steps), 2.0)          # 每毫秒都在快速移动, 比正常的2像素/毫秒还快

    def test_slack_leaves_sync_margin(self):
        # flick的判定窗口是±160ms: 推迟不能把手动同步的误差余量吃光
        self.assertLessEqual(algo3.FLICK_SLACK_MS, 60)


class StatsTest(unittest.TestCase):
    def test_stats_reports_peak_and_drops(self):
        holds = line_dict([note(100.0, x, 3, 64.0) for x in (-6.0, -2.0, 2.0, 6.0)])
        chart = chart_of(holds)
        st = {}
        algo2.solve(chart, quiet(), 16, stats=st)
        self.assertEqual(st, {'dropped': 0, 'pool_peak': 4, 'pause_presses': 0})
        st = {}
        algo2.solve(chart, quiet(), 3, stats=st)
        self.assertEqual(st['dropped'], 1)


if __name__ == '__main__':
    unittest.main()
