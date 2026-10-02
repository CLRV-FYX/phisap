"""iOS方案离线分析工具(tools/ios_sim.py)的冒烟测试: 表格结构对、辅助函数对、跑得通。
真正的数字(触点上限/时间容忍度等)见 docs/ios_feasibility.md 和 docs/ios_sim_results.md, 那些要在真实谱面上跑。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import ios_sim as S  # noqa: E402
from algo.algo_base import TouchAction, VirtualTouchEvent  # noqa: E402
from tests.test_algo import line_dict, note  # noqa: E402


def small_chart_json():
    """一条判定线: 蓝键/长条/黄键/红键各几个(bpm120, 时间都在 1~4 秒)"""
    notes = ([note(t, x, 1) for t, x in ((64, -3.0), (80, 0.0), (96, 3.0))] + [note(112, -2.0, 3, 24.0)] +
             [note(t, x, 2) for t, x in ((100, 1.0), (120, -1.0))] + [note(t, 0.0, 4) for t in (136, 150)])
    return {'formatVersion': 3, 'offset': 0.0, 'judgeLineList': [line_dict(notes)]}


class HelpersTest(unittest.TestCase):
    def test_layouts_leave_at_least_one_tap_hold_pointer(self):
        for cap in (2, 5, 8, 16):
            for k, n in S.layouts_for(cap):
                self.assertGreaterEqual(cap - k - n, 1)
        self.assertEqual(S.layouts_for(0), [])
        self.assertEqual(S.layouts_for(1), [(0, 0)])           # 只有1个触点: 全给tap/hold
        self.assertIn((1, 0), S.layouts_for(5))
        self.assertNotIn((2, 3), S.layouts_for(5))           # 2+3=5, 没给tap/hold留触点
        self.assertTrue(set(S.layouts_for(8, quick=True)) < set(S.layouts_for(8)))

    def test_jitter_plan_keeps_event_count_and_per_pointer_order(self):
        ans = defaultdict(list)
        for i in range(200):
            ans[i * 3].append(VirtualTouchEvent((float(i), 0.0), TouchAction.DOWN if i % 2 == 0 else TouchAction.UP, 7))
        out = S.jitter_plan(ans, sd=30.0, offset=-10)
        self.assertEqual(sum(len(v) for v in out.values()), 200)
        seq = [e.action for t in sorted(out) for e in out[t]]
        self.assertEqual(seq, [TouchAction.DOWN if i % 2 == 0 else TouchAction.UP for i in range(200)])
        self.assertEqual(S.jitter_plan(ans, 0.0, 0).keys(), ans.keys())      # σ=0, 偏移0: 原样

    def test_formatting(self):
        self.assertEqual(S.cell({'notes': 200, 'perfect': 199, 'badmiss': 1, 'good': 0}), '99.5% (1)')
        self.assertEqual(S.cell({}), 'n/a')
        self.assertEqual(S.table(['a', 'b'], [['1', '2']]), '| a | b |\n|---|---|\n| 1 | 2 |')
        self.assertEqual(S.add({'x': 1}, {'x': 2, 'y': 3}), {'x': 3, 'y': 3})


class CommandsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, 'small.json')
        with open(cls.path, 'w', encoding='utf-8') as f:
            json.dump(small_chart_json(), f)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            S.main(list(argv))
        return buf.getvalue()

    def test_timing_table_has_a_row_per_offset(self):
        out = self.run_cli('timing', self.path, '--pointers', '10', '--offsets=-20,0,40', '--jitters', '0,20')
        self.assertIn('| -20 |', out)
        self.assertIn('| +0 |', out)
        self.assertIn('| +40 |', out)
        self.assertIn('±10ms', out)
        self.assertIn('100.0%', out)               # 这张小谱面在小偏差下全中

    def test_timing_with_explicit_layout(self):
        out = self.run_cli('timing', self.path, '--pointers', '5', '--layout', '1,0', '--offsets', '0', '--jitters', '0')
        self.assertIn('扫屏1个+滑键0个', out)

    def test_jitter_table(self):
        out = self.run_cli('jitter', self.path, '--sigmas', '0,10', '--offset=-5')
        self.assertIn('| 0 |', out)
        self.assertIn('| 10 |', out)
        self.assertIn('-5ms', out)

    def test_caps_table_and_cap_is_respected(self):
        out = self.run_cli('caps', self.path, '--caps', '5,16', '--quick')
        self.assertIn('5指', out)
        self.assertIn('16指', out)
        self.assertIn('合计', out)
        self.assertNotIn('!峰值', out)             # 规划的同时触点峰值不超过上限

    def test_floor_table(self):
        out = self.run_cli('floor', self.path, '--floors', '5,80', '--caps', '5', '--quick')
        self.assertIn('5ms (现在)', out)
        self.assertIn('80ms', out)
        import algo.algo2 as algo2
        self.assertEqual(algo2.MAX_RELEASE_MS, 5)  # 用完恢复, 不污染别的测试

    def test_payload_table(self):
        out = self.run_cli('payload', self.path, '--pointers', '10')
        self.assertIn('actions条目', out)
        self.assertIn('MB', out)


if __name__ == '__main__':
    unittest.main()
