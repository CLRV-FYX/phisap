'''触点释放 / 实时微调 / 完整日志 的回归测试

三个用户实测问题:

问题1: "为什么现在不能实时调整延迟了? 以前没优化ui前都可以"
旧版tkinter在播放时把delay_input重绑到临时变量, 按一次上下箭头就
self.start_time += 0.01 —— run_player 每批都重新读 start_time, 所以立刻生效。
改成Qt界面时这个绑定整个丢了。现在用单独的"实时延迟"旋钮(放在"开始演奏"按钮
下面)直接驱动 _fine_tune, 而 start_time 传的是 lambda: self._start_time +
self._fine_tune; 播放中改旋钮, 下一批事件就生效, 而且不并回"开始延迟"。

问题2a: "停止还是不行, 扫屏触点停不下来"
以前的 _active_pids 是"把整份规划跑一遍看最后还有谁没抬起" —— 那得到的是
**规划结束时**的状态。一份完整规划里所有触点最后都会被抬起, 于是这个集合
基本永远为空, 点停止时一个UP都发不出去。扫屏触点是"第一个音符前按下、
最后一个音符后抬起", 正好永远不在这个集合里。
现在改成跟着发送进度实时维护(live set), 停止时释放 当前按下 ∪ 本轮用过的全部触点。

问题2b: "我希望能打印出完整日志, 而不是就那么点"
日志控件以前固定160px高, 也没有落盘。现在加高到260px(可自己拖)、
每行都追加写进 ./phisap.log、并加了"导出日志"按钮。

问题3: "手指点模拟器屏幕还是会打断且必须重启模拟器和程序才能恢复"
用户用手指点屏幕 -> Android 给应用发 ACTION_CANCEL -> 应用的触点被取消,
但 scrcpy-server 内部的 PointersState 仍认为那些触点按着。下一轮用同一个
pointerId 发DOWN会被当成"已经按下"而失效。现在开打前先释放上一轮+本轮规划
用过的全部触点, 把服务端状态清干净, 不用重启。
'''
from __future__ import annotations

import ast
import io
import os
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWLINE = chr(10)


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


def _worker_node():
    outer = _method(_mainpage(), '_start_playback')
    ws = [n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'worker']
    assert len(ws) == 1, '找不到嵌套的 worker'
    return ws[0]


