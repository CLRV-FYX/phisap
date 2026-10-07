"""Regression checks for the APK's standalone root-only process loader."""
from pathlib import Path
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from app_dex import build_dex  # noqa: E402
from dexlib import audit_dex  # noqa: E402


class RootOnlyLoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dex = build_dex()
        audit_dex(cls.dex)
        cls.dex_text = cls.dex.decode('latin1')
        cls.launcher = (ROOT / 'tools/native/inside.sh').read_text(encoding='utf-8')
        cls.app_builder = (ROOT / 'tools/app_dex.py').read_text(encoding='utf-8')
        cls.manifest = (ROOT / 'android/pocket/AndroidManifest.xml').read_text(encoding='utf-8')
        cls.injector = (ROOT / 'tools/native/inject.c').read_text(encoding='utf-8')

    def test_dex_has_a_new_visible_root_build_and_no_system_load_wait_path(self):
        self.assertIn('PhiSAP Root 3.7'.encode(), self.dex)
        self.assertIn('loader-version.txt'.encode(), self.dex)
        self.assertIn('旧版等待状态已清除'.encode(), self.dex)
        self.assertNotIn(b'System.load', self.dex)

    def test_dex_does_not_reference_external_xposed_apis(self):
        for descriptor in (
            'de/robv/android/xposed',
            'IXposedHookLoadPackage',
            'IXposedHookZygoteInit',
            'XposedHelpers',
            'assets/xposed_init',
            'LSPosed',
        ):
            with self.subTest(descriptor=descriptor):
                self.assertNotIn(descriptor, self.dex_text)

    def test_dex_does_not_modify_system_wrap_properties(self):
        self.assertNotIn('wrap.', self.dex_text)
        self.assertNotIn('resetprop', self.dex_text)

    def test_first_run_migrates_old_mode_and_clears_stale_status(self):
        load_saved = self.app_builder.split("load.const_string(1, 'loader-version.txt')", 1)[1].split(
            "load.const_string(1, 'game.txt')", 1
        )[0]
        self.assertIn("load.const_string(0, '3.7')", load_saved)
        self.assertIn("load.label('current_mode')", load_saved)
        self.assertIn('load.const(1, 1)', load_saved)
        self.assertIn("M(ACT, 'setMode'", load_saved)
        self.assertIn('loader-version.txt', load_saved)
        self.assertIn('旧版等待状态已清除', load_saved)

    def test_root_worker_reports_su_denial_but_preserves_script_diagnostics(self):
        worker = self.app_builder.split('def _worker(dex: DexBuilder):', 1)[1].split(
            'def _injector(dex: DexBuilder):', 1
        )[0]
        self.assertIn("M(ACT, 'readFile'", worker)
        self.assertIn("'inside.sh'", worker)
        self.assertIn("'status.txt'", worker)
        self.assertIn('SU 请求失败（退出码 ', worker)
        self.assertIn('正在请求 root 权限并加载进程内钩子', worker)

    def test_app_stages_the_complete_root_loader_payload(self):
        enter = self.app_builder.split("ei = Asm(8, 1)", 1)[1].split(
            "dex.add_method(ACT, 'enterInside'", 1
        )[0]
        for asset in ('inside.sh', 'phisap-tapd', 'phisap-inject', 'libphisap.so'):
            with self.subTest(asset=asset):
                self.assertIn(asset, enter)
        self.assertIn('injects the packaged native library into the already-running process', enter)

    def test_launcher_requires_root_and_embedded_direct_injector(self):
        self.assertIn('id -u', self.launcher)
        self.assertIn('没有获得 root 权限', self.launcher)
        self.assertIn('libphisap-inject.so', self.launcher)
        self.assertIn('"$INJ" "$target_pid" "$SO_USE"', self.launcher)
        self.assertIn('PTRACE_ATTACH', self.injector)
        self.assertIn('resolve_open(pid)', self.injector)
        self.assertIn('dlopen', self.injector)

    def test_running_game_is_not_force_stopped_and_maps_is_the_success_handshake(self):
        self.assertIn('检测到 Phigros 正在运行；不强停', self.launcher)
        self.assertNotIn('force-stop', self.launcher)
        self.assertIn('/proc/$pid/maps', self.launcher)
        self.assertIn("grep -q 'libphisap\\.so'", self.launcher)
        self.assertIn('已确认 libphisap.so 映射在 Phigros 进程', self.launcher)
        self.assertIn('30 秒内钩子未完成', self.launcher)
        already_loaded = self.launcher.split('pid=$(hooked_pid || true)', 1)[1].split('\n# Verify', 1)[0]
        self.assertIn('跳过重复注入', already_loaded)
        self.assertNotIn('exit 0', already_loaded)

    def test_no_external_framework_or_system_wrap_is_required(self):
        combined = self.launcher + self.manifest
        for forbidden in ('LSPosed', 'xposed', 'zygisk', 'wrap.'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), combined.lower())
        self.assertNotIn('xposedmodule', self.manifest)
        self.assertNotIn('xposedminversion', self.manifest)
        self.assertIn('setenforce 1', self.launcher)
        self.assertIn("trap 'restore_selinux' EXIT", self.launcher)

    def test_built_apk_contains_the_direct_root_injector(self):
        apk_path = ROOT / 'android/phisap-pocket.apk'
        with zipfile.ZipFile(apk_path) as apk:
            names = set(apk.namelist())
            packaged_dex = apk.read('classes.dex')
            inject = apk.read('assets/phisap-inject')
            native = apk.read('assets/libphisap.so')
            script = apk.read('assets/inside.sh').decode('utf-8')
        audit_dex(packaged_dex)
        self.assertIn(b'phisap-inject-26', inject)
        self.assertIn('用法: phisap-inject <pid> <so>'.encode(), inject)
        self.assertNotIn(b'zygisk', inject.lower())
        self.assertNotIn(b'zygisk', native.lower())
        self.assertNotIn(b'phisap-boot-25', inject.lower())
        self.assertIn('lib/arm64-v8a/libphisap-inject.so', names)
        self.assertIn('assets/libphisap.so', names)
        self.assertIn('assets/inside.sh', names)
        self.assertIn('libphisap-inject.so', script)
        self.assertNotIn('assets/xposed_init', names)
        self.assertNotIn('META-INF/xposed/scope.list', names)
        self.assertNotIn('IXposedHookZygoteInit', packaged_dex.decode('latin1'))


if __name__ == '__main__':
    unittest.main()
