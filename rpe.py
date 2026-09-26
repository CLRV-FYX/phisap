"""RPE谱面(RPE/PEConverter输出的JSON) → 官方v3格式的转换器。

phisap内部只保留一条解析链路(官方v1/v2/v3, 见chart.py)。
导入RPE谱面时在导入阶段把它转换成等价的v3结构, 之后与官谱走完全相同的
解析/规划流程, 不引入第二套运行时解析逻辑。

换算关系(由官方v3谱与RPE转换谱的逐点交叉验证确认, 见tools/verify_v3.py):
+ 时间: RPE的[b, s, w] = (b + s/w)拍, v3时间单位 = 1/32拍 → v3_time = beats * 32
+ 音符x: v3单位 = RPE像素 / 75
+ 判定线位置: v3分数(0..1, 1280x720画布) ↔ RPE画布中心坐标(1350x900)
    x: v3_frac = RPE_x / 1350 + 0.5
    y: v3_frac = RPE_y / 900 + 0.5
+ 旋转: v3角度 = -RPE角度
+ 透明度: v3(0..1) = RPE(0..255) / 255
+ 下落速度: 阶梯函数, v3 value = RPE value / 4.5
+ 音符类型: RPE {1 tap, 2 hold, 3 flick, 4 drag} → v3 {1 tap, 2 drag, 3 hold, 4 flick}
+ 按住时长: v3 holdTime = (endTime - startTime)拍 * 32
"""

import json

# RPE→v3 音符类型映射
TYPE_MAP = {1: 1, 2: 3, 3: 4, 4: 2}

NOTE_X_SCALE = 75.0          # RPE像素 = v3单位 * 75
SPEED_SCALE = 4.5            # RPE速度值 = v3 value * 4.5
RPE_CANVAS_W = 1350.0        # RPE画布宽(中心坐标)
RPE_CANVAS_H = 900.0         # RPE画布高(中心坐标)
UNIT_SENTINEL = -999999.0    # v3的"歌曲开始前恒定段"起点(与官方文件一致)
TAIL = 1e9                   # v3的末尾延伸(与官方文件一致)


def rpe_beat(t) -> float:
    """RPE时间[b, s, w] → 拍数"""
    if isinstance(t, (int, float)):
        return float(t)
    b, s, w = t
    return b + s / w


def is_rpe(d: dict) -> bool:
    """判断一个dict是否是RPE谱面文件"""
    return (
        isinstance(d, dict)
        and 'formatVersion' not in d
        and 'BPMList' in d
        and 'META' in d
        and isinstance(d.get('judgeLineList'), list)
    )


def detect_kind(d) -> str:
    """识别谱面文件类型: 'official'(官谱v1/v2/v3) / 'rpe' / 'unknown'"""
    if not isinstance(d, dict):
        return 'unknown'
    if d.get('formatVersion') in (1, 2, 3) and isinstance(d.get('judgeLineList'), list):
        return 'official'
    if is_rpe(d):
        return 'rpe'
    return 'unknown'


def _ev_at(events, beats):
    """在events中线性求值beats时刻的值(与官方语义一致), 未覆盖时返回None"""
    for e in events:
        s0, s1 = rpe_beat(e['startTime']), rpe_beat(e['endTime'])
        if s0 <= beats <= s1:
            f = (beats - s0) / (s1 - s0) if s1 != s0 else 0.0
            return e['start'] + (e['end'] - e['start']) * f
    return None


