"""真实调用 PointerTracker 的行为测试。

这一层刻意做成"直接调用真实类"而不是AST静态检查: "停止时该抬哪个触点"
这种逻辑以前只靠AST断言, 结果把实时跟踪删掉之后测试照样全绿 —— 静态检查
看不出行为变了。这里让算错就一定红。
"""
from __future__ import annotations

import ast
import io
import os
import unittest

from algo.algo_base import TouchAction, VirtualTouchEvent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ev(action, pid):
    return VirtualTouchEvent((1.0, 1.0), action, pid)


class PointerTrackerBehaviorTest(unittest.TestCase):

    def _tracker(self):
        # 从main.py取出真实的PointerTracker来执行(沙箱没有PyQt5, 不能import main)
        with io.open(os.path.join(ROOT, 'main.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        node = next((n for n in tree.body
                     if isinstance(n, ast.ClassDef) and n.name == 'PointerTracker'), None)
        self.assertIsNotNone(node, 'main.py 里找不到 PointerTracker 类')
        ns = {'TouchAction': TouchAction}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<pt>', 'exec'), ns)
        return ns['PointerTracker']()

    def test_sweep_stays_down_mid_song(self):
        '''扫屏触点从第一个音符前按下, 到最后一个音符后才抬起 —— 中途必须算按着'''
        t = self._tracker()
        SWEEP_A, SWEEP_B = 20000, 20001
        # 按下按钮那一刻: 两个扫屏触点按下
        t.update([_ev(TouchAction.DOWN, SWEEP_A), _ev(TouchAction.DOWN, SWEEP_B)])
        # 歌曲进行中: 只有MOVE
        for _ in range(100):
            t.update([_ev(TouchAction.MOVE, SWEEP_A)])
        self.assertEqual(t.live, {SWEEP_A, SWEEP_B}, '扫屏触点必须算作按着')
        self.assertIn(SWEEP_A, t.release_set())
        self.assertIn(SWEEP_B, t.release_set())

    def test_tap_released_after_up(self):
        '''tap按下又抬起之后不该再算按着, 但仍要能清服务端残留状态'''
        t = self._tracker()
        t.update([_ev(TouchAction.DOWN, 20005)])
        t.update([_ev(TouchAction.UP, 20005)])
        self.assertEqual(t.live, set())
        self.assertEqual(t.release_set(), {20005})

    def test_full_plan_ends_empty_but_release_set_is_not(self):
        '''原bug: 跑完整份规划后 live 是空的, 但 release_set 不能空'''
        t = self._tracker()
        t.update([_ev(TouchAction.DOWN, 20000)])
        t.update([_ev(TouchAction.MOVE, 20000)])
        t.update([_ev(TouchAction.UP, 20000)])
        self.assertEqual(t.live, set(), 'live 应该空了')
        self.assertTrue(t.release_set(), '但release_set不能空, 否则停止时什么都不发')

    def test_mixed_pointers(self):
        '''扫屏+滑键+tap混在一起时, live 只包含当下真按着的'''
        t = self._tracker()
        t.update([_ev(TouchAction.DOWN, 20000), _ev(TouchAction.DOWN, 20001)])
        t.update([_ev(TouchAction.DOWN, 20005)])
        t.update([_ev(TouchAction.UP, 20005)])
        t.update([_ev(TouchAction.DOWN, 20010)])
        self.assertEqual(t.live, {20000, 20001, 20010})

    def test_all_pids_records_everything_used(self):
        '''all_pids 要记下这一轮用过的所有触点(清理服务端残留用)'''
        t = self._tracker()
        t.update([_ev(TouchAction.DOWN, 20000)])
        t.update([_ev(TouchAction.UP, 20000)])
        t.update([_ev(TouchAction.DOWN, 20003)])
        self.assertEqual(t.all_pids, {20000, 20003})

    def test_empty_update_is_safe(self):
        t = self._tracker()
        t.update([])
        self.assertEqual(t.live, set())
        self.assertEqual(t.release_set(), set())


class WorkerUsesTrackerTest(unittest.TestCase):
    '''worker 必须把 PointerTracker 接上(静态检查这层)'''

    def _worker(self):
        with io.open(os.path.join(ROOT, 'main.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        mp = next(n for n in tree.body
                  if isinstance(n, ast.ClassDef) and n.name == 'MainPage')
        outer = next(n for n in mp.body
                     if isinstance(n, ast.FunctionDef) and n.name == '_start_playback')
        ws = [n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'worker']
        self.assertEqual(len(ws), 1, '找不到嵌套的 worker')
        return ws[0]

    def test_worker_instantiates_tracker(self):
        seg = ast.dump(self._worker())
        self.assertIn('PointerTracker', seg, 'worker 没有用 PointerTracker')

    def _attrs(self, node):
        '''收集 worker 里所有 x.yyy 的属性名(ast.dump里它们是分开的节点)'''
        return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

    def test_send_updates_tracker(self):
        attrs = self._attrs(self._worker())
        self.assertIn('update', attrs, 'send 没有更新触点状态')

    def test_active_pids_points_at_tracker_live(self):
        '''self._active_pids 必须就是 tracker.live 这个对象本身'''
        attrs = self._attrs(self._worker())
        self.assertIn('live', attrs, '_active_pids 没有指向 tracker.live')
        self.assertIn('all_pids', attrs, '_all_pids 没有指向 tracker.all_pids')


if __name__ == '__main__':
    unittest.main()
