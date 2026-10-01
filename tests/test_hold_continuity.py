'''长条触点坐标连续性 + 播放结束后"实时延迟"归零。

#1 的来龙去脉(Chart_AT.json, 用户报告"前八个长条分两波, 第二波必断一个"):

谱面里这8个长条所在的判定线, moveY 在 2.0(屏幕坐标 y=-720) 和 0.7/0.5 之间
**零过渡时间跳变** —— 判定线真的在瞬移, 长条跟着瞬移出屏幕再回来。

旧的规划用 note_point -> recalc_pos: 音符在屏幕内时取音符本身, 越界时返回
"垂直弦的中点"。这两套表示不连续, 于是手指被要求在1ms内从 (768, 2.6) 跳到
(768, 360) 再跳回 (192, 720) —— 实测单次 357px / 679px, 全谱面最大 1603px。

hold_point 改成"沿垂直于判定线的方向夹回屏幕内": 音符滑出屏幕上沿时手指连续地
滑到 y=1 并一直按在屏幕边缘上, 沿判定线方向的投影始终精确等于音符位置,
判定不受影响(判定只看投影, 见 tools/judge_sim.py 的 local_x)。
'''
import ast
import io
import math
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Note:
    def __init__(self, x=0.0, time=0.0, hold=1.0):
        self.x, self.time, self.hold = x, time, hold


class _Line:
    """水平判定线: 位置 (640, y(t)), 角度恒为0。y 由若干 (t, y) 线性插值给出。"""

    def __init__(self, pts, bpm=120.0):
        self.pts = sorted(pts)
        self.bpm = bpm

    def time(self, second):
        return second * self.bpm / 1.875

    def seconds(self, t):
        return t * 1.875 / self.bpm

    def pos(self, t):
        if t <= self.pts[0][0]:
            return 640.0, self.pts[0][1]
        for (t0, y0), (t1, y1) in zip(self.pts, self.pts[1:]):
            if t <= t1:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                return 640.0, y0 + (y1 - y0) * f
        return 640.0, self.pts[-1][1]

    def angle(self, t):
        return 0.0

    def pos_of(self, note, t):
        return self.pos(t)


def _hold_module():
    import sys
    sys.path.insert(0, ROOT)
    from algo import algo_base
    return algo_base


class HoldContinuityTest(unittest.TestCase):
    '''hold_point: 越界时夹到屏幕边缘, 手指位置连续, 且沿判定线方向的投影不变'''

    def setUp(self):
        self.ab = _hold_module()

    def test_on_screen_returns_note_itself(self):
        line = _Line([(0, 300.0), (10, 500.0)])
        note = _Note()
        for ms in (0, 100, 500, 1000):
            self.assertEqual(self.ab.hold_point(line, note, ms),
                             self.ab.note_point(line, note, ms),
                             f'{ms}ms 在屏幕内时应与旧行为一致')

    def test_off_screen_result_is_on_screen(self):
        line = _Line([(0, 300.0), (10, -900.0)])   # 一路滑出屏幕上沿
        note = _Note()
        for ms in range(0, 2001, 25):
            p = self.ab.hold_point(line, note, ms)
            self.assertTrue(0 <= p[0] <= 1280 and 0 <= p[1] <= 720,
                            f'{ms}ms 手指跑出屏幕: {p}')

    def test_projection_along_line_is_exact(self):
        """判定只看沿判定线方向的投影, 所以夹位不能改动它(水平线时就是x)"""
        line = _Line([(0, 300.0), (10, -900.0), (20, 900.0)])
        note = _Note()
        for ms in range(0, 4001, 20):
            t = line.time(ms / 1000)
            want_x = line.pos(t)[0]
            got = self.ab.hold_point(line, note, ms)
            self.assertAlmostEqual(got[0], want_x, places=6,
                                   msg=f'{ms}ms 沿判定线方向的投影被夹位改动了')

    def test_no_big_jump_when_leaving_screen(self):
        """核心回归: 旧实现在音符越界那一毫秒要求手指跳几百像素"""
        line = _Line([(0, 700.0), (2, -800.0), (10, -800.0), (12, 700.0)])
        note = _Note()
        n = 24001
        old = [self.ab.note_point(line, note, k / 2) for k in range(n)]
        new = [self.ab.hold_point(line, note, k / 2) for k in range(n)]
        jump = lambda pts: max(math.hypot(b[0] - a[0], b[1] - a[1])
                               for a, b in zip(pts, pts[1:]))
        # 旧的: 越界瞬间 357px 级别的跳变
        self.assertGreater(jump(old), 200, '旧实现的跳变没有复现出来, 测试失去意义')
        # 新的: 只剩沿判定线方向必须跟的那一点位移, 每0.5ms不超过几十像素
        self.assertLess(jump(new), 60, 'hold_point 仍有大跳变')

    def test_hold_path_stays_continuous_on_real_chart(self):
        """Chart_AT.json 的8个长条: 手指相邻毫秒最大跳变里, 越界那几次不能是大头"""
        path = os.path.join(ROOT, 'Chart_AT.json')
        if not os.path.isfile(path):
            self.skipTest('Chart_AT.json 不在工作区')
        import json
        from chart import Chart
        from note import NoteType
        chart = Chart.from_dict(json.load(io.open(path, encoding='utf-8-sig')))
        holds = []
        for li, line in enumerate(chart.judge_lines):
            for nt in line.notes_above + line.notes_below:
                if nt.type == NoteType.HOLD:
                    s = round(line.seconds(nt.time) * 1000)
                    holds.append((li, line, nt, s,
                                  s + round(line.seconds(nt.hold) * 1000)))
        holds.sort(key=lambda h: h[3])
        self.assertGreaterEqual(len(holds), 8)
        for li, line, note, s, e in holds[:8]:
            hms = math.ceil(line.seconds(note.hold) * 1000)
            pts = [self.ab.hold_point(line, note, s + o) for o in range(hms + 1)]
            worst = max(math.hypot(b[0] - a[0], b[1] - a[1])
                        for a, b in zip(pts, pts[1:]))
            # 判定线本身在瞬移, 沿判定线方向必须跟着跳; 但不能再叠加
            # "越界时跳到屏幕中央"那一份几百像素的垂直跳变
            self.assertLess(worst, 1300, f'line{li} 手指跳变 {worst:.0f}px 过大')


class LiveDelayResetTest(unittest.TestCase):
    '''播放结束/停止后, "实时延迟"必须自动归零'''

    @staticmethod
    def _src():
        with io.open(os.path.join(ROOT, 'main.py'), encoding='utf-8') as f:
            return f.read()

    @staticmethod
    def _method(name):
        for node in ast.parse(LiveDelayResetTest._src()).body:
            if isinstance(node, ast.ClassDef) and node.name == 'MainPage':
                for n in node.body:
                    if isinstance(n, ast.FunctionDef) and n.name == name:
                        return n
        raise AssertionError(f'找不到 {name}')

    def test_reset_go_zeroes_live_delay(self):
        seg = ast.dump(self._method('_reset_go'))
        self.assertIn('live_delay_spin', seg, '_reset_go 没有复位"实时延迟"旋钮')
        self.assertIn('_fine_tune', seg, '_reset_go 没有清零 _fine_tune')

    def test_reset_go_is_wired_to_playback_finished(self):
        src = self._src()
        self.assertIn('playback_finished.connect(self._reset_go)', src,
                      '播放结束信号没有接到 _reset_go')


if __name__ == '__main__':
    unittest.main()