def _move_events(layer: dict) -> list[dict]:
    """把RPE分离的moveX/moveY事件合并成v3的judgeLineMoveEvents(x+y同时间边界)。

    取x/y边界并集, 在并集边界上求值后逐段线性连接。
    注意: RPE的move事件允许在边界处"瞬移"(前一段end != 后一段start, 判定线jump到
    新位置)。求值约定与游戏/官方一致: 边界处取前一段的end, 边界之后立即取后一段的
    start。因此每个区间的start取"边界之后"的值、end取"边界之前"的值, 瞬移由
    相邻两个事件在公共边界处自然表达。
    """
    xs = layer.get('moveXEvents', [])
    ys = layer.get('moveYEvents', [])
    bounds = {0.0}
    for e in xs:
        bounds.add(rpe_beat(e['startTime']))
        bounds.add(rpe_beat(e['endTime']))
    for e in ys:
        bounds.add(rpe_beat(e['startTime']))
        bounds.add(rpe_beat(e['endTime']))
    bounds = sorted(bounds)

    def sample(evs, boundary: float, just_after: bool) -> float:
        """边界处取值。just_after=True取边界之后的值(从该边界开始的事件的start),
        否则取边界之前的值(覆盖到该边界的事件, 即list中首个覆盖它的事件)。"""
        if just_after:
            for e in evs:
                if rpe_beat(e['startTime']) == boundary:
                    return e['start']
        v = _ev_at(evs, boundary)
        return 0.0 if v is None else v

    x_after = [sample(xs, b, True) for b in bounds]
    x_before = [sample(xs, b, False) for b in bounds]
    y_after = [sample(ys, b, True) for b in bounds]
    y_before = [sample(ys, b, False) for b in bounds]

    def fx(v):
        return v / RPE_CANVAS_W + 0.5

    def fy(v):
        return v / RPE_CANVAS_H + 0.5

    events = []
    units = [b * 32 for b in bounds]
    if units[0] > UNIT_SENTINEL:
        events.append({
            'startTime': UNIT_SENTINEL, 'endTime': units[0],
            'start': fx(x_after[0]), 'end': fx(x_after[0]),
            'start2': fy(y_after[0]), 'end2': fy(y_after[0]),
        })
    for i in range(len(units) - 1):
        if units[i + 1] <= units[i]:
            continue
        events.append({
            'startTime': units[i], 'endTime': units[i + 1],
            'start': fx(x_after[i]), 'end': fx(x_before[i + 1]),
            'start2': fy(y_after[i]), 'end2': fy(y_before[i + 1]),
        })
    if events and events[-1]['endTime'] < TAIL:
        events[-1]['endTime'] = TAIL
    return events


def _speed_events(layer: dict, bpm: float):
    """RPE阶梯速度事件 → v3 speedEvents, 并返回用于推导floor的带_floor副本"""
    evs = []
    cur = 0.0
    for e in layer.get('speedEvents', []):
        s = rpe_beat(e['startTime']) * 32
        en = rpe_beat(e['endTime']) * 32
        value = e['start'] / SPEED_SCALE
        evs.append({'startTime': s, 'endTime': en, 'value': value, '_floor': cur})
        cur += 1.875 * (en - s) * value / bpm
    return evs


def _floor_at(speed_evs, bpm: float, t: float) -> float:
    """与JudgeLine.from_dict_v3相同的floor累积推导(含末尾外推)"""
    for e in speed_evs:
        if e['startTime'] <= t <= e['endTime']:
            return e['_floor'] + (t - e['startTime']) * e['value'] * 1.875 / bpm
    if speed_evs:
        last = speed_evs[-1]
        if t >= last['startTime']:
            return last['_floor'] + (t - last['startTime']) * last['value'] * 1.875 / bpm
    return 0.0


def _pick_bpm(bpm_list: list, beats: float) -> float | None:
    cur = None
    for entry in sorted(bpm_list, key=lambda e: rpe_beat(e['startTime'])):
        if rpe_beat(entry['startTime']) <= beats:
            cur = entry['bpm']
    return cur


