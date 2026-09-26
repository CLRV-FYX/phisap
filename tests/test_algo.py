"""规划算法(algo1/algo2)测试: 判定线移动/旋转时的hold与flick跟随、触点上限、事件序列合法性。

用一个简化的"判定器"检查规划结果: Phigros按触点在判定线方向上的投影判定音符,
与判定线的垂直距离不影响判定。
"""
import io
import math
import random
import unittest
from bisect import bisect_right

from rich.console import Console

import algo.algo1
import algo.algo2
from algo.algo_base import TouchAction
from chart import Chart

ALGOS = (('algo1', algo.algo1.solve), ('algo2', algo.algo2.solve))
TAIL = 1e9


def quiet():
    return Console(file=io.StringIO(), width=200)


def line_dict(notes, move=None, rotate=None, bpm=120.0):
    return {
        'bpm': bpm,
        'notesAbove': notes,
        'notesBelow': [],
        'speedEvents': [{'startTime': 0.0, 'endTime': TAIL, 'value': 1.0}],
        'judgeLineDisappearEvents': [{'startTime': -999999.0, 'endTime': TAIL, 'start': 1.0, 'end': 1.0}],
        'judgeLineMoveEvents': move or [{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.5, 'end': 0.5,
                                         'start2': 0.5, 'end2': 0.5}],
        'judgeLineRotateEvents': rotate or [{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.0, 'end': 0.0}],
    }


def note(t, x, n_type=1, hold=0.0):
    return {'type': n_type, 'time': t, 'positionX': x, 'holdTime': hold, 'speed': 1.0, 'floorPosition': 0.0}


def chart_of(*lines):
    return Chart.from_dict({'formatVersion': 3, 'offset': 0.0, 'judgeLineList': list(lines)})


class Timeline:
    """按时间回放规划结果, 查询任意毫秒时屏幕上的触点"""

    def __init__(self, ans):
        self.times, self.states = [], []
        state = {}
        self.errors = []
        self.max_active = 0
        for ms in sorted(ans):
            for e in ans[ms]:
                if e.action == TouchAction.DOWN:
                    if e.pointer in state:
                        self.errors.append(f'{ms}: pointer {e.pointer} DOWN twice')
                    state[e.pointer] = tuple(e.pos)
                elif e.action == TouchAction.MOVE:
                    if e.pointer not in state:
                        self.errors.append(f'{ms}: pointer {e.pointer} MOVE while up')
                    state[e.pointer] = tuple(e.pos)
                elif e.action == TouchAction.UP:
                    if e.pointer not in state:
                        self.errors.append(f'{ms}: pointer {e.pointer} UP while up')
                    state.pop(e.pointer, None)
                self.max_active = max(self.max_active, len(state))
            self.times.append(ms)
            self.states.append(dict(state))

    def at(self, ms):
        i = bisect_right(self.times, ms) - 1
        return self.states[i] if i >= 0 else {}


def judge_offset(line, t_ms, pos):
    """触点在判定线方向上相对线中心的投影(像素)"""
    t = line.time(t_ms / 1000)
    cx, cy = line.pos(t)
    a = -line.angle(t) * math.pi / 180
    return (pos[0] - cx) * math.cos(a) + (pos[1] - cy) * math.sin(a)


def rotating_line(notes):
    # 线在屏幕下方(y分数0.3), 1秒到3秒之间逆时针转过120度, 同时向右移动
    return line_dict(
        notes,
        move=[{'startTime': -999999.0, 'endTime': 64.0, 'start': 0.4, 'end': 0.4, 'start2': 0.3, 'end2': 0.3},
              {'startTime': 64.0, 'endTime': 192.0, 'start': 0.4, 'end': 0.6, 'start2': 0.3, 'end2': 0.3},
              {'startTime': 192.0, 'endTime': TAIL, 'start': 0.6, 'end': 0.6, 'start2': 0.3, 'end2': 0.3}],
        rotate=[{'startTime': -999999.0, 'endTime': 64.0, 'start': 0.0, 'end': 0.0},
                {'startTime': 64.0, 'endTime': 192.0, 'start': 0.0, 'end': 120.0},
                {'startTime': 192.0, 'endTime': TAIL, 'start': 120.0, 'end': 120.0}],
    )


