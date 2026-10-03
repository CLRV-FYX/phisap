"""暂停键保护回归测试 —— “部分谱面会按到暂停键”。

音符落在屏幕左上角的暂停键区域(algo_base.PAUSE_BUTTON_BOX)时, 触点原本就点在音符的位置上, 于是“按到暂停键”。
判定只看触点在判定线方向上的投影(垂直判定, 与垂直于判定线的高度无关), 所以把触点沿垂直于判定线的方向挪出暂停键区域,
判定结果不变。

真实谱面实测(仓库外的 PhiResources, 49首AT, 判定模拟器, algo3f/algo3 × 16/10触点): 修复前 algo3f@16 有14首谱面
共38次按在暂停键上、50次抬在暂停键上; 修复后一次都没有, 判定结果与修复前逐张谱面完全一致。
这里用合成谱面和仓库里的 Chart_AT.json 覆盖同样的结构。
"""
import io
import json
import math
import os
import sys
import unittest
from contextlib import contextmanager
from unittest import mock

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import algo.algo1 as algo1  # noqa: E402
import algo.algo2 as algo2  # noqa: E402
import algo.algo3 as algo3  # noqa: E402
import algo.algo3f as algo3f  # noqa: E402
import algo.algo_base as algo_base  # noqa: E402
from algo.algo_base import (PAUSE_BUTTON_BOX, PAUSE_EXIT_MARGIN, TouchAction, avoid_pause_button, flick_path,  # noqa: E402
                            hold_point, in_pause_box, note_point, note_state, pause_free_intervals, pause_presses,
                            warn_pause_presses)
from chart import Chart  # noqa: E402
from judge_sim import simulate  # noqa: E402
from tests.test_algo import TAIL, chart_of, line_dict, note  # noqa: E402
from tests.test_algo3 import non_perfect  # noqa: E402

PW, PH = PAUSE_BUTTON_BOX
FREE_X, FREE_Y = PW + PAUSE_EXIT_MARGIN, PH + PAUSE_EXIT_MARGIN      # 出口: 暂停键区域外扩一圈
SWEEP_BASE = algo_base.SWEEP_POINTER_BASE_MIN


def quiet():
    return Console(file=io.StringIO(), width=200)


def trig(deg):
    """判定线角度(度, 谱面里的值)对应的 (sa, ca): 和 note_state 的返回值一致"""
    a = -deg * math.pi / 180
    return math.sin(a), math.cos(a)


def corner_line(notes, x_px=128.0, y_px=36.0, rot=0.0):
    """静止的判定线, 中心在 (x_px, y_px) 像素(1280x720, 原点在左上角), 转过 rot 度"""
    return line_dict(
        notes,
        move=[{'startTime': -999999.0, 'endTime': TAIL, 'start': x_px / 1280, 'end': x_px / 1280,
               'start2': 1 - y_px / 720, 'end2': 1 - y_px / 720}],
        rotate=[{'startTime': -999999.0, 'endTime': TAIL, 'start': rot, 'end': rot}])


def spinning_line(notes, x_px, y_px, deg0, deg1, t0=64.0, t1=192.0):
    """中心固定, 从 t0 到 t1(bpm120: 1秒~3秒)角度从 deg0 转到 deg1"""
    fx, fy = x_px / 1280, 1 - y_px / 720
    return line_dict(
        notes,
        move=[{'startTime': -999999.0, 'endTime': TAIL, 'start': fx, 'end': fx, 'start2': fy, 'end2': fy}],
        rotate=[{'startTime': -999999.0, 'endTime': t0, 'start': deg0, 'end': deg0},
                {'startTime': t0, 'endTime': t1, 'start': deg0, 'end': deg1},
                {'startTime': t1, 'endTime': TAIL, 'start': deg1, 'end': deg1}])


@contextmanager
def guard_off():
    """关掉暂停键保护(旧行为), 用来证明测试谱面确实会按到暂停键、以及保护不改变判定"""
    with mock.patch.object(algo_base, 'avoid_pause_button', lambda pos, sa, ca: pos), \
            mock.patch.object(algo_base, 'pause_free_intervals', lambda bx, by, nx, ny, lo, hi: [(lo, hi, 'all')]):
        yield


