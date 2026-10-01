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
    """把 load_chart_file 从 main.py 里抽出来真跑(main.py 依赖 PyQt5, 整体导入不了)。

    它现在会调用 is_plan_cache, 所以那个函数和它用到的 _PLAN_CACHE_RE 也要一起抽出来,
    少一个就 NameError, 被它自己的 except 吞掉之后表现为"莫名返回0/莫名失败"。
    """
    ns = {'json': json, 'os': os, 're': __import__('re')}
    from rpe import detect_kind, rpe_to_official_v3
    from chart import Chart
    ns['detect_kind'] = detect_kind
    ns['rpe_to_official_v3'] = rpe_to_official_v3
    ns['Chart'] = Chart
    tree = ast.parse(_src())
    picked = [n for n in tree.body
              if (isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == '_PLAN_CACHE_RE' for t in n.targets))
              or (isinstance(n, ast.FunctionDef) and n.name in ('is_plan_cache', 'load_chart_file'))]
    self_names = [n.name for n in picked if isinstance(n, ast.FunctionDef)]
    assert sorted(self_names) == ['is_plan_cache', 'load_chart_file'], self_names
    exec(compile(ast.Module(body=picked, type_ignores=[]), '<main-extract>', 'exec'), ns)
    return ns['load_chart_file']


def _is_plan_cache():
    ns = {'os': os, 're': __import__('re')}
    node = next(n for n in ast.parse(_src()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'is_plan_cache')
    pat = next(n for n in ast.parse(_src()).body
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == '_PLAN_CACHE_RE' for t in n.targets))
    exec(compile(ast.Module(body=[pat, node], type_ignores=[]), '<main-extract>', 'exec'), ns)
    return ns['is_plan_cache']


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


class PlanCacheTest(unittest.TestCase):
    """规划缓存文件(*.ans.vN.json)不是谱面, 三处地方都必须认出来。

    用户实际踩的坑: 缓存版本号从 v9 升到 v10 之后, find_chart_path 里
    "PLAN_CACHE_SUFFIX not in f" 只能排除当前版本, 上一版留下的
    Chart_AT.ans.v9.json 不再被过滤, 被当成谱面选中去规划。
    """

    def test_pattern_matches_every_version(self):
        f = _is_plan_cache()
        for name in ('Chart_AT.ans.v9.json', 'Chart_AT.ans.v10.json',
                     'chart_in.ans.v11.json', 'x.ans.v1.json'):
            self.assertTrue(f(name), f'{name} 应该被认出是规划缓存')
        for name in ('Chart_AT.json', 'Chart.json', 'ans.v9.json',
                     'Chart_AT.json.bak', 'x.ans.json', 'Chart_AT.ans.json'):
            self.assertFalse(f(name), f'{name} 不该被误判成规划缓存')

    def test_load_chart_file_rejects_plan_cache(self):
        """读到的若是缓存文件, 报错必须说清楚"这是规划结果不是谱面" """
        fn = _load_chart_file()
        d = tempfile.mkdtemp(prefix='phisap-cache-')
        p = os.path.join(d, 'Chart_AT.ans.v9.json')
        with io.open(p, 'w', encoding='utf-8') as f:
            json.dump({'0': [{'pos': [1, 2], 'action': 0, 'pointer': 1000}]}, f)
        with self.assertRaises(ValueError) as cm:
            fn(p)
        msg = str(cm.exception)
        self.assertIn('Chart_AT.ans.v9.json', msg)
        self.assertIn('规划缓存', msg)

    def test_find_chart_path_skips_old_cache(self):
        """目录里同时有谱面和旧版缓存时, 必须选中谱面"""
        node = next(n for n in ast.parse(_src()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'find_chart_path')
        pat = next(n for n in ast.parse(_src()).body
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '_PLAN_CACHE_RE' for t in n.targets))
        fn_node = next(n for n in ast.parse(_src()).body
                       if isinstance(n, ast.FunctionDef) and n.name == 'is_plan_cache')
        ns = {'os': os, 're': __import__('re'), 'PLAN_CACHE_SUFFIX': '.ans.v10.json',
              'chart_difficulty': lambda f: 'AT' if f.lower().startswith('chart_at') else None}
        exec(compile(ast.Module(body=[pat, fn_node, node], type_ignores=[]),
                     '<main-extract>', 'exec'), ns)

        root = tempfile.mkdtemp(prefix='phisap-tracks-')
        cwd = os.getcwd()
        try:
            os.chdir(root)
            folder = os.path.join('./Assets/Tracks', 'SongA')
            os.makedirs(folder)
            # 旧版缓存排在前面的情况也要对
            for name in ('Chart_AT.ans.v9.json', 'Chart_AT.json', 'Chart_AT.ans.v10.json'):
                with io.open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                    f.write('{}')
            got = ns['find_chart_path']('SongA', 'AT')
            self.assertEqual(os.path.basename(got), 'Chart_AT.json',
                             f'选中了 {got}, 应该是 Chart_AT.json')
            # 目录里只剩缓存时要返回None, 而不是把缓存当谱面
            os.remove(os.path.join(folder, 'Chart_AT.json'))
            self.assertIsNone(ns['find_chart_path']('SongA', 'AT'))
        finally:
            os.chdir(cwd)

    def test_import_songs_guards_plan_cache(self):
        seg = ast.dump(next(n for n in ast.parse(_src()).body
                            if isinstance(n, ast.ClassDef) and n.name == 'MainPage'
                            for n2 in n.body
                            if isinstance(n2, ast.FunctionDef) and n2.name == 'import_songs'))
        self.assertIn('is_plan_cache', seg, 'import_songs 没有拦规划缓存文件')


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
