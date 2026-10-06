"""Static regression checks for the APK's actual in-process LSPosed loader."""
from pathlib import Path
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from app_dex import build_dex  # noqa: E402
from dexlib import audit_dex  # noqa: E402


class XposedLoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dex = build_dex()
        audit_dex(cls.dex)
        cls.dex_text = cls.dex.decode('latin1')
        cls.launcher = (ROOT / 'tools/native/inside.sh').read_text(encoding='utf-8')
        cls.app_builder = (ROOT / 'tools/app_dex.py').read_text(encoding='utf-8')
        cls.manifest = (ROOT / 'android/pocket/AndroidManifest.xml').read_text(encoding='utf-8')

    def test_real_xposed_entry_uses_standard_api_descriptors(self):
        for descriptor in (
            'IXposedHookLoadPackage',
            'IXposedHookZygoteInit',
            'XC_LoadPackage$LoadPackageParam',
            'IXposedHookZygoteInit$StartupParam',
            'XposedHelpers',
        ):
            with self.subTest(descriptor=descriptor):
                self.assertIn(descriptor, self.dex_text)
        self.assertNotIn('IXposedHookLoadPackage$LoadPackageParam', self.dex_text)

    def test_system_load_is_called_from_application_attach_callback(self):
        for value in (
            'Lapp/phisap/pocket/AttachHook;',
            'Lapp/phisap/pocket/NativeLoader;',
            'android.app.Application',
            'assets/libphisap.so',
            'libphisap.so.tmp',
            'setReadOnly',
            'renameTo',
            'Ljava/lang/System;',
            'System.load succeeded in target process',
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.dex_text)

    def test_apk_is_declared_as_an_xposed_module(self):
        self.assertIn('android:name="xposedmodule" android:value="true"', self.manifest)
        self.assertIn('android:name="xposedminversion" android:value="93"', self.manifest)
        # No resource array is added to the aapt2 table because the hand-built
        # UI DEX depends on stable id/layout resource IDs. Scope is selected in
        # LSPosed, and HookEntry itself package-gates the callback.
        self.assertIn('com.PigeonGames.Phigros', self.dex_text)

    def test_launcher_only_succeeds_after_maps_confirms_loaded_library(self):
        self.assertIn('/proc/$pid/maps', self.launcher)
        self.assertIn("grep -q 'libphisap", self.launcher)
        self.assertIn('已确认 libphisap.so 映射在游戏进程', self.launcher)
        self.assertNotIn('phisap-inject', self.launcher)
        self.assertNotIn('setenforce', self.launcher)
        self.assertNotIn('PTRACE', self.launcher.upper())
        self.assertIn('LSPosed 只在进程启动时加载模块', self.launcher)

    def test_in_process_startup_prepares_config_before_game_launch(self):
        enter = self.app_builder.split("ei = Asm(8, 1)", 1)[1].split("dex.add_method(ACT, 'enterInside'", 1)[0]
        self.assertNotIn("M(ACT, 'openGame'", enter)
        self.assertIn("('inside.sh', 'phisap-tapd')", enter)
        self.assertIn('must prepare hook config and tapd before launching the game', self.app_builder)

    def test_built_apk_contains_the_hook_and_not_the_old_injector(self):
        apk_path = ROOT / 'android/phisap-pocket.apk'
        with zipfile.ZipFile(apk_path) as apk:
            names = set(apk.namelist())
            packaged_dex = apk.read('classes.dex')
            native = apk.read('assets/libphisap.so')
            xposed_init = apk.read('assets/xposed_init').decode('ascii').strip()
        audit_dex(packaged_dex)
        self.assertEqual(xposed_init, 'app.phisap.pocket.HookEntry')
        self.assertIn('IXposedHookZygoteInit', packaged_dex.decode('latin1'))
        self.assertEqual(native[:4], bytes([0x7f]) + b'ELF')
        self.assertIn('assets/inside.sh', names)
        self.assertNotIn('assets/phisap-inject', names)
        self.assertNotIn('lib/arm64-v8a/libphisap-inject.so', names)


if __name__ == '__main__':
    unittest.main()
