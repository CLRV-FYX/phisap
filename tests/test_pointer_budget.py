"""触点预算回归测试 —— Chart_AT.json "前八个长条分两波, 第二波必断一个"。

用户现象: 一张谱面前8个长条分两波(每波4个), 第一波全中, 第二波必定断一个。

两个缺陷, 都只在"两个密集段之间隔着几百毫秒空拍"的谱面上才暴露(所以以前用
随机谱面的测试碰不到, 随机谱面里音符密密麻麻, 每一帧都有事干):

1. algo2.PointerAllocator.allocate 的步骤0把 released_at 记成 self.now(这一帧的
   时刻), 而不是真实抬起时刻。空拍之后 self.now 比真实抬起晚一大截,
   PID_REUSE_COOLDOWN_MS(40ms)被凭空拉长, 刚刚释放的触点被当成"还在冷却"。
   Chart_AT 第二波长条因此拿不到刚释放的pid, 只能另开新pid(把触点预算吃光),
   或者在被限流之后直接被丢弃。

2. algo2.PointerAllocator._alloc 用 MAX_POINTERS(Android硬上限16)而不是
   max_pointers_count 作为增长上限。solve_with 已经扣掉扫屏/滑键触点, 这里再涨
   就偷走了留给它们的名额, 总并发突破当前后端上限(scrcpy官方10 / 补丁16 /
   MaaTouch 10), 多出来的DOWN被服务端**静默丢弃** —— 用户看到的就是
   "某一波长条莫名断一个", 而且日志里一个字都不报。

修复前用 Chart_AT.json + MaaTouch(10触点)实测: 计划用到15个不同pid, 最高11个
同时按下, 43个时刻超过10, 其中72464ms正好落在第二波长条区间(6957-9281)内。
修复后: 最多10个pid, 最高10个同时按下, 0个时刻超限, 8个长条全部完整。
"""
import io
import unittest

from rich.console import Console

import algo.algo2 as algo2
import algo.algo3f as algo3f
from algo.algo_base import TouchAction
from chart import Chart
from note import NoteType

TAIL = 1e9

# 判定线时间用"拍", bpm=120 时 1.875/bpm = 15.625ms/拍。下面这些取值都是16的倍数,
# 也就是250ms的整数倍, 好算:
W1 = (192.0, 224.0, 256.0, 288.0)      # 3000 / 3500 / 4000 / 4500 ms
W2 = (512.0, 544.0, 576.0, 608.0)      # 8000 / 8500 / 9000 / 9500 ms
HOLD = 128.0                           # 2000ms
FLICKS = (470.0, 480.0, 490.0, 500.0)  # 7343~7812ms, 落在两波之间的空拍里
XS = (-4.5, -1.5, 1.5, 4.5)            # positionX, 让4个长条落在不同位置
WARM = (192.0, 208.0, 224.0, 240.0, 256.0)   # 3000..4000ms, 5个长条(占满名额)
CHORD = 768.0                          # 12000ms, 6个长条同时按下


def quiet():
    return Console(file=io.StringIO(), width=200)


def line_dict(notes, bpm=120.0):
    return {
        'bpm': bpm,
        'notesAbove': notes,
        'notesBelow': [],
        'speedEvents': [{'startTime': 0.0, 'endTime': TAIL, 'value': 1.0}],
        'judgeLineDisappearEvents': [{'startTime': -999999.0, 'endTime': TAIL, 'start': 1.0, 'end': 1.0}],
        'judgeLineMoveEvents': [{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.5, 'end': 0.5,
                                 'start2': 0.5, 'end2': 0.5}],
        'judgeLineRotateEvents': [{'startTime': -999999.0, 'endTime': TAIL, 'start': 0.0, 'end': 0.0}],
    }


def note(t, x, n_type=1, hold=0.0):
    return {'type': n_type, 'time': t, 'positionX': x, 'holdTime': hold, 'speed': 1.0, 'floorPosition': 0.0}


def chart_of(*lines):
    return Chart.from_dict({'formatVersion': 3, 'offset': 0.0, 'judgeLineList': list(lines)})