def brute_nearest_exit(pos, sa, ca):
    """暴力搜索: 沿垂直方向找离 pos 最近、在屏幕内且在暂停键区域(含出口边距)外的位移 s, 没有返回 None"""
    nx, ny = -sa, ca
    best = None
    for k in range(-3000, 3001):
        s = k * 0.5
        x, y = pos[0] + nx * s, pos[1] + ny * s
        if not (1 <= x <= 1279 and 1 <= y <= 719) or (x < FREE_X and y < FREE_Y):
            continue
        if best is None or abs(s) < abs(best):
            best = s
    return best


def events_of(ans, pred):
    return [(ms, e) for ms in sorted(ans) for e in ans[ms] if pred(e)]


def normal_events_in_box(ans):
    """tap/hold/flick/drag 触点(不含扫屏、滑键触点和接力以外的特殊触点)的任意事件落在暂停键区域里的"""
    return events_of(ans, lambda e: e.pointer < SWEEP_BASE and in_pause_box(e.pos))


class AvoidPauseButtonTest(unittest.TestCase):
    def test_outside_the_box_is_untouched(self):
        for deg in range(0, 180, 15):
            sa, ca = trig(deg)
            for p in ((PW, 10.0), (10.0, PH), (640.0, 360.0), (1279.0, 719.0), (PW + 0.5, PH + 0.5)):
                with self.subTest(deg=deg, p=p):
                    self.assertEqual(avoid_pause_button(p, sa, ca), p)

    def test_horizontal_line_moves_straight_down(self):
        # 水平判定线: 垂直方向是y, 沿线的投影是x
        sa, ca = trig(0)
        for p in ((80.0, 40.0), (20.0, 150.0), (150.0, 20.0)):
            x, y = avoid_pause_button(p, sa, ca)
            self.assertAlmostEqual(x, p[0], places=6)
            self.assertAlmostEqual(y, FREE_Y, places=6)

    def test_vertical_line_moves_straight_right(self):
        sa, ca = trig(90)
        for p in ((80.0, 40.0), (20.0, 150.0), (150.0, 20.0)):
            x, y = avoid_pause_button(p, sa, ca)
            self.assertAlmostEqual(x, FREE_X, places=6)
            self.assertAlmostEqual(y, p[1], places=6)

    def test_projection_kept_and_exit_is_the_nearest_for_every_angle(self):
        # 沿判定线方向的投影不变(判定不受影响); 躲得开时挪出暂停键区域, 而且是最近的出口
        checked = 0
        for deg in range(0, 180, 9):
            sa, ca = trig(deg)
            nx, ny = -sa, ca
            for p in ((10.0, 10.0), (80.0, 40.0), (150.0, 150.0), (159.0, 5.0), (5.0, 159.0), (100.0, 100.0),
                      (30.0, 120.0)):
                q = avoid_pause_button(p, sa, ca)
                with self.subTest(deg=deg, p=p, q=q):
                    self.assertLessEqual(abs((q[0] - p[0]) * ca + (q[1] - p[1]) * sa), 1.0)   # 沿线投影
                    self.assertTrue(1 <= q[0] <= 1279 and 1 <= q[1] <= 719)
                    best = brute_nearest_exit(p, sa, ca)
                    if best is None:
                        continue                       # 躲不开的情形见下一个测试
                    checked += 1
                    self.assertFalse(q[0] < PW and q[1] < PH)
                    self.assertLessEqual(abs((q[0] - p[0]) * nx + (q[1] - p[1]) * ny), abs(best) + 1.5)
        self.assertGreater(checked, 100)

    def test_unavoidable_corner_does_not_crash_and_stays_on_screen(self):
        # 判定线45度、音符在屏幕角落: 垂直线(从(0,80)到(80,0)的一小段)整段都在暂停键区域里, 躲不开
        sa, ca = trig(-45)
        p = (40.0, 40.0)
        self.assertIsNone(brute_nearest_exit(p, sa, ca))
        q = avoid_pause_button(p, sa, ca)
        self.assertTrue(1 <= q[0] <= 1279 and 1 <= q[1] <= 719)
        # 退而求其次: 离屏幕角落更远
        self.assertGreater(math.hypot(*q), math.hypot(*p))

    def test_idempotent(self):
        # 长条/红键的起点已经处理过, 进入分配器时 recalc_pos 还会再处理一遍: 躲得开时必须原样返回;
        # 躲不开(兜底落在区域里)时至多再漂移一两个像素
        for deg in range(0, 180, 10):
            sa, ca = trig(deg)
            for p in ((80.0, 40.0), (150.0, 150.0), (30.0, 120.0)):
                once = avoid_pause_button(p, sa, ca)
                twice = avoid_pause_button(once, sa, ca)
                with self.subTest(deg=deg, p=p):
                    if brute_nearest_exit(p, sa, ca) is not None:
                        self.assertEqual(twice, once)
                    else:
                        self.assertLess(math.hypot(twice[0] - once[0], twice[1] - once[1]), 2.0)

    def test_free_intervals_are_split_by_the_box(self):
        r = math.sqrt(0.5)
        # 垂直方向是 (r, -r) 的斜线从 (100, 200) 穿过暂停键区域: 进入前(low)和穿出后(high)各剩一段
        lo, hi = -140.0, 280.0
        (l1, h1, t1), (l2, h2, t2) = pause_free_intervals(100.0, 200.0, r, -r, lo, hi)
        self.assertEqual((t1, t2), ('low', 'high'))
        self.assertAlmostEqual(l1, lo)
        self.assertAlmostEqual(h1, (200.0 - FREE_Y) / r)         # y 降到 168 之前
        self.assertAlmostEqual(l2, (FREE_X - 100.0) / r)         # x 升到 168 之后
        self.assertAlmostEqual(h2, hi)
        # 垂直方向沿y、从暂停键里往下: 只剩区域下方一段
        self.assertEqual(pause_free_intervals(80.0, 40.0, 0.0, 1.0, -39.0, 679.0), [(FREE_Y - 40.0, 679.0, 'high')])
        # 不经过暂停键区域: 整段, 标签 all
        self.assertEqual(pause_free_intervals(640.0, 360.0, 0.0, 1.0, -300.0, 300.0), [(-300.0, 300.0, 'all')])
        # 整条线都在区域里: 空
        self.assertEqual(pause_free_intervals(40.0, 40.0, r, -r, -55.0, 55.0), [])


