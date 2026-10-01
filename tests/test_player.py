import gc
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algo.algo_base import TouchAction, VirtualTouchEvent  # noqa: E402
from player import LATE_WARN_MS, run_player  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.00001   # 忙等时时间也在走
        return self.t

    def sleep(self, s):
        self.t += s


def ev(action, pid=1):
    return VirtualTouchEvent((100.0, 100.0), action, pid)


class TestPlayer(unittest.TestCase):
    def test_batches_sent_on_time_and_in_order(self):
        clock = FakeClock()
        plan = [(0, [ev(TouchAction.DOWN)]), (50, [ev(TouchAction.MOVE), ev(TouchAction.DOWN, 2)]),
                (120, [ev(TouchAction.UP), ev(TouchAction.UP, 2)])]
        sent = []

        def send(events):
            sent.append((round(clock.t * 1000), [(e.action, e.pointer) for e in events]))
            clock.t += 0.0002

        stats = run_player(send, iter(plan), lambda: 0.0, lambda: True, clock=clock, sleep=clock.sleep)
        self.assertEqual([t for t, _ in sent], [0, 50, 120])
        self.assertEqual(sent[1][1], [(TouchAction.MOVE, 1), (TouchAction.DOWN, 2)])   # 同一时刻一次发送
        self.assertEqual((stats.batches, stats.events), (3, 5))
        self.assertEqual(stats.late, [])
        self.assertEqual(gc.get_freeze_count(), 0)   # 结束后解冻

    def test_late_batches_reported(self):
        clock = FakeClock()
        plan = [(0, [ev(TouchAction.DOWN)]), (10, [ev(TouchAction.MOVE)]), (20, [ev(TouchAction.UP)])]

        def send(events):
            if events[0].action == TouchAction.DOWN:
                clock.t += 0.045   # 模拟一次45ms的卡顿
        stats = run_player(send, iter(plan), lambda: 0.0, lambda: True, clock=clock, sleep=clock.sleep)
        # late 现在是4元组: (计划时刻ms, 延迟ms, 是否含按下/抬起, 发送耗时ms)
        self.assertEqual([(t, k) for t, _, k, _ in stats.late], [(10, False), (20, True)])
        self.assertAlmostEqual(stats.max_late, 35, delta=1)
        # 卡顿发生在 DOWN(t=0)那一批的 send 里。它的"发送耗时"归自己,
        # 但它自己不迟到(late=0)所以不进 late 列表; 它造成的35ms迟到记在下一批(t=10)上。
        # 这正是拆开记的意义: 看到"t=10迟35ms但它自己只发了1ms", 就知道停顿不在这一批。
        by_t = {t: (d, snd) for t, d, _, snd in stats.late}
        self.assertAlmostEqual(by_t[10][0], 35, delta=1)
        self.assertLess(by_t[10][1], 2.0, 't=10自己发得很快, 迟到是上一批拖出来的')
        self.assertAlmostEqual(stats.max_send_ms, 45, delta=1,
                               msg='DOWN那批45ms的发送耗时没被单独记下来')
        text = '\n'.join(stats.summary(0))
        self.assertIn(f'超过{LATE_WARN_MS}ms', text)
        self.assertIn('0.02秒', text)
        self.assertIn('单批最长发送耗时45ms', text)
        self.assertIn('传输链路', text)

    def test_stop(self):
        clock = FakeClock()
        plan = [(0, [ev(TouchAction.DOWN)]), (1000, [ev(TouchAction.UP)])]
        sent = []
        stats = run_player(sent.append, iter(plan), lambda: 0.0, lambda: clock.t < 0.5, clock=clock, sleep=clock.sleep)
        self.assertEqual(len(sent), 1)
        self.assertEqual(stats.batches, 1)


if __name__ == '__main__':
    unittest.main()
