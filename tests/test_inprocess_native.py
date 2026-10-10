"""Regression checks for the hand-built APK's in-process native path.

These inspect the low-level sources rather than importing the separate Gradle
implementation; the APK packages the files under tools/native/.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK_ASM = (ROOT / 'tools/native/hook.S').read_text(encoding='utf-8')
HOOK_C = (ROOT / 'tools/native/hook.c').read_text(encoding='utf-8')
TAPD_C = (ROOT / 'tools/native/tapd.c').read_text(encoding='utf-8')
INSIDE_SH = (ROOT / 'tools/native/inside.sh').read_text(encoding='utf-8')
INJECT_C = (ROOT / 'tools/native/inject.c').read_text(encoding='utf-8')
UPDATE_CMD = (ROOT / 'update.cmd').read_text(encoding='utf-8')


class HookTrampolineRegressionTest(unittest.TestCase):
    def test_original_arguments_survive_get_tramp_call(self):
        entry = HOOK_ASM.split('hook_entry:', 1)[1].split('// x0=fn', 1)[0]
        get_tramp = entry.index('bl get_tramp')
        original = entry.index('blr x16')
        self.assertIn('mov x16, x0', entry)
        self.assertNotIn('blr x0', entry)
        # Restore all AArch64 argument registers (including the IL2CPP MethodInfo
        # argument and FP/vector arguments) after the C helper and before calling
        # the original method.
        for restore in (
            'ldp x0, x1, [sp, #16]', 'ldp x2, x3, [sp, #32]',
            'ldp x4, x5, [sp, #48]', 'ldp x6, x7, [sp, #64]',
            'ldp x8, x9, [sp, #80]', 'ldp q0, q1, [sp, #96]',
            'ldp q2, q3, [sp, #128]', 'ldp q4, q5, [sp, #160]',
            'ldp q6, q7, [sp, #192]',
        ):
            with self.subTest(registers=restore):
                at = entry.index(restore)
                self.assertGreater(at, get_tramp)
                self.assertLess(at, original)

    def test_original_return_is_preserved_around_sampling(self):
        entry = HOOK_ASM.split('hook_entry:', 1)[1].split('// x0=fn', 1)[0]
        original = entry.index('blr x16')
        sample = entry.index('bl sample_line', original)
        for save in (
            'stp x0, x1, [sp, #224]', 'stp q0, q1, [sp, #240]',
            'stp q2, q3, [sp, #272]',
        ):
            with self.subTest(registers=save):
                at = entry.index(save)
                self.assertGreater(at, original)
                self.assertLess(at, sample)
        for restore in (
            'ldp q2, q3, [sp, #272]', 'ldp q0, q1, [sp, #240]',
            'ldp x0, x1, [sp, #224]',
        ):
            self.assertGreater(entry.index(restore), sample)


class TouchDaemonRegressionTest(unittest.TestCase):
    def test_stream_parser_never_copies_unbounded_reads_into_accumulator(self):
        stream = TAPD_C.split('static int feed_messages(', 1)[1].split('\nstatic void disconnect_client', 1)[0]
        self.assertIn('sizeof(struct Msg) - stream->used', stream)
        self.assertIn('n < need ? n : need', stream)
        self.assertIn('stream->bytes + stream->used, bytes, take', stream)
        self.assertNotIn('acc_n + n', TAPD_C)

    @unittest.skipUnless(sys.platform.startswith('linux') and (shutil.which('cc') or shutil.which('gcc')),
                         'requires a Linux C compiler')
    def test_stream_parser_handles_split_and_coalesced_messages(self):
        compiler = shutil.which('cc') or shutil.which('gcc')
        source = (ROOT / 'tools/native/tapd.c').as_posix()
        harness = f'''\
#define main phisap_tapd_main
#include "{source}"
#undef main
#include <assert.h>
int main(void) {{
    struct MsgStream stream = {{{{0}}, 0}};
    struct Msg a = {{MAGIC, 8, 0, 1920, 1080}};
    struct Msg b = {{MAGIC, 8, 0, 2560, 1440}};
    unsigned char wire[sizeof a + sizeof b];
    memcpy(wire, &a, sizeof a);
    memcpy(wire + sizeof a, &b, sizeof b);
    assert(feed_messages(&stream, wire, 7) == 0);
    assert(stream.used == 7);
    assert(feed_messages(&stream, wire + 7, sizeof wire - 7) == 0);
    assert(stream.used == 0 && uw == 2560 && uh == 1440);
    struct Msg bad = {{0, 8, 0, 1920, 1080}};
    assert(feed_messages(&stream, (const unsigned char *)&bad, sizeof bad) == -1);
    assert(stream.used == 0);
    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as td:
            c_file = Path(td) / 'tapd_stream_test.c'
            exe = Path(td) / 'tapd_stream_test'
            c_file.write_text(harness, encoding='utf-8')
            subprocess.run(
                [compiler, '-D_GNU_SOURCE', '-std=c11', str(c_file), '-o', str(exe)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run([str(exe)], check=True, capture_output=True, text=True)

    def test_disconnect_releases_every_active_touch(self):
        disconnect = TAPD_C.split('static void disconnect_client(', 1)[1].split('\nstatic long long monotonic_ms', 1)[0]
        self.assertIn('stream->used = 0', disconnect)
        self.assertIn('lift_all();', disconnect)
        self.assertIn('active_slot[s]', TAPD_C)

    def test_display_configuration_is_only_a_startup_fallback(self):
        main = TAPD_C.split('int main(void) {', 1)[1]
        self.assertEqual(main.count('load_cfg();'), 1)
        self.assertIn('if (changed && dev_ready)', main)
        self.assertIn('destroy_dev();', main)
        self.assertIn('client == polled_client', main)
        self.assertIn('POLLERR | POLLNVAL', main)

    def test_hook_rechecks_game_resolution_and_resends_it_to_tapd(self):
        ensure_screen = HOOK_C.split('static int ensure_screen(void) {', 1)[1].split('\nstatic int ensure_geom', 1)[0]
        self.assertIn('now + 500', ensure_screen)
        self.assertIn('w != screen_w || h != screen_h', ensure_screen)
        self.assertIn('release_all();', ensure_screen)
        self.assertIn('tap_send(8, 0, w, h)', ensure_screen)
        self.assertIn('connected_now && action != 8', HOOK_C)


class InProcessLauncherRegressionTest(unittest.TestCase):
    def test_remote_loader_waits_for_thread_stops_and_validates_return_sentinel(self):
        invoker = INJECT_C.split('static int invoke_remote(', 1)[1].split('\n#ifndef PTRACE_O_TRACEFORK', 1)[0]
        self.assertIn('waitpid(pid, &st, __WALL)', invoker)
        self.assertIn('stop_sig == SIGSEGV && have_regs && regs.pc == 0', invoker)
        self.assertIn('Other signals (including an internal dlopen SIGSEGV) are not success.', invoker)

    def test_packaged_injector_exposes_only_the_direct_pid_so_entry(self):
        main = INJECT_C.split('int main(int argc, char **argv) {', 1)[1]
        self.assertIn('用法: phisap-inject <pid> <so>', main)
        self.assertNotIn('boot_main(argv', main)
        self.assertNotIn('strcmp(argv[1], "boot")', main)
        self.assertIn('resolve_open(pid)', main)
        self.assertIn('attach_any(pid', main)

    def test_code_page_failures_include_address_and_errno(self):
        text_writer = INJECT_C.split('static int poke_text_exact(', 1)[1].split(
            'static int poke_exact(pid_t pid, uint64_t addr, const void *src, size_t n) {', 1
        )[0]
        self.assertIn('PTRACE_POKETEXT', text_writer)
        self.assertNotIn('process_vm_writev(pid', text_writer)
        self.assertIn('text write %s at %llx errno=%d', INJECT_C)
        self.assertIn('text_write_failed("代码洞")', INJECT_C)
        self.assertIn('text_write_failed("入口分支")', INJECT_C)

    def test_windows_updater_targets_the_branch_with_the_current_fixes(self):
        self.assertIn("$Branch = 'arena/47d37e24-phisap'", UPDATE_CMD)
        self.assertNotIn("$Branch = 'arena/369a6f58-phisap'", UPDATE_CMD)
        self.assertNotIn("$Branch = 'arena/01a0fd15-phisap'", UPDATE_CMD)

    def test_windows_updater_describes_the_root_only_android_install(self):
        self.assertIn('并授予 root 即可', UPDATE_CMD)
        self.assertIn('无需安装或配置 LSPosed', UPDATE_CMD)
        self.assertIn('已打开时会直接注入并核对 maps', UPDATE_CMD)

    def test_tap_daemon_is_restarted_for_the_current_apk_assets(self):
        stop = INSIDE_SH.split('stop_old_tapd() {', 1)[1].split('\n}', 1)[0]
        self.assertIn('killall phisap-tapd libphisap-tapd.so', stop)
        self.assertIn('pidof libphisap-tapd.so', stop)

    def test_launcher_directly_injects_and_only_succeeds_after_maps_confirmation(self):
        self.assertIn('id -u', INSIDE_SH)
        self.assertIn('"$INJ" "$target_pid" "$SO_USE"', INSIDE_SH)
        self.assertIn('/proc/$pid/maps', INSIDE_SH)
        self.assertIn("grep -q 'libphisap\\.so'", INSIDE_SH)
        self.assertIn('已确认 libphisap.so 映射在 Phigros 进程', INSIDE_SH)
        self.assertIn('libil2cpp\\.so', INSIDE_SH)
        self.assertIn('30 秒内钩子未完成', INSIDE_SH)
        self.assertNotIn('force-stop', INSIDE_SH)
        self.assertNotIn('boot "$PKG"', INSIDE_SH)
        self.assertNotIn('LSPosed', INSIDE_SH)
        self.assertIn('setenforce 0', INSIDE_SH)
        self.assertIn('setenforce 1', INSIDE_SH)

    def test_native_initialization_times_out_before_launcher_and_reports_retry_action(self):
        self.assertIn('i < 150', HOOK_C)  # 15 s IL2CPP initialization ceiling
        self.assertIn('i < 100 && !line', HOOK_C)  # 10 s class lookup ceiling
        self.assertIn('15 秒内初始化失败', HOOK_C)
        self.assertIn('没有 JudgeLineControl；', HOOK_C)
        self.assertIn('重启游戏重试', HOOK_C)
        self.assertIn('while [ "$i" -lt 60 ]', INSIDE_SH)  # 30 s outer ceiling
        self.assertIn('"没找到 il2cpp"*|"没有 JudgeLineControl"*', INSIDE_SH)

if __name__ == '__main__':
    unittest.main()
