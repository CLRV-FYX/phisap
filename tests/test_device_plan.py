"""电脑版导出的计划，手机版要能按同一格式读回来。"""
import io
import json
import shlex
import shutil
import subprocess
import unittest

from algo.algo_base import TouchAction, VirtualTouchEvent
from apk_tools import AdbError, push_device_plan
from device_plan import load_device_plan, export_device_plan


class DevicePlanTest(unittest.TestCase):
    def test_roundtrip_keeps_order_and_actions(self):
        ans = {
            20: [VirtualTouchEvent((100.4, 200.6), TouchAction.MOVE, 1001)],
            10: [VirtualTouchEvent((640, 360), TouchAction.DOWN, 1000)],
        }
        buf = io.StringIO()
        export_device_plan(ans, buf, name='Chart_AT')
        obj = json.loads(buf.getvalue())
        self.assertEqual(obj['format'], 1)
        self.assertEqual(obj['width'], 1280)
        self.assertEqual(obj['height'], 720)
        self.assertEqual(obj['name'], 'Chart_AT')
        self.assertEqual(obj['events'], [
            [10, TouchAction.DOWN.value, 1000, 640, 360],
            [20, TouchAction.MOVE.value, 1001, 100, 201],
        ])
        again = load_device_plan(io.StringIO(buf.getvalue()))
        self.assertEqual(again['events'], obj['events'])

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            load_device_plan(io.StringIO('{"format": 9, "width": 1280, "height": 720, "events": []}'))


class AdbPlanTransferTest(unittest.TestCase):
    class FakeAdb:
        def __init__(self, fail_su=False, fail_direct=False):
            self.fail_su = fail_su
            self.fail_direct = fail_direct
            self.run_calls = []
            self.shell_calls = []

        def run(self, *args, **kwargs):
            self.run_calls.append((args, kwargs))
            return ''

        def shell(self, command, timeout=None):
            self.shell_calls.append(command)
            if command.startswith('su -c ') and self.fail_su:
                raise AdbError('su denied')
            if 'id -u' in command and self.fail_direct:
                raise AdbError('root required')
            return ''

    def test_root_transfer_targets_private_dir_atomically(self):
        adb = self.FakeAdb()
        self.assertEqual(push_device_plan(adb, '{"format":1}'), '已通过 ADB + root 传到手机')
        self.assertEqual(adb.run_calls[0][0][0], 'push')
        su, flag, script = shlex.split(adb.shell_calls[0])
        self.assertEqual((su, flag), ('su', '-c'))
        self.assertIn('[ "$(id -u)" -ne 0 ]', script)
        self.assertIn('test -d "$app_root"', script)
        self.assertIn('chmod 600 "$tmp"', script)
        self.assertIn('mv -f "$tmp" "$app_files/plan.json"', script)
        self.assertNotIn('/sdcard/phisap/plan.json', script)

    def test_root_adbd_is_a_supported_fallback_after_su_denial(self):
        adb = self.FakeAdb(fail_su=True)
        self.assertEqual(push_device_plan(adb, '{"format":1}'), '已通过 ADB + root 传到手机')
        self.assertEqual(len(adb.shell_calls), 3)  # su, root adbd script, then launch activity
        self.assertIn('id -u', adb.shell_calls[1])

    def test_unprivileged_shared_storage_fallback_cannot_report_success(self):
        adb = self.FakeAdb(fail_su=True, fail_direct=True)
        with self.assertRaisesRegex(AdbError, '请确认 shell 的 SU 授权'):
            push_device_plan(adb, '{"format":1}')
        self.assertEqual(len(adb.shell_calls), 2)
        script = shlex.split(adb.shell_calls[0])[2]
        self.assertIn('app_files=/data/data/app.phisap.pocket/files', script)
        self.assertIn('"$app_files/plan.json"', script)
        self.assertIn('id -u', adb.shell_calls[1])

    def test_generated_root_copy_script_has_valid_posix_syntax(self):
        if not shutil.which('sh'):
            self.skipTest('requires a POSIX shell')
        adb = self.FakeAdb()
        push_device_plan(adb, '{"format":1}')
        script = shlex.split(adb.shell_calls[0])[2]
        subprocess.run(['sh', '-n'], input=script, text=True, check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
