"""algo3(扫屏)测试, 以及用判定模拟器(tools/judge_sim.py, 照搬Phira的判定逻辑)检查规划结果。"""
import io
import os
import random
import sys
import unittest

from rich.console import Console

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

import algo.algo1  # noqa: E402
import algo.algo2  # noqa: E402
import algo.algo3  # noqa: E402
from algo.algo_base import TouchAction, thin_path  # noqa: E402
from judge_sim import simulate  # noqa: E402
from tests.test_algo import TAIL, Timeline, chart_of, line_dict, note, rotating_line  # noqa: E402

KINDS = ('tap', 'drag', 'hold', 'flick')


def quiet():
    return Console(file=io.StringIO(), width=200)


def non_perfect(stats):
    return {k: {r: v for r, v in stats[k].items() if r != 'perfect' and v} for k in KINDS
            if any(v for r, v in stats[k].items() if r != 'perfect')}


def vertical_line(notes, x_frac=0.3):
    """竖直的判定线(转过90度): 判定只看y方向的投影, 只沿x扫动的触点对它无效"""
    return line_dict(
        notes,
        move=[{'startTime': -999999.0, 'endTime': TAIL, 'start': x_frac, 'end': x_frac, 'start2': 0.5, 'end2': 0.5}],
        rotate=[{'startTime': -999999.0, 'endTime': TAIL, 'start': 90.0, 'end': 90.0}],
    )


def drag_flick_chart():
    # bpm120: 32单位 = 0.5秒
    rot = rotating_line([note(t, x, 2) for t, x in ((70, -3.0), (100, 2.0), (130, 4.5), (160, -1.0))] +
                        [note(t, x, 4) for t, x in ((80, 1.0), (120, -4.0), (150, 3.0), (190, 0.5))])
    ver = vertical_line([note(t, x, 2) for t, x in ((72, -4.0), (104, 0.0), (136, 4.0))] +
                        [note(t, x, 4) for t, x in ((88, -3.5), (152, 3.5))])
    flat = line_dict([note(t, x, 2) for t, x in zip(range(200, 260, 4), [-7, -5, -3, -1, 1, 3, 5, 7] * 2)] +
                     [note(t, -x, 4) for t, x in zip(range(202, 262, 8), [-6, -2, 2, 6] * 2)])
    return chart_of(rot, ver, flat)


class TestSweeper(unittest.TestCase):
    def test_drag_and_flick_on_any_angle(self):
        chart = drag_flick_chart()
        for mp in (16, 10):
            ans = algo.algo3.solve(chart, quiet(), mp)
            for fps in (60, 30, 120):
                for seed in range(3):
                    with self.subTest(max_pointers=mp, fps=fps, seed=seed):
                        st = simulate(chart, ans, fps=fps, seed=seed)
                        self.assertEqual(non_perfect(st), {})

    def test_simultaneous_flicks(self):
        # 同一时刻4个flick(不同位置): 每个扫屏触点每帧只能判定一个flick, 靠判定窗口内的多帧完成
        chart = chart_of(line_dict([note(128, x, 4) for x in (-6.0, -2.0, 2.0, 6.0)] +
                                   [note(160, x, 4) for x in (-5.0, -1.0, 3.0, 7.0)]))
        ans = algo.algo3.solve(chart, quiet(), 16)
        for seed in range(5):
            with self.subTest(seed=seed):
                self.assertEqual(non_perfect(simulate(chart, ans, seed=seed)), {})

    def test_sweeper_down_does_not_steal_taps(self):
        # 在drag段开始前密集放置tap: 扫屏触点按下的时间和位置必须避开这些tap
        rng = random.Random(7)
        taps = [note(t, rng.uniform(-7, 7), 1) for t in range(0, 140, 3)]
        drags = [note(t, rng.uniform(-6, 6), 2) for t in range(100, 180, 2)]
        chart = chart_of(line_dict(taps), line_dict(drags))
        ans = algo.algo3.solve(chart, quiet(), 16)
        for seed in range(4):
            with self.subTest(seed=seed):
                st = simulate(chart, ans, seed=seed)
                self.assertEqual(non_perfect(st), {})
                stolen = [n for n in st['notes'] if n.kind in ('tap', 'hold') and n.by and n.by[0] >= 2000]
                self.assertEqual(stolen, [])

    def test_sweepers_only_when_needed(self):
        chart = chart_of(line_dict([note(t, 0.0, 1) for t in range(0, 64, 8)] + [note(640, 0.0, 2)]))
        ans = algo.algo3.solve(chart, quiet(), 16)
        sweep = sorted(ms for ms, evs in ans.items() for e in evs if e.pointer >= 2000)
        self.assertTrue(sweep)
        # drag在10秒: 扫屏只在它前后一小段时间内
        self.assertGreater(sweep[0], 10000 - 1000 - 2100)
        self.assertLess(sweep[-1], 10000 + 1000)

    def test_rows_and_limits(self):
        chart = drag_flick_chart()
        for mp, rows in ((16, 4), (10, 3)):
            with self.subTest(max_pointers=mp):
                ans = algo.algo3.solve(chart, quiet(), mp)
                tl = Timeline(ans)
                self.assertEqual(tl.errors, [])
                self.assertLessEqual(tl.max_active, mp)
                ys = {e.pos[1] for evs in ans.values() for e in evs if e.pointer >= 2000}
                self.assertEqual(len(ys), rows)
                # 行距小于判定宽度(302像素), 且覆盖到屏幕上下边缘附近
                ys = sorted(ys)
                self.assertTrue(all(b - a < 300 for a, b in zip(ys, ys[1:])))
                self.assertLess(ys[0], 151)
                self.assertGreater(ys[-1], 720 - 151)

    def test_sweep_speed(self):
        # 60fps下相邻两帧之间扫过的距离必须小于判定宽度, 否则可能"跳过"音符
        for k in range(4):
            half = algo.algo3.SWEEP_HALF_PERIODS[k]
            xs = [algo.algo3.triangle(t, 0, 20.0, half, True) for t in range(0, 2000)]
            self.assertTrue(all(20.0 <= x <= 1260.0 for x in xs))
            step = max(abs(xs[t + 17] - xs[t]) for t in range(len(xs) - 17))
            self.assertLess(step, 280)

    def test_random_charts_valid(self):
        rng = random.Random(99)
        for seed in range(3):
            lines = []
            for _ in range(3):
                notes = [note(rng.randrange(0, 1600), rng.uniform(-8, 8), k,
                              rng.choice([8.0, 32.0]) if k == 3 else 0.0)
                         for k in (rng.choice([1, 2, 3, 4]) for _ in range(60))]
                lines.append(line_dict(notes))
            chart = chart_of(*lines)
            for mp in (16, 10):
                with self.subTest(seed=seed, max_pointers=mp):
                    tl = Timeline(algo.algo3.solve(chart, quiet(), mp))
                    self.assertEqual(tl.errors[:5], [])
                    self.assertLessEqual(tl.max_active, mp)
                    for ms in tl.times:
                        for x, y in tl.at(ms).values():
                            self.assertTrue(0 <= x <= 1280 and 0 <= y <= 720)