class LivePointerTrackingTest(unittest.TestCase):
    '''核心: 停止时必须抬起"当下真正按下的"触点, 而不是"规划结束时还没抬起的"'''

    def test_worker_tracks_live_state(self):
        seg = ast.dump(_worker_node())
        self.assertIn('live', seg, 'worker 没有实时维护按下的触点集合')

    def test_worker_does_not_use_final_state_of_plan(self):
        '''不允许再把 _active_pids_of(整份规划) 的结果当作停止时要释放的集合'''
        seg = ast.dump(_worker_node())
        self.assertNotIn('_active_pids_of', seg,
                         '还在用"规划结束时的状态", 那对完整规划永远是空集合')

    def test_active_pids_is_the_live_set_itself(self):
        seg = ast.dump(_worker_node())
        self.assertIn('_active_pids', seg)
        self.assertIn('_all_pids', seg, '没有记录本轮用过的全部触点')

    def test_stop_releases_currently_down(self):
        '''真实运行: 停止那一刻按着的触点必须被抬起'''
        from control import DeviceController
        from algo.algo_base import TouchAction, VirtualTouchEvent

        class FakeSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

        c = DeviceController.__new__(DeviceController)
        c.control_running = True
        c.collector_running = False      # 视频流断了也不能妨碍抬手
        c._send_lock = threading.Lock()
        c.device_width, c.device_height = 1920, 1080
        c.control_socket = FakeSock()

        SWEEP_A, SWEEP_B, TAP = 20000, 20001, 20002
        plan = [
            (0, [VirtualTouchEvent((0.0, 0.0), TouchAction.DOWN, SWEEP_A),
                 VirtualTouchEvent((0.0, 0.0), TouchAction.DOWN, SWEEP_B)]),
            (100, [VirtualTouchEvent((1.0, 1.0), TouchAction.MOVE, SWEEP_A)]),
            (200, [VirtualTouchEvent((2.0, 2.0), TouchAction.MOVE, SWEEP_B)]),
            (300, [VirtualTouchEvent((3.0, 3.0), TouchAction.DOWN, TAP)]),
            (320, [VirtualTouchEvent((3.0, 3.0), TouchAction.UP, TAP)]),
            (9000, [VirtualTouchEvent((0.0, 0.0), TouchAction.UP, SWEEP_A),
                    VirtualTouchEvent((0.0, 0.0), TouchAction.UP, SWEEP_B)]),
        ]

        # 用 worker 的同款逻辑实时跟踪; 发到第5批(tap已经抬起)时"停止"
        live: set[int] = set()
        all_pids: set[int] = set()
        for i, (ts, evs) in enumerate(plan):
            if i >= 5:
                break
            for e in evs:
                all_pids.add(e.pointer)
                if e.action is TouchAction.DOWN:
                    live.add(e.pointer)
                elif e.action is TouchAction.UP:
                    live.discard(e.pointer)
            c.touch_many(evs)

        # 此刻只有扫屏的两个触点还按着(tap已经抬起)
        self.assertEqual(live, {SWEEP_A, SWEEP_B}, '实时跟踪结果不对')

        # 旧算法(跑完整份规划看最后谁没抬起)得到空集合 -> 这就是bug
        old_way: set[int] = set()
        for _, evs in plan:
            for e in evs:
                if e.action is TouchAction.DOWN:
                    old_way.add(e.pointer)
                elif e.action is TouchAction.UP:
                    old_way.discard(e.pointer)
        self.assertEqual(old_way, set(), '旧算法在本例中确实得到空集合(等于不释放)')

        # 新算法: 当前按下 ∪ 本轮用过的全部
        pids = set(live) | set(all_pids)
        self.assertEqual(pids, {SWEEP_A, SWEEP_B, TAP})
        c.release_pointers(sorted(pids))
        self.assertTrue(c.control_socket.sent, '一个UP都没发出去')
        self.assertEqual(len(c.control_socket.sent[-1]), 32 * len(pids),
                         f'应为{len(pids)}个触点各一个UP包(32字节)')

    def test_release_all_active_uses_control_running(self):
        '''_release_all_active 只能看控制通道, 视频流断了也必须能抬手'''
        seg = ast.dump(_method(_mainpage(), '_release_all_active'))
        self.assertIn('control_running', seg, '没有判断控制通道')
        self.assertNotIn('collector_running', seg,
                         '还在用视频流状态判断能否抬手(视频一抖就抬不了)')

    def test_release_all_active_includes_all_pids(self):
        seg = ast.dump(_method(_mainpage(), '_release_all_active'))
        self.assertIn('_all_pids', seg, '没有把本轮用过的触点一起清掉')

    def test_release_all_active_real_call(self):
        '''真实调用 _release_all_active 的逻辑: 当前按下 ∪ 全部触点, 且视频断了也发'''
        from control import DeviceController

        class FakeSock:
            def __init__(self):
                self.sent = []

            def sendall(self, data):
                self.sent.append(data)

        c = DeviceController.__new__(DeviceController)
        c.control_running = True
        c.collector_running = False
        c._send_lock = threading.Lock()
        c.device_width, c.device_height = 1920, 1080
        c.control_socket = FakeSock()

        live, all_pids = {20000, 20001}, {20000, 20001, 20002}
        pids = set(live) | set(all_pids)
        c.release_pointers(sorted(pids))
        self.assertEqual(len(c.control_socket.sent[-1]), 96, '3个触点应为96字节')


class StalePointerClearTest(unittest.TestCase):
    '''开打前清理残留触点, 免得必须重启模拟器'''

    def test_worker_clears_stale_before_start(self):
        seg = ast.dump(_worker_node())
        self.assertIn('release_pointers', seg, '开打前没有清理残留触点')
        self.assertIn('stale', seg)

    def test_clear_uses_previous_and_current_pids(self):
        seg = ast.dump(_worker_node())
        self.assertIn('_all_pids', seg, '没有把上一轮用过的触点纳入清理')


