"""iOS方案: 规划结果 -> WebDriverAgent W3C actions 的转换(algo/ios_actions.py)。

转换必须满足两件事:
1. 结构符合WDA源码里的语义(每个输入源一条独立时间线、第一个pointerMove创建触点、每次按下新开一个源、
   不带pressure), 否则真机上要么被拒绝要么按下/抬起时刻错位;
2. 转换前后判定结果不变(哪怕轨迹经过精简): 用参考解释器 replay_actions 把JSON还原成事件, 再交给判定模拟器。
没有iPhone也能跑, 所以这里测的是数据转换本身; 设备端的真实行为(时序精度、触点上限、最短接触时间)
要用 tools/ios_probe.py 到真机上测, 见 docs/ios_feasibility.md。
"""
import io
import json
import math
import os
import sys
import unittest

from rich.console import Console

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import algo.algo3f as algo3f  # noqa: E402
from algo.algo_base import TouchAction, VirtualTouchEvent  # noqa: E402
from algo.ios_actions import (GAP_LINEAR_MS, Life, Mapping, build_actions, count_items, fit_mapping,  # noqa: E402
                              hold_polyline, life_to_source, peak_pointers, replay_actions, simplify, split_lives)
from chart import Chart  # noqa: E402
from judge_sim import simulate  # noqa: E402
from tests.test_algo3 import drag_flick_chart, non_perfect  # noqa: E402
from tests.test_hold_relay import teleport_chart  # noqa: E402

DOWN, MOVE, UP = TouchAction.DOWN, TouchAction.MOVE, TouchAction.UP
IDENTITY = Mapping(0.0, 0.0, 1.0, 1280.0, 720.0)


def quiet():
    return Console(file=io.StringIO(), width=200)


def plan(*events):
    """(ms, pointer, action, (x, y)) ... -> {ms: [VirtualTouchEvent]}"""
    out = {}
    for ms, pid, action, pos in events:
        out.setdefault(ms, []).append(VirtualTouchEvent(pos, action, pid))
    return out


class SplitLivesTest(unittest.TestCase):
    def test_lives_are_cut_at_down_and_up(self):
        ans = plan((100, 1, DOWN, (10.0, 10.0)), (100, 2, DOWN, (50.0, 50.0)), (110, 1, MOVE, (20.0, 10.0)),
                   (110, 1, MOVE, (30.0, 10.0)), (120, 1, UP, (30.0, 10.0)), (200, 1, DOWN, (5.0, 5.0)),
                   (205, 1, UP, (5.0, 5.0)), (500, 2, UP, (50.0, 50.0)),
                   (150, 3, MOVE, (1.0, 1.0)), (160, 4, UP, (2.0, 2.0)))       # 没按下就出现的MOVE/UP被忽略
        lives = split_lives(ans)
        self.assertEqual([(lf.pointer, lf.t0, lf.t1) for lf in lives], [(1, 100, 120), (2, 100, 500), (1, 200, 205)])
        # 同一毫秒的两个MOVE只留最后一个
        self.assertEqual(lives[0].points, ((100, (10.0, 10.0)), (110, (30.0, 10.0)), (120, (30.0, 10.0))))

    def test_dangling_pointer_gets_a_closing_up(self):
        lives = split_lives(plan((100, 1, DOWN, (1.0, 1.0)), (150, 1, MOVE, (2.0, 2.0))))
        self.assertEqual([(lf.t0, lf.t1) for lf in lives], [(100, 150)])

    def test_peak_counts_relay_as_not_simultaneous(self):
        a, b, c = Life(1, 0, 10, ((0, (0.0, 0.0)),)), Life(2, 10, 20, ((10, (0.0, 0.0)),)), \
            Life(3, 5, 15, ((5, (0.0, 0.0)),))
        self.assertEqual(peak_pointers([a, b]), 1)          # 同一毫秒一个抬起一个按下: 接力
        self.assertEqual(peak_pointers([a, b, c]), 2)


