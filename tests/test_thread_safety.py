'''"停止演奏失效"回归测试(沙箱没有PyQt, 用AST做静态断言)

事故回放: 播放线程在 finally 里用
    QMetaObject.invokeMethod(self, '_reset_go', Qt.QueuedConnection)
复位按钮。PyQt的 invokeMethod 只能在元对象里找方法, 而普通Python方法没有注册进
元对象, 于是抛 "No such method MainPage::_reset_go()", 线程直接死掉:
  - _reset_go 永远不执行 -> 按钮永远显示"停止演奏"且仍连着 _stop
  - _running 永远为True
  - 用户点停止只会再打一次"正在停止", 界面卡死, 再也停不下来

正确的跨线程调GUI方式是 pyqtSignal + emit。这些测试守住这个约定。
'''
from __future__ import annotations

import ast
import io
import os
import unittest

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'main.py')


def _src() -> str:
    with io.open(MAIN, encoding='utf-8') as f:
        return f.read()


def _class_node(name: str) -> ast.ClassDef:
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f'找不到类 {name}')


def _func(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f'{cls.name} 里找不到方法 {name}')


def _calls(fn: ast.FunctionDef) -> set[str]:
    '''收集函数体内直接调用的 self.xxx() 以及 self.xxx.emit() 里的 xxx'''
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            val = node.func.value
            if isinstance(val, ast.Name) and val.id == 'self':
                out.add(node.func.attr)
            elif (isinstance(val, ast.Attribute)
                  and isinstance(val.value, ast.Name) and val.value.id == 'self'):
                # self.<signal>.emit(...) -> 记录 <signal>
                out.add(val.attr)
    return out


class NoInvokeMethodTest(unittest.TestCase):
    '''绝对不能再出现 invokeMethod 调普通Python方法'''

    def test_no_invoke_method_call(self):
        src = _src()
        # 只在注释里允许出现这个词
        code_lines = [l for l in src.splitlines()
                      if 'invokeMethod' in l and not l.lstrip().startswith('#')]
        # 注释掉的说明文字里也会带, 过滤掉纯注释行后不应再有真实调用
        real = [l for l in code_lines if 'QMetaObject.invokeMethod(' not in l or '#' in l.split('QMetaObject')[0]]
        self.assertEqual(real, [], f'仍有 invokeMethod 调用: {real}')

    def test_no_qmetaobject_import_in_threads(self):
        src = _src()
        self.assertNotIn('QMetaObject', src.replace('QMetaObject.invokeMethod', ''))


class MainPageSignalsTest(unittest.TestCase):
    '''MainPage 必须有跨线程信号, 且连到对应槽'''

    def test_required_signals_declared(self):
        cls = _class_node('MainPage')
        names = {n.targets[0].id for n in cls.body
                 if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                 and isinstance(n.value, ast.Call)
                 and getattr(n.value.func, 'id', '') == 'pyqtSignal'}
        for need in ('playback_finished', 'vauto_abort', 'vauto_launch', 'log_line'):
            self.assertIn(need, names, f'MainPage 缺少信号 {need}')

    def test_signals_connected_in_build(self):
        cls = _class_node('MainPage')
        build = _func(cls, '_build')
        src_seg = ast.dump(build)
        for sig, slot in (('playback_finished', '_reset_go'),
                          ('vauto_abort', '_vauto_abort'),
                          ('vauto_launch', '_vauto_launch')):
            self.assertIn(f"'{sig}'", src_seg, f'{sig} 没有在 _build 里 connect')
            self.assertIn(f"'{slot}'", src_seg, f'{slot} 没有在 _build 里 connect')

    def test_log_line_connected(self):
        cls = _class_node('MainPage')
        init = _func(cls, '__init__')
        seg = ast.dump(init)
        self.assertIn("'log_line'", seg)
        self.assertIn("'_append_log'", seg)