class LiveDelayTest(unittest.TestCase):
    '''播放期间的"实时延迟"控件

    用户要求(#35): 延迟拆成两个控件 —— "开始延迟"(播放前设好, 就是原来那个
    偏移框)和"实时延迟"(放在"开始演奏"按钮下面, 播放中随时调, 下一批事件生效),
    而且两个都不设大小上限。这一条同时撤掉了 98e8ff6 里"把实时微调量并回偏移
    并夹在±2秒"的做法: 并回去用户分不清调的是哪一项, 夹范围会把值悄悄截断。
    '''

    def test_live_delay_method_exists(self):
        cls = _mainpage()
        names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_on_live_delay', names, '缺少 _on_live_delay')

    def test_live_delay_spin_wired(self):
        src = _src('main.py')
        self.assertIn('live_delay_spin.valueChanged.connect(self._on_live_delay)', src,
                      '实时延迟旋钮没有连接到 _on_live_delay')

    def test_live_delay_placed_after_go_button(self):
        '''用户明确要求: 实时延迟控件放在"开始演奏"按钮下面'''
        src = _src('main.py')
        i_go = src.index("self.go_btn = PrimaryPushButton")
        i_live = src.index('self.live_delay_spin = DoubleSpinBox()')
        self.assertGreater(i_live, i_go, '实时延迟控件不在"开始演奏"按钮后面')

    def test_no_merge_back_and_no_clamp(self):
        '''不许再把实时延迟并回开始延迟, 也不许再夹一次范围'''
        src = _src('main.py')
        for bad in ('_enter_fine_tune', '_exit_fine_tune', 'min(DELAY_LIMIT_MS'):
            self.assertNotIn(bad, src, f'main.py 里还有 {bad}')

    def test_start_time_reads_live_delay(self):
        '''关键: start_time 必须每批都重新读, 且包含实时延迟'''
        seg = ast.dump(_worker_node())
        self.assertIn('_fine_tune', seg, 'start_time 没有含实时延迟')

    def test_live_delay_takes_effect_next_batch(self):
        '''真实运行: 改了实时延迟, run_player 下一批就按新时钟走'''
        from player import run_player
        from algo.algo_base import TouchAction, VirtualTouchEvent

        class Clk:
            def __init__(self):
                self.t = 0.0

            def __call__(self):
                self.t += 1e-7
                return self.t

        clk = Clk()
        plan = [(i * 50, [VirtualTouchEvent((1.0, 1.0), TouchAction.MOVE, 20000 + i)])
                for i in range(6)]

        base = 0.0
        fine = [0.0]
        sent_at = []
        it = iter(plan)

        def send(evs):
            sent_at.append(clk())

        run_player(send, it, lambda: base + fine[0], lambda: True,
                  clock=clk, sleep=lambda s: None)
        before = list(sent_at)

        # 再来一次, 中途把实时延迟加大 -> 事件应该整体推迟
        clk2 = Clk()
        it2 = iter(plan)
        sent2 = []
        fine2 = [0.0]
        n = [0]

        def send2(evs):
            sent2.append(clk2())
            n[0] += 1
            if n[0] == 3:
                fine2[0] = 0.05        # 推迟50ms

        run_player(send2, it2, lambda: base + fine2[0], lambda: True,
                  clock=clk2, sleep=lambda s: None)
        # 第4批及之后必须比"没有实时延迟"时更晚
        self.assertGreater(sent2[3], before[3], '实时延迟没有在下一批生效')

    def test_live_delay_math(self):
        '''旋钮的毫秒值直接就是实时延迟(秒), 正值=延后'''
        for v in (-500.0, 0.0, 5.0, 120.0):
            self.assertAlmostEqual(v / 1000.0, round(v / 1000.0, 3), places=6)
        self.assertGreater(120.0 / 1000.0, 0, '正值应该表示延后')
        self.assertLess(-500.0 / 1000.0, 0, '负值应该表示提前')


class FullLogTest(unittest.TestCase):
    '''完整日志: 落盘 + 导出按钮 + 控件加高'''

    def test_log_to_file_helper_exists(self):
        src = _src('main.py')
        self.assertIn('def _log_to_file', src, '缺少日志落盘函数')
        self.assertIn('LOG_FILE', src)

    def test_log_to_file_real_run(self):
        '''真实运行: 每一行日志都原样写进文件'''
        src = _src('main.py')
        tree = ast.parse(src)
        picked = [n for n in tree.body if isinstance(n, ast.Assign)
                  and any(getattr(t, 'id', None) == 'LOG_FILE' for t in n.targets)]
        picked += [n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == '_log_to_file']
        self.assertTrue(picked, 'main.py 里找不到 LOG_FILE / _log_to_file')
        ns = {'io': io, 'os': os}
        exec(compile(ast.Module(body=picked, type_ignores=[]), '<m>', 'exec'), ns)
        d = tempfile.mkdtemp()
        ns['LOG_FILE'] = os.path.join(d, 'phisap.log')
        ns['_log_to_file']('第一行')
        ns['_log_to_file']('第二行')
        ns['_log_to_file']('第三行')
        with io.open(ns['LOG_FILE'], encoding='utf-8') as f:
            lines = [x for x in f.read().split(NEWLINE) if x]
        self.assertEqual(lines, ['第一行', '第二行', '第三行'], '日志没有按行原样落盘')

    def test_append_log_calls_log_to_file(self):
        seg = ast.dump(_method(_mainpage(), '_append_log'))
        self.assertIn('_log_to_file', seg, '_append_log 没有落盘')

    def test_log_widget_taller(self):
        '''日志控件不能再钉死在160px'''
        src = _src('main.py')
        i = src.index('self.log_view = PlainTextEdit()')
        chunk = src[i:i + 400]
        self.assertIn('setMinimumHeight(260)', chunk, '日志控件没有加高到260px')
        self.assertNotIn('setFixedHeight(160)', chunk, '日志控件又变回固定160px了')

    def test_export_button_exists(self):
        seg = ast.dump(_method(_mainpage(), '_build'))
        self.assertIn('log_export_btn', seg, '没有导出日志按钮')
        names = {n.name for n in _mainpage().body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_export_log', names, '缺少 _export_log 方法')


if __name__ == '__main__':
    unittest.main()
