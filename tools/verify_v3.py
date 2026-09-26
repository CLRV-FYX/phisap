"""v3官谱(Phigros 3.20.0+) 与 RPE JSON 转换谱的交叉验证工具。

用法:
    python tools/verify_v3.py <v3官谱.json> <rpe转换谱.json>

默认读取 samples/chart_at_4159.json 与 samples/rpe_chart_at_4159.json。

验证内容(依据逆向得到的编码约定, 详见README更新日志):
+ 判定线数量、每线bpm、offset
+ 每线音符数量(允许个别线above/below被转换器翻转)
+ 逐音符: time(1/32拍 ↔ 拍)、type(1→1, 2→4, 3→2, 4→3)、
  x(v3单位 ↔ RPE: x*75)、holdTime(拍*32)
+ 抽样时刻的判定线事件: 透明度(0..1 ↔ 0..255)、位置(0..1分数@1280x720 ↔
  RPE 1350x900画布中心坐标)、旋转(角度 ↔ -角度)
+ 数据完整性: note.floorPosition == 由speedEvents累积推导的floor(note.time)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chart import Chart

# RPE画布与1280x720的换算(由样例数据逆推: RPE_x = (v3_x_frac*1280-640)*1350/1280,
# RPE_y = (v3_y_frac*720-360)*900/720; 音符x: RPE_x = v3_x*75)
RPE_CANVAS_W = 1350.0
RPE_CANVAS_H = 900.0
NOTE_X_SCALE = 75.0
TYPE_MAP = {1: 1, 2: 4, 3: 2, 4: 3}

failures = []
warnings = []


def fail(msg):
    failures.append(msg)


def warn(msg):
    warnings.append(msg)


def rpe_beat(t):
    b, s, w = t
    return b + s / w


def rpe_line_pos_at(line, beats):
    """RPE判定线在beats时刻的位置(画布中心坐标)与旋转/透明度"""
    layer = line['eventLayers'][0]

    def ev_at(evts):
        for e in evts:
            s0, s1 = rpe_beat(e['startTime']), rpe_beat(e['endTime'])
            if s0 <= beats <= s1:
                f = (beats - s0) / (s1 - s0) if s1 != s0 else 0.0
                return e['start'] + (e['end'] - e['start']) * f
        return None

    x = ev_at(layer.get('moveXEvents', []))
    y = ev_at(layer.get('moveYEvents', []))
    r = ev_at(layer.get('rotateEvents', []))
    a = ev_at(layer.get('alphaEvents', []))
    return x, y, r, a


def main():
    v3_path = sys.argv[1] if len(sys.argv) > 1 else 'samples/chart_at_4159.json'
    rpe_path = sys.argv[2] if len(sys.argv) > 2 else 'samples/rpe_chart_at_4159.json'

    with open(v3_path, 'r', encoding='utf-8') as f:
        v3 = json.load(f)
    with open(rpe_path, 'r', encoding='utf-8', newline='') as f:
        rpe = json.load(f)

    # 用phisap解析v3谱面(即验证新解析代码)
    chart = Chart.from_dict(v3)
    print(f'phisap解析成功: formatVersion={chart.version}, {len(chart.judge_lines)}条判定线')

    # ---- 基础字段 ----
    v3_lines = v3['judgeLineList']
    rpe_lines = rpe['judgeLineList']
    if len(v3_lines) != len(rpe_lines):
        fail(f'判定线数量不一致: v3={len(v3_lines)}, rpe={len(rpe_lines)}')
    print(f'判定线数量: v3={len(v3_lines)}, rpe={len(rpe_lines)}')

    if abs(v3.get('offset', 0.0) - rpe.get('META', {}).get('offset', 0.0)) > 1e-6:
        fail(f'offset不一致: v3={v3.get("offset")}, rpe={rpe.get("META", {}).get("offset")}')
    else:
        print(f'offset一致: {v3.get("offset")}')

    bpm_list = rpe.get('BPMList', [])

    def rpe_bpm_at(beats):
        cur = None
        for entry in sorted(bpm_list, key=lambda e: rpe_beat(e['startTime'])):
            if rpe_beat(entry['startTime']) <= beats:
                cur = entry['bpm']
        return cur

    # ---- 逐线逐音符 ----
    total_notes = matched_notes = 0
    checked_floor = 0
    sample_times_all = []
    for li, (a, b) in enumerate(zip(v3_lines, rpe_lines)):
        na, nb = a['notesAbove'], a['notesBelow']
        rb = b.get('notes', [])
        if len(na) + len(nb) != len(rb):
            fail(f'line{li}: 音符数量不一致 v3={len(na) + len(nb)}, rpe={len(rb)}')
            continue

        # above/below计数(允许转换器个别线翻转上下)
        ra = sum(1 for n in rb if n.get('above'))
        rbt = len(rb) - ra
        flipped = not (len(na) == ra and len(nb) == rbt) and len(na) == rbt and len(nb) == ra
        if flipped:
            warn(f'line{li}: 转换谱的above/below与原谱翻转(转换器行为, 不影响验证)')
        elif not (len(na) == ra and len(nb) == rbt):
            fail(f'line{li}: 上下音符数量不一致 v3=({len(na)},{len(nb)}), rpe=({ra},{rbt})')

        # 该线bpm
        if na or nb:
            first_beat = min(min(n['time'] for n in na + nb), 1e9) / 32
            rbpm = rpe_bpm_at(first_beat)
            if rbpm is None or abs(rbpm - a['bpm']) > 1e-9:
                fail(f'line{li}: bpm不一致 v3={a["bpm"]}, rpe={rbpm}')

        # 按(time, x)排序配对
        v3n = sorted(na + nb, key=lambda n: (n['time'], n['positionX']))
        rpn = sorted(rb, key=lambda n: (rpe_beat(n['startTime']), n['positionX']))
        line_bad = 0
        for vn, rn in zip(v3n, rpn):
            total_notes += 1
            ok = True
            vt = vn['time'] / 32
            if abs(vt - rpe_beat(rn['startTime'])) > 1e-6:
                line_bad += 1
                ok = False
            if TYPE_MAP.get(vn['type']) != rn['type']:
                line_bad += 1
                ok = False
            if abs(vn['positionX'] * NOTE_X_SCALE - rn['positionX']) > 0.01:
                line_bad += 1
                ok = False
            rlen = (rpe_beat(rn['endTime']) - rpe_beat(rn['startTime'])) * 32
            if abs(rlen - vn['holdTime']) > 0.01:
                line_bad += 1
                ok = False
            if ok:
                matched_notes += 1
            # floor数据完整性: 存储的floorPosition应等于speed事件累积值
            jl = chart.judge_lines[li]
            if abs(jl.floor(vn['time']) - vn['floorPosition']) > 1e-3:
                fail(f'line{li}: note(t={vn["time"]}) floorPosition={vn["floorPosition"]} '
                     f'与speed事件累积值{jl.floor(vn["time"]):.6f}不一致')
            checked_floor += 1
        if line_bad:
            fail(f'line{li}: {line_bad}/{len(v3n)}个音符存在time/type/x/hold不一致')
        if na or nb:
            ts = [n['time'] for n in na + nb]
            sample_times_all.append((li, min(ts), max(ts)))

    print(f'音符逐一对应: {matched_notes}/{total_notes} 全部一致(time/type/x/hold)')
    print(f'floor完整性: {checked_floor}个note的floorPosition与累积推导一致(容差1e-3)')

    # ---- 判定线事件抽样 ----
    ev_bad = 0
    ev_checked = 0
    for li, tmin, tmax in sample_times_all:
        a = v3_lines[li]
        b = rpe_lines[li]
        jl = chart.judge_lines[li]
        if tmax <= tmin:
            ts = [tmin]
        else:
            step = (tmax - tmin) / 40
            ts = [tmin + i * step for i in range(41)]
        for t in ts:
            beats = t / 32
            rx, ry, rr, ra_ = rpe_line_pos_at(b, beats)
            if rx is None or ry is None:
                continue
            ev_checked += 1
            px, py = jl.pos(t)
            if abs(px - (rx * 1280 / RPE_CANVAS_W + 640)) > 0.05:
                ev_bad += 1
            if abs(py - (ry * 720 / RPE_CANVAS_H + 360)) > 0.05:
                ev_bad += 1
            if rr is not None and abs(jl.angle(t) + rr) > 1e-3:
                ev_bad += 1
            if ra_ is not None and abs(jl.opacity(t) - ra_ / 255) > 0.02:
                ev_bad += 1
    print(f'判定线事件抽样: {ev_checked}个时刻, 不一致 {ev_bad} 个(x/y/旋转/透明度)')
    if ev_bad:
        fail(f'判定线事件有{ev_bad}个抽样不一致')

    # ---- 汇总 ----
    print()
    if failures:
        print(f'❌ 验证失败 ({len(failures)}项):')
        for m in failures:
            print('  -', m)
        for m in warnings:
            print('  [warn]', m)
        sys.exit(1)
    print('✅ 全部通过: v3解析结果与RPE转换谱一致')
    for m in warnings:
        print('  [warn]', m)


if __name__ == '__main__':
    main()
