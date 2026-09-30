'''MaaTouch 协议编码 + 下载源粘性 的离线单元测试(不依赖adb/PyQt/网络)'''
from __future__ import annotations

import unittest

from algo.algo_base import TouchAction, VirtualTouchEvent
from maatouch import MaaTouchController, MAATOUCH_MAX_POINTERS, MAATOUCH_EXPECTED_SIZE
from downloader import Downloader, SOURCES, DEFAULT_SOURCE


def _ev(action: TouchAction, x: float, y: float, pid: int = 20001):
    return VirtualTouchEvent(action=action, pos=(x, y), pointer=pid)


class _RecordingStdin:
    '''假的 stdin: 记录写进去的字节'''

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def flush(self) -> None:
        pass


class _FakeMaaTouch(MaaTouchController):
    '''绕过 __init__(不连adb), 只测协议编码。
    注入假 stdin, 这样走的是真正的 _write(包括 _closed 守卫)。'''

    def __init__(self):
        self.device_width = 2400
        self.device_height = 1080
        self._closed = False
        self.max_pointers = MAATOUCH_MAX_POINTERS
        self._stdin = _RecordingStdin()
        self._write_lock = __import__('threading').Lock()

    @property
    def sent(self) -> list[str]:
        return [c.decode('ascii') for c in self._stdin.chunks]


class MaaTouchProtocolTest(unittest.TestCase):
    def setUp(self):
        self.c = _FakeMaaTouch()

    def test_down_move_up_encoding(self):
        '''一批 DOWN+MOVE+UP 必须编码成 minitouch 协议, 并以 c 提交'''
        self.c.touch_many([
            _ev(TouchAction.DOWN, 100.4, 200.6, 20001),
            _ev(TouchAction.MOVE, 300.2, 400.8, 20002),
            _ev(TouchAction.UP, 0, 0, 20001),
        ])
        self.assertEqual(len(self.c.sent), 1)
        out = self.c.sent[0]
        self.assertEqual(
            out,
            'd 20001 100 201 255\nm 20002 300 401 255\nu 20001\nc\n',
        )

    def test_commit_present(self):
        '''没有 c 就不会真正下发, 每批都必须带 c'''
        self.c.touch_many([_ev(TouchAction.DOWN, 5, 5)])
        self.assertTrue(self.c.sent[0].endswith('c\n'))

    def test_empty_batch_noop(self):
        self.c.touch_many([])
        self.assertEqual(self.c.sent, [])

    def test_release_pointers(self):
        self.c.release_pointers([20001, 20002])
        self.assertEqual(self.c.sent, ['u 20001\nu 20002\nc\n'])

    def test_release_pointers_empty(self):
        self.c.release_pointers([])
        self.c.release_pointers(None)
        self.assertEqual(self.c.sent, [])

    def test_reset_all(self):
        self.c.reset_all()
        self.assertEqual(self.c.sent, ['r\n'])

    def test_pointer_id_not_remapped_on_our_side(self):
        '''我们把原样id发给MaaTouch, 由它重映射; 自己不能提前改'''
        self.c.touch_many([_ev(TouchAction.DOWN, 1, 2, 20099)])
        self.assertIn('d 20099 1 2 255', self.c.sent[0])

    def test_rounding(self):
        self.c.touch_many([_ev(TouchAction.DOWN, 10.5, 20.5)])
        # Python round(10.5)=10, round(20.5)=20 (banker's rounding) —— 只验证是整数
        self.assertRegex(self.c.sent[0], r'd \d+ 1[02] 2[01] 255')

    def test_write_after_close_ignored(self):
        self.c._closed = True
        self.c.touch_many([_ev(TouchAction.DOWN, 1, 1)])
        self.assertEqual(self.c.sent, [])

    def test_supports_visual_watch_false(self):
        '''MaaTouch没有视频流, 不支持视觉自动开始'''
        self.assertFalse(self.c.supports_visual_watch)
        with self.assertRaises(NotImplementedError):
            self.c.start_activity_watch()