def two_wave_chart():
    """4条判定线, 每条: 第一波1个长条 + 一个flick + 第二波1个长条。

    两波之间隔着 6505ms -> 8000ms 的空拍(1500ms), 中间只有flick —— 正是
    Chart_AT.json 那个"第二波必断一个"的结构。
    """
    lines = []
    for k, x in enumerate(XS):
        notes = [
            note(W1[k], x, n_type=3, hold=HOLD),
            note(FLICKS[k], 0.0, n_type=4),
            note(W2[k], x, n_type=3, hold=HOLD),
        ]
        if k == 0:
            # 第二波第一个hold所在的这一帧(8000ms)再多两个tap: 第二波一共要
            # 5个触点, 正好把 algo2 的名额用满。没有 released_at 修复时,
            # 这一帧的所有指针都在"冷却中", 只能另开新pid去偷名额。
            notes += [note(512.0, -3.0, n_type=1), note(512.0, 3.0, n_type=1)]
        lines.append(line_dict(notes))
    return chart_of(*lines)


def chord_chart():
    """5个长条先占满名额并释放, 空拍之后6个长条同时按下(外加3个flick占着滑键名额)。

    algo3f 的预算是 2扫屏 + 3滑键 + 5个tap/hold = 10。6个同时长条必然超预算:
    正确做法是显式丢一个并报警; 错误做法是另开新pid, 让总并发突破后端上限。
    """
    notes = [note(t, -4.5 + 2.0 * i, n_type=3, hold=HOLD) for i, t in enumerate(WARM)]
    notes += [note(700.0 + 10.0 * k, 0.0, n_type=4) for k in range(3)]
    notes += [note(CHORD, -4.5 + 1.5 * i, n_type=3, hold=HOLD) for i in range(6)]
    return chart_of(line_dict(notes))


def long_hold_spans(ans):
    """(pid, down_ms, up_ms), 只统计tap/hold池(pid<2000)且时长>1000ms的"""
    out = []
    for pid in sorted({e.pointer for evs in ans.values() for e in evs}):
        if pid >= 2000:
            continue
        cur = None
        for ms in sorted(ans):
            for e in ans[ms]:
                if e.pointer != pid:
                    continue
                if e.action == TouchAction.DOWN and cur is None:
                    cur = ms
                elif e.action == TouchAction.UP and cur is not None:
                    if ms - cur > 1000:
                        out.append((pid, cur, ms))
                    cur = None
    return out


def downs(ans, lo, hi):
    return [e.pointer for ms in sorted(ans) if lo <= ms <= hi
            for e in ans[ms] if e.action == TouchAction.DOWN]


def max_active(ans):
    """全曲同时按下的最大触点数"""
    delta = {}
    for ms, evs in ans.items():
        delta[ms] = delta.get(ms, 0) + sum(
            1 if e.action == TouchAction.DOWN else (-1 if e.action == TouchAction.UP else 0) for e in evs)
    cur = peak = 0
    for ms in sorted(delta):
        cur += delta[ms]
        peak = max(peak, cur)
    return peak


def replay_errors(ans):
    """按 scrcpy-server 的 PointersState 语义回放: 对已按下的指针再DOWN会被静默忽略。"""
    down, errors = set(), []
    for ms in sorted(ans):
        for e in ans[ms]:
            if e.action == TouchAction.DOWN:
                if e.pointer in down:
                    errors.append(f'{ms}: pid {e.pointer} 重复DOWN')
                down.add(e.pointer)
            elif e.action == TouchAction.MOVE:
                if e.pointer not in down:
                    errors.append(f'{ms}: pid {e.pointer} 未按下就MOVE')
            elif e.action == TouchAction.UP:
                if e.pointer not in down:
                    errors.append(f'{ms}: pid {e.pointer} 未按下就UP')
                down.discard(e.pointer)
    return errors


