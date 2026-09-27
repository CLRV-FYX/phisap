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
        self.assertEqual([(t, k) for t, _, k in stats.late], [(10, False), (20, True)])
        self.assertAlmostEqual(stats.max_late, 35, delta=1)
        text = '\n'.join(stats.summary(0))
        self.assertIn(f'超过{LATE_WARN_MS}ms', text)
        self.assertIn('0.02秒', text)

    def test_stop(self):
        clock = FakeClock()
        plan = [(0, [ev(TouchAction.DOWN)]), (1000, [ev(TouchAction.UP)])]
        sent = []
        stats = run_player(sent.append, iter(plan), lambda: 0.0, lambda: clock.t < 0.5, clock=clock, sleep=clock.sleep)
        self.assertEqual(len(sent), 1)
        self.assertEqual(stats.batches, 1)


if __name__ == '__main__':
    unittest.main()
