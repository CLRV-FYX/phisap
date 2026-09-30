'''交叉校验: main.py 调用 run_player 时传的关键字参数, player.py 必须都支持

这是上面那个 TypeError 的根因防护。静态AST检查只看了 main.py 自己,
没发现"main.py 传了 should_continue 而 player.py 没实现"——
因为问题出在**两个文件不同步**, 单看任一个文件都发现不了。
'''
from __future__ import annotations

import ast
import inspect
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _main_src() -> str:
    with io.open(os.path.join(ROOT, 'main.py'), encoding='utf-8') as f:
        return f.read()


class RunPlayerCallSiteTest(unittest.TestCase):
    def _run_player_calls(self):
        '''找出 main.py 里所有 run_player(...) 调用, 返回 [(行号, {关键字参数名})]'''
        tree = ast.parse(_main_src())
        out = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != 'run_player':
                continue
            kw = {k.arg for k in node.keywords if k.arg is not None}
            out.append((node.lineno, kw))
        return out

    def test_call_sites_exist(self):
        calls = self._run_player_calls()
        self.assertTrue(calls, 'main.py 里没找到 run_player 调用')

    def test_every_kwarg_is_supported(self):
        '''关键: 调用方传的每个关键字参数, run_player 必须真的接受'''
        from player import run_player
        params = set(inspect.signature(run_player).parameters)
        calls = self._run_player_calls()
        bad = []
        for lineno, kw in calls:
            missing = kw - params
            if missing:
                bad.append(f'main.py:{lineno} 传了 {sorted(missing)}, 但 run_player 不接收')
        self.assertEqual(bad, [], '调用方与实现不同步:\n  ' + '\n  '.join(bad))

    def test_positional_args_also_ok(self):
        '''位置参数个数不能超过形参个数(顺序也得对)'''
        from player import run_player
        params = list(inspect.signature(run_player).parameters)
        tree = ast.parse(_main_src())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'run_player'):
                n_pos = len(node.args)
                self.assertLessEqual(n_pos, len(params),
                                     f'main.py:{node.lineno} 传了{n_pos}个位置参数, '
                                     f'run_player 只有{len(params)}个形参')

    def test_manual_path_passes_should_continue(self):
        '''manual/prestarted 路径(手指已按在屏幕上)也必须能立刻停"""
        '''
        calls = self._run_player_calls()
        with_kw = [(ln, kw) for ln, kw in calls if 'should_continue' in kw]
        self.assertTrue(with_kw, '没有任何 run_player 调用传 should_continue, 停止时会停不干净')
        # main.py 里有两条路径(manual/prestarted 和 普通), 两条都要能停
        self.assertEqual(len(calls), len(with_kw),
                         '有 run_player 调用没传 should_continue, 那条路径停止时停不干净')


if __name__ == '__main__':
    unittest.main()