class NotePointTest(unittest.TestCase):
    def test_tap_in_the_corner_moves_along_the_normal(self):
        # 水平线在屏幕上沿, 蓝键在 (56, 36): 只在垂直方向挪, 水平位置不变
        chart = chart_of(corner_line([note(64, -1.0, 1)]))
        line = chart.judge_lines[0]
        x, y = note_point(line, line.notes[0], 1000)
        self.assertAlmostEqual(x, 56.0, places=6)
        self.assertAlmostEqual(y, FREE_Y, places=6)

    def test_rotated_line_moves_along_its_own_normal(self):
        chart = chart_of(corner_line([note(64, -1.0, 1)], rot=90.0))
        line = chart.judge_lines[0]
        x, y = note_point(line, line.notes[0], 1000)
        self.assertAlmostEqual(x, FREE_X, places=6)
        self.assertAlmostEqual(y, 108.0, places=6)

    def test_hold_path_never_enters_the_box(self):
        # 判定线绕着角落里的一点转半圈, 长条一直在判定线中心: 每一毫秒的位置都要在暂停键区域外
        chart = chart_of(spinning_line([note(64, 0.0, 3, 128.0)], 100.0, 100.0, 0.0, 180.0))
        line = chart.judge_lines[0]
        n = line.notes[0]
        for ms in range(900, 3200, 3):
            q = hold_point(line, n, ms)
            with self.subTest(ms=ms):
                self.assertFalse(in_pause_box(q), q)
                (cx, cy), sa, ca = note_state(line, n, ms)
                self.assertLessEqual(abs((q[0] - cx) * ca + (q[1] - cy) * sa), 1.0)   # 沿线投影不变

    def test_flick_path_is_a_full_swipe_outside_the_box(self):
        chart = chart_of(corner_line([note(64, -1.0, 4)]))
        line = chart.judge_lines[0]
        path = flick_path(line, line.notes[0], 1000, -50, 50, 100)
        self.assertEqual(len(path), 101)
        for q in path:
            self.assertFalse(in_pause_box(q), q)
            self.assertAlmostEqual(q[0], 56.0, places=6)           # 沿线投影(x)不变
        ys = [q[1] for q in path]
        self.assertAlmostEqual(abs(ys[0] - ys[-1]), 200.0, delta=1.0)  # 滑满2*radius, 速度不降
        self.assertEqual(sorted(ys, reverse=ys[0] > ys[-1]), ys)       # 整段单调, 没有中途换边
        self.assertGreaterEqual(min(ys), FREE_Y - 1e-6)

    def test_flick_path_on_a_spinning_line_keeps_one_side(self):
        # 判定线绕角落转动, 红键的整段滑动每一毫秒都在区域外
        chart = chart_of(spinning_line([note(128, 0.0, 4)], 100.0, 100.0, 0.0, 180.0))
        line = chart.judge_lines[0]
        for center in (1500, 2000, 2500):
            for reverse in (False, True):
                path = flick_path(line, line.notes[0], center, -50, 50, 100, reverse=reverse)
                with self.subTest(center=center, reverse=reverse):
                    self.assertEqual([q for q in path if in_pause_box(q)], [])

    def test_flick_path_far_from_the_box_is_unchanged(self):
        # 离暂停键远的红键: 和以前一样(有没有保护结果都一样)
        chart = chart_of(corner_line([note(64, 0.0, 4)], x_px=640.0, y_px=360.0, rot=30.0))
        line = chart.judge_lines[0]
        on = flick_path(line, line.notes[0], 1000, -50, 50, 100)
        with guard_off():
            off = flick_path(line, line.notes[0], 1000, -50, 50, 100)
        self.assertEqual(on, off)


