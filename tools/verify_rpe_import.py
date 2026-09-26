"""RPE导入转换的往返验证工具。

把RPE谱面转换成官方v3结构后, 与官方v3原谱逐点对比,
确认导入后的RPE谱面与官方原谱在phisap内部完全等价。

用法:
    python tools/verify_rpe_import.py <v3官谱.json> <rpe谱.json>

默认读取 samples/chart_at_4159.json 与 samples/rpe_chart_at_4159.json。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chart import Chart
from rpe import detect_kind, rpe_to_official_v3

failures = []


def fail(msg):
    failures.append(msg)


def main():
    v3_path = sys.argv[1] if len(sys.argv) > 1 else 'samples/chart_at_4159.json'
    rpe_path = sys.argv[2] if len(sys.argv) > 2 else 'samples/rpe_chart_at_4159.json'

    with open(v3_path, encoding='utf-8') as f:
        official = json.load(f)
    with open(rpe_path, encoding='utf-8', newline='') as f:
        rpe = json.load(f)

    assert detect_kind(official) == 'official', '官谱识别失败'
    assert detect_kind(rpe) == 'rpe', 'RPE识别失败'

    converted, warns = rpe_to_official_v3(rpe)
    chart_off = Chart.from_dict(official)
    chart_rpe = Chart.from_dict(converted)
    print(f'转换成功: {len(converted["judgeLineList"])}条判定线, 警告{len(warns)}条')
    for w in warns:
        print(f'  [warn] {w}')

    if abs(chart_off.offset - chart_rpe.offset) > 1e-6:
        fail(f'offset不一致: {chart_off.offset} vs {chart_rpe.offset}')

    matched = total = 0
    for li, (a, b) in enumerate(zip(chart_off.judge_lines, chart_rpe.judge_lines)):
        na = sorted(a.notes, key=lambda n: (n.time, n.x))
        nb = sorted(b.notes, key=lambda n: (n.time, n.x))
        if len(na) != len(nb):
            fail(f'line{li}: 音符数量不一致 {len(na)} vs {len(nb)}')
            continue
        if abs(a.bpm - b.bpm) > 1e-9:
            fail(f'line{li}: bpm不一致 {a.bpm} vs {b.bpm}')
        for x, y in zip(na, nb):
            total += 1
            ok = (
                abs(x.time - y.time) < 1e-6
                and x.type == y.type
                and abs(x.x - y.x) < 1e-9
                and abs(x.hold - y.hold) < 1e-6
                and abs(x.floor - y.floor) < 1e-3
            )
            if not ok:
                fail(f'line{li}: note不一致 off=({x.time},{x.type.name},{x.x:.4f},{x.hold}) '
                     f'conv=({y.time},{y.type.name},{y.x:.4f},{y.hold})')
            else:
                matched += 1
    print(f'音符逐一对比: {matched}/{total} 完全一致(time/type/x/hold/floor)')

    # 判定线事件抽样: 位置/旋转/透明度/floor
    ev_bad = ev_checked = floor_bad = 0
    for li, (a, b) in enumerate(zip(chart_off.judge_lines, chart_rpe.judge_lines)):
        if not a.notes:
            continue
        tmin = min(n.time for n in a.notes)
        tmax = max(n.time for n in a.notes)
        step = (tmax - tmin) / 40 if tmax > tmin else 0
        ts = [tmin + i * step for i in range(41)] if step else [tmin]
        for t in ts:
            pa, pb = a.pos(t), b.pos(t)
            if abs(pa[0] - pb[0]) > 0.05 or abs(pa[1] - pb[1]) > 0.05:
                ev_bad += 1
            if abs(a.angle(t) - b.angle(t)) > 1e-3:
                ev_bad += 1
            if abs(a.opacity(t) - b.opacity(t)) > 0.02:
                ev_bad += 1
            if abs(a.floor(t) - b.floor(t)) > 1e-3:
                floor_bad += 1
            ev_checked += 1
    print(f'判定线事件抽样: {ev_checked}个时刻, 位置/旋转/透明度不一致{ev_bad}个, floor不一致{floor_bad}个')
    if ev_bad:
        fail(f'{ev_bad}个抽样时刻判定线事件不一致')
    if floor_bad:
        fail(f'{floor_bad}个抽样时刻floor不一致')

    print()
    if failures:
        print(f'❌ 往返验证失败 ({len(failures)}项):')
        for m in failures[:20]:
            print('  -', m)
        sys.exit(1)
    print('✅ 往返验证通过: RPE转换谱与官方v3原谱在phisap内部完全等价')


if __name__ == '__main__':
    main()
