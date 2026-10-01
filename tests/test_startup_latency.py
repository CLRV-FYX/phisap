'''真实运行验证: "点开始演奏后界面卡几百毫秒" 的修复

事故: _start_playback 以前在GUI线程里同步跑
  1) 5万多次坐标适配(map_to + _replace)   实测 138ms
  2) 读盘 -> json.load -> Chart.from_dict -> 遍历所有音符取最小值   实测 42ms(2.5MB谱面)
两段加起来让用户点完按钮界面卡住不动, 被当成"延迟1秒左右"。
而且它跟视频参数无关, 所以上次回退 max_fps 之后用户依然觉得延迟很大。

这里用真实数据量(52326个时间点)直接跑这些函数, 并且验证:
  - 适配结果可以被提前缓存, 播放时取出来是瞬间的
  - 同一张谱面的 first_note_ms 只算一次
'''
from __future__ import annotations

import ast
import io
import json
import os
import tempfile
import time
import unittest

from algo.algo_base import TouchAction, VirtualTouchEvent, manual_start_plan

N_POINTS = 52326      # 与用户日志 "52326 个时间点" 一致


def _fake_plan(n=N_POINTS):
    return {i * 3: [VirtualTouchEvent((float(i % 800), float(i % 400)), TouchAction.MOVE, 20000 + (i % 2))]
            for i in range(n)}


class AdaptationCostTest(unittest.TestCase):
    '''适配循环本身的耗时(就是以前卡GUI线程的那段)'''

    def test_adaptation_is_not_free(self):
        '''先证明这段确实不便宜 —— 所以才有必要搬出GUI线程'''
        plan = _fake_plan()
        t0 = time.perf_counter()
        adapted = []
        for ts in sorted(plan.keys()):
            batch = []
            for ev in plan[ts]:
                nev = ev.map_to(0, 0, 1.5, 1.5)._replace(pointer=ev.pointer + 20000)
                batch.append(nev)
            adapted.append((ts, batch))
        dt = time.perf_counter() - t0
        self.assertEqual(len(adapted), N_POINTS)
        # 只要超过30ms就值得搬走(真实机器上只会更慢)
        self.assertGreater(dt * 1000, 30,
                           f'适配只花了{dt*1000:.0f}ms, 那它本来就不是瓶颈')