def rpe_to_official_v3(rpe: dict) -> tuple[dict, list[str]]:
    """把RPE谱面dict转换成官方v3结构dict。返回(结果, 警告列表)。"""
    warnings: list[str] = []
    meta = rpe.get('META', {}) or {}
    bpm_list = rpe.get('BPMList', []) or []
    lines_out = []
    skipped_fake = 0

    for li, line in enumerate(rpe['judgeLineList']):
        layers = line.get('eventLayers', []) or []
        if not layers:
            warnings.append(f'判定线{li}: 没有eventLayers, 按空事件处理')
            layer = {}
        elif len(layers) > 1:
            warnings.append(f'判定线{li}: 有{len(layers)}个事件层, 仅使用第1层')
            layer = layers[0]
        else:
            layer = layers[0]

        # 该线的bpm: 取首个音符时刻BPMList中的值 × bpmfactor
        notes_in = line.get('notes', []) or []
        if notes_in:
            first_beat = min(rpe_beat(n['startTime']) for n in notes_in)
        else:
            first_beat = 0.0
        base_bpm = _pick_bpm(bpm_list, first_beat)
        if base_bpm is None:
            base_bpm = 120.0
            warnings.append(f'判定线{li}: BPMList未覆盖起始时刻, 使用默认bpm 120')
        if len({e.get('bpm') for e in bpm_list}) > 1:
            warnings.append(f'BPMList包含多个bpm, v3格式每线只能存一个, 已取起始时刻的值')
        bpm = base_bpm * (line.get('bpmfactor', 1.0) or 1.0)

        speed_raw = _speed_events(layer, bpm)

        def to_time(t):
            return rpe_beat(t) * 32

        rotate = []
        for e in layer.get('rotateEvents', []):
            rotate.append({'startTime': to_time(e['startTime']), 'endTime': to_time(e['endTime']),
                           'start': -e['start'], 'end': -e['end']})
        alpha = []
        for e in layer.get('alphaEvents', []):
            alpha.append({'startTime': to_time(e['startTime']), 'endTime': to_time(e['endTime']),
                          'start': e['start'] / 255.0, 'end': e['end'] / 255.0})

        def with_sentinel(events):
            if not events:
                return []
            evs = [dict(e) for e in events]
            if evs[0]['startTime'] > UNIT_SENTINEL:
                evs.insert(0, {'startTime': UNIT_SENTINEL, 'endTime': evs[0]['startTime'],
                               'start': evs[0]['start'], 'end': evs[0]['start']})
            if evs[-1]['endTime'] < TAIL:
                evs[-1]['endTime'] = TAIL
            return evs

        above, below = [], []
        for n in notes_in:
            if n.get('isFake'):
                skipped_fake += 1
                continue
            t = rpe_beat(n['startTime']) * 32
            if n['type'] not in TYPE_MAP:
                warnings.append(f'判定线{li}: 未知音符类型{n["type"]}已跳过')
                continue
            ti = int(round(t)) if abs(t - round(t)) < 1e-6 else t
            above_out = {
                'type': TYPE_MAP[n['type']],
                'time': ti,
                'positionX': n['positionX'] / NOTE_X_SCALE,
                'holdTime': max(0.0, (rpe_beat(n['endTime']) - rpe_beat(n['startTime'])) * 32),
                'speed': n.get('speed', 1.0),
                'floorPosition': _floor_at(speed_raw, bpm, ti),
            }
            (above if n.get('above') else below).append(above_out)

        lines_out.append({
            'bpm': bpm,
            'notesAbove': above,
            'notesBelow': below,
            'speedEvents': [{k: v for k, v in e.items() if k != '_floor'} for e in speed_raw],
            'judgeLineMoveEvents': _move_events(layer),
            'judgeLineRotateEvents': with_sentinel(rotate),
            'judgeLineDisappearEvents': with_sentinel(alpha),
        })

    if skipped_fake:
        warnings.append(f'已跳过{skipped_fake}个fake音符(不参与判定, 自动打歌无需执行)')

    out = {
        'formatVersion': 3,
        'offset': meta.get('offset', 0.0) or 0.0,
        'judgeLineList': lines_out,
    }
    return out, warnings


__all__ = ['is_rpe', 'detect_kind', 'rpe_to_official_v3', 'rpe_beat', 'TYPE_MAP']
