"""iOS方案的离线分析: 不需要iPhone, 用判定模拟器回答"WDA批量回放要多准、触点上限多少才够用"。

背景见 docs/ios_feasibility.md。Android后端是电脑边播边发, iOS(WDA)只能把整首歌排好一次性交给设备回放,
所以准确度由三件事决定, 这里逐个量化(判定规则: Phigros/Phira, 60fps, 随机帧相位):

  timing  起点偏差 x 回放抖动: 设备端回放的时间偏移(整首歌一个常数, 同步误差) 和 每个事件的随机抖动
          各自能容忍多大。偏差/抖动列出来的是"Perfect占比 (漏+Bad个数)"。
  jitter  回放抖动: 校准好整体偏移之后, 每个事件各自按高斯分布(σ)随机延迟, 同一触点内事件的先后顺序不变,
          看σ多大开始掉Perfect —— 这是设备端回放精度的要求。
  caps    同时触点上限(iPhone系统上限据说是5, iPad约11, 对注入的触点是否同样生效需真机确认):
          对每个上限搜索最合适的布局(扫屏触点数k, 滑键触点数n, 其余触点处理tap/hold), 给出最好的结果。
  floor   最短接触时间: iOS可能丢弃过短的接触(SideTap作者提到), 若每次tap必须按住T毫秒才可靠,
          触点占用时间变长, 密集处触点更不够用 —— 看T=5(现在)/40/80/125ms时结果掉多少。
  payload 一首歌转成W3C actions有多大(能不能一次请求发完): 触点数、条目数、JSON体积。

用法:
    python tools/ios_sim.py timing <谱面.json>... [--pointers 10] [--layout k,n] [--jobs 2]
                                                  [--offsets=-100,-80,...,100] [--jitters 0,20,40,60,80]
    python tools/ios_sim.py jitter <谱面.json>... [--sigmas 0,5,10,15,20,30,40] [--offset=-10] [--pointers 10]
                                                  [--layout k,n] [--jobs 2]
    python tools/ios_sim.py caps   <谱面.json>... [--caps 5,6,8,10,16] [--quick] [--jobs 2]
    python tools/ios_sim.py floor  <谱面.json>... [--floors 5,40,80,125] [--caps 5,10] [--jobs 2]
    python tools/ios_sim.py payload <谱面.json>... [--pointers 10] [--tolerance 3]
谱面可以是官方格式或RPE格式。输出是markdown表格, 末尾的汇总行把所有谱面合在一起算。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from rich.console import Console  # noqa: E402

import algo.algo2 as algo2  # noqa: E402
import algo.algo3 as algo3  # noqa: E402
import algo.algo3f as algo3f  # noqa: E402
from algo.ios_actions import build_actions, count_items, fit_mapping, peak_pointers, split_lives  # noqa: E402
from chart import Chart  # noqa: E402
from judge_sim import simulate  # noqa: E402
from rpe import detect_kind, rpe_to_official_v3  # noqa: E402

KINDS = ('tap', 'drag', 'hold', 'flick')


def load_chart(path: str) -> Chart:
    with open(path, encoding='utf-8-sig') as f:
        d = json.load(f)
    if detect_kind(d) == 'rpe':
        d, _ = rpe_to_official_v3(d)
    return Chart.from_dict(d)


def chart_name(path: str) -> str:
    base = os.path.basename(os.path.dirname(os.path.abspath(path)))
    name = os.path.splitext(os.path.basename(path))[0]
    return f'{base.rsplit(".", 1)[0]} {name}' if base and base not in ('.', '') else name


def quiet() -> Console:
    return Console(file=io.StringIO(), width=200)


def shifted(ans: dict, d: int) -> dict:
    out = defaultdict(list)
    for ms, evs in ans.items():
        out[ms + d].extend(evs)
    return out


def score(result: dict) -> dict:
    """{'notes', 'perfect', 'badmiss', 'good'}"""
    notes = sum(sum(result[k].values()) for k in KINDS)
    return {'notes': notes,
            'perfect': sum(result[k]['perfect'] for k in KINDS),
            'good': sum(result[k]['good'] for k in KINDS),
            'badmiss': sum(result[k]['bad'] + result[k]['miss'] for k in KINDS)}


def add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def cell(s: dict) -> str:
    if not s or not s['notes']:
        return 'n/a'
    return f"{100 * s['perfect'] / s['notes']:.1f}% ({s['badmiss']})"


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---'] * len(headers)) + '|']
    lines += ['| ' + ' | '.join(r) + ' |' for r in rows]
    return '\n'.join(lines)


# ---------------------------------------------------------------- 布局: 扫屏触点数k / 滑键触点数n / 其余给tap+hold

def layouts_for(cap: int, quick: bool = False) -> list[tuple[int, int]]:
    ks, ns = ((1, 2), range(0, 4)) if quick else ((0, 1, 2), range(0, 5))
    return [(k, n) for k in ks for n in ns if cap - k - n >= 1]


def plan_layout(chart: Chart, cap: int, k: int, n: int):
    """和 algo3.solve_with 一样的拼装, 但扫屏/滑键触点个数由调用方指定。放不下返回 None。
    返回 (规划, tap/hold丢掉的音符数)"""
    console = quiet()
    sweepers = algo3f.axis_sweepers()[:k]
    rest = cap - len(sweepers) - n
    if rest < 1:
        return None
    stats: dict = {}
    ans = algo2.solve(algo3._without_sweep_notes(chart), console, rest, stats=stats, warn_pause=False)
    if sweepers:
        intervals = algo3.whole_song_interval(chart, algo3f.START_BEFORE, algo3f.END_AFTER)
        for ms, evs in algo3.plan_sweepers(chart, sweepers, intervals, console).items():
            ans[ms].extend(evs)
    if n:
        for ms, evs in algo3.plan_flick_fingers(chart, n, console)[0].items():
            ans[ms].extend(evs)
    return ans, stats['dropped']


def best_layout(chart: Chart, cap: int, quick: bool = False, seed: int = 1):
    """搜索布局, 返回 (score, (k, n), 规划); 一个都放不下返回 None"""
    best = None
    for k, n in layouts_for(cap, quick):
        res = plan_layout(chart, cap, k, n)
        if res is None:
            continue
        ans, _ = res
        sc = score(simulate(chart, ans, fps=60, seed=seed, phigros=True))
        key = (sc['perfect'], -sc['badmiss'])
        if best is None or key > best[0]:
            best = (key, sc, (k, n), ans)
    return None if best is None else (best[1], best[2], best[3])


# ---------------------------------------------------------------- 各命令的单谱面工作函数(给进程池用)

def work_timing(args):
    path, pointers, offsets, jitters, layout = args
    chart = load_chart(path)
    if layout is None:
        ans = algo3f.solve(chart, quiet(), pointers)
    else:
        res = plan_layout(chart, pointers, *layout)
        if res is None:
            raise SystemExit(f'布局 k={layout[0]} n={layout[1]} 在 {pointers} 个触点里放不下')
        ans = res[0]
    out = {}
    for j in jitters:
        for off in offsets:
            # jitter_ms=J: 每批事件额外延迟 U[0, J], 均值J/2; 先整体提前J/2, 等价于 偏差off + 抖动±J/2
            plan = shifted(ans, off - j // 2)
            out[(off, j)] = score(simulate(chart, plan, fps=60, seed=1, phigros=True, jitter_ms=float(j)))
    return path, out


def jitter_plan(ans: dict, sd: float, offset: int, seed: int = 1) -> dict:
    """每个事件独立地多延迟 N(0, sd) ms; 同一个触点内的事件保持原来的先后顺序(设备按时间线回放, 不会乱序)"""
    rng = random.Random(seed)
    last: dict = {}
    out = defaultdict(list)
    for ms in sorted(ans):
        for e in ans[ms]:
            t = max(ms + offset + round(rng.gauss(0, sd)), last.get(e.pointer, -10 ** 9))
            last[e.pointer] = t
            out[t].append(e)
    return out


def work_jitter(args):
    path, pointers, sigmas, offset, layout = args
    chart = load_chart(path)
    if layout is None:
        ans = algo3f.solve(chart, quiet(), pointers)
    else:
        ans = plan_layout(chart, pointers, *layout)[0]
    return path, {sd: score(simulate(chart, jitter_plan(ans, sd, offset), fps=60, seed=1, phigros=True))
                  for sd in sigmas}


def work_caps(args):
    path, caps, quick = args
    chart = load_chart(path)
    n_notes = sum(len(line.notes) for line in chart.judge_lines)
    out = {}
    for cap in caps:
        if cap >= 16:
            ans = algo3f.solve(chart, quiet(), cap)
            sc = score(simulate(chart, ans, fps=60, seed=1, phigros=True))
            out[cap] = (sc, ('默认', ''), peak_pointers(split_lives(ans)))
            continue
        res = best_layout(chart, cap, quick)
        if res is None:
            out[cap] = None
            continue
        sc, layout, ans = res
        out[cap] = (sc, layout, peak_pointers(split_lives(ans)))
    return path, n_notes, out


def work_floor(args):
    path, floors, caps, quick = args
    chart = load_chart(path)
    out = {}
    saved = algo2.MAX_RELEASE_MS
    try:
        for floor in floors:
            algo2.MAX_RELEASE_MS = floor        # tap 按下后 floor ms 才抬起(触点占用变长)
            for cap in caps:
                res = best_layout(chart, cap, quick)
                out[(floor, cap)] = None if res is None else (res[0], res[1])
    finally:
        algo2.MAX_RELEASE_MS = saved
    return path, out


def work_payload(args):
    path, pointers, tolerance = args
    chart = load_chart(path)
    ans = algo3f.solve(chart, quiet(), pointers)
    events = sum(len(v) for v in ans.values())
    lives = split_lives(ans)
    raw_points = sum(len(lf.points) for lf in lives)
    t = time.perf_counter()
    body = build_actions(ans, fit_mapping(852.0, 393.0), tolerance=tolerance)
    build_s = time.perf_counter() - t
    text = json.dumps(body, separators=(',', ':'))
    span = (max(ans) - min(ans)) / 1000 if ans else 0.0
    return path, {'events': events, 'lives': len(lives), 'raw_points': raw_points, 'items': count_items(body),
                  'bytes': len(text), 'peak': peak_pointers(lives), 'seconds': span, 'build_s': build_s}


def run_pool(fn, jobs: list, workers: int):
    if workers <= 1:
        return [fn(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, jobs))


# ---------------------------------------------------------------- 命令

def cmd_timing(a) -> None:
    offsets = [int(x) for x in a.offsets.split(',')]
    jitters = [int(x) for x in a.jitters.split(',')]
    layout = None if a.layout is None else tuple(int(x) for x in a.layout.split(','))
    results = run_pool(work_timing, [(p, a.pointers, offsets, jitters, layout) for p in a.charts], a.jobs)
    total = {}
    for _, out in results:
        for key, sc in out.items():
            total[key] = add(total.get(key, {}), sc)
    what = 'algo3f' if layout is None else f'扫屏{layout[0]}个+滑键{layout[1]}个'
    print(f'### 起点偏差 x 回放抖动 ({what}, {a.pointers}触点, {len(a.charts)}张谱面合计, '
          f'格内: Perfect占比 (Bad+Miss个数))\n')
    print('偏差>0 = 设备端回放比计划晚; 抖动±J = 每个事件额外随机提前/推迟 J/2 以内(均匀分布)。\n')
    print(table(['偏差ms \\ 抖动'] + [f'±{j // 2}ms' for j in jitters],
                [[f'{off:+d}'] + [cell(total[(off, j)]) for j in jitters] for off in offsets]))
    print()
    for path, out in results:
        worst = min(out.items(), key=lambda kv: kv[1]['perfect'] / max(kv[1]['notes'], 1))
        print(f'- {chart_name(path)}: 最差格 偏差{worst[0][0]:+d}ms 抖动±{worst[0][1] // 2}ms -> {cell(worst[1])}')


def cmd_jitter(a) -> None:
    sigmas = [float(x) for x in a.sigmas.split(',')]
    layout = None if a.layout is None else tuple(int(x) for x in a.layout.split(','))
    results = run_pool(work_jitter, [(p, a.pointers, sigmas, a.offset, layout) for p in a.charts], a.jobs)
    total = {}
    for _, out in results:
        for sd, sc in out.items():
            total[sd] = add(total.get(sd, {}), sc)
    what = 'algo3f' if layout is None else f'扫屏{layout[0]}个+滑键{layout[1]}个'
    print(f'### 回放抖动 σ (整体偏移已校准为{a.offset:+d}ms, {what}, {a.pointers}触点, {len(a.charts)}张谱面合计)\n')
    print('每个事件独立延迟 N(0, σ) ms, 同一触点内的事件顺序不变。格内: Perfect占比 (Bad+Miss个数) / Good个数。\n')
    print(table(['σ(ms)', 'Perfect占比 (Bad+Miss)', 'Good'],
                [[f'{sd:g}', cell(total[sd]), str(total[sd]['good'])] for sd in sigmas]))


def cmd_caps(a) -> None:
    caps = [int(x) for x in a.caps.split(',')]
    results = run_pool(work_caps, [(p, caps, a.quick) for p in a.charts], a.jobs)
    rows, total = [], {}
    for path, n_notes, out in results:
        row = [chart_name(path), str(n_notes)]
        for cap in caps:
            r = out[cap]
            if r is None:
                row.append('n/a')
                continue
            sc, (k, n), peak = r
            total[cap] = add(total.get(cap, {}), sc)
            row.append(f'{cell(sc)} k{k}n{n}' + ('' if peak <= cap else f' !峰值{peak}'))
        rows.append(row)
    rows.append(['合计', str(sum(r[1] for r in results))] + [cell(total.get(cap)) for cap in caps])
    print(f'### 同时触点上限 (最优布局; 格内: Perfect占比 (Bad+Miss个数) kK=扫屏触点数 nN=滑键触点数)\n')
    print(table(['谱面', '音符数'] + [f'{c}指' for c in caps], rows))


def cmd_floor(a) -> None:
    floors = [int(x) for x in a.floors.split(',')]
    caps = [int(x) for x in a.caps.split(',')]
    results = run_pool(work_floor, [(p, floors, caps, a.quick) for p in a.charts], a.jobs)
    total = {}
    for _, out in results:
        for key, r in out.items():
            if r is not None:
                total[key] = add(total.get(key, {}), r[0])
    print('### 最短接触时间 (每次tap至少按住T毫秒; 格内: Perfect占比 (Bad+Miss个数), 最优布局)\n')
    print(table(['T \\ 触点上限'] + [f'{c}指' for c in caps],
                [[f'{f}ms' + (' (现在)' if f == 5 else '')] + [cell(total.get((f, c))) for c in caps] for f in floors]))


def cmd_payload(a) -> None:
    results = run_pool(work_payload, [(p, a.pointers, a.tolerance) for p in a.charts], a.jobs)
    rows = [[chart_name(p), f"{r['seconds']:.0f}s", str(r['events']), str(r['lives']), str(r['raw_points']),
             str(r['items']), f"{r['bytes'] / 1e6:.2f}MB", str(r['peak']), f"{r['build_s']:.2f}s"]
            for p, r in results]
    print(f'### 一首歌转成W3C actions的规模 (algo3f, {a.pointers}触点, 轨迹精简误差{a.tolerance}px)\n')
    print(table(['谱面', '时长', '规划事件', '按下次数(=输入源数)', '精简前的点', 'actions条目', 'JSON体积', '同时触点峰值',
                 '转换耗时'], rows))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('timing', 'jitter', 'caps', 'floor', 'payload'):
        sp = sub.add_parser(name)
        sp.add_argument('charts', nargs='+')
        sp.add_argument('--jobs', type=int, default=1, help='并行进程数(每张谱面一个任务)')
        if name == 'jitter':
            sp.add_argument('--pointers', type=int, default=10)
            sp.add_argument('--sigmas', default='0,5,10,15,20,30,40')
            sp.add_argument('--offset', type=int, default=-10, help='整体偏移(ms); 负数要写成 --offset=-10')
            sp.add_argument('--layout', default=None, help='k,n: 扫屏触点数,滑键触点数(默认用algo3f)')
        if name == 'timing':
            sp.add_argument('--pointers', type=int, default=10)
            sp.add_argument('--offsets', default='-100,-80,-60,-40,-20,0,20,40,60,80,100',
                            help='起点偏差(ms), 逗号分隔; 以负数开头时要写成 --offsets=-60,0,60')
            sp.add_argument('--jitters', default='0,20,40,60,80', help='抖动总宽度J(ms), 对应 ±J/2')
            sp.add_argument('--layout', default=None, help='k,n: 扫屏触点数,滑键触点数(默认用algo3f)')
        if name in ('caps', 'floor'):
            sp.add_argument('--caps', default='5,6,8,10,16' if name == 'caps' else '5,10')
            sp.add_argument('--quick', action='store_true', help='只搜索 k∈{1,2} n∈0..3, 快很多')
        if name == 'floor':
            sp.add_argument('--floors', default='5,40,80,125')
        if name == 'payload':
            sp.add_argument('--pointers', type=int, default=10)
            sp.add_argument('--tolerance', type=float, default=3.0)
    a = ap.parse_args(argv)
    {'timing': cmd_timing, 'jitter': cmd_jitter, 'caps': cmd_caps, 'floor': cmd_floor, 'payload': cmd_payload}[a.cmd](a)


if __name__ == '__main__':
    main()