class StopRobustnessTest(unittest.TestCase):
    '''停止必须可靠: 手动停止立即复位 + _reset_go 幂等'''

    def test_stop_resets_ui_immediately(self):
        cls = _class_node('MainPage')
        stop = _func(cls, '_stop')
        self.assertIn('_reset_go', _calls(stop),
                      '_stop 必须立即复位按钮, 不能只等播放线程的信号'
                      '(播放线程可能阻塞在socket上, 它的信号永远发不出来)')

    def test_reset_go_is_idempotent(self):
        '''_reset_go 会被调用两次(手动停止一次 + 播放结束信号一次),
        不幂等就会把 clicked 连上两份 run, 点一下开始两次。
        校验: 必须存在 go_btn.text()=='开始演奏' 的判断, 且 connect(self.run)
        位于以该判断结果为条件的 if 块内部。'''
        cls = _class_node('MainPage')
        reset = _func(cls, '_reset_go')

        # 1) 找出 "按钮文字 == 开始演奏" 的判断, 记下它被赋给的变量名
        guard_vars = set()
        for node in ast.walk(reset):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Compare):
                cmp_ = node.value
                is_text_cmp = (
                    isinstance(cmp_.left, ast.Call)
                    and isinstance(cmp_.left.func, ast.Attribute)
                    and cmp_.left.func.attr == 'text'
                    and isinstance(cmp_.left.func.value, ast.Attribute)
                    and cmp_.left.func.value.attr == 'go_btn'
                    and any(isinstance(c, ast.Constant) and c.value == '开始演奏'
                            for c in cmp_.comparators)
                )
                if is_text_cmp:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            guard_vars.add(t.id)
        self.assertTrue(guard_vars, '_reset_go 里没有 go_btn.text()=="开始演奏" 的判断')

        # 2) connect(self.run) 必须在这个判断的 if 块里
        def uses_guard(test) -> bool:
            return any(isinstance(n, ast.Name) and n.id in guard_vars
                       for n in ast.walk(test))

        guarded = []
        for node in ast.walk(reset):
            if not isinstance(node, ast.If) or not uses_guard(node.test):
                continue
            for n in ast.walk(node):
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'connect'
                        and isinstance(n.func.value, ast.Attribute)
                        and n.func.value.attr == 'clicked'
                        and any(isinstance(a, ast.Attribute) and a.attr == 'run' for a in n.args)):
                    guarded.append(node)
        self.assertTrue(guarded,
                        'connect(self.run) 没有被"按钮已复位"判断包住, 重复调用会连两份')

    def test_worker_uses_signal_not_direct_gui(self):
        '''播放线程只能通过信号通知GUI'''
        cls = _class_node('MainPage')
        start = _func(cls, '_start_playback')
        # 找嵌套的 worker
        worker = None
        for node in ast.walk(start):
            if isinstance(node, ast.FunctionDef) and node.name == 'worker':
                worker = node
                break
        self.assertIsNotNone(worker, '找不到 worker 线程函数')
        calls = _calls(worker)
        self.assertIn('playback_finished', calls or set(),
                      'worker 必须 emit playback_finished')
        # worker 里不允许直接碰按钮/同步按钮
        for bad in ('go_btn', 'sync_btn', 'log_view', 'setText', 'setEnabled'):
            self.assertNotIn(bad, calls or set(),
                             f'worker 里直接操作了 {bad}, 这是跨线程碰GUI')


class LogThreadSafetyTest(unittest.TestCase):
    def test_log_goes_through_signal(self):
        cls = _class_node('MainPage')
        log = _func(cls, 'log')
        self.assertIn('log_line', _calls(log), 'log() 必须走信号, 否则跨线程写日志不安全')

    def test_append_log_touches_widget(self):
        cls = _class_node('MainPage')
        self.assertTrue(hasattr(cls, 'body'))
        names = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_append_log', names, '缺少真正刷新控件的 _append_log 槽')


if __name__ == '__main__':
    unittest.main()