def corner_chart():
    """四种音符都落在暂停键区域里: 屏幕上沿的水平线、屏幕左沿的竖直线、绕角落转动的线。
    不同判定线上的蓝键/长条头至少隔125ms(bpm120: 8单位), 免得手指在别的线的判定窗口里互相抢音符。"""
    top = corner_line([note(t, x, tp, h) for t, x, tp, h in (
        (72, -1.0, 1, 0.0), (96, 0.0, 1, 0.0), (104, 1.0, 2, 0.0), (112, -0.5, 4, 0.0), (136, -1.0, 3, 24.0),
        (170, -1.2, 1, 0.0), (178, -0.2, 4, 0.0), (186, 0.6, 2, 0.0))], x_px=128.0, y_px=36.0)
    left = corner_line([note(t, x, tp, h) for t, x, tp, h in (
        (80, 3.0, 1, 0.0), (88, 2.0, 2, 0.0), (120, 2.5, 4, 0.0), (150, 1.5, 3, 32.0), (200, 3.2, 1, 0.0))],
        x_px=30.0, y_px=360.0, rot=90.0)
    spin = spinning_line([note(t, 0.0, tp, h) for t, tp, h in (
        (64, 1, 0.0), (90, 2, 0.0), (128, 4, 0.0), (160, 3, 40.0), (216, 1, 0.0))], 100.0, 100.0, 0.0, 180.0)
    return chart_of(top, left, spin)


ALGOS = (('algo1', algo1.solve), ('algo2', algo2.solve), ('algo3', algo3.solve), ('algo3f', algo3f.solve))
SWEEP_ALGOS = (('algo3', algo3.solve), ('algo3f', algo3f.solve))