class SimplifyTest(unittest.TestCase):
    @staticmethod
    def max_error(original, kept):
        """原来每个点到(按时间线性插值的)精简轨迹的距离"""
        worst = 0.0
        for t, (x, y) in original:
            for (ta, (xa, ya)), (tb, (xb, yb)) in zip(kept, kept[1:]):
                if ta <= t <= tb:
                    f = (t - ta) / (tb - ta) if tb > ta else 0.0
                    worst = max(worst, math.hypot(x - (xa + (xb - xa) * f), y - (ya + (yb - ya) * f)))
                    break
        return worst

    def test_straight_swipe_is_two_points(self):
        pts = tuple((t, (100.0 + 2.0 * t, 300.0)) for t in range(0, 101))
        self.assertEqual(len(simplify(pts, 3.0)), 2)

    def test_triangle_wave_error_is_bounded(self):
        # 扫屏触点: 每6ms一个点, 单程103ms的三角波
        pts = []
        for t in range(0, 2000, 6):
            u = (t / 103.0) % 2
            pts.append((t, (20.0 + 1240.0 * (u if u <= 1 else 2 - u), 360.0)))
        pts = tuple(pts)
        for tol in (1.0, 3.0, 8.0):
            kept = simplify(pts, tol)
            self.assertLess(len(kept), len(pts) / 3)
            self.assertLessEqual(self.max_error(pts, kept), tol + 1e-6)

    def test_teleport_is_kept(self):
        pts = ((0, (100.0, 100.0)), (500, (100.0, 100.0)), (501, (900.0, 600.0)), (1000, (900.0, 600.0)))
        kept = simplify(pts, 3.0)
        self.assertEqual(kept, pts)        # 瞬移前后的点一个都不能丢

    def test_no_tolerance_keeps_everything(self):
        pts = tuple((t, (float(t), 0.0)) for t in range(10))
        self.assertEqual(simplify(pts, 0.0), pts)


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.ans = plan((1000, 1, DOWN, (100.0, 200.0)), (1005, 1, UP, (100.0, 200.0)),
                        (1100, 2, DOWN, (300.0, 400.0)), (1150, 2, MOVE, (400.0, 400.0)), (1200, 2, UP, (400.0, 400.0)))

    def test_each_press_is_its_own_source_with_a_timeline(self):
        body = build_actions(self.ans, IDENTITY)
        self.assertEqual([s['id'] for s in body['actions']], ['p0', 'p1'])
        for src in body['actions']:
            self.assertEqual(src['type'], 'pointer')
            self.assertEqual(src['parameters'], {'pointerType': 'touch'})
        first, second = (s['actions'] for s in body['actions'])
        # 第一个源: 从零点按下(不需要pause), 原地5ms后抬起
        self.assertEqual([i['type'] for i in first], ['pointerMove', 'pointerDown', 'pause', 'pointerUp'])
        self.assertEqual(first[0], {'type': 'pointerMove', 'duration': 0, 'x': 100.0, 'y': 200.0, 'origin': 'viewport'})
        self.assertEqual(first[2], {'type': 'pause', 'duration': 5})
        # 第二个源: 先等100ms(相对第一个事件), 移到起点按下; 事件间隔50ms>GAP_LINEAR_MS, 所以先停在原地49ms,
        # 最后1ms才跳到(400,400)(规划里的MOVE是采样保持, 不能变成50ms的缓慢滑动), 再停50ms抬起
        self.assertEqual([i['type'] for i in second],
                         ['pause', 'pointerMove', 'pointerDown', 'pause', 'pointerMove', 'pause', 'pointerUp'])
        self.assertEqual([i['duration'] for i in second if i['type'] == 'pause'], [100, 49, 50])
        self.assertEqual((second[4]['duration'], second[4]['x'], second[4]['y']), (1, 400.0, 400.0))

    def test_offsets_add_up_to_the_planned_times(self):
        body = build_actions(self.ans, IDENTITY, tolerance=0)
        for src, (t0, t1) in zip(body['actions'], ((0, 5), (100, 200))):
            total = sum(i.get('duration', 0) for i in src['actions'])
            self.assertEqual(total, t1)                                   # 抬起时刻
            pre = 0
            for i in src['actions']:
                if i['type'] == 'pointerDown':
                    break
                pre += i.get('duration', 0)
            self.assertEqual(pre, t0)                                     # 按下时刻

    def test_wda_unfriendly_fields_are_absent(self):
        text = json.dumps(build_actions(self.ans, IDENTITY))
        self.assertNotIn('pressure', text)            # 没有3D Touch的设备带pressure会报错
        self.assertNotIn('"mouse"', text)             # WDA只支持touch
        self.assertNotIn('pointerCancel', text)

    def test_start_ms_shifts_the_zero(self):
        body = build_actions(self.ans, IDENTITY, start_ms=900, tolerance=0)
        self.assertEqual(body['actions'][0]['actions'][0], {'type': 'pause', 'duration': 100})

    def test_min_contact_lengthens_short_presses(self):
        body = build_actions(self.ans, IDENTITY, min_contact_ms=40)
        short = body['actions'][0]['actions']
        self.assertEqual(short[2], {'type': 'pause', 'duration': 40})
        self.assertEqual(short[3]['type'], 'pointerUp')
        long_ = body['actions'][1]['actions']
        self.assertEqual(long_[-2], {'type': 'pause', 'duration': 50})    # 已经够长的不动

    def test_empty_plan(self):
        self.assertEqual(build_actions({}, IDENTITY), {'actions': []})

    def test_mapping_letterboxes_like_the_android_adapter(self):
        m = fit_mapping(852.0, 393.0)
        s = 393 / 720
        self.assertAlmostEqual(m.scale, s)
        self.assertAlmostEqual(m.x_offset, (852 - 1280 * s) / 2)
        self.assertAlmostEqual(m.y_offset, 0.0)
        x, y = m.apply((640.0, 360.0))
        self.assertAlmostEqual(x, 426.0)                 # 屏幕正中
        self.assertAlmostEqual(y, 196.5)
        for corner in ((0.0, 0.0), (1280.0, 720.0), (-50.0, 900.0)):      # 出界的点被收进屏幕
            x, y = m.apply(corner)
            self.assertTrue(0 < x < 852 and 0 < y < 393)

    def test_sparse_samples_are_held_then_jump(self):
        # 两个事件隔30ms(>GAP_LINEAR_MS): 停在原地29ms, 最后1ms跳过去
        life = Life(7, 500, 530, ((500, (10.0, 10.0)), (530, (40.0, 10.0))))
        src = life_to_source(life, 'x', 500, IDENTITY, tolerance=0)
        self.assertEqual([i['type'] for i in src['actions']],
                         ['pointerMove', 'pointerDown', 'pause', 'pointerMove', 'pointerUp'])
        self.assertEqual(src['actions'][2]['duration'], 29)
        self.assertEqual(src['actions'][3]['duration'], 1)

    def test_dense_samples_are_a_linear_move(self):
        # 扫屏每6ms一个点: 连续移动, pointerMove的duration就是间隔
        self.assertLessEqual(6, GAP_LINEAR_MS)
        life = Life(7, 0, 12, ((0, (10.0, 360.0)), (6, (82.0, 360.0)), (12, (154.0, 360.0))))
        src = life_to_source(life, 'x', 0, IDENTITY, tolerance=0)
        moves = [i for i in src['actions'] if i['type'] == 'pointerMove']
        self.assertEqual([(m['duration'], m['x']) for m in moves], [(0, 10.0), (6, 82.0), (6, 154.0)])

    def test_hold_polyline_inserts_a_dwell_point_only_for_big_gaps(self):
        pts = ((0, (0.0, 0.0)), (4, (1.0, 0.0)), (100, (50.0, 0.0)))
        self.assertEqual(hold_polyline(pts), ((0, (0.0, 0.0)), (4, (1.0, 0.0)), (99, (1.0, 0.0)), (100, (50.0, 0.0))))


