'''run_player 的 should_continue / 代际检查 真实运行测试

事故回放: main.py 给 run_player 传了 should_continue=..., 但 player.py 里
根本没这个参数 —— 编辑静默失败了, 于是运行时报
    TypeError: run_player() got an unexpected keyword argument 'should_continue'
    (演奏一启动就崩, 日志里能看到 File "main.py", line 1118, in worker)
静态的AST检查发现不了这种"调用方和实现不同步", 所以这里直接用真实调用验证。
'''
from __future__ import annotations

import inspect
import unittest

from algo.algo_base import TouchAction, VirtualTouchEvent
from player import run_player


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.00001
        return self.t

    def sleep(self, s):
        self.t += s


def _plan(n=6):
    '''n 个时间点的假规划, 每批一个DOWN'''
    return [(i * 10, [VirtualTouchEvent(TouchAction.DOWN, (float(i), 1.0), 20000 + i)])
            for i in range(n)]


class ShouldContinueTest(unittest.TestCase):
    def test_signature_accepts_kwarg(self):
        '''main.py 必须能传 should_continue, 否则一启动就 TypeError'''
        sig = inspect.signature(run_player)
        self.assertIn('should_continue', sig.parameters,
                      'run_player 缺少 should_continue 参数(main.py 会传它)')

    def test_actually_runs_with_kwarg(self):
        '''真实调用: 带上 should_continue 必须能正常跑完(复现之前的TypeError)'''
        sent = []
        stats = run_player(sent.append, iter(_plan(4)),
                           lambda: 0.0, lambda: True,
                           clock=FakeClock(), sleep=FakeClock().sleep,
                           should_continue=lambda: True)
        self.assertGreater(stats.batches, 0, '一批都没发出去')
        self.assertEqual(len(sent), stats.batches)

    def test_should_continue_false_stops_immediately(self):
        '''should_continue 返回False时必须立刻停止, 不再发送任何事件'''
        sent = []
        clock = FakeClock()
        # 第3批之后禁止继续
        state = {'n': 0}

        def cont():
            state['n'] = len(sent)
            return len(sent) < 3

        run_player(sent.append, iter(_plan(50)),
                   lambda: 0.0, lambda: True,
                   clock=clock, sleep=clock.sleep,
                   should_continue=cont)
        self.assertEqual(len(sent), 3, f'应该发3批就停, 实际发了{len(sent)}批')

    def test_running_false_still_works(self):
        '''不传 should_continue(默认None)时行为不变'''
        sent = []
        clock = FakeClock()
        run_player(sent.append, iter(_plan(3)),
                   lambda: 0.0, lambda: True,
                   clock=clock, sleep=clock.sleep)
        self.assertEqual(len(sent), 3)

    def test_first_event_with_should_continue(self):
        '''main.py 的 manual/prestarted 路径用 first_event + should_continue'''
        plan = _plan(5)
        sent = []
        clock = FakeClock()
        stats = run_player(sent.append, iter(plan[1:]),
                           lambda: 0.0, lambda: True,
                           clock=clock, sleep=clock.sleep,
                           first_event=plan[0],
                           should_continue=lambda: True)
        self.assertEqual(len(sent), 5)
        self.assertEqual(stats.batches, 5)

    def test_stale_generation_stops(self):
        '''模拟"用户停止后代纪变化": 旧worker必须立刻闭嘴'''
        sent = []
        clock = FakeClock()
        gen = {'v': 1}
        my_gen = gen['v']
        plan = iter(_plan(100))

        def send(events):
            sent.append(events)
            if len(sent) == 2:
                gen['v'] = 2      # 用户点了停止 -> 代际+1

        run_player(send, plan, lambda: 0.0, lambda: True,
                   clock=clock, sleep=clock.sleep,
                   should_continue=lambda: my_gen == gen['v'])
        self.assertEqual(len(sent), 2, f'代纪变化后应立刻停, 实际发了{len(sent)}批')


if __name__ == '__main__':
    unittest.main()
