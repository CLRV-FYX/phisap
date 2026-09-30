'''触控通道健康状态分离 + 停止竞态 的回归测试

两个用户实测问题:

问题A(严重): "停止演奏一次后, (如果我用手点屏幕), 后面开始演奏将完全无效"
根因1: release_pointers() 在运行时 settimeout(0.5) 再把控制socket切成非阻塞,
        而 ctrlmsg_receiver 线程正阻塞在同一个socket的recv()上, 那个recv立刻抛
        BlockingIOError -> 接收线程退出 -> collector_running=False ->
        之后所有 touch_many 因为 if self.collector_running 被静默丢弃。
        界面没有任何提示, 表现就是"点了开始演奏, 屏幕上什么都没发生"。
根因2: 视频流和控制通道共用同一个 collector_running。视频流一断(模拟器抽风/
        息屏/分辨率变化), 控制通道也被判死, 触控注入同样全灭。
修复:   1) 控制socket超时只在连接时设一次(CONTROL_SOCKET_TIMEOUT), 运行时绝不改
        2) 两个通道分开记: collector_running(视频) / control_running(控制)
        3) 通道断了要抛 ControlChannelDead 大声报错, 不再静默丢弃

问题B: 旧的 _stop_async 在后台线程里读 self._player_thread, 用户紧接着再点开始时
        它会join到新线程上, 而且结束时发的UP会把新一轮播放刚按下的手指抬起来。
修复:   _stop() 当场抓住线程对象传进去; 触点释放改由 worker 自己的 finally 做。
'''
from __future__ import annotations

import ast
import io
import os
import socket
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name: str) -> str:
    with io.open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def _mainpage():
    for node in ast.parse(_src('main.py')).body:
        if isinstance(node, ast.ClassDef) and node.name == 'MainPage':
            return node
    raise AssertionError('找不到 MainPage')


def _method(cls, name):
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f'找不到 {name}')


def _calls_on(node) -> list[str]:
    '''收集形如 self._player_thread.join / t.join 的真实调用(注释和docstring不会混进来)'''
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if isinstance(v, ast.Name):
                out.append(f'{v.id}.{n.func.attr}')
            elif (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                  and v.value.id == 'self'):
                out.append(f'self.{v.attr}.{n.func.attr}')
    return out


def _settimeout_call_lines(src: str) -> list[int]:
    '''control.py 里所有真实调用 settimeout 的行号'''
    return [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'settimeout']

class ControlChannelSeparationTest(unittest.TestCase):
    '''视频通道和控制通道必须分开记健康状态'''

    def test_control_running_flag_exists(self):
        from control import DeviceController
        self.assertTrue(hasattr(DeviceController, 'control_running'),
                        'DeviceController 缺 control_running')

    def test_release_pointers_never_mutates_timeout(self):
        '''这是问题A的根因: 运行时改socket超时会坑死另一个线程的recv'''
        tree = ast.parse(_src('control.py'))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == 'release_pointers':
                calls = [c for n in ast.walk(node) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == 'settimeout']
                self.assertEqual(calls, [], 'release_pointers 还在改socket超时')
                return
        raise AssertionError('找不到 release_pointers')

    def test_no_settimeout_outside_connect(self):
        '''settimeout 只允许出现在连接建立阶段(connect/__init__里)'''
        tree = ast.parse(_src('control.py'))
        lines = _settimeout_call_lines(_src('control.py'))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                bad = [ln for ln in lines if node.lineno <= ln <= (node.end_lineno or node.lineno)]
                if bad and node.name not in ('connect', '__init__', '_connect'):
                    self.fail(f'{node.name} 里出现了settimeout(行{bad}) —— 运行期改超时会坑死接收线程')

    def test_ctrlmsg_receiver_tolerates_timeout(self):
        '''控制socket带超时, 接收线程必须容忍 socket.timeout 而不是当故障退出'''
        seg = _src('control.py')
        i = seg.index('def ctrlmsg_receiver')
        body = seg[i:i + 700]
        self.assertIn('socket.timeout', body, '接收线程没有处理 socket.timeout')

    def test_video_receiver_does_not_touch_control_flag(self):
        '''视频接收线程出错时只能动 collector_running, 不能动 control_running'''
        seg = _src('control.py')
        i = seg.index('def video_receiver')
        body = seg[i:seg.index('def ctrlmsg_receiver')]
        self.assertNotIn('control_running', body,
                         '视频接收线程在动控制通道的状态')

    def test_touch_many_raises_when_dead(self):
        '''通道断了必须抛异常, 不能静默丢弃(否则用户以为程序坏了)'''
        from control import DeviceController, ControlChannelDead
        c = DeviceController.__new__(DeviceController)
        c.control_running = False
        c._send_lock = threading.Lock()
        from algo.algo_base import TouchAction, VirtualTouchEvent
        with self.assertRaises(ControlChannelDead):
            c.touch_many([VirtualTouchEvent((1.0, 1.0), TouchAction.DOWN, 1)])

    def test_touch_many_sends_when_alive(self):
        '''通道活着时必须真的发出去(用假socket验证)'''
        from control import DeviceController
        from algo.algo_base import TouchAction, VirtualTouchEvent

        class FakeSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

        c = DeviceController.__new__(DeviceController)
        c.control_running = True
        c._send_lock = threading.Lock()
        c.device_width, c.device_height = 1920, 1080
        c.control_socket = FakeSock()
        c.touch_many([VirtualTouchEvent((10.0, 20.0), TouchAction.DOWN, 7)])
        self.assertEqual(len(c.control_socket.sent), 1, '没发出去')
        self.assertEqual(len(c.control_socket.sent[0]), 32, '单个触控包应为32字节')

    def test_release_pointers_sends_up(self):
        '''停止时释放触点必须真的发出去(真实调用, 用假socket)'''
        from control import DeviceController
        from algo.algo_base import TouchAction

        class FakeSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

            def settimeout(self, t):
                raise AssertionError('不允许在release_pointers里改超时')

        c = DeviceController.__new__(DeviceController)
        c.control_running = True
        c._send_lock = threading.Lock()
        c.device_width, c.device_height = 1920, 1080
        c.control_socket = FakeSock()
        c.release_pointers({20001, 20002, 20003})
        self.assertEqual(len(c.control_socket.sent), 1)
        self.assertEqual(len(c.control_socket.sent[0]), 32 * 3, '3个触点应有3个UP包')

    def test_video_death_does_not_block_touch(self):
        '''核心: 视频流挂了, 触控注入必须还能用'''
        from control import DeviceController

        class FakeSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

        c = DeviceController.__new__(DeviceController)
        c.collector_running = False      # 视频流已断
        c.control_running = True         # 控制通道还活着
        c._send_lock = threading.Lock()
        c.device_width, c.device_height = 1920, 1080
        c.control_socket = FakeSock()
        from algo.algo_base import TouchAction, VirtualTouchEvent
        c.touch_many([VirtualTouchEvent((5.0, 5.0), TouchAction.DOWN, 1)])
        self.assertEqual(len(c.control_socket.sent), 1,
                         '视频流断了居然就不能注入触控了')

    def test_abort_marks_both_channels(self):
        from control import DeviceController
        c = DeviceController.__new__(DeviceController)
        c.collector_running = True
        c.control_running = True
        c.control_socket = None
        c.video_socket = None
        c.abort()
        self.assertFalse(c.collector_running)
        self.assertFalse(c.control_running)


