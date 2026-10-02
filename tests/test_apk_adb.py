"""apk_tools 的 adb 提取部分: 进度/取消/校验/跳过/分包/OBB, 全部用假 adb(tests/fake_adb.py)驱动。

真机上没法在测试里跑, 所以假 adb 照着 adb 的命令行约定应答: devices / shell pm / ls / stat / pull,
并且能模拟慢速传输(给取消和进度留时间)、传输被截断、传一半失败、老系统没有 stat 等情况。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apk_tools as A  # noqa: E402
from unity_fixtures import build_apk, chart_json, make_package  # noqa: E402

FAKE = os.path.join(HERE, 'fake_adb.py')
PKG = A.PHIGROS_PACKAGE


class FakeDevice:
    """在临时目录里搭一个假设备: 状态文件 + 若干「设备上的文件」(其实是本地文件)"""

    def __init__(self, tmp: str, **state):
        self.tmp = tmp
        self.state_path = os.path.join(tmp, 'state.json')
        self.calls_path = os.path.join(tmp, 'calls.jsonl')
        self.state = {'packages': ['com.android.settings', PKG], 'log_file': self.calls_path}
        self.state.update(state)
        self.sources: dict[str, str] = self.state.setdefault('sources', {})
        self.write()

    def write(self):
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f)

    def add_file(self, remote: str, content: bytes) -> str:
        local = os.path.join(self.tmp, 'device', remote.strip('/').replace('/', '__'))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, 'wb') as f:
            f.write(content)
        self.sources[remote] = local
        self.write()
        return local

    def install(self, apks: dict[str, bytes], obbs: dict[str, bytes] | None = None, version: str = '3.20.0'):
        base = '/data/app/~~abc==/com.PigeonGames.Phigros-xyz=='
        self.state.setdefault('pm_paths', {})[PKG] = [f'{base}/{n}' for n in apks]
        for n, data in apks.items():
            self.add_file(f'{base}/{n}', data)
        if obbs:
            folder = f'/sdcard/Android/obb/{PKG}'
            self.state.setdefault('dirs', {})[folder] = list(obbs)
            for n, data in obbs.items():
                self.add_file(f'{folder}/{n}', data)
        self.state.setdefault('versions', {})[PKG] = version
        self.write()

    def adb(self, serial=None) -> A.Adb:
        return A.Adb(serial, exe=[sys.executable, FAKE], timeout=20)

    def calls(self) -> list[list[str]]:
        if not os.path.exists(self.calls_path):
            return []
        with open(self.calls_path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def pulls(self) -> list[list[str]]:
        return [c for c in self.calls() if 'pull' in c]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        patcher = mock.patch.dict(os.environ, {'FAKE_ADB_STATE': os.path.join(self.tmp, 'state.json')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.dest = os.path.join(self.tmp, 'apk')
        self.tracks = os.path.join(self.tmp, 'tracks')

    def device(self, **state) -> FakeDevice:
        return FakeDevice(self.tmp, **state)


# ---------------------------------------------------------------- 纯函数

class TestFormatting(unittest.TestCase):
    def test_size(self):
        self.assertEqual(A.format_size(0), '0 B')
        self.assertEqual(A.format_size(1023), '1023 B')
        self.assertEqual(A.format_size(1536), '1.5 KB')
        self.assertEqual(A.format_size(5 * 1024 ** 2), '5.0 MB')
        self.assertEqual(A.format_size(int(2.4 * 1024 ** 3)), '2.4 GB')
        self.assertEqual(A.format_size(3 * 1024 ** 4), '3072.0 GB')

    def test_duration(self):
        self.assertEqual(A.format_duration(0), '0:00')
        self.assertEqual(A.format_duration(59.4), '0:59')
        self.assertEqual(A.format_duration(61), '1:01')
        self.assertEqual(A.format_duration(3725), '1:02:05')
        self.assertEqual(A.format_duration(-5), '0:00')


class TestMeter(unittest.TestCase):
    def make(self, total=1000, **kw):
        self.t = 0.0
        self.events = []
        m = A.Meter(lambda d, t, s: self.events.append((d, t, s)), total, clock=lambda: self.t, **kw)
        return m

    def test_throttling_but_final_always_emitted(self):
        m = self.make(1000)
        for i in range(1, 11):
            self.t += 0.001  # 比 50ms 快得多
            m.update(i * 100)
        self.assertEqual(self.events[0][0], 100)           # 第一次一定发
        self.assertLess(len(self.events), 5)               # 中间的被节流
        self.assertEqual(self.events[-1][:2], (1000, 1000))  # 到 100% 那次一定发

    def test_done_never_decreases_and_clamped(self):
        m = self.make(100)
        m.update(60, force=True)
        m.update(30, force=True)   # 倒退 -> 忽略
        m.update(500, force=True)  # 超出 -> 夹到 total
        self.assertEqual([e[0] for e in self.events], [60, 60, 100])

    def test_rate_and_eta_text(self):
        m = self.make(10 * 1024 * 1024, label='提取 base.apk')
        for step in range(1, 7):
            self.t = step * 0.5
            m.update(step * 1024 * 1024, force=True)
        text = self.events[-1][2]
        self.assertIn('提取 base.apk', text)
        self.assertIn('60%', text)
        self.assertIn('6.0 MB / 10.0 MB', text)
        self.assertIn('2.0 MB/s', text)       # 每 0.5 秒 1MB
        self.assertIn('剩余 0:02', text)      # 还差 4MB, 2MB/s

    def test_unknown_total(self):
        m = self.make(0)
        m.update(2048, force=True)
        self.assertEqual(self.events[-1][:2], (2048, 0))
        self.assertIn('2.0 KB', self.events[-1][2])
        self.assertNotIn('%', self.events[-1][2])

    def test_items_unit_has_no_byte_text(self):
        m = self.make(100, unit='items')
        self.t = 1
        m.update(10, '解包谱面 1/10', force=True)
        self.t = 2
        m.update(30, '解包谱面 3/10', force=True)
        text = self.events[-1][2]
        self.assertIn('解包谱面 3/10', text)
        self.assertNotIn('KB', text)
        self.assertIn('剩余', text)

    def test_no_callback_is_fine(self):
        A.Meter(None, 10).update(5, force=True)


class TestDevices(unittest.TestCase):
    def test_parse_skips_noise(self):
        out = ('* daemon not running; starting now at tcp:5037\n* daemon started successfully\n'
               'List of devices attached\nABC123\tdevice\nemulator-5554\toffline\n'
               '0123456789\tunauthorized\nLINUXBOX\tno permissions (user in plugdev group); see [x]\n\n')
        devs = A.parse_devices(out)
        self.assertEqual([(d.serial, d.state) for d in devs[:3]],
                         [('ABC123', 'device'), ('emulator-5554', 'offline'), ('0123456789', 'unauthorized')])
        self.assertTrue(devs[0].ready)
        self.assertFalse(devs[1].ready)
        self.assertEqual(len(devs), 4)

    def test_pick_serial(self):
        d = A.Device
        self.assertEqual(A.pick_serial([d('A', 'device')]), 'A')
        self.assertEqual(A.pick_serial([d('A', 'device'), d('B', 'device')], 'B'), 'B')
        self.assertEqual(A.pick_serial([d('A', 'offline'), d('B', 'device')]), 'B')
        with self.assertRaisesRegex(A.AdbError, '多台设备.*A.*B'):
            A.pick_serial([d('A', 'device'), d('B', 'device')])
        with self.assertRaisesRegex(A.AdbError, '没有检测到安卓设备'):
            A.pick_serial([])
        with self.assertRaisesRegex(A.AdbError, '授权'):
            A.pick_serial([d('A', 'unauthorized')])
        with self.assertRaisesRegex(A.AdbError, '离线'):
            A.pick_serial([d('A', 'offline')])
        with self.assertRaisesRegex(A.AdbError, 'X 没有连接'):
            A.pick_serial([d('A', 'device')], 'X')
        with self.assertRaisesRegex(A.AdbError, '授权'):
            A.pick_serial([d('A', 'unauthorized'), d('B', 'device')], 'A')


# ---------------------------------------------------------------- adb 查询

class TestAdbQueries(BaseCase):
    def test_list_devices_and_serial_flag(self):
        dev = self.device(devices=[['AAA', 'device'], ['BBB', 'device']])
        devs = A.list_devices(dev.adb('BBB'))  # devices 命令不带 -s
        self.assertEqual([d.serial for d in devs], ['AAA', 'BBB'])
        self.assertEqual(dev.calls()[0], ['devices'])
        dev.adb('BBB').shell('pm list packages')
        self.assertEqual(dev.calls()[-1][:2], ['-s', 'BBB'])

    def test_find_package(self):
        dev = self.device()
        self.assertEqual(A.find_package(dev.adb()), PKG)
        dev.state['packages'] = ['com.android.settings', 'com.foo.phigros.cn']
        dev.write()
        self.assertEqual(A.find_package(dev.adb()), 'com.foo.phigros.cn')  # 唯一的模糊匹配
        dev.state['packages'] = ['com.a.phigros', 'com.b.Phigros.clone']
        dev.write()
        with self.assertRaisesRegex(A.AdbError, 'com.a.phigros.*com.b.Phigros.clone'):
            A.find_package(dev.adb())
        dev.state['packages'] = ['com.android.settings']
        dev.write()
        with self.assertRaisesRegex(A.AdbError, '没有安装'):
            A.find_package(dev.adb())
        self.assertEqual(A.find_package(dev.adb(), 'com.android.settings'), 'com.android.settings')

    def test_version(self):
        dev = self.device()
        dev.install({'base.apk': b'x'}, version='3.20.1')
        self.assertEqual(A.package_version(dev.adb(), PKG), '3.20.1')
        self.assertIsNone(A.package_version(dev.adb(), 'com.not.there'))

    def test_apk_paths_base_first(self):
        dev = self.device()
        dev.install({'split_config.arm64_v8a.apk': b'1', 'base.apk': b'2', 'split_asset.apk': b'3'})
        names = [p.rsplit('/', 1)[-1] for p in A.apk_paths(dev.adb(), PKG)]
        self.assertEqual(names, ['base.apk', 'split_asset.apk', 'split_config.arm64_v8a.apk'])
        with self.assertRaisesRegex(A.AdbError, 'pm path'):
            A.apk_paths(dev.adb(), 'com.not.there')

    def test_obb_paths(self):
        dev = self.device()
        self.assertEqual(A.obb_paths(dev.adb(), PKG), [])  # 目录不存在 = 没有(TapTap 版)
        dev.install({'base.apk': b'x'}, {'main.82.com.PigeonGames.Phigros.obb': b'o', 'readme.txt': b't'})
        self.assertEqual(A.obb_paths(dev.adb(), PKG), [f'/sdcard/Android/obb/{PKG}/main.82.com.PigeonGames.Phigros.obb'])
        # 第一个根目录没有, 第二个根目录有
        dev.state['dirs'] = {f'/storage/emulated/0/Android/obb/{PKG}': ['patch.1.obb']}
        dev.write()
        self.assertEqual(A.obb_paths(dev.adb(), PKG), [f'/storage/emulated/0/Android/obb/{PKG}/patch.1.obb'])

    def test_obb_order_newest_last_numeric_not_lexicographic(self):
        dev = self.device()
        folder = f'/sdcard/Android/obb/{PKG}'
        dev.state['dirs'] = {folder: [f'patch.83.{PKG}.obb', f'main.82.{PKG}.obb', f'main.9.{PKG}.obb',
                                      'weird.obb', f'main.100.{PKG}.obb']}
        dev.write()
        names = [p.rsplit('/', 1)[-1] for p in A.obb_paths(dev.adb(), PKG)]
        # main 在前(9 < 82 < 100, 按数字不是按字典序), patch 在后, 认不出的最后
        self.assertEqual(names, [f'main.9.{PKG}.obb', f'main.82.{PKG}.obb', f'main.100.{PKG}.obb',
                                 f'patch.83.{PKG}.obb', 'weird.obb'])

    def test_obb_permission_denied_is_reported_not_hidden(self):
        dev = self.device()
        dev.install({'base.apk': b'x'})
        dev.state['denied_dirs'] = [f'/sdcard/Android/obb/{PKG}', f'/storage/emulated/0/Android/obb/{PKG}']
        dev.write()
        warnings = []
        self.assertEqual(A.obb_paths(dev.adb(), PKG, warnings.append), [])
        self.assertEqual(len(warnings), 1)
        self.assertIn('没有权限', warnings[0])
        self.assertIn('拷出来', warnings[0])
        # 目录不存在不算权限问题, 不该告警
        warnings.clear()
        dev.state['denied_dirs'] = []
        dev.write()
        self.assertEqual(A.obb_paths(dev.adb(), PKG, warnings.append), [])
        self.assertEqual(warnings, [])

    def test_remote_size_fallbacks(self):
        dev = self.device()
        dev.add_file('/data/x.apk', b'a' * 1234)
        self.assertEqual(A.remote_size(dev.adb(), '/data/x.apk'), 1234)
        dev.state['no_stat'] = True  # 老系统: stat 不支持, 只能解析 ls -l
        dev.write()
        self.assertEqual(A.remote_size(dev.adb(), '/data/x.apk'), 1234)
        dev.state['no_size'] = True
        dev.write()
        self.assertIsNone(A.remote_size(dev.adb(), '/data/x.apk'))
        self.assertIsNone(A.remote_size(dev.adb(), '/data/missing.apk'))

    def test_paths_with_spaces_are_quoted(self):
        dev = self.device()
        dev.add_file('/sdcard/my files/a b.apk', b'z' * 10)
        self.assertEqual(A.remote_size(dev.adb(), '/sdcard/my files/a b.apk'), 10)

    def test_adb_missing_gives_guidance(self):
        with self.assertRaisesRegex(A.AdbError, 'platform-tools'):
            A.Adb(exe=os.path.join(self.tmp, 'no-such-adb')).run('devices')

    def test_adb_error_hints(self):
        dev = self.device(devices=[['A', 'device'], ['B', 'device']])
        with self.assertRaisesRegex(A.AdbError, '多台设备'):
            dev.adb().shell('pm list packages')
        dev2 = FakeDevice(self.tmp, devices=[['A', 'offline']])
        with self.assertRaises(A.AdbError):
            dev2.adb('A').shell('pm list packages')

    def test_find_adb_prefers_env(self):
        with mock.patch.dict(os.environ, {'PHISAP_ADB': '/opt/x/adb'}):
            self.assertEqual(A.find_adb(), '/opt/x/adb')


# ---------------------------------------------------------------- 拉取

class TestPullFile(BaseCase):
    def setUp(self):
        super().setUp()
        self.data = os.urandom(300_000)
        self.dev = self.device(chunk=30_000)
        self.dev.add_file('/data/app/base.apk', self.data)
        self.local = os.path.join(self.dest, 'base.apk')

    def test_success_with_progress(self):
        seen = []
        got = A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=len(self.data),
                          progress=seen.append, poll=0.01)
        self.assertEqual(got, len(self.data))
        with open(self.local, 'rb') as f:
            self.assertEqual(f.read(), self.data)
        self.assertFalse(os.path.exists(self.local + '.part'))
        self.assertEqual(seen[-1], len(self.data))
        self.assertEqual(seen, sorted(seen))  # 单调不减

    def test_progress_reflects_slow_transfer(self):
        self.dev.state['chunk_sleep'] = 0.05
        self.dev.write()
        seen = []
        A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=len(self.data),
                    progress=seen.append, poll=0.02)
        mids = [n for n in seen if 0 < n < len(self.data)]
        self.assertGreaterEqual(len(mids), 3)  # 传输过程中确实看到了中间进度

    def test_size_mismatch_detected_and_partial_removed(self):
        self.dev.state['truncate'] = 1000
        self.dev.write()
        with self.assertRaisesRegex(A.AdbError, '传输不完整'):
            A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=len(self.data), poll=0.01)
        self.assertFalse(os.path.exists(self.local))
        self.assertFalse(os.path.exists(self.local + '.part'))

    def test_failure_exit_code(self):
        self.dev.state['pull_fail'] = True
        self.dev.write()
        with self.assertRaisesRegex(A.AdbError, 'device went offline'):
            A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=len(self.data), poll=0.01)
        self.assertFalse(os.path.exists(self.local + '.part'))
        self.assertFalse(os.path.exists(self.local))

    def test_missing_remote_file(self):
        with self.assertRaisesRegex(A.AdbError, 'No such file'):
            A.pull_file(self.dev.adb(), '/data/app/nope.apk', self.local, poll=0.01)

    def test_cancel_mid_transfer(self):
        self.dev.state['chunk'] = 10_000
        self.dev.state['chunk_sleep'] = 0.1  # 30 块 * 0.1s = 3s, 足够中途取消
        self.dev.write()
        cancel = threading.Event()

        def on_progress(n):
            if n > 0:
                cancel.set()

        t0 = time.monotonic()
        with self.assertRaises(A.Cancelled):
            A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=len(self.data),
                        progress=on_progress, cancel=cancel, poll=0.02)
        self.assertLess(time.monotonic() - t0, 2.0)  # 没有等传完(整个传输要 3 秒)
        self.assertFalse(os.path.exists(self.local))
        self.assertFalse(os.path.exists(self.local + '.part'))

    def test_stale_part_file_is_replaced(self):
        os.makedirs(self.dest)
        with open(self.local + '.part', 'wb') as f:
            f.write(b'garbage' * 100)
        A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=len(self.data), poll=0.01)
        self.assertEqual(os.path.getsize(self.local), len(self.data))

    def test_works_without_size(self):
        A.pull_file(self.dev.adb(), '/data/app/base.apk', self.local, size=None, poll=0.01)
        self.assertEqual(os.path.getsize(self.local), len(self.data))


class TestPullPackage(BaseCase):
    def setUp(self):
        super().setUp()
        self.base = os.urandom(120_000)
        self.split = os.urandom(50_000)
        self.obb = os.urandom(200_000)
        self.dev = self.device(chunk=20_000)
        self.dev.install({'base.apk': self.base, 'split_config.arm64_v8a.apk': self.split},
                         {'main.82.com.PigeonGames.Phigros.obb': self.obb})
        self.logs = []
        self.events = []

    def pull(self, **kw):
        kw.setdefault('progress', lambda d, t, s: self.events.append((d, t, s)))
        kw.setdefault('log', self.logs.append)
        return A.pull_package(self.dev.adb(), PKG, self.dest, **kw)

    def test_pulls_all_apks_and_obb_in_order(self):
        r = self.pull()
        names = [os.path.basename(p) for p in r.files]
        self.assertEqual(names, ['base.apk', 'split_config.arm64_v8a.apk', 'main.82.com.PigeonGames.Phigros.obb'])
        self.assertTrue(all(os.path.dirname(p) == os.path.join(self.dest, PKG) for p in r.files))
        with open(r.files[2], 'rb') as f:
            self.assertEqual(f.read(), self.obb)
        self.assertEqual(r.total_bytes, len(self.base) + len(self.split) + len(self.obb))
        self.assertEqual(r.version, '3.20.0')
        self.assertEqual(r.skipped, [])
        self.assertEqual(len(self.dev.pulls()), 3)

    def test_progress_is_aggregate_monotonic_and_ends_at_100(self):
        self.pull()
        total = len(self.base) + len(self.split) + len(self.obb)
        self.assertTrue(all(t == total for _, t, _ in self.events))
        dones = [d for d, _, _ in self.events]
        self.assertEqual(dones, sorted(dones))
        self.assertEqual(dones[-1], total)
        self.assertIn('base.apk', self.events[1][2] if len(self.events) > 1 else self.events[0][2])

    def test_second_run_skips_existing_same_size_files(self):
        self.pull()
        self.dev.pulls()  # 触发读取
        calls_before = len(self.dev.pulls())
        r = self.pull()
        self.assertEqual(len(r.skipped), 3)
        self.assertEqual(len(self.dev.pulls()), calls_before)  # 没有重新拉
        self.assertEqual(self.events[-1][0], self.events[-1][1])  # 进度仍然走到 100%
        self.assertTrue(any('跳过' in m for m in self.logs))

    def test_partial_skip_only_pulls_the_missing_one(self):
        r1 = self.pull()
        os.remove(r1.files[1])
        with open(r1.files[0], 'ab') as f:  # base.apk 大小不对了 -> 也要重拉
            f.write(b'x')
        before = len(self.dev.pulls())
        r2 = self.pull()
        self.assertEqual(len(self.dev.pulls()) - before, 2)
        self.assertEqual([os.path.basename(p) for p in r2.skipped], ['main.82.com.PigeonGames.Phigros.obb'])
        with open(r2.files[0], 'rb') as f:
            self.assertEqual(f.read(), self.base)

    def test_force_repulls(self):
        self.pull()
        before = len(self.dev.pulls())
        self.pull(force=True)
        self.assertEqual(len(self.dev.pulls()) - before, 3)

    def test_without_obb(self):
        r = self.pull(include_obb=False)
        self.assertEqual(len(r.files), 2)
        self.assertEqual(len(self.dev.pulls()), 2)

    def test_permission_denied_obb_warning_reaches_the_log(self):
        self.dev.state['dirs'] = {}
        self.dev.state['denied_dirs'] = [f'/sdcard/Android/obb/{PKG}']
        self.dev.write()
        r = A.pull_package(self.dev.adb(), PKG, self.dest, log=self.logs.append)
        self.assertEqual(len(r.files), 2)  # APK 照常拉
        self.assertTrue(any('没有权限读取' in m for m in self.logs))

    def test_taptap_style_no_obb_logs_hint(self):
        dev = FakeDevice(os.path.join(self.tmp), packages=[PKG], log_file=self.dev.calls_path)
        dev.install({'base.apk': self.base})
        r = A.pull_package(dev.adb(), PKG, self.dest, log=self.logs.append)
        self.assertEqual(len(r.files), 1)
        self.assertTrue(any('没有找到 OBB' in m for m in self.logs))

    def test_package_fallback_to_fuzzy_match(self):
        self.dev.state['packages'] = ['com.fork.phigros']
        self.dev.state['pm_paths'] = {'com.fork.phigros': self.dev.state['pm_paths'][PKG]}
        self.dev.state['dirs'] = {}
        self.dev.write()
        r = A.pull_package(self.dev.adb(), PKG, self.dest, log=self.logs.append)
        self.assertEqual(r.package, 'com.fork.phigros')
        self.assertTrue(os.path.isdir(os.path.join(self.dest, 'com.fork.phigros')))
        self.assertTrue(any('改用 com.fork.phigros' in m for m in self.logs))

    def test_not_installed(self):
        self.dev.state['packages'] = ['com.android.settings']
        self.dev.write()
        with self.assertRaisesRegex(A.AdbError, '没有安装'):
            self.pull()

    def test_disk_space_checked_before_pulling(self):
        usage = shutil.disk_usage(self.tmp)
        with mock.patch('apk_tools.shutil.disk_usage', return_value=usage._replace(free=1024)):
            with self.assertRaisesRegex(A.PackageError, '磁盘空间不足'):
                self.pull()
        self.assertEqual(self.dev.pulls(), [])  # 一个文件都没拉

    def test_device_serial_is_used(self):
        dev = FakeDevice(self.tmp, devices=[['AAA', 'device'], ['BBB', 'device']], packages=[PKG],
                         log_file=self.dev.calls_path)
        dev.install({'base.apk': b'z' * 100})
        A.pull_package(dev.adb('BBB'), PKG, self.dest, log=self.logs.append)
        self.assertTrue(all(c[:2] == ['-s', 'BBB'] for c in dev.calls()))

    def test_cancel_before_start_and_between_files(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(A.Cancelled):
            self.pull(cancel=cancel)
        self.assertEqual(self.dev.pulls(), [])
        cancel.clear()
        self.dev.state['chunk_sleep'] = 0.03
        self.dev.write()

        def stop_after_first_file(d, t, s):
            if d >= len(self.base):
                cancel.set()

        with self.assertRaises(A.Cancelled):
            self.pull(cancel=cancel, progress=stop_after_first_file)
        left = sorted(os.listdir(os.path.join(self.dest, PKG)))
        self.assertTrue(all(not n.endswith('.part') for n in left))

    def test_unknown_sizes_give_indeterminate_progress(self):
        self.dev.state['no_size'] = True
        self.dev.write()
        r = self.pull(include_obb=False)
        self.assertEqual(len(r.files), 2)
        self.assertTrue(all(t == 0 for _, t, _ in self.events))
        self.assertGreater(self.events[-1][0], 0)


# ---------------------------------------------------------------- 一条龙

class TestPipeline(BaseCase):
    def make_google_play_style(self):
        """Google Play 版: APK 很小只带 catalog, bundle 在 OBB 里 —— 解包必须跨文件找"""
        charts = {
            'Assets/Tracks/Foo.Bar.0/Chart_IN.json': chart_json('in'),
            'Assets/Tracks/Foo.Bar.0/Chart_AT #77.json': chart_json('at'),
            'Assets/Tracks/Baz.Qux.0/Chart_SP.json': chart_json('sp'),
        }
        full = os.path.join(self.tmp, 'full.zip')
        make_package(full, charts)
        import zipfile
        apk_members, obb_members = {}, {}
        with zipfile.ZipFile(full) as z:
            for n in z.namelist():
                (apk_members if n.endswith('catalog.json') else obb_members)[n] = z.read(n)
        apk = build_apk(os.path.join(self.tmp, 'base.apk'), apk_members)
        obb = build_apk(os.path.join(self.tmp, 'main.obb'), obb_members)
        return open(apk, 'rb').read(), open(obb, 'rb').read()

    def test_google_play_layout_end_to_end(self):
        apk, obb = self.make_google_play_style()
        dev = self.device()
        dev.install({'base.apk': apk}, {'main.82.com.PigeonGames.Phigros.obb': obb})
        events, logs = [], []
        res = A.pull_and_extract(None, PKG, apk_dir=self.dest, tracks_dir=self.tracks, adb=dev.adb(),
                                 progress=lambda d, t, s: events.append((d, t, s)), log=logs.append)
        self.assertEqual(res.extract.total, 3)
        self.assertEqual(res.extract.written, 3)
        self.assertEqual(sorted(os.listdir(self.tracks)), ['Baz.Qux', 'Foo.Bar'])
        self.assertEqual(sorted(os.listdir(os.path.join(self.tracks, 'Foo.Bar'))), ['Chart_AT.json', 'Chart_IN.json'])
        self.assertEqual(os.listdir(os.path.join(self.tracks, 'Baz.Qux')), ['Chart.json'])
        # 两个阶段, 每个阶段内部单调, 阶段切换时从头开始
        s1 = [e for e in events if e[2].startswith('[1/2]')]
        s2 = [e for e in events if e[2].startswith('[2/2]')]
        self.assertTrue(s1 and s2)
        self.assertEqual(events, s1 + s2)
        for stage in (s1, s2):
            dones = [d for d, _, _ in stage]
            self.assertEqual(dones, sorted(dones))
            self.assertEqual(stage[-1][0], stage[-1][1])  # 每个阶段都走到 100%
        self.assertTrue(any('解包完成' in m for m in logs))
        self.assertEqual(res.deleted, [])
        self.assertTrue(all(os.path.exists(p) for p in res.pull.files))

    def test_delete_archives_after_success(self):
        apk, obb = self.make_google_play_style()
        dev = self.device()
        dev.install({'base.apk': apk}, {'main.obb': obb})
        res = A.pull_and_extract(None, PKG, apk_dir=self.dest, tracks_dir=self.tracks, adb=dev.adb(),
                                 delete_archives=True)
        self.assertEqual(len(res.deleted), 2)
        self.assertTrue(all(not os.path.exists(p) for p in res.deleted))
        self.assertEqual(res.extract.total, 3)

    def test_archives_kept_when_extraction_has_failures(self):
        charts = {'Assets/Tracks/A.B.0/Chart_IN.json': chart_json('in'), 'Assets/Tracks/A.B.0/Chart_HD.json': chart_json('hd')}
        pkg = os.path.join(self.tmp, 'p.apk')
        names = make_package(pkg, charts)
        # 把 HD 的 bundle 换成垃圾
        import zipfile
        broken = os.path.join(self.tmp, 'broken.apk')
        with zipfile.ZipFile(pkg) as zin, zipfile.ZipFile(broken, 'w') as zout:
            for n in zin.namelist():
                data = zin.read(n)
                if names['Assets/Tracks/A.B.0/Chart_HD.json'] in n:
                    data = b'UnityFS\0' + b'\xff' * 300
                zout.writestr(zipfile.ZipInfo(n), data)
        dev = self.device()
        dev.install({'base.apk': open(broken, 'rb').read()})
        res = A.pull_and_extract(None, PKG, apk_dir=self.dest, tracks_dir=self.tracks, adb=dev.adb(),
                                 delete_archives=True)
        self.assertEqual(len(res.extract.failed), 1)
        self.assertEqual(res.deleted, [])
        self.assertTrue(os.path.exists(res.pull.files[0]))  # 有失败时不能删, 用户还要重试

    def test_cancel_during_pull_leaves_no_partial_files_or_charts(self):
        apk, obb = self.make_google_play_style()
        dev = self.device(chunk=2000, chunk_sleep=0.02)
        dev.install({'base.apk': apk}, {'main.obb': obb})
        cancel = threading.Event()

        def stop(d, t, s):
            if d > 0:
                cancel.set()

        with self.assertRaises(A.Cancelled):
            A.pull_and_extract(None, PKG, apk_dir=self.dest, tracks_dir=self.tracks, adb=dev.adb(),
                               progress=stop, cancel=cancel)
        self.assertFalse(os.path.exists(self.tracks))
        leftovers = [n for _r, _d, fs in os.walk(self.dest) for n in fs if n.endswith('.part')]
        self.assertEqual(leftovers, [])


class TestCommandLine(BaseCase):
    def test_extract_command(self):
        pkg = os.path.join(self.tmp, 'p.apk')
        make_package(pkg, {'Assets/Tracks/A.B.0/Chart_IN.json': chart_json('in')})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = A.main(['extract', pkg, '--tracks', self.tracks])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.tracks, 'A.B', 'Chart_IN.json')))
        self.assertIn('1 首歌共 1 份谱面', out.getvalue())
        self.assertIn('100%', err.getvalue())  # 命令行也有进度条

    def test_errors_return_nonzero_not_traceback(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = A.main(['extract', os.path.join(self.tmp, 'missing.apk')])
        self.assertEqual(code, 2)
        self.assertIn('错误', out.getvalue())
        with mock.patch.dict(os.environ, {'PHISAP_ADB': os.path.join(self.tmp, 'no-adb')}):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = A.main(['pull'])
        self.assertEqual(code, 2)
        self.assertIn('adb', out.getvalue())


if __name__ == '__main__':
    unittest.main()