class RoundTripTest(unittest.TestCase):
    @staticmethod
    def position_at(events, pid, t):
        """pid 在 t 时刻的位置: 最后一个不晚于 t 的 DOWN/MOVE(没按下返回 None)"""
        pos = None
        for ms in sorted(events):
            if ms > t:
                break
            for e in events[ms]:
                if e.pointer == pid:
                    pos = None if e.action == UP else e.pos
        return pos

    def test_round_trip_keeps_sample_and_hold(self):
        # 规划里的位置是“采样保持”; 转成actions再还原(不精简), 每一毫秒的位置都和原来一样(坐标取到0.1)
        ans = plan((1000, 1, DOWN, (100.0, 200.0)), (1020, 1, MOVE, (110.0, 210.0)), (1040, 1, MOVE, (150.0, 250.0)),
                   (1050, 1, UP, (150.0, 250.0)), (1200, 2, DOWN, (300.0, 400.0)), (1205, 2, UP, (300.0, 400.0)))
        back = replay_actions(build_actions(ans, IDENTITY, start_ms=1000, tolerance=0))
        lives = split_lives(ans)
        for pid, lf in ((0, lives[0]), (1, lives[1])):
            for t in range(lf.t0, lf.t1):
                want = self.position_at(ans, lf.pointer, t)
                got = self.position_at(back, pid, t - 1000)
                with self.subTest(pointer=lf.pointer, t=t):
                    self.assertIsNotNone(got)
                    self.assertLess(math.hypot(got[0] - want[0], got[1] - want[1]), 0.08)
            self.assertIsNone(self.position_at(back, pid, lf.t1 - 1000))          # 抬起之后没有触点
        ups = sorted(ms + 1000 for ms, evs in back.items() for e in evs if e.action == UP)
        self.assertEqual(ups, [1050, 1205])

    def test_teleport_is_not_smeared_into_a_slow_glide(self):
        # 判定线瞬移: 长条手指在旧位置停了300ms, 瞬移那一毫秒才跳过去。若把两个事件间的300ms当作线性滑动,
        # 手指就会在这300ms里缓慢漂向新位置(旧位置的判定区早就丢了)。
        ans = plan((0, 1, DOWN, (200.0, 300.0)), (300, 1, MOVE, (900.0, 500.0)), (600, 1, UP, (900.0, 500.0)))
        back = replay_actions(build_actions(ans, IDENTITY, tolerance=3.0))
        for t in (1, 100, 250, 298):
            self.assertEqual(self.position_at(back, 0, t), (200.0, 300.0))
        self.assertEqual(self.position_at(back, 0, 300), (900.0, 500.0))
        self.assertEqual(self.position_at(back, 0, 450), (900.0, 500.0))

    def test_simplified_round_trip_keeps_press_times_and_shrinks(self):
        chart = teleport_chart()
        ans = algo3f.solve(chart, quiet(), 16)
        body = build_actions(ans, IDENTITY, tolerance=3.0)
        back = replay_actions(body)
        self.assertEqual(len(split_lives(back)), len(split_lives(ans)))
        # 每次按下/抬起的时刻一样
        orig = sorted((lf.t0, lf.t1) for lf in split_lives(ans))
        base = split_lives(ans)[0].t0
        new = sorted((lf.t0 + base, lf.t1 + base) for lf in split_lives(back))
        self.assertEqual(orig, new)
        # 压缩有效
        raw_items = sum(len(lf.points) for lf in split_lives(ans))
        self.assertLess(count_items(body), raw_items)

    def test_judging_matches_the_direct_plan(self):
        # 端到端: 规划 -> W3C actions(精简) -> 还原 -> 判定模拟器, 和直接用规划判定的结果一样
        for name, chart in (('teleport', teleport_chart()), ('drag_flick', drag_flick_chart())):
            ans = algo3f.solve(chart, quiet(), 16)
            lives = split_lives(ans)
            base = lives[0].t0
            back = replay_actions(build_actions(ans, IDENTITY, tolerance=3.0))
            back = {ms + base: evs for ms, evs in back.items()}
            for fps in (60, 30):
                for seed in range(2):
                    with self.subTest(chart=name, fps=fps, seed=seed):
                        self.assertEqual(non_perfect(simulate(chart, ans, fps=fps, seed=seed, phigros=True)), {})
                        self.assertEqual(non_perfect(simulate(chart, back, fps=fps, seed=seed, phigros=True)), {})