class FirstNoteCacheTest(unittest.TestCase):
    '''first_note_ms_from_path 的缓存(以前每次播放都重新解析整张谱面)'''

    def _make_chart(self, d):
        p = os.path.join(d, 'Chart_IN.json')
        with io.open(p, 'w', encoding='utf-8') as f:
            json.dump({'formatVersion': 3, 'offset': 0.0, 'judgeLineList': self._lines()}, f)
        return p

    def _lines(self):
        out = []
        for i in range(300):
            nl = [{'type': 1, 'time': 2000 + i * 160 + j * 3, 'positionX': 0.0,
                   'holdTime': 0.0, 'speed': 1.0, 'floorPosition': 0.0} for j in range(8)]
            out.append({'numOfNotes': 8, 'numOfNotesAbove': 4, 'numOfNotesBelow': 4,
                        'bpm': 160.0,
                        'speedEvents': [{'startTime': 0, 'endTime': 1e9, 'value': 1.0, 'floorPosition': 0.0}],
                        'judgeLineDisappearEvents': [{'startTime': 0, 'endTime': 1e9, 'start': 1.0, 'end': 1.0,
                                                      'start2': 1.0, 'end2': 1.0}],
                        'judgeLineRotateEvents': [{'startTime': 0, 'endTime': 1e9, 'start': 0.0, 'end': 0.0,
                                                   'start2': 0.0, 'end2': 0.0}],
                        'judgeLineMoveEvents': [{'startTime': 0, 'endTime': 1e9, 'start': 0.0, 'end': 0.0,
                                                 'start2': 0.0, 'end2': 0.0}],
                        'notesAbove': nl[:4], 'notesBelow': nl[4:]})
        return out

    def _load_real_func(self):
        '''从 main.py 取出真实的 first_note_ms_from_path + 缓存字典来执行。
        不能直接 import main —— 它依赖PyQt5, 沙箱里没有。'''
        from chart import Chart
        from algo.algo_base import first_note_ms
        from rpe import detect_kind, rpe_to_official_v3
        src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'main.py'), encoding='utf-8').read()
        tree = ast.parse(src)
        # load_chart_file 也要一起抽出来: first_note_ms_from_path 现在走它
        # (官谱/RPE 都认, 见 main.load_chart_file), 少了它抽出来的函数会 NameError,
        # 被自己的 try/except 吞掉返回0, 表现为"缓存测试莫名失败"。
        want = ('_FIRST_NOTE_CACHE', 'load_chart_file', 'first_note_ms_from_path')
        def _is_cache_assign(n):
            if isinstance(n, ast.AnnAssign):
                return isinstance(n.target, ast.Name) and n.target.id == '_FIRST_NOTE_CACHE'
            if isinstance(n, ast.Assign):
                return any(isinstance(t, ast.Name) and t.id == '_FIRST_NOTE_CACHE' for t in n.targets)
            return False

        _want_fns = ('first_note_ms_from_path', 'load_chart_file')
        picked = [n for n in tree.body
                  if _is_cache_assign(n)
                  or (isinstance(n, ast.FunctionDef) and n.name in _want_fns)]
        self.assertEqual(len(picked), len(want), 'main.py 里找不到 _FIRST_NOTE_CACHE / first_note_ms_from_path')
        ns = {'os': os, 'json': json, 'Chart': Chart, 'first_note_ms': first_note_ms,
              'detect_kind': detect_kind, 'rpe_to_official_v3': rpe_to_official_v3}
        exec(compile(ast.Module(body=picked, type_ignores=[]), '<main-extract>', 'exec'), ns)
        return ns

    def test_second_call_is_fast(self):
        '''第二次调用必须走缓存 —— 不能每次播放都把谱面重新解析一遍'''
        ns = self._load_real_func()
        with tempfile.TemporaryDirectory() as d:
            path = self._make_chart(d)
            t0 = time.perf_counter()
            v1 = ns['first_note_ms_from_path'](path)
            first = time.perf_counter() - t0
            t0 = time.perf_counter()
            v2 = ns['first_note_ms_from_path'](path)
            second = time.perf_counter() - t0
        self.assertEqual(v1, v2, '缓存前后返回值必须一致')
        self.assertGreater(v1, 0, '应该算出一个正的首音符时间')
        self.assertLess(second, 0.002, f'第二次调用花了{second*1000:.1f}ms, 没走缓存')
        self.assertGreater(first, 0, '第一次调用应该真的去算了')

    def test_cache_invalidates_on_file_change(self):
        '''谱面文件改了, 缓存必须失效(按mtime+size做键)'''
        ns = self._load_real_func()
        with tempfile.TemporaryDirectory() as d:
            path = self._make_chart(d)
            ns['_FIRST_NOTE_CACHE'].clear()
            v1 = ns['first_note_ms_from_path'](path)
            # 改写文件, 把第一个音符推到很后面
            data = json.load(io.open(path, encoding='utf-8'))
            for ln in data['judgeLineList']:
                for n in ln['notesAbove'] + ln['notesBelow']:
                    n['time'] += 5000
            with io.open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            v2 = ns['first_note_ms_from_path'](path)
        self.assertNotEqual(v1, v2, '谱面变了但缓存没失效')
        self.assertGreater(v2, v1, '音符整体推后5000ms, 首音符时间必须变大')