class TestThinPath(unittest.TestCase):
    def test_static_hold_sends_only_last(self):
        self.assertEqual(thin_path([(100.0, 200.0)] * 500), [499])

    def test_fast_motion_kept(self):
        path = [(100.0 + 10 * i, 200.0) for i in range(50)]
        self.assertEqual(thin_path(path, (90.0, 200.0)), list(range(50)))

    def test_slow_motion_error_bounded(self):
        path = [(100.0 + 0.7 * i, 200.0) for i in range(1000)]
        keep = thin_path(path)
        self.assertLess(len(keep), 400)
        last = None
        for i, p in enumerate(path):
            if i in keep:
                last = p
            if last is not None:
                self.assertLess(abs(p[0] - last[0]), 6.0 + 1e-9)


class TestJudgeSim(unittest.TestCase):
    def test_empty_plan_misses_everything(self):
        chart = chart_of(line_dict([note(64, 0.0, k) for k in (1, 2, 4)] + [note(96, 0.0, 3, 32.0)]))
        st = simulate(chart, {})
        self.assertEqual(sum(st[k]['miss'] for k in KINDS), 4)

    def test_early_tap_is_good(self):
        from algo.algo_base import VirtualTouchEvent
        chart = chart_of(line_dict([note(64, 0.0, 1)]))  # 1秒
        for down, expect in ((1000, 'perfect'), (880, 'good'), (800, 'bad'), (1300, 'miss')):
            ans = {down: [VirtualTouchEvent((640.0, 360.0), TouchAction.DOWN, 1)],
                   down + 1: [VirtualTouchEvent((640.0, 360.0), TouchAction.UP, 1)]}
            with self.subTest(down=down):
                st = simulate(chart, ans, fps=240)
                self.assertEqual(st['tap'][expect], 1)

    def test_all_algos_on_rotating_line(self):
        chart = chart_of(rotating_line([note(t, x, k) for t, x, k in
                                        ((70, -3.0, 1), (90, 2.0, 2), (110, 4.0, 4), (130, -2.0, 1),
                                         (150, 1.0, 2), (170, -4.0, 4))] + [note(80, 3.0, 3, 96.0)]))
        for name, solve in (('algo1', algo.algo1.solve), ('algo2', algo.algo2.solve), ('algo3', algo.algo3.solve)):
            with self.subTest(algo=name):
                self.assertEqual(non_perfect(simulate(chart, solve(chart, quiet(), 16), seed=3)), {})


if __name__ == '__main__':
    unittest.main()