class CornerChartTest(unittest.TestCase):
    def test_the_test_chart_really_presses_pause_without_the_guard(self):
        # 防止测试空转: 关掉保护时这张谱面一定会按到暂停键
        chart = corner_chart()
        with guard_off():
            for name, solve in ALGOS:
                with self.subTest(algo=name):
                    ans = solve(chart, quiet(), 16)
                    self.assertGreaterEqual(len(pause_presses(ans)), 5)
                    self.assertGreater(len(normal_events_in_box(ans)), 5)

    def test_no_press_and_no_normal_event_in_the_box(self):
        chart = corner_chart()
        for name, solve in ALGOS:
            for mp in (16, 10):
                with self.subTest(algo=name, max_pointers=mp):
                    ans = solve(chart, quiet(), mp)
                    self.assertEqual(pause_presses(ans), [])
                    self.assertEqual(normal_events_in_box(ans), [])

    def test_judging_is_not_affected(self):
        # 垂直判定: 挪动之后所有音符照样Perfect(几种帧率/随机帧相位/判定规则)。
        # 只看 algo3/algo3f: algo1/algo2 的黄键会按下一个手指, 那个手指会抢走附近其他线上的蓝键(和暂停键无关),
        # 它们改看下一个测试(有保护和没保护判定结果一样)
        chart = corner_chart()
        for name, solve in SWEEP_ALGOS:
            ans = solve(chart, quiet(), 16)
            for fps in (60, 30):
                for seed in range(3):
                    for rule in ({}, {'phigros': True}):
                        with self.subTest(algo=name, fps=fps, seed=seed, rule=tuple(rule)):
                            self.assertEqual(non_perfect(simulate(chart, ans, fps=fps, seed=seed, **rule)), {})

    def test_guard_does_not_change_the_result_of_judging(self):
        # 所有算法: 有保护的规划和关掉保护的规划, 判定结果逐个音符完全一样
        chart = corner_chart()
        for name, solve in ALGOS:
            with guard_off():
                off = solve(chart, quiet(), 16)
            on = solve(chart, quiet(), 16)
            for fps in (60, 30):
                for seed in range(3):
                    with self.subTest(algo=name, fps=fps, seed=seed):
                        self.assertEqual(non_perfect(simulate(chart, on, fps=fps, seed=seed)),
                                         non_perfect(simulate(chart, off, fps=fps, seed=seed)))

    def test_judging_survives_timing_offsets(self):
        # 整条触控链路晚/早几十毫秒(手动同步误差): 触点离音符垂直方向远了一点, 沿线的投影误差只有 s*sin(转角), 远小于判定宽度
        chart = corner_chart()
        ans = algo3f.solve(chart, quiet(), 16)
        for d in (-50, 50):
            shifted = type(ans)(list)
            for ms, evs in ans.items():
                shifted[ms + d].extend(evs)
            with self.subTest(offset=d):
                self.assertEqual(non_perfect(simulate(chart, shifted, fps=60, seed=1)), {})


class UnavoidableWarningTest(unittest.TestCase):
    def test_unavoidable_press_is_reported(self):
        # 45度的判定线正好擦过 (40, 40): 垂直线整段都在暂停键区域里, 躲不开 -> 统计并警告, 而不是悄悄按下去
        chart = chart_of(corner_line([note(64, 0.0, 1)], x_px=40.0, y_px=40.0, rot=-45.0))
        stats, buf = {}, io.StringIO()
        ans = algo2.solve(chart, Console(file=buf, width=200), 16, stats=stats)
        self.assertGreaterEqual(stats['pause_presses'], 1)
        self.assertEqual(stats['pause_presses'], len(pause_presses(ans)))
        self.assertIn('暂停键', buf.getvalue())

    def test_clean_plan_has_no_warning(self):
        chart = corner_chart()
        buf = io.StringIO()
        algo2.solve(chart, Console(file=buf, width=200), 16)
        self.assertNotIn('暂停键', buf.getvalue())
        self.assertEqual(warn_pause_presses(algo2.solve(chart, quiet(), 16), quiet()), 0)