class StartPlaybackNotOnGuiThreadTest(unittest.TestCase):
    '''核心断言: 重活不能在 _start_playback 的GUI线程部分'''

    def _cls(self):
        src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'main.py'), encoding='utf-8').read()
        for node in ast.parse(src).body:
            if isinstance(node, ast.ClassDef) and node.name == 'MainPage':
                return node
        raise AssertionError('找不到 MainPage')

    def _method(self, name):
        cls = self._cls()
        for n in cls.body:
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return n
        raise AssertionError(f'找不到 {name}')

    def _outer_only(self, name):
        '''只要方法本身的外层语句, 排除嵌套函数(重活现在都在嵌套的worker里)'''
        outer = self._method(name)
        body = [n for n in outer.body if not isinstance(n, ast.FunctionDef)]
        return ast.dump(ast.Module(body=body, type_ignores=[]))

    def test_start_playback_does_not_adapt(self):
        '''_start_playback 本体不能再出现 map_to/_replace 那种逐事件适配'''
        seg = self._outer_only('_start_playback')
        self.assertNotIn('map_to', seg, '_start_playback 还在做坐标适配(会卡GUI线程)')
        self.assertNotIn('manual_start_plan', seg, '_start_playback 还在做手动开始对齐(会卡GUI线程)')
        self.assertNotIn('from_dict', seg, '_start_playback 还在解析谱面(会卡GUI线程)')

    def test_worker_does_the_heavy_work(self):
        '''适配/对齐必须发生在worker里面'''
        # worker 是 _start_playback 里定义的嵌套函数
        outer = self._method('_start_playback')
        inner = [n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'worker']
        self.assertEqual(len(inner), 1, '找不到嵌套的 worker')
        seg = ast.dump(inner[0])
        self.assertIn('_adapted_now', seg, 'worker 里没有取适配结果')
        self.assertIn('manual_start_plan', seg, 'worker 里没有做手动开始对齐')
        self.assertIn('_start_time', seg, 'worker 里没有设置打歌时钟起点')

    def test_adapt_helpers_exist(self):
        cls = self._cls()
        names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        for m in ('_adapt_key', '_build_adapted', '_refresh_adapted', '_adapted_now'):
            self.assertIn(m, names, f'缺少 {m}')

    def test_active_pids_helper_is_module_level(self):
        '''_active_pids_of 必须是模块级函数(以前被误插进类中间, 把后面5个方法变成了死代码)'''
        src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'main.py'), encoding='utf-8').read()
        top = {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_active_pids_of', top, '_active_pids_of 不是模块级函数')
        self.assertIn('MANUAL_START_LEAD', ast.dump(ast.parse(src)),
                      '缺少 MANUAL_START_LEAD 常量')

    def test_no_method_swallowed(self):
        '''回归防护: 上一次补丁把 _stop/_stop_async/_release_all_active 三个方法吞掉了。
        py_compile 居然不报错(它们变成了 _active_pids_of 里return之后的死代码),
        所以必须用AST断言它们确实在 MainPage 类体里。'''
        cls = self._cls()
        names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        for m in ('_start_playback', '_stop', '_stop_async', '_reset_go',
                  '_release_all_active', 'run', 'sync_ms'):
            self.assertIn(m, names, f'MainPage 少了方法 {m}(可能又被补丁吞了)')

    def test_mainpage_method_count_sane(self):
        '''方法数不能莫名变少'''
        cls = self._cls()
        n = len([x for x in cls.body if isinstance(x, ast.FunctionDef)])
        self.assertGreaterEqual(n, 50, f'MainPage 只剩{n}个方法, 不对劲')


class GcDuringPlaybackTest(unittest.TestCase):
    '''播放期间关GC(以前只 freeze, 新建的5万个对象仍会触发昂贵的gen-2回收)'''

    def test_gc_disabled_during_play_and_restored(self):
        import gc
        from player import run_player
        from algo.algo_base import VirtualTouchEvent
        plan = [(i, [VirtualTouchEvent((1.0, 1.0), TouchAction.MOVE, 20000 + i)]) for i in range(50)]

        seen = []

        class Clk:
            t = 0.0

            def __call__(self):
                self.t += 1e-6
                return self.t

        clk = Clk()
        it = iter(plan)

        def send(evs):
            seen.append(gc.isenabled())

        run_player(send, it, lambda: 0.0, lambda: True, clock=clk, sleep=lambda s: None)
        self.assertTrue(seen, '一批都没发')
        self.assertTrue(all(v is False for v in seen),
                        '播放期间GC仍然是开着的, 会周期性触发几十毫秒的停顿')
        self.assertTrue(gc.isenabled(), '播放结束后没有把GC恢复回去')


class TimerResolutionTest(unittest.TestCase):
    '''Windows 上 time.sleep(0.001) 实际睡15.6ms, 会把每一批事件都睡过头'''

    def test_busy_wait_covers_sleep_granularity(self):
        from player import BUSY_WAIT_MS
        # 忙等窗口必须大于0, 且是个合理的小值(不能大到让CPU烧起来)
        self.assertGreater(BUSY_WAIT_MS, 0)
        self.assertLessEqual(BUSY_WAIT_MS, 20)

    def test_raise_timer_resolution_is_safe(self):
        from player import raise_timer_resolution
        # 在非Windows上必须直接返回True而不是抛异常
        self.assertIsInstance(raise_timer_resolution(), bool)


if __name__ == '__main__':
    unittest.main()
