'''设置页面要能用手指拖动滚动(触屏电脑)。

用户报告(#34): "触屏电脑: 我能用手指滑动页面, 但程序不可以"。
确认过的含义是: 在触屏电脑上用手指划 phisap 自己的设置界面, 页面不跟随滚动。

原因: MainPage 是 QScrollArea(qfluentwidgets 的 ScrollArea), 而 QScrollArea
默认只认滚轮和滚动条, 不认"按住内容拖动"。修法是 QScroller.grabGesture 抓一个
TouchGesture, 不用自己写触摸逻辑。

注意两条约束:
  1. 只抓 TouchGesture。LeftMouseButtonGesture 会把左键按下事件延迟
     (QScrollerProperties::MousePressEventDelay 默认0.25秒), 页面上所有
     按钮/下拉框/数字框的点击都会变肉 —— 不能为了修滚动把点击体验搭进去。
  2. 整段必须包 try。某些 PyQt 构建/平台上没有 QScroller, 抓不到也不能让
     界面起不来。
'''
import ast
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name: str) -> str:
    with io.open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def _mainpage():
    for node in ast.parse(_src('main.py')).body:
        if isinstance(node, ast.ClassDef) and node.name == 'MainPage':
            return node
    raise AssertionError('找不到 MainPage')


def _method(name):
    for n in _mainpage().body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f'找不到 {name}')


def _attrs(node):
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


class TouchScrollTest(unittest.TestCase):
    '''MainPage 必须能被手指拖动滚动'''

    def test_method_exists(self):
        names = {n.name for n in _mainpage().body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_enable_touch_scroll', names, '缺少 _enable_touch_scroll')

    def test_build_calls_it(self):
        seg = ast.dump(_method('_build'))
        self.assertIn('_enable_touch_scroll', seg, '_build 没有开启触摸滚动')

    def test_uses_qscroller_touch_gesture(self):
        src = _src('main.py')
        self.assertIn('QScroller.grabGesture', src, '没有用 QScroller')
        self.assertIn('QScroller.TouchGesture', src, '没有抓 TouchGesture')

    def test_no_left_mouse_button_gesture(self):
        '''左键手势会延迟点击0.25秒, 不许抓。

        只查可执行代码(属性名/变量名), 不查整篇源码 —— 注释里本来就会提到
        "故意不抓 LeftMouseButtonGesture", 用字符串匹配会误伤自己。
        '''
        used = set()
        for n in ast.walk(_method('_enable_touch_scroll')):
            if isinstance(n, ast.Attribute):
                used.add(n.attr)
            elif isinstance(n, ast.Name):
                used.add(n.id)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                used.update(n.value.split())
        self.assertNotIn('LeftMouseButtonGesture', used,
                         '抓了左键手势, 页面上的点击会被延迟')

    def test_guarded_against_missing_qscroller(self):
        '''QScroller 在某些PyQt构建上不存在, 抓不到也不能让界面起不来'''
        handlers = [n for n in ast.walk(_method('_enable_touch_scroll'))
                    if isinstance(n, ast.ExceptHandler)]
        self.assertGreaterEqual(len(handlers), 2, '没有把导入/抓手势都包进 try')

    def test_mainpage_is_a_scroll_area(self):
        '''前提: MainPage 确实是可滚动的那一层, 且内容可自适应'''
        src = _src('main.py')
        self.assertIn('class MainPage(ScrollArea)', src, 'MainPage 不是 ScrollArea')
        self.assertIn('setWidgetResizable(True)', src, '内容没有设为可自适应')


if __name__ == '__main__':
    unittest.main()
