'''谱面加载必须同时认官谱和 RPE 谱面。

用户实际遇到的崩溃: 选了一张 RPE 谱面点"开始生成规划", 直接
    KeyError: 'formatVersion'
因为 PlanThread.run 里是裸的 Chart.from_dict(json.load(f))。RPE 谱面没有
formatVersion(它靠 BPMList/META/judgeLineList 识别), 必须先经
rpe_to_official_v3 转成官方v3结构才能进 Chart.from_dict。

诡异的是 import_songs 那条"导入谱面"的路本来就有转换, 所以 RPE 谱面能导入、
却没法规划 —— 现在两条路都走同一个 load_chart_file。
'''
import ast
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _src():
    with io.open(os.path.join(ROOT, 'main.py'), encoding='utf-8') as f:
        return f.read()


def _function(name):
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'找不到模块级函数 {name}')


def _load_chart_file():
    """把 load_chart_file 从 main.py 里抽出来真跑(main.py 依赖 PyQt5, 整体导入不了)"""
    ns = {'json': json, 'os': os}
    from rpe import detect_kind, rpe_to_official_v3
    from chart import Chart
    ns['detect_kind'] = detect_kind
    ns['rpe_to_official_v3'] = rpe_to_official_v3
    ns['Chart'] = Chart
    src = ast.get_source_segment(_src(), _function('load_chart_file'))
    assert src and 'load_chart_file' in src
    exec(compile(src, 'main.py:load_chart_file', 'exec'), ns)
    return ns['load_chart_file']


OFFICIAL_V3 = {
    'formatVersion': 3,
    'offset': 0.0,
    'judgeLineList': [{
        'notesAbove': [], 'notesBelow': [], 'bpm': 120.0,
        'speedEvents': [{'startTime': 0.0, 'endTime': 1e9, 'value': 1.0}],
        'judgeLineDisappearEvents': [],
        'judgeLineMoveEvents': [],
        'judgeLineRotateEvents': [],
    }],
}

RPE_MIN = {
    'META': {'id': 'TestRpe', 'name': 'test', 'level': 'SP Lv.1', 'charter': 'x'},
    'BPMList': [{'startTime': [0, 0, 1], 'bpm': 120.0}],
    'judgeLineList': [],
}


class LoadChartFileTest(unittest.TestCase):
    """load_chart_file: 官谱和RPE都认, 认不出来给看得懂的报错"""

    def setUp(self):
        # 必须是实例属性: 存成类属性的话, 普通函数会变成绑定方法,
        # self.fn(p) 就等于 fn(self, p), 报"takes 1 positional argument but 2 were given"
        self.fn = _load_chart_file()
        self.tmp = tempfile.mkdtemp(prefix='phisap-chart-')

    def _write(self, obj, name='chart.json'):
        p = os.path.join(self.tmp, name)
        with io.open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
        return p

    def test_official_v3(self):
        p = self._write(OFFICIAL_V3, 'official.json')
        chart, warns = self.fn(p)
        self.assertEqual(len(chart.judge_lines), 1)
        self.assertEqual(warns, [])

    def test_rpe_chart_is_converted(self):
        """RPE 谱面以前在这里 KeyError: 'formatVersion'"""
        p = self._write(RPE_MIN, 'rpe.json')
        chart, warns = self.fn(p)
        # 转换结果是官方v3结构, 能被 Chart.from_dict 接受
        self.assertIsNotNone(chart)
        self.assertGreaterEqual(chart.version, 1)

    def test_unknown_format_raises_readable_error(self):
        p = self._write({'META': {'id': 'x'}}, 'broken.json')
        with self.assertRaises(ValueError) as cm:
            self.fn(p)
        msg = str(cm.exception)
        self.assertIn('broken.json', msg, '报错里要带文件名, 不然用户不知道是哪一张')
        self.assertIn('formatVersion', msg)

    def test_future_formatversion_is_rejected(self):
        d = dict(OFFICIAL_V3)
        d['formatVersion'] = 99
        p = self._write(d, 'future.json')
        with self.assertRaises(ValueError):
            self.fn(p)

    def test_no_keyerror_leaks(self):
        """裸 Chart.from_dict 对非官谱只会抛 KeyError, 用户完全看不懂"""
        p = self._write(RPE_MIN, 'rpe2.json')
        try:
            self.fn(p)
        except KeyError:
            self.fail('还在漏 KeyError, RPE 转换没有生效')
        except ValueError:
            pass


class CallSiteTest(unittest.TestCase):
    """两条读谱面的路都必须走 load_chart_file, 不许再裸奔"""

    def test_plan_thread_uses_loader(self):
        for node in ast.parse(_src()).body:
            if isinstance(node, ast.ClassDef) and node.name == 'PlanThread':
                for n in node.body:
                    if isinstance(n, ast.FunctionDef) and n.name == 'run':
                        seg = ast.dump(n)
                        self.assertIn('load_chart_file', seg,
                                      'PlanThread.run 没有用 load_chart_file')
                        self.assertNotIn('from_dict', seg,
                                         'PlanThread.run 还在直接调 Chart.from_dict')
                        return
        self.fail('找不到 PlanThread.run')

    def test_first_note_ms_uses_loader(self):
        seg = ast.dump(_function('first_note_ms_from_path'))
        self.assertIn('load_chart_file', seg,
                      'first_note_ms_from_path 没有用 load_chart_file')
        self.assertNotIn('from_dict', seg,
                         'first_note_ms_from_path 还在直接调 Chart.from_dict')

    def test_no_bare_chart_from_dict_left(self):
        """全文不该再有 Chart.from_dict(json.load(...)) 这种裸调用"""
        for node in ast.walk(ast.parse(_src())):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == 'from_dict':
                # load_chart_file 内部那一处是唯一允许的
                self.assertEqual(node.lineno, _function('load_chart_file').end_lineno,
                                 f'第{node.lineno}行还有一处裸的 from_dict')


if __name__ == '__main__':
    unittest.main()