class DownloaderSourceStickyTest(unittest.TestCase):
    '''用户选的下载源必须是权威的, 任何自动逻辑都不许改它'''

    def setUp(self):
        # 沙箱里没装 requests, load_index 会提前抛错; 置成真值绕开(网络调用已被 fake 掉)
        import downloader as D
        self._old_requests = D.requests
        D.requests = object()

    def tearDown(self):
        import downloader as D
        D.requests = self._old_requests

    def _make(self, source='jsdelivr'):
        d = Downloader.__new__(Downloader)
        d.tracks_dir = './Assets/Tracks'
        d.source = source
        d.source_used = source
        d._dirs_source = source
        d.song_index = {}
        d._dir_index = {}
        d._all_dirs = []
        d._loaded = False
        return d

    def test_load_index_keeps_user_source(self):
        '''load_index 成功后只能记 source_used, 不能改 source'''
        d = self._make('jsdelivr')
        seen = []

        def fake_get(url, timeout=20, **kw):
            seen.append(url)
            if 'Chart_info.json' in url:
                class R:
                    def json(self):
                        return {'Songs': {'a.0': {'Name': 'A'}}}
                return R()
            raise AssertionError(url)

        d._get = fake_get
        d.load_index(force=True, use_cache=False)
        self.assertEqual(d.source, 'jsdelivr')          # 用户选择没被动过
        self.assertEqual(d.source_used, 'jsdelivr')     # 实际来源也记对了
        self.assertEqual(len(d.song_index), 1)
        self.assertIn('cdn.jsdelivr.net', seen[0])      # 真的走了jsDelivr

    def test_load_index_fallback_records_used_not_selected(self):
        '''用户选的源挂了, 兜底到github: 只记 source_used, source 仍是用户选的'''
        d = self._make('jsdelivr')

        def fake_get(url, timeout=20, **kw):
            if 'jsdelivr' in url:
                raise OSError('CDN挂了')
            class R:
                def json(self):
                    return {'Songs': {'a.0': {'Name': 'A'}}}
            return R()

        d._get = fake_get
        d.load_index(force=True, use_cache=False)
        self.assertEqual(d.source, 'jsdelivr')
        self.assertEqual(d.source_used, 'github')

    def test_index_cache_does_not_override_source(self):
        '''读磁盘缓存时不能把 source 覆盖成缓存里记的源(旧版就是这个bug)'''
        import json, os, tempfile
        d = self._make('jsdelivr')
        with tempfile.TemporaryDirectory() as td:
            import downloader as D
            old = D.INDEX_CACHE_FILE
            D.INDEX_CACHE_FILE = os.path.join(td, 'cache.json')
            try:
                with open(D.INDEX_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump({'source': 'github', 'Songs': {'a.0': {'Name': 'A'}}}, f)
                ok = d._load_index_cache()
                self.assertTrue(ok)
                self.assertEqual(d.source, 'jsdelivr')       # 关键断言
                self.assertEqual(d.source_used, 'github')
            finally:
                D.INDEX_CACHE_FILE = old

    def test_load_dirs_keeps_user_source(self):
        '''jsDelivr没有目录API, 要借github的列目录, 但不能把 source 改成github'''
        d = self._make('jsdelivr')
        calls = []

        def fake_get(url, timeout=20, **kw):
            calls.append(url)
            if 'api.github.com' in url:
                class R:
                    def json(self):
                        return [{'name': 'a.0', 'type': 'dir'}, {'name': 'x.json', 'type': 'file'}]
                return R()
            raise OSError('no api')

        d._get = fake_get
        d._load_dirs()
        self.assertEqual(d._all_dirs, ['a.0'])
        self.assertEqual(d.source, 'jsdelivr')       # 关键断言: 没被改成github
        self.assertEqual(d._dirs_source, 'github')   # 只是记下来

    def test_set_source_resets_loaded(self):
        d = self._make('github')
        d._loaded = True
        d._all_dirs = ['a.0']
        d.set_source('jsdelivr')
        self.assertEqual(d.source, 'jsdelivr')
        self.assertFalse(d._loaded)
        self.assertEqual(d._all_dirs, [])

    def test_set_source_same_noop(self):
        d = self._make('github')
        d._loaded = True
        d.set_source('github')
        self.assertTrue(d._loaded)   # 同一个源不该清状态

    def test_download_chart_fallback_keeps_source(self):
        '''下载时用户源挂了要兜底, 但 source 不许被改写'''
        d = self._make('jsdelivr')
        d._loaded = True
        d.song_index = {'a.0': {'Name': 'A', 'AT': True}}
        d._dir_index = {'a.0': 'a.0'}

        def fake_get(url, timeout=30, **kw):
            if 'jsdelivr' in url:
                raise OSError('CDN挂了')
            class R:
                content = b'{"a":1}'
            return R()

        d._get = fake_get
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            d.tracks_dir = td
            got = []
            d.download_chart('a.0', 'AT', on_done=lambda ok, path, err: got.append((ok, path, err)))
        self.assertTrue(got[0][0], got[0][2])
        self.assertEqual(d.source, 'jsdelivr')       # 关键断言
        self.assertEqual(d.source_used, 'github')

    def test_raw_candidates_user_first(self):
        d = self._make('kkgithub')
        cands = d._raw_candidates()
        self.assertEqual(cands[0], 'kkgithub')
        self.assertEqual(sorted(cands), sorted(SOURCES.keys()))


class BackendInterfaceTest(unittest.TestCase):
    '''两个触控后端必须可互换(main.py 按 duck typing 用)'''

    NEEDED = ('touch_many', 'tap', 'touch', 'release_pointers',
              'device_width', 'device_height', 'collector_running', 'max_pointers',
              'close', 'start_activity_watch', 'stop_activity_watch')

    def test_maatouch_has_all(self):
        for name in self.NEEDED:
            self.assertTrue(hasattr(MaaTouchController, name), f'MaaTouchController 缺 {name}')

    def test_scrcpy_has_all(self):
        from control import DeviceController
        for name in self.NEEDED:
            self.assertTrue(hasattr(DeviceController, name), f'DeviceController 缺 {name}')

    def test_maatouch_max_pointers_is_10(self):
        self.assertEqual(MAATOUCH_MAX_POINTERS, 10)
        self.assertEqual(MAATOUCH_EXPECTED_SIZE, 13775)

    def test_both_report_max_pointers(self):
        c = _FakeMaaTouch()
        self.assertEqual(c.max_pointers, 10)

if __name__ == '__main__':
    unittest.main()