class TestFollowLine(unittest.TestCase):
    def test_hold_follows_rotating_line(self):
        # bpm120: 32单位=0.5秒。hold从1.25秒(80)开始, 持续1.5秒(96单位)
        chart = chart_of(rotating_line([note(80, 3.0, n_type=3, hold=96.0)]))
        line = chart.judge_lines[0]
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                tl = Timeline(solve(chart, quiet(), 16))
                self.assertEqual(tl.errors, [])
                for ms in range(1250, 2750, 5):
                    offsets = [judge_offset(line, ms, p) for p in tl.at(ms).values()]
                    self.assertTrue(any(abs(o - 216.0) < 15 for o in offsets),
                                    f'{name}: {ms}ms 时没有触点在hold的判定范围内: {offsets}')

    def test_flick_follows_rotating_line(self):
        chart = chart_of(rotating_line([note(t, -2.0, n_type=4) for t in (96, 128, 160)]))
        line = chart.judge_lines[0]
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                tl = Timeline(solve(chart, quiet(), 16))
                self.assertEqual(tl.errors, [])
                for t in (96, 128, 160):
                    ms = round(line.seconds(t) * 1000)
                    for d in range(-20, 21, 5):
                        now, prev = tl.at(ms + d), tl.at(ms + d - 2)
                        ok = [pid for pid, p in now.items()
                              if abs(judge_offset(line, ms + d, p) + 144.0) < 30 and pid in prev and prev[pid] != p]
                        self.assertTrue(ok, f'{name}: flick@{t} {d:+}ms 没有在判定范围内滑动的触点')

    def test_tap_on_rotated_line_below_center(self):
        # 线在屏幕下方旋转了60度时, 点击位置必须在线的实际位置上(曾因y方向镜像而错位)
        chart = chart_of(line_dict(
            [note(64, 2.0)],
            move=[{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.5, 'end': 0.5, 'start2': 0.2, 'end2': 0.2}],
            rotate=[{'startTime': -999999.0, 'endTime': TAIL, 'start': 60.0, 'end': 60.0}]))
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                ans = solve(chart, quiet(), 16)
                downs = [e for e in ans[1000] if e.action == TouchAction.DOWN]
                self.assertEqual(len(downs), 1)
                x, y = downs[0].pos
                self.assertAlmostEqual(x, 640 + 144 * math.cos(math.radians(60)), places=3)
                self.assertAlmostEqual(y, 576 - 144 * math.sin(math.radians(60)), places=3)


class TestPointerLimit(unittest.TestCase):
    def test_14_simultaneous_taps(self):
        chart = chart_of(line_dict([note(64, -8.0 + i * 1.2) for i in range(14)]))
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                ans = solve(chart, quiet(), 16)
                downs = {e.pointer for e in ans[1000] if e.action == TouchAction.DOWN}
                self.assertEqual(len(downs), 14)
                tl = Timeline(ans)
                self.assertEqual(tl.errors, [])
                self.assertLessEqual(tl.max_active, 16)

    def test_more_than_limit_does_not_crash(self):
        # 12个同时按住的hold + 之后的tap: 上限10时不崩溃, 并给出警告
        notes = [note(64, -8.0 + i * 1.4, n_type=3, hold=64.0) for i in range(12)] + [note(80, 0.0)]
        chart = chart_of(line_dict(notes))
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                con = quiet()
                ans = solve(chart, con, 10)
                self.assertIn('警告', con.file.getvalue())
                tl = Timeline(ans)
                self.assertEqual(tl.errors, [])
                if name == 'algo2':
                    self.assertLessEqual(tl.max_active, 10)

    def test_releases_idle_pointers_instead_of_raising(self):
        # 连续密集的tap/drag会留下闲置触点, 超过上限时应提前抬起而不是报错
        notes = []
        for k in range(40):
            notes += [note(64 + k * 2, -6.0 + (k * 7 % 12), n_type=2), note(64 + k * 2, 6.0 - (k * 5 % 12), n_type=1)]
        chart = chart_of(line_dict(notes))
        for name, solve in ALGOS:
            with self.subTest(algo=name):
                tl = Timeline(solve(chart, quiet(), 4))
                self.assertEqual(tl.errors, [])


class TestRandomCharts(unittest.TestCase):
    def test_event_sequences_valid(self):
        rng = random.Random(1234)
        for seed in range(4):
            lines = []
            for _ in range(3):
                notes = []
                for _ in range(60):
                    t = rng.randrange(0, 1600)
                    kind = rng.choice([1, 2, 3, 4])
                    notes.append(note(t, rng.uniform(-8, 8), kind, rng.choice([8.0, 32.0, 64.0]) if kind == 3 else 0.0))
                move, rotate = [], []
                t0 = -999999.0
                for k in range(8):
                    t1 = k * 200.0 + 200.0
                    move.append({'startTime': t0, 'endTime': t1, 'start': rng.uniform(0.1, 0.9),
                                 'end': rng.uniform(0.1, 0.9), 'start2': rng.uniform(0.1, 0.9), 'end2': rng.uniform(0.1, 0.9)})
                    rotate.append({'startTime': t0, 'endTime': t1, 'start': rng.uniform(-180, 180), 'end': rng.uniform(-180, 180)})
                    t0 = t1
                move.append(dict(move[-1], startTime=t0, endTime=TAIL))
                rotate.append(dict(rotate[-1], startTime=t0, endTime=TAIL))
                lines.append(line_dict(notes, move, rotate, bpm=rng.choice([120.0, 150.0, 200.0])))
            chart = chart_of(*lines)
            for name, solve in ALGOS:
                with self.subTest(seed=seed, algo=name):
                    tl = Timeline(solve(chart, quiet(), 16))
                    self.assertEqual(tl.errors[:5], [])
                    self.assertLessEqual(tl.max_active, 16)
                    for ms in tl.times:
                        for x, y in tl.at(ms).values():
                            self.assertTrue(-1 <= x <= 1281 and -1 <= y <= 721, f'{name}: 触点超出屏幕 {(x, y)}')


if __name__ == '__main__':
    unittest.main()
