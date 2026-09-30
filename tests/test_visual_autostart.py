'''视觉自动开始(帧体积突变检测)的离线单元测试 — 不依赖PyQt/adb'''
from __future__ import annotations

import threading
import unittest

from control import DeviceController


class _FakeCtrl(DeviceController):
    '''只取用 activity 检测相关逻辑, 不连接设备'''


def _feed(ctrl: DeviceController, sizes: list[int]) -> None:
    for s in sizes:
        ctrl._activity_feed(s)


class VisualAutostartTest(unittest.TestCase):
    def setUp(self):
        self.c = _FakeCtrl.__new__(_FakeCtrl)

    def tearDown(self):
        self.c.stop_activity_watch()

    def test_static_then_jump_fires(self):
        '''静态选曲界面(帧很小) -> 进入演奏(帧体积跳增) 必须触发'''
        ev = self.c.start_activity_watch(cooldown=0.0)
        _feed(self.c, [4000] * 30)          # 预热移动平均窗口
        self.assertFalse(ev.is_set(), '静态界面不应误触发')
        _feed(self.c, [40000])              # 突然跳增(音符下落+背景流动)
        self.assertTrue(ev.is_set(), '界面跳变应触发')

    def test_no_false_trigger_on_gradual_change(self):
        '''帧体积缓慢变化(选曲界面动画)不应触发'''
        ev = self.c.start_activity_watch(cooldown=0.0)
        _feed(self.c, [3000 + i * 30 for i in range(200)])   # 3KB -> 9KB
        self.assertFalse(ev.is_set(), '缓慢变化不应触发')

    def test_watch_off_is_noop(self):
        '''不开监视时 _activity_feed 必须什么都不做(不能抛异常)'''
        self.c._activity_feed(999999)
        self.assertFalse(getattr(self.c, '_activity_event', None) is not None)

    def test_small_frames_ignored(self):
        '''过小的帧(空帧/掉线)不参与判定'''
        ev = self.c.start_activity_watch(cooldown=0.0)
        _feed(self.c, [500] * 40)
        _feed(self.c, [10])
        self.assertFalse(ev.is_set())

    def test_cooldown_blocks_retrigger(self):
        '''冷却时间内不应重复触发'''
        ev = self.c.start_activity_watch(cooldown=60.0)
        _feed(self.c, [4000] * 30)
        _feed(self.c, [40000])
        self.assertTrue(ev.is_set())
        ev.clear()
        _feed(self.c, [4000] * 30)
        _feed(self.c, [40000])
        self.assertFalse(ev.is_set(), '冷却期内不应重复触发')

    def test_wait_returns_on_event(self):
        '''waiter 线程能通过 Event.wait() 及时拿到触发'''
        ev = self.c.start_activity_watch(cooldown=0.0)
        got: list[bool] = []

        def waiter():
            got.append(ev.wait(timeout=2.0))

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        _feed(self.c, [4000] * 30)
        _feed(self.c, [40000])
        t.join(timeout=3)
        self.assertEqual(got, [True])


if __name__ == '__main__':
    unittest.main()