def load_chart_at():
    path = os.path.join(ROOT, 'Chart_AT.json')
    if not os.path.isfile(path):
        raise unittest.SkipTest('Chart_AT.json 不在工作区')
    with io.open(path, encoding='utf-8-sig') as f:
        return Chart.from_dict(json.load(f))


class ChartATTest(unittest.TestCase):
    """仓库里的 Chart_AT.json: 一首歌的actions有多大(决定能不能一次请求发完)"""

    @classmethod
    def setUpClass(cls):
        cls.chart = load_chart_at()
        cls.ans = algo3f.solve(cls.chart, quiet(), 10)
        cls.lives = split_lives(cls.ans)

    def test_one_source_per_press(self):
        body = build_actions(self.ans, fit_mapping(852.0, 393.0))
        self.assertEqual(len(body['actions']), len(self.lives))
        self.assertEqual(len({s['id'] for s in body['actions']}), len(self.lives))

    def test_simplification_shrinks_the_payload_a_lot(self):
        raw = sum(len(lf.points) for lf in self.lives)
        body = build_actions(self.ans, fit_mapping(852.0, 393.0), tolerance=3.0)
        self.assertLess(count_items(body), raw / 2)
        size = len(json.dumps(body, separators=(',', ':')))
        self.assertLess(size, 3_000_000)           # 一首歌的请求体不会大到几十MB

    def test_pointer_peak_is_the_planned_cap(self):
        self.assertLessEqual(peak_pointers(self.lives), 10)


if __name__ == '__main__':
    unittest.main()