class SweeperTest(unittest.TestCase):
    def test_up_is_moved_out_of_the_box(self):
        # 扫屏触点在 y=90 这一行(整行都在暂停键的高度范围里)左右扫: 区间正好在它扫到x<160时结束, 抬起要推迟到区域外
        sw = algo3.Sweeper(algo3.SWEEP_POINTER_BASE, True, 90.0, 20.0, 1260.0, 97, 0.37, True, 0)
        chart = chart_of(line_dict([note(64, 0.0, 2), note(96, 0.0, 2)]))
        start, end = 1500, 4000
        down_t = start - algo3.SETTLE_MS
        v0 = 700.0
        t0 = down_t + algo3.SETTLE_MS
        # 找一个 end, 原本抬起时的位置在暂停键里
        end = next(e for e in range(3000, 6000)
                   if in_pause_box(sw.point(algo3.triangle(e, t0, v0, sw.half_period, sw.forward, sw.lo, sw.hi))))
        with mock.patch.object(algo3, '_choose_down', lambda heads, s, sweeper, preferred: (down_t, v0, True)):
            ans = algo3.plan_sweepers(chart, [sw], [[start, end]])
        ups = events_of(ans, lambda e: e.action == TouchAction.UP)
        self.assertEqual(len(ups), 1)
        up_ms = next(ms for ms in ans if any(e.action == TouchAction.UP for e in ans[ms]))
        self.assertFalse(in_pause_box(ups[0][1].pos))
        self.assertTrue(end < up_ms <= end + algo3.SAFE_END_SEARCH)

    def test_down_fallback_stays_outside_the_box(self):
        horizontal = algo3.Sweeper(2000, True, 90.0, 20.0, 1260.0, 97)       # y=90: 整行经过暂停键区域
        vertical = algo3.Sweeper(2001, False, 100.0, 20.0, 700.0, 71)        # x=100: 整列经过暂停键区域
        clear = algo3.Sweeper(2002, True, 360.0, 20.0, 1260.0, 103)          # y=360: 不经过
        self.assertGreaterEqual(algo3._outside_pause(horizontal, 20.0), FREE_X)
        self.assertGreaterEqual(algo3._outside_pause(vertical, 30.0), FREE_Y)
        self.assertEqual(algo3._outside_pause(horizontal, 700.0), 700.0)
        self.assertEqual(algo3._outside_pause(clear, 20.0), 20.0)
        # 找不到安全按下位置时(所有位置都被tap/hold占住)的兜底也要避开暂停键
        with mock.patch.object(algo3, '_safe_values', lambda heads, t, sw: []):
            t, v, safe = algo3._choose_down([], 1000, horizontal, 20.0)
        self.assertFalse(safe)
        self.assertFalse(in_pause_box(horizontal.point(v)))

    def test_every_row_sweeper_press_is_outside(self):
        # 整个algo3(16/10触点, 每行一个扫屏触点)的按下和抬起都不在暂停键区域里
        chart = corner_chart()
        for mp in (16, 10):
            ans = algo3.solve(chart, quiet(), mp)
            with self.subTest(max_pointers=mp):
                self.assertEqual(pause_presses(ans), [])
                sweepers = events_of(ans, lambda e: SWEEP_BASE <= e.pointer < algo3.FLICK_FINGER_BASE)
                self.assertTrue(sweepers)


def load_chart_at():
    path = os.path.join(ROOT, 'Chart_AT.json')
    if not os.path.isfile(path):
        raise unittest.SkipTest('Chart_AT.json 不在工作区')
    with io.open(path, encoding='utf-8-sig') as f:
        return Chart.from_dict(json.load(f))


class ChartATTest(unittest.TestCase):
    """仓库里的 Chart_AT.json(Entrance to the Chaos AT): 有长条随判定线飘过左上角"""

    @classmethod
    def setUpClass(cls):
        cls.chart = load_chart_at()

    def test_the_chart_really_touches_the_corner_without_the_guard(self):
        with guard_off():
            ans = algo3f.solve(self.chart, quiet(), 16)
        n = len(normal_events_in_box(ans))
        if n <= 20:
            self.skipTest(f'当前 Chart_AT.json 不是会大量擦过暂停键的旧谱(只有 {n} 次)')
        self.assertGreater(n, 20)

    def test_no_event_of_a_normal_pointer_in_the_box(self):
        for name, solve in (('algo3f', algo3f.solve), ('algo3', algo3.solve)):
            for mp in (16, 10):
                with self.subTest(algo=name, max_pointers=mp):
                    ans = solve(self.chart, quiet(), mp)
                    self.assertEqual(normal_events_in_box(ans), [])
                    self.assertEqual(pause_presses(ans), [])

    def test_judging_matches_the_unguarded_plan(self):
        with guard_off():
            off = algo3f.solve(self.chart, quiet(), 16)
        on = algo3f.solve(self.chart, quiet(), 16)
        for seed in (1, 2):
            with self.subTest(seed=seed):
                self.assertEqual(non_perfect(simulate(self.chart, on, fps=60, seed=seed, phigros=True)),
                                 non_perfect(simulate(self.chart, off, fps=60, seed=seed, phigros=True)))


if __name__ == '__main__':
    unittest.main()