class PointerAllocatorRegressionTest(unittest.TestCase):
    """直接驱动 PointerAllocator, 复现"两波长条"的触点分配"""

    def _press_wave(self, alloc, start, count, step=10, hold_ms=200, x0=100.0):
        for i in range(count):
            pos = (x0 + 100.0 * i, 300.0)
            f = algo2.Frame(start + i * step)
            f.add(NoteType.HOLD, pos, 0.0, tuple(pos for _ in range(hold_ms)))
            alloc.allocate(f)

    def test_sparse_gap_does_not_waste_pointers(self):
        # 第一波5个hold占满名额, 空拍960ms之后第二波5个hold:
        # 第二波必须复用第一波刚释放的触点, 而不是另开新pid。
        alloc = algo2.PointerAllocator(5, 1000)
        self._press_wave(alloc, 0, 5)            # 0..40ms按下, 各自持续200ms
        self._press_wave(alloc, 1000, 5)         # 1000..1040ms按下
        ans = alloc.done()
        first = downs(ans, 0, 40)
        second = downs(ans, 1000, 1040)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertEqual(sorted(first), sorted(second),
                         '空拍之后的第二波没有复用第一波释放的触点')
        self.assertEqual(alloc.dropped, [], '空拍之后一个音符都不该被丢')

    def test_released_at_is_the_real_up_time(self):
        # released_at 必须是真实抬起时刻(=release_deadline), 不是"发现它的那一帧"。
        # 谱面稀疏时两者能差出上千毫秒, 冷却期被凭空拉长。
        alloc = algo2.PointerAllocator(5, 1000)
        self._press_wave(alloc, 0, 5)
        self._press_wave(alloc, 1000, 5)
        alloc.done()
        # 5个hold分别在0/10/20/30/40按下, 路径长200 -> release_deadline=205..245
        expected = {1000 + i: 205 + 10 * i for i in range(5)}
        for pid, ra in alloc.released_at.items():
            if pid in expected:
                self.assertEqual(ra, expected[pid],
                                 f'pid {pid} 的 released_at 记的不是真实抬起时刻')

    def test_alloc_never_grows_past_budget(self):
        # 所有触点都在40ms冷却期内时: 不许另开新pid偷扫屏/滑键触点的名额,
        # 必须显式报告"这个音符执行不了"。
        alloc = algo2.PointerAllocator(5, 1000)
        self._press_wave(alloc, 0, 5, step=1, hold_ms=100)     # 0..4按下, rd=105..109
        self._press_wave(alloc, 115, 5, step=1, hold_ms=100)   # 115ms, 全在冷却中
        alloc.done()
        self.assertLessEqual(len(alloc.pointers), 5,
                             '触点分配突破了 solve_with 分配的名额')
        self.assertTrue(alloc.dropped,
                        '名额不足时必须显式报告, 不能静默突破后端同时触点上限')


class EndToEndBudgetTest(unittest.TestCase):
    """端到端: 走 algo3f 全流程, 检查触点数不超后端上限"""

    def _check(self, chart, mp):
        con = quiet()
        ans = algo3f.solve(chart, con, mp)
        pids = {e.pointer for evs in ans.values() for e in evs}
        # 扫屏触点 + 滑键触点 + algo2名额 恰好等于 mp, 一个都不许多
        self.assertLessEqual(len(pids), mp,
                             f'计划用到{len(pids)}个不同触点, 超过后端上限{mp}')
        # 同时按下的触点也不许超过后端上限(超了会被服务端静默丢弃)
        self.assertLessEqual(max_active(ans), mp, '同时按下的触点超过了后端上限')
        self.assertEqual(replay_errors(ans), [])
        return ans, con.file.getvalue()

    def test_two_waves_stay_within_budget(self):
        for mp in (16, 10):
            with self.subTest(max_pointers=mp):
                ans, _ = self._check(two_wave_chart(), mp)
                spans = sorted(long_hold_spans(ans), key=lambda s: s[1])
                self.assertEqual(len(spans), 8, '两波长条没有全部规划出来')
                first = {s[0] for s in spans[:4]}
                second = {s[0] for s in spans[4:]}
                self.assertEqual(first, second,
                                 '第二波长条没有复用第一波的触点, 而是另开了新pid')

    def test_over_budget_drops_visibly_instead_of_stealing_pointers(self):
        # 6个同时长条: mp=16时名额(10个)充足, 全中; mp=10时只剩5个名额,
        # 必须丢1个并报警, 不许另开新pid静默偷扫屏/滑键触点的名额。
        for mp, expect in ((16, 6), (10, 5)):
            with self.subTest(max_pointers=mp):
                ans, out = self._check(chord_chart(), mp)
                spans = sorted(long_hold_spans(ans), key=lambda s: s[1])
                chord = [s for s in spans if s[1] >= CHORD * 15.625 - 50]
                self.assertEqual(len(chord), expect, '超过名额的长条没有被显式丢掉')
                if expect < 6:
                    self.assertIn('警告', out, '名额不足时没有报警')


if __name__ == '__main__':
    unittest.main()
