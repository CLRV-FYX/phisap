'''主界面分标签页(用户要求"现在所有功能都在主界面, 多分几个标签页方便使用")。

以前曲目/在线下载/规划/设备/设置/演奏/日志全部堆在一个滚动页面里, 改个设置要滚很久。
现在用 qfluentwidgets 的 Pivot(分段导航) + QStackedWidget 分成4页:
曲目 / 规划与设备 / 演奏 / 日志, 每页内部照样可以滚动。

这些测试盯三件容易在后续改动中被破坏的事:
  1. 卡片必须挂在某一页上, 不能又被丢回根布局(一分心就全堆回去了)
  2. 跨线程信号连接不能被重构弄丢(丢了就是"停止演奏"按钮再也回不来)
  3. 不许出现 3 参数的 addLayout(layout, stretch, alignment) —— Qt5/QFluent 只收2个,
     一写上去启动就崩
'''
import ast
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GROUPS = ('song_group', 'plan_group', 'dev_group', 'dl_group', 'setting_group', 'play_group')
PAGES = ('p_song', 'p_plan', 'p_play', 'p_log')
SIGNALS = ('playback_finished.connect(self._reset_go)',
           'vauto_abort.connect(self._vauto_abort)',
           'vauto_launch.connect(self._vauto_launch)')


def _src():
    with io.open(os.path.join(ROOT, 'main.py'), encoding='utf-8') as f:
        return f.read()


def _build():
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.ClassDef) and node.name == 'MainPage':
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name == '_build':
                    return n
    raise AssertionError('找不到 MainPage._build')


def _addwidget_targets():
    """{被添加的对象名: 加到哪个布局上}"""
    out = {}
    for n in ast.walk(_build()):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == 'addWidget'):
            continue
        if not isinstance(f.value, ast.Name):
            continue
        arg = n.args[0] if n.args else None
        name = None
        if isinstance(arg, ast.Name):
            name = arg.id
        elif isinstance(arg, ast.Attribute):
            name = arg.attr
        out[name] = f.value.id
    return out


class TabsTest(unittest.TestCase):
    def test_pivot_and_stack_imported(self):
        src = _src()
        self.assertIn('Pivot', src, '没有导入/使用 Pivot')
        self.assertIn('QStackedWidget', src, '没有导入 QStackedWidget')

    def test_build_creates_pivot_and_stack(self):
        seg = ast.dump(_build())
        self.assertIn('pivot', seg, '_build 没有创建 Pivot')
        self.assertIn('stack', seg, '_build 没有创建 QStackedWidget')

    def test_every_group_is_on_a_tab_page(self):
        t = _addwidget_targets()
        for g in GROUPS:
            self.assertIn(g, t, f'{g} 没有出现在 _build 里')
            self.assertIn(t[g], PAGES, f'{g} 挂到了 {t[g]} 上, 不在任何标签页里')

    def test_root_layout_only_gets_title_pivot_stack(self):
        """根布局只应有标题、Pivot、QStackedWidget —— 卡片不能再堆回去"""
        t = _addwidget_targets()
        on_root = [k for k, v in t.items() if v == 'outer']
        self.assertEqual(sorted(str(k) for k in on_root), ['pivot', 'stack'],
                         f'根布局上还挂着别的东西: {on_root}')

    def test_four_tabs_defined(self):
        seg = ast.dump(_build())
        for key in ('song', 'plan', 'play', 'log'):
            self.assertIn(key, seg, f'缺少标签页 {key}')

    def test_signal_connections_survived(self):
        src = _src()
        for sig in SIGNALS:
            self.assertIn(sig, src, f'跨线程信号连接丢了: {sig}')

    def test_no_three_arg_addlayout(self):
        """长期约束: addLayout(layout, stretch, alignment) 在 Qt5/QFluent 只收2个参数"""
        for node in ast.walk(ast.parse(_src())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == 'addLayout' and len(node.args) >= 3:
                self.fail(f'第{node.lineno}行 addLayout 用了{len(node.args)}个参数')

    def test_each_page_gets_a_stretch(self):
        seg = ast.dump(_build())
        self.assertIn('tab_layouts', seg, '没有遍历标签页补伸缩')


if __name__ == '__main__':
    unittest.main()