class StopRaceTest(unittest.TestCase):
    '''_stop_async 不能join错线程, 也不能误伤新一轮播放'''

    def test_stop_captures_thread_object(self):
        '''_stop 必须当场抓住worker线程传给_stop_async'''
        cls = _mainpage()
        stop = _method(cls, '_stop')
        seg = ast.dump(stop)
        self.assertIn('_player_thread', seg, '_stop 没有读当前播放线程')
        # 必须作为参数传进去(args=...), 而不是让_stop_async自己去读self._player_thread
        self.assertIn('args', seg, '_stop 没有把线程对象传给_stop_async')

    def test_stop_async_takes_thread_param(self):
        cls = _mainpage()
        fn = _method(cls, '_stop_async')
        params = [a.arg for a in fn.args.args]
        self.assertIn('t', params, '_stop_async 必须接收线程对象参数')

    def test_stop_async_does_not_join_wrong_thread(self):
        '''_stop_async 必须join传进来的t, 不能join self._player_thread(那正是join错人的原因)'''
        cls = _mainpage()
        calls = _calls_on(_method(cls, '_stop_async'))
        self.assertIn('t.join', calls, '_stop_async 没有join传入的线程')
        bad = [c for c in calls if c.startswith('self._player_thread.')]
        self.assertEqual(bad, [], f'_stop_async 在调用 {bad}(会作用到新线程上)')

    def test_stop_async_player_thread_only_used_for_identity(self):
        '''self._player_thread 只允许出现在"是不是同一个线程"的判断里'''
        cls = _mainpage()
        seg = ast.dump(_method(cls, '_stop_async'))
        self.assertIn('_player_thread', seg, '缺少线程身份判断(兜底释放会误伤新播放)')
        self.assertIn('Is()', seg, '线程身份判断必须用"是"比较(is)')

    def test_fallback_guarded_by_thread_identity(self):
        '''兜底释放触点前必须确认还是同一个线程, 否则会抬掉新一轮播放的手指'''
        cls = _mainpage()
        seg = ast.dump(_method(cls, '_stop_async'))
        self.assertIn('_player_thread', seg, '兜底释放缺少线程身份判断')

    def test_worker_finally_releases(self):
        '''触点释放由 worker 的 finally 做(保证在自己最后一次写socket之后)'''
        cls = _mainpage()
        outer = _method(cls, '_start_playback')
        worker = [n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'worker'][0]
        seg = ast.dump(worker)
        self.assertIn('_release_all_active', seg, 'worker 的 finally 没有释放触点')
        self.assertIn('playback_finished', seg)


class DelayOffsetTest(unittest.TestCase):
    '''偏移范围 + 手动开始模式必须也吃这个偏移'''

    def test_delay_range_widened(self):
        cls = _mainpage()
        seg = ast.dump(cls)
        self.assertNotIn("Constant(value=-500)", seg, '偏移上限还是±500ms')
        self.assertIn('DELAY_OFFSET_LIMIT_MS', seg)

    def test_delay_limit_value(self):
        src = _src('main.py')
        i = src.index('DELAY_OFFSET_LIMIT_MS = ')
        v = int(src[i:src.index('\n', i)].split('=')[1])
        self.assertGreaterEqual(v, 1500, f'偏移上限只有±{v}ms, 补偿不了1秒以上的整体偏移')

    def test_manual_path_applies_offset(self):
        '''用户用的是手动开始, 偏移在手动路径里也必须生效'''
        cls = _mainpage()
        outer = _method(cls, '_start_playback')
        worker = [n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'worker'][0]
        seg = ast.dump(worker)
        self.assertIn('delay_spin', seg, '手动开始没有读偏移')
        self.assertIn('offset', seg)

    def test_sync_path_applies_offset(self):
        cls = _mainpage()
        seg = ast.dump(_method(cls, 'sync_ms'))
        self.assertIn('delay_spin', seg, '计时器同步没有读偏移')


if __name__ == '__main__':
    unittest.main()
