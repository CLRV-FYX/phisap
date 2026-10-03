"""界面里「从游戏安装包提取谱面」这一组的测试。

分两层:
  1. 静态检查(AST, 任何环境都跑): 控件挂在曲目页、信号和槽接好、后台任务函数里不碰界面控件、
     进度信号不用会溢出的 int、关窗口时会取消后台任务……
  2. 运行时冒烟(子进程里起一个离屏的 Qt 界面, 用假 adb 和现拼的 APK/OBB 真的点按钮):
     进度条单调走到 100%、取消能清理干净、失败会变红且按钮恢复、谱面库和难度下拉框会刷新。
     没有 Qt/图形库的环境(退出码 77)自动跳过; 断言失败(退出码 1)才算失败。
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(ROOT, 'main.py')


def _src() -> str:
    with io.open(MAIN, encoding='utf-8') as f:
        return f.read()


def _tree() -> ast.Module:
    return ast.parse(_src())


def _class(name: str) -> ast.ClassDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f'找不到类 {name}')


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f'{cls.name} 里找不到方法 {name}')


def _attr_chain(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


class TestStatic(unittest.TestCase):
    def test_group_sits_between_song_and_download_groups_on_the_song_page(self):
        build = _method(_class('MainPage'), '_build')
        order = []
        for n in ast.walk(build):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'addWidget'
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == 'p_song'
                    and n.args and isinstance(n.args[0], ast.Name)):
                order.append((n.lineno, n.args[0].id))
        names = [name for _, name in sorted(order)]
        self.assertIn('apk_group', names)
        self.assertLess(names.index('song_group'), names.index('apk_group'))
        self.assertLess(names.index('apk_group'), names.index('dl_group'))

    def test_widgets_and_signal_hookups(self):
        src = _src()
        for conn in ('self.apk_all_btn.clicked.connect(self._apk_pull_and_extract)',
                     'self.apk_pull_btn.clicked.connect(self._apk_pull_only)',
                     'self.apk_pick_btn.clicked.connect(self._apk_pick_and_extract)',
                     'self.apk_local_btn.clicked.connect(self._apk_extract_local)',
                     'self.apk_cancel_btn.clicked.connect(self._apk_cancel)',
                     't.progress.connect(self._apk_on_progress)',
                     't.finished_ok.connect(self._apk_done)',
                     't.cancelled.connect(self._apk_cancelled)',
                     't.failed.connect(self._apk_failed)',
                     't.finished.connect(self._apk_finished)'):
            self.assertIn(conn, src, f'缺少连接: {conn}')

    def test_determinate_progress_bar(self):
        src = _src()
        self.assertIn('self.apk_progress = ProgressBar()', src)  # 确定进度的条, 不是不确定动画的那种
        self.assertIn('self.apk_progress.setRange(0, 1000)', src)
        self.assertRegex(src, r'from qfluentwidgets import \([^)]*\bProgressBar\b')

    def test_progress_signal_is_float_not_int(self):
        """安装包超过 2GB, int 信号(C int)会溢出; 所以信号只传 0~1 的比例"""
        cls = _class('ApkTaskThread')
        sigs = {}
        for n in cls.body:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and _attr_chain(n.value.func) == 'pyqtSignal':
                sigs[n.targets[0].id] = [_attr_chain(a) for a in n.value.args]
        self.assertEqual(sigs['progress'], ['float', 'str'])
        for name in ('log_line', 'finished_ok', 'cancelled', 'failed'):
            self.assertIn(name, sigs)
        self.assertNotIn('int', sigs['progress'])

    def test_thread_run_reports_all_outcomes_via_signals(self):
        run = _method(_class('ApkTaskThread'), 'run')
        text = ast.dump(run)
        for needle in ('Cancelled', 'ApkToolError', 'cancelled', 'failed', 'finished_ok'):
            self.assertTrue(needle in text, f'ApkTaskThread.run 里没有处理 {needle}')
        # run 里只能 emit 信号, 不能碰任何 self.xxx 控件(线程类里本来就没有控件)
        for n in ast.walk(run):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'self':
                self.assertIn(n.attr, {'_task', '_on_progress', 'cancel_event', 'log_line', 'cancelled', 'failed',
                                       'finished_ok', 'progress'}, f'run 里用了 self.{n.attr}')

    def test_task_closures_never_touch_widgets(self):
        """交给后台线程的 task 函数里不能出现 self —— 线程里碰控件是未定义行为。
        需要的值(包名/序列号/开关)必须在 GUI 线程里先读成普通变量。"""
        cls = _class('MainPage')
        checked = 0
        for name in ('_apk_pull_and_extract', '_apk_pull_only', '_apk_extract'):
            fn = _method(cls, name)
            for n in ast.walk(fn):
                if isinstance(n, ast.FunctionDef) and n.name == 'task':
                    checked += 1
                    for x in ast.walk(n):
                        if isinstance(x, ast.Name) and x.id == 'self':
                            self.fail(f'{name}.task 里用了 self(第{x.lineno}行)')
        self.assertEqual(checked, 3)

    def test_closing_the_window_cancels_running_task(self):
        """MainPage 是 Window 的子控件, 关主窗口时 MainPage.closeEvent 不会被调用(Qt 只给顶层窗口发),
        所以必须由 Window.closeEvent 转交; 否则退出时 QThread 还在跑, 进程直接崩。"""
        shutdown = ast.dump(_method(_class('MainPage'), '_apk_shutdown'))
        for needle in ('_apk_thread', 'cancel', 'wait'):
            self.assertTrue(needle in shutdown, f'_apk_shutdown 里没有 {needle}')
        window_close = ast.dump(_method(_class('Window'), 'closeEvent'))
        self.assertTrue('_apk_shutdown' in window_close, 'Window.closeEvent 没有收尾后台任务')
        # 关主窗口时 MainPage.closeEvent 不会被调用, 设置必须由 Window 自己存。
        # 写的是 ./cache/settings.ini, 不是 ./cache 这个目录。
        self.assertTrue('save_cache' in window_close, 'Window.closeEvent 没有保存设置')
        self.assertNotIn('self.main_page.closeEvent', window_close)
        page_close = ast.dump(_method(_class('MainPage'), 'closeEvent'))
        self.assertTrue('_apk_shutdown' in page_close)

    def test_finish_restores_buttons_via_qthread_finished(self):
        """按钮恢复挂在 QThread.finished 上(无论成功/取消/失败都会触发), 不能只挂在成功分支"""
        fin = _method(_class('MainPage'), '_apk_finished')
        self.assertTrue('_apk_set_running' in ast.dump(fin))

    def test_done_handler_refreshes_library_and_difficulty_list(self):
        done = _method(_class('MainPage'), '_apk_done')
        text = ast.dump(done)
        self.assertTrue('refresh_songs' in text, '解包完成后要刷新曲目列表')
        self.assertTrue('song_selected' in text, 'refresh_songs 不更新难度下拉框, 要再载入一遍当前曲目')
        self.assertTrue('_apk_playing' in text, '演奏中不能动界面状态(会把停止按钮禁用掉)')

    def test_play_button_refuses_while_extracting_and_extraction_refuses_while_playing(self):
        cls = _class('MainPage')
        run = _method(cls, 'run')
        first = ast.dump(run.body[0])
        self.assertTrue('_apk_busy' in first, 'run() 开头应先拦住"提取/解包进行中"')
        for name in ('_apk_pull_and_extract', '_apk_pull_only', '_apk_pick_and_extract', '_apk_extract_local'):
            fn = _method(cls, name)
            self.assertTrue('_apk_can_start' in ast.dump(fn.body[0]), f'{name} 应先检查 _apk_can_start')
        can = ast.dump(_method(cls, '_apk_can_start'))
        self.assertTrue('_apk_playing' in can)
        playing = ast.dump(_method(cls, '_apk_playing'))
        self.assertTrue('_running' in playing and '_vauto_waiting' in playing)

    def test_entry_point_adds_local_platform_tools_to_path(self):
        src = _src()
        tail = src[src.index("if __name__ == '__main__':"):]
        self.assertIn('apk_tools.add_local_adb_to_path()', tail)
        self.assertLess(tail.index('add_local_adb_to_path'), tail.index('QApplication('))  # 要赶在任何 adb 调用之前

    def test_ez_difficulty_is_recognised(self):
        """下载器和难度下拉框都有 EZ, chart_difficulty 以前却不认, EZ 谱面永远选不到"""
        for node in ast.walk(_tree()):
            if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == '_KNOWN_DIFFICULTIES':
                self.assertIn('EZ', ast.literal_eval(node.value))
                return
        self.fail('找不到 _KNOWN_DIFFICULTIES')

    def test_main_does_not_import_qt_in_apk_tools(self):
        with io.open(os.path.join(ROOT, 'apk_tools.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                mods = [n.module or '']
            for m in mods:
                self.assertFalse(m.startswith(('PyQt', 'qfluentwidgets')), f'apk_tools 不能依赖 Qt: {m}')


# ---------------------------------------------------------------- 运行时冒烟

SMOKE = r'''
import os, sys, tempfile, time, zipfile, json
ROOT, HERE = sys.argv[1], sys.argv[2]
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
os.chdir(tempfile.mkdtemp())
try:
    from PyQt5.QtWidgets import QApplication
    app = QApplication([])
    import main
except Exception as e:  # 没有 Qt / 图形库: 环境问题, 不是代码问题
    print('ENV', type(e).__name__, e)
    sys.exit(77)

from unittest import mock
import apk_tools
from test_apk_adb import FakeDevice
from unity_fixtures import make_package, chart_json, build_apk

FAKE = os.path.join(HERE, 'fake_adb.py')
EXE = [sys.executable, FAKE]
tmp = tempfile.mkdtemp()


def pump(cond, timeout=60.0):
    t0 = time.time()
    while not cond():
        app.processEvents()
        time.sleep(0.01)
        if time.time() - t0 > timeout:
            raise AssertionError('等待超时')
    for _ in range(10):  # 让排队中的信号都投递完
        app.processEvents()
        time.sleep(0.005)


def package(charts, filler=0):
    full = os.path.join(tmp, 'full.zip')
    make_package(full, charts)
    cat, rest = {}, {}
    with zipfile.ZipFile(full) as z:
        for n in z.namelist():
            (cat if n.endswith('catalog.json') else rest)[n] = z.read(n)
    if filler:
        rest['assets/bin/Data/filler.bin'] = os.urandom(filler)
    apk = open(build_apk(os.path.join(tmp, 'b.apk'), cat), 'rb').read()
    obb = open(build_apk(os.path.join(tmp, 'm.obb'), rest), 'rb').read()
    return apk, obb


def fresh_cwd():
    """每个场景换一个干净的工作目录: 程序里的路径都是相对 ./Assets, 上一个场景留下的
    APK/OBB 会被「大小相同, 跳过」逻辑认出来, 让后面的场景根本不传输。"""
    os.chdir(tempfile.mkdtemp())


def device(**state):
    os.environ['FAKE_ADB_STATE'] = os.path.join(tmp, 'state.json')
    return FakeDevice(tmp, **state)


def new_page():
    page = main.MainPage()
    page.resize(900, 1400)
    page.show()
    app.processEvents()
    return page


def sample_run(page, start):
    """点按钮, 记录每一个送到界面的进度事件(frac, 文字)和进度条上实际显示的值, 直到后台线程结束。
    事件是在槽里截的, 一个不漏; 单靠轮询控件会漏掉两次采样之间的变化。"""
    events, values = [], []
    orig = page._apk_on_progress

    def spy(frac, text):
        events.append((frac, text))
        orig(frac, text)

    page._apk_on_progress = spy  # _apk_begin 连信号时取的就是这个实例属性
    try:
        start()
        t0 = time.time()
        while page._apk_busy() and time.time() - t0 < 60:
            app.processEvents()
            values.append(page.apk_progress.value())
            time.sleep(0.01)
        pump(lambda: not page._apk_busy())
    finally:
        del page._apk_on_progress
    return events, values


CHARTS = {
    'Assets/Tracks/Foo.Bar.0/Chart_IN.json': chart_json('in'),
    'Assets/Tracks/Foo.Bar.0/Chart_AT #77.json': chart_json('at'),
    'Assets/Tracks/Foo.Bar.0/Chart_EZ.json': chart_json('ez'),
    'Assets/Tracks/Baz.Qux.0/Chart_SP.json': chart_json('sp'),
}


def diff_items(page):
    return [page.diff_box.itemText(i) for i in range(page.diff_box.count())]


def scenario_happy_path():
    fresh_cwd()
    apk, obb = package(CHARTS, filler=2_000_000)
    dev = device(chunk=120_000, chunk_sleep=0.03)
    dev.install({'base.apk': apk}, {'main.82.com.PigeonGames.Phigros.obb': obb})
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        page = new_page()
        assert not page.apk_progress.isVisible(), '空闲时进度条应隐藏'
        assert not page.apk_cancel_btn.isEnabled()
        events, values = sample_run(page, page.apk_all_btn.click)
        s1 = [e for e in events if e[1].startswith('[1/2]')]
        s2 = [e for e in events if e[1].startswith('[2/2]')]
        assert s1 and s2 and events == s1 + s2, [t for _, t in events][:6]
        for stage in (s1, s2):  # 两个阶段各自从头走到 100%, 阶段内不倒退
            fracs = [f for f, _ in stage]
            assert fracs == sorted(fracs) and fracs[-1] == 1.0, fracs
        assert max(values) > 0, '进度条控件上从没显示过进度'
        assert page.apk_progress.value() == 1000
        assert page.apk_progress.isVisible()
        assert page.apk_status.text().startswith('完成:'), page.apk_status.text()
        for b in (page.apk_all_btn, page.apk_pull_btn, page.apk_pick_btn, page.apk_local_btn, page.apk_pkg_edit):
            assert b.isEnabled(), '结束后控件应恢复可用'
        assert not page.apk_cancel_btn.isEnabled()
        lib = {d: sorted(os.listdir(os.path.join('Assets/Tracks', d))) for d in os.listdir('Assets/Tracks')}
        assert lib == {'Foo.Bar': ['Chart_AT.json', 'Chart_EZ.json', 'Chart_IN.json'], 'Baz.Qux': ['Chart.json']}, lib
        songs = [page.song_box.itemText(i) for i in range(page.song_box.count())]
        assert songs == ['Baz.Qux', 'Foo.Bar'], songs
        # 谱库原来是空的: 选中的第一首歌也必须有难度可选(refresh_songs 自己不会填难度下拉框)
        assert diff_items(page) == ['SP'], diff_items(page)
        page.song_box.setCurrentText('Foo.Bar')
        assert diff_items(page) == ['AT', 'IN', 'EZ'], diff_items(page)  # EZ 也能选到
        log = page.log_view.toPlainText()
        assert '解包完成' in log and '已提取 base.apk' in log, log
        assert os.path.isdir('Assets/APK/com.PigeonGames.Phigros')
        page.close()


def scenario_cancel():
    fresh_cwd()
    apk, obb = package(CHARTS, filler=2_000_000)
    dev = device(chunk=40_000, chunk_sleep=0.08)  # 慢到足够点取消
    dev.install({'base.apk': apk}, {'main.82.com.PigeonGames.Phigros.obb': obb})
    os.makedirs('Assets/Tracks', exist_ok=True)
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        page = new_page()
        page.apk_all_btn.click()
        pump(lambda: page.apk_progress.value() > 0 and page.apk_cancel_btn.isEnabled(), 30)
        assert not page.apk_all_btn.isEnabled(), '运行中其它按钮应禁用, 防止重复启动'
        page.apk_cancel_btn.click()
        assert not page.apk_cancel_btn.isEnabled()
        pump(lambda: not page._apk_busy(), 30)
        assert page.apk_status.text() == '已取消', page.apk_status.text()
        assert page.apk_all_btn.isEnabled() and not page.apk_cancel_btn.isEnabled()
        parts = [f for _r, _d, fs in os.walk('Assets/APK') for f in fs if f.endswith('.part')]
        assert parts == [], parts
        assert os.listdir('Assets/Tracks') == [], '取消后不应写出谱面'
        page.close()


def scenario_failure_turns_bar_red_and_recovers():
    fresh_cwd()
    device(devices=[])  # 没有设备
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        page = new_page()
        sample_run(page, page.apk_pull_btn.click)
        assert page.apk_status.text().startswith('失败:'), page.apk_status.text()
        assert '没有检测到安卓设备' in page.apk_status.text()
        assert page.apk_progress.isError(), '失败时进度条应变成错误态'
        assert page.apk_all_btn.isEnabled() and page.apk_pkg_edit.isEnabled()
        assert '提取/解包失败' in page.log_view.toPlainText()
        # 再来一次(这次有设备)必须能成功, 并且错误态要清掉
        apk, obb = package(CHARTS)
        dev = device()
        dev.install({'base.apk': apk}, {'main.obb': obb})
        sample_run(page, page.apk_pull_btn.click)
        assert not page.apk_progress.isError()
        assert page.apk_status.text().startswith('已提取 2 个文件'), page.apk_status.text()
        page.close()


def scenario_local_extract_and_plan_cache():
    fresh_cwd()
    apk, obb = package(CHARTS)
    base = os.path.join('Assets', 'APK', 'com.PigeonGames.Phigros')
    os.makedirs(base)
    open(os.path.join(base, 'base.apk'), 'wb').write(apk)
    open(os.path.join(base, 'main.obb'), 'wb').write(obb)
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        page = new_page()
        sample_run(page, page.apk_local_btn.click)
        assert page.apk_status.text().startswith('完成:'), page.apk_status.text()
        page.song_box.setCurrentText('Foo.Bar')
        page.diff_box.setCurrentText('IN')
        # 造一份旧规划缓存并载入, 然后用内容不同的新包再解包一次
        cache = os.path.join('Assets/Tracks/Foo.Bar/Chart_IN.ans.v12.json')
        open(cache, 'w').write('[]')
        page.song_selected('Foo.Bar')
        page.diff_box.setCurrentText('IN')
        changed = dict(CHARTS)
        changed['Assets/Tracks/Foo.Bar.0/Chart_IN.json'] = chart_json('in-v2')
        apk2, obb2 = package(changed)
        open(os.path.join(base, 'base.apk'), 'wb').write(apk2)
        open(os.path.join(base, 'main.obb'), 'wb').write(obb2)
        sample_run(page, page.apk_local_btn.click)
        assert not os.path.exists(cache), '谱面更新后旧规划缓存必须作废'
        assert page.plan_path is None and page._raw_ans is None, '界面里挂着的旧规划也要清掉'
        assert page.song_box.currentText() == 'Foo.Bar' and page.diff_box.currentText() == 'IN', '保留用户选的歌和难度'
        assert '重新规划' in page.log_view.toPlainText()
        page.close()


def scenario_no_local_files():
    fresh_cwd()
    device()
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        page = new_page()
        page.apk_local_btn.click()
        app.processEvents()
        assert not page._apk_busy(), '没有可解包的文件时不应启动任务'
        assert page.apk_all_btn.isEnabled()
        page.close()


def scenario_refused_while_playing():
    fresh_cwd()
    apk, obb = package(CHARTS)
    dev = device()
    dev.install({'base.apk': apk}, {'main.obb': obb})
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        page = new_page()
        sample_run(page, page.apk_all_btn.click)
        page.song_box.setCurrentText('Foo.Bar')
        page.go_btn.setEnabled(True)  # 假装正在演奏: 这个按钮是「停止演奏」
        pulls_before = len(dev.pulls())
        for flag in ('_running', '_vauto_waiting'):
            setattr(page, flag, True)
            for btn in (page.apk_all_btn, page.apk_pull_btn, page.apk_local_btn):
                btn.click()
                app.processEvents()
                assert not page._apk_busy(), f'{flag} 时不应启动提取/解包'
            assert len(dev.pulls()) == pulls_before, '演奏期间不能碰 adb'
            assert page.go_btn.isEnabled(), '停止按钮不能被动到'
            setattr(page, flag, False)
        # 演奏结束后恢复正常
        sample_run(page, page.apk_local_btn.click)
        assert page.apk_status.text().startswith('完成:'), page.apk_status.text()
        # 任务进行中的"完成"回调碰上演奏(理论上启动时已经拦住了, 这里直接喂结果验证兜底): 不刷新界面
        page.go_btn.setEnabled(True)
        page._running = True
        res = apk_tools.ExtractResult(written=1, overwritten=1, songs={'Foo.Bar'})
        page._apk_done(res)
        assert page.go_btn.isEnabled(), '演奏中收到完成回调不能禁用停止按钮'
        assert '暂不刷新' in page.log_view.toPlainText()
        page._running = False
        page.close()


def scenario_closing_the_window_stops_the_task():
    fresh_cwd()
    apk, obb = package(CHARTS, filler=2_000_000)
    dev = device(chunk=40_000, chunk_sleep=0.08)  # 慢到关窗口时还在传
    dev.install({'base.apk': apk}, {'main.82.com.PigeonGames.Phigros.obb': obb})
    with mock.patch('apk_tools.find_adb', return_value=EXE):
        win = main.Window()
        win.show()
        app.processEvents()
        page = win.main_page
        page.apk_all_btn.click()
        pump(lambda: page.apk_progress.value() > 0, 30)
        thread = page._apk_thread
        assert thread.isRunning()
        t0 = time.time()
        win.close()  # 子控件 MainPage 不会收到 closeEvent, 要靠 Window.closeEvent 转交
        assert not thread.isRunning(), '关主窗口后台线程必须已经结束, 否则退出时 QThread 被销毁会崩溃'
        assert time.time() - t0 < 6
        parts = [f for _r, _d, fs in os.walk('Assets/APK') for f in fs if f.endswith('.part')]
        assert parts == [], parts


for fn in (scenario_happy_path, scenario_cancel, scenario_failure_turns_bar_red_and_recovers,
           scenario_local_extract_and_plan_cache, scenario_no_local_files, scenario_refused_while_playing,
           scenario_closing_the_window_stops_the_task):
    try:
        fn()
        print('PASS', fn.__name__, flush=True)
    except BaseException:
        import traceback
        traceback.print_exc()
        print('FAIL', fn.__name__, flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
print('ALL OK', flush=True)
# 不走解释器的正常退出流程: 一堆顶层窗口按随机顺序析构, PyQt 偶尔会在退出时段错误,
# 那是测试脚本的收尾问题, 不是被测代码的问题, 不能让它把测试结果弄成失败
os._exit(0)
'''


class TestRuntimeSmoke(unittest.TestCase):
    def test_gui_flow_with_fake_adb(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, 'smoke.py')
            with open(script, 'w', encoding='utf-8') as f:
                f.write(SMOKE)
            env = dict(os.environ)
            env.setdefault('QT_QPA_PLATFORM', 'offscreen')
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            try:
                r = subprocess.run([sys.executable, script, ROOT, HERE], capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', env=env, timeout=240)
            except subprocess.TimeoutExpired as e:
                self.fail(f'界面冒烟测试超时\n{e.stdout}\n{e.stderr}')
        out = (r.stdout or '') + (r.stderr or '')
        if r.returncode == 77:
            self.skipTest('这个环境起不了离屏 Qt 界面: ' + out.strip().splitlines()[-1][:200])
        self.assertEqual(r.returncode, 0, out[-4000:])
        self.assertIn('ALL OK', r.stdout)
        for name in ('scenario_happy_path', 'scenario_cancel', 'scenario_failure_turns_bar_red_and_recovers',
                     'scenario_local_extract_and_plan_cache', 'scenario_no_local_files',
                     'scenario_refused_while_playing', 'scenario_closing_the_window_stops_the_task'):
            self.assertIn('PASS ' + name, r.stdout)


if __name__ == '__main__':
    unittest.main()
