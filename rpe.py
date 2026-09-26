"""RPE谱面(Re:PhiEdit / PEConverter输出的JSON) → 官方v3格式的转换器。

phisap内部只保留一条解析链路(官方v1/v3, 见chart.py)。
导入RPE谱面时在导入阶段把它转换成等价的v3结构, 之后与官谱走完全相同的
解析/规划流程, 不引入第二套运行时解析逻辑。

事件语义与 Phira(TeamFlos/phira, prpr/src/parse/rpe.rs) 保持一致:
+ 时间: RPE的[b, s, w] = (b + s/w)拍, 按BPMList(支持变速)换算成秒, 所有插值都在"秒"上进行
+ 缓动: easingType 1~29(与Phira的RPE_TWEEN_MAP相同)、easingLeft/easingRight截取、贝塞尔缓动
+ 事件层: eventLayers中所有层的值**相加**
+ 父子线: father >= 0 时, 子线位置 = 父线位置 + 按父线角度旋转后的子线位置;
  rotateWithFather 为 true 时子线角度再加上父线角度
+ 相邻事件之间的空档保持前一个事件的end; 边界时刻取后一个事件的start

v3只支持线性事件, 因此缓动/父线旋转造成的曲线运动会被细分采样成多段线性事件
(采样间隔1/240秒; 误差: 平滑曲线处位置 < 0.3像素、角度 < 0.02度, bounce等曲线的尖角处约1像素以内),
线性部分原样输出。

换算关系:
+ v3时间单位 = 1/32拍(按v3线bpm) → v3_time = 秒 * bpm / 1.875, bpm取BPMList的第一个值
+ 音符x: v3单位 = RPE像素 / 75
+ 判定线位置: v3分数(0..1, 1280x720画布, y以底部为0) ↔ RPE画布中心坐标(1350x900, y向上)
    x: v3_frac = RPE_x / 1350 + 0.5
    y: v3_frac = RPE_y / 900 + 0.5
+ 旋转: v3角度(逆时针) = -RPE角度(顺时针)
+ 透明度: v3(0..1) = RPE(0..255) / 255
+ 下落速度: v3 value = RPE value / 4.5 (只影响floorPosition, 不影响自动打歌)
+ 音符类型: RPE {1 tap, 2 hold, 3 flick, 4 drag} → v3 {1 tap, 2 drag, 3 hold, 4 flick}
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from typing import Callable

# RPE→v3 音符类型映射
TYPE_MAP = {1: 1, 2: 3, 3: 4, 4: 2}

NOTE_X_SCALE = 75.0          # RPE像素 = v3单位 * 75
SPEED_SCALE = 4.5            # RPE速度值 = v3 value * 4.5
RPE_CANVAS_W = 1350.0        # RPE画布宽(中心坐标)
RPE_CANVAS_H = 900.0         # RPE画布高(中心坐标)
UNIT_SENTINEL = -999999.0    # v3的"歌曲开始前恒定段"起点(与官方文件一致)
TAIL = 1e9                   # v3的末尾延伸(与官方文件一致)

# 曲线运动的采样参数
SAMPLE_STEP = 1 / 240        # 细分采样间隔(秒)
MAX_SAMPLES = 20000          # 单个区间的最大采样数(超长缓动事件自动放宽间隔)
MAX_MERGE = 512              # 合并时单段最多跨越的采样点数
TOL_POS = 0.3                # 合并容差: 位置(RPE像素)
TOL_ROT = 0.02               # 合并容差: 角度(度)
TOL_ALPHA = 1.0              # 合并容差: 透明度(0..255)


def rpe_beat(t) -> float:
    """RPE时间[b, s, w] → 拍数"""
    if isinstance(t, (int, float)):
        return float(t)
    b, s, w = t
    return b + s / w if w else float(b)


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


# ---------------------------------------------------------------- 缓动函数(与Phira tween.rs一致)

def _in_sine(x): return 1 - math.cos(x * math.pi / 2)
def _in_quad(x): return x * x
def _in_cubic(x): return x * x * x
def _in_quart(x): return x ** 4
def _in_quint(x): return x ** 5
def _in_expo(x): return 2.0 ** (10 * (x - 1))
def _in_circ(x): return 1 - math.sqrt(max(0.0, 1 - x * x))


def _in_back(x):
    c1 = 1.70158
    c3 = c1 + 1
    return (c3 * x - c1) * x * x


def _in_elastic(x):
    c4 = 2 * math.pi / 3
    return -(2.0 ** (10 * x - 10) * math.sin((x * 10 - 10.75) * c4))


def _in_bounce(x):
    n1, d1 = 7.5625, 2.75
    x = 1 - x
    if x < 1 / d1:
        g = n1 * x * x
    elif x < 2 / d1:
        g = n1 * (x - 1.5 / d1) ** 2 + 0.75
    elif x < 2.5 / d1:
        g = n1 * (x - 2.25 / d1) ** 2 + 0.9375
    else:
        g = n1 * (x - 2.625 / d1) ** 2 + 0.984375
    return 1 - g


def _out(f):
    return lambda x: 1 - f(1 - x)


def _io(f):
    def g(x):
        x *= 2
        return f(x) / 2 if x < 1 else 1 - f(2 - x) / 2
    return g


def _linear(x):
    return x


# RPE easingType → 缓动函数(Phira RPE_TWEEN_MAP), 0/1为线性, 超出范围按线性处理
_EASINGS: list[Callable[[float], float]] = [
    _linear, _linear,
    _out(_in_sine), _in_sine, _out(_in_quad), _in_quad, _io(_in_sine), _io(_in_quad),
    _out(_in_cubic), _in_cubic, _out(_in_quart), _in_quart, _io(_in_cubic), _io(_in_quart),
    _out(_in_quint), _in_quint, _out(_in_expo), _in_expo, _out(_in_circ), _in_circ,
    _out(_in_back), _in_back, _io(_in_circ), _io(_in_back), _out(_in_elastic), _in_elastic,
    _out(_in_bounce), _in_bounce, _io(_in_bounce), _io(_in_elastic),
]


def _cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """CSS风格三次贝塞尔缓动(同 gre/bezier-easing, Phira的BezierTween)"""
    x1 = min(max(x1, 0.0), 1.0)
    x2 = min(max(x2, 0.0), 1.0)

    def bx(t): return ((1 - 3 * x2 + 3 * x1) * t + (3 * x2 - 6 * x1)) * t * t + 3 * x1 * t
    def by(t): return ((1 - 3 * y2 + 3 * y1) * t + (3 * y2 - 6 * y1)) * t * t + 3 * y1 * t
    def dbx(t): return 3 * (1 - 3 * x2 + 3 * x1) * t * t + 2 * (3 * x2 - 6 * x1) * t + 3 * x1

    def f(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        t = x
        for _ in range(8):  # Newton
            d = dbx(t)
            if abs(d) < 1e-7:
                break
            t2 = t - (bx(t) - x) / d
            if not 0 <= t2 <= 1:
                break
            t = t2
            if abs(bx(t) - x) < 1e-7:
                return by(t)
        lo, hi = 0.0, 1.0  # 二分兜底(bx单调)
        for _ in range(50):
            t = (lo + hi) / 2
            if bx(t) < x:
                lo = t
            else:
                hi = t
        return by((lo + hi) / 2)
    return f


def event_tween(e: dict) -> Callable[[float], float] | None:
    """RPE事件的缓动函数; 线性时返回None"""
    if e.get('bezier'):
        pts = e.get('bezierPoints') or [0.0, 0.0, 1.0, 1.0]
        if len(pts) == 4:
            return _cubic_bezier(*map(float, pts))
    et = int(e.get('easingType', 1) or 1)
    et = max(et, 1)
    f = _EASINGS[et] if et < len(_EASINGS) else _linear
    if f is _linear:
        return None
    left = min(max(float(e.get('easingLeft', 0.0) or 0.0), 0.0), 1.0)
    right = min(max(float(e.get('easingRight', 1.0) if e.get('easingRight') is not None else 1.0), 0.0), 1.0)
    if (abs(left) < 1e-4 and abs(right - 1) < 1e-4) or left >= right:
        return f
    fl, fr = f(left), f(right)
    if fr == fl:
        return None

    def clamped(x):
        return (f(left + (right - left) * x) - fl) / (fr - fl)
    return clamped


# ---------------------------------------------------------------- BPM

class BpmList:
    """RPE拍数 → 秒(支持BPM变化)"""

    def __init__(self, bpm_list: list) -> None:
        items = sorted(
            ((rpe_beat(e['startTime']), float(e['bpm'])) for e in (bpm_list or []) if float(e.get('bpm', 0) or 0) > 0),
            key=lambda it: it[0],
        )
        if not items:
            items = [(0.0, 120.0)]
        self.beats = [b for b, _ in items]
        self.bpms = [v for _, v in items]
        self.secs = [0.0] * len(items)
        # 第一段的起点视为0拍(与Phira一致: 第一个BPM从0拍开始生效)
        self.beats[0] = 0.0
        for i in range(1, len(items)):
            self.secs[i] = self.secs[i - 1] + (self.beats[i] - self.beats[i - 1]) * 60 / self.bpms[i - 1]

    def seconds(self, beat: float) -> float:
        i = max(0, bisect_right(self.beats, beat) - 1)
        return self.secs[i] + (beat - self.beats[i]) * 60 / self.bpms[i]

    def time(self, t) -> float:
        return self.seconds(rpe_beat(t))


# ---------------------------------------------------------------- 事件层求值(Phira的Keyframe语义)

class Layer:
    """单个事件层中一种事件的关键帧序列。

    每个事件产生两个关键帧: (开始时刻, start, 缓动) 与 (结束时刻, end, 保持)。
    值在关键帧之间按前一个关键帧的缓动插值; 事件之间的空档保持前一个事件的end。
    """

    def __init__(self, events: list, bpm: BpmList, factor: float = 1.0) -> None:
        evs = sorted(events, key=lambda e: rpe_beat(e['startTime']))
        self.times: list[float] = []
        self.values: list[float] = []
        # 每个关键帧到下一关键帧的插值方式: 'hold' / None(线性) / 缓动函数
        self.tweens: list = []
        for e in evs:
            st, en = bpm.time(e['startTime']), bpm.time(e['endTime'])
            en = max(en, st)
            self.times.append(st)
            self.values.append(float(e['start']) * factor)
            self.tweens.append(event_tween(e))
            self.times.append(en)
            self.values.append(float(e['end']) * factor)
            self.tweens.append('hold')

    def __bool__(self) -> bool:
        return bool(self.times)

    def _segment(self, i: int, t: float) -> float:
        v1 = self.values[i]
        tw = self.tweens[i]
        if tw == 'hold' or i + 1 >= len(self.times):
            return v1
        t1, t2 = self.times[i], self.times[i + 1]
        if t2 <= t1:
            return self.values[i + 1]
        x = (t - t1) / (t2 - t1)
        y = x if tw is None else tw(x)
        return v1 + (self.values[i + 1] - v1) * y

    def value(self, t: float, before: bool = False) -> float:
        """t时刻的值; before=True时取左极限(边界之前的值), 否则取右极限(边界之后的值)"""
        if not self.times:
            return 0.0
        i = (bisect_left(self.times, t) if before else bisect_right(self.times, t)) - 1
        if i < 0:
            return self.values[0]
        if i >= len(self.times) - 1:
            return self.values[-1]
        return self._segment(i, t)

    def is_linear(self, a: float, b: float) -> bool:
        """区间(a, b)内(其中不含任何关键帧)是否为线性/恒定"""
        if not self.times:
            return True
        i = bisect_right(self.times, a) - 1
        if i < 0 or i >= len(self.times) - 1:
            return True
        tw = self.tweens[i]
        return tw is None or tw == 'hold'

    def is_constant(self, a: float, b: float) -> bool:
        if not self.times:
            return True
        i = bisect_right(self.times, a) - 1
        if i < 0 or i >= len(self.times) - 1:
            return True
        return self.tweens[i] == 'hold' or self.values[i] == self.values[i + 1]


class Channel:
    """一种事件(moveX/moveY/rotate/alpha/speed)在所有事件层上的和"""

    def __init__(self, layers: list[dict], key: str, bpm: BpmList, factor: float = 1.0) -> None:
        self.layers = [Layer(ly[key], bpm, factor) for ly in layers if ly.get(key)]
        self.layers = [ly for ly in self.layers if ly]

    def value(self, t: float, before: bool = False) -> float:
        return sum(ly.value(t, before) for ly in self.layers)

    def breakpoints(self) -> set[float]:
        s: set[float] = set()
        for ly in self.layers:
            s.update(ly.times)
        return s

    def is_linear(self, a: float, b: float) -> bool:
        return all(ly.is_linear(a, b) for ly in self.layers)

    def is_constant(self, a: float, b: float) -> bool:
        return all(ly.is_constant(a, b) for ly in self.layers)


class LineMotion:
    """判定线的世界坐标(RPE画布坐标, y向上)与角度(RPE角度, 顺时针为正), 含父线变换"""

    def __init__(self, line: dict, bpm: BpmList) -> None:
        layers = [ly for ly in (line.get('eventLayers') or []) if ly]
        self.mx = Channel(layers, 'moveXEvents', bpm)
        self.my = Channel(layers, 'moveYEvents', bpm)
        self.rot = Channel(layers, 'rotateEvents', bpm)
        self.alpha = Channel(layers, 'alphaEvents', bpm)
        self.rotate_with_father = bool(line.get('rotateWithFather', False))
        self.father: LineMotion | None = None

    def chain(self) -> list[LineMotion]:
        """自身及所有祖先"""
        out, cur = [], self
        while cur is not None:
            out.append(cur)
            cur = cur.father
        return out

    def rotation(self, t: float, before: bool = False) -> float:
        r = self.rot.value(t, before)
        if self.rotate_with_father and self.father is not None:
            r += self.father.rotation(t, before)
        return r

    def state(self, t: float, before: bool = False) -> tuple[float, float, float]:
        x, y = self.mx.value(t, before), self.my.value(t, before)
        if self.father is not None:
            fx, fy, _ = self.father.state(t, before)
            th = math.radians(self.father.rotation(t, before))
            c, s = math.cos(th), math.sin(th)
            # Phira: parent_pos + Rotation2(-rpe_angle) * child_pos (y向上的坐标系)
            x, y = fx + x * c + y * s, fy - x * s + y * c
        return x, y, self.rotation(t, before)

    def breakpoints(self) -> set[float]:
        s: set[float] = set()
        for m in self.chain():
            for ch in (m.mx, m.my, m.rot):
                s |= ch.breakpoints()
        return s

    def is_linear(self, a: float, b: float) -> bool:
        for m in self.chain():
            if not (m.mx.is_linear(a, b) and m.my.is_linear(a, b) and m.rot.is_linear(a, b)):
                return False
        # 父线在旋转时, 子线的世界位置是曲线(即使各自都是线性事件)
        for m in self.chain()[1:]:
            if not m.rot.is_constant(a, b):
                return False
        return True


# ---------------------------------------------------------------- 采样 → 线性段

def _sample_points(a: float, b: float) -> list[float]:
    n = max(2, min(MAX_SAMPLES, math.ceil((b - a) / SAMPLE_STEP)))
    return [a + (b - a) * k / n for k in range(n + 1)]


def _fits(ts: list[float], vals: list[tuple], i: int, j: int, tols: tuple) -> bool:
    """i..j之间的点是否都在 i→j 线性插值的容差内"""
    t0, t1 = ts[i], ts[j]
    vi, vj = vals[i], vals[j]
    span = t1 - t0
    for k in range(i + 1, j):
        f = (ts[k] - t0) / span
        vk = vals[k]
        for c, tol in enumerate(tols):
            if abs(vi[c] + (vj[c] - vi[c]) * f - vk[c]) > tol:
                return False
    return True


def _simplify(ts: list[float], vals: list[tuple], tols: tuple) -> list[int]:
    """合并采样点: 返回保留点的下标, 保证被合并掉的点与线性插值的误差都在容差内。
    每段用倍增+二分寻找最远可合并的点。"""
    keep = [0]
    i, n = 0, len(ts) - 1
    while i < n:
        hi_limit = min(n, i + MAX_MERGE)
        good, step = i + 1, 1
        bad = None
        while True:  # 倍增
            cand = min(i + step * 2, hi_limit)
            if cand <= good:
                break
            if _fits(ts, vals, i, cand, tols):
                good = cand
                if cand == hi_limit:
                    break
                step *= 2
            else:
                bad = cand
                break
        if bad is not None:  # 二分
            lo, hi = good, bad
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if _fits(ts, vals, i, mid, tols):
                    lo = mid
                else:
                    hi = mid
            good = lo
        keep.append(good)
        i = good
    return keep


def _piecewise(bounds: list[float], sample: Callable[[float, bool], tuple],
               is_linear: Callable[[float, float], bool], groups: list[tuple]) -> list[list[tuple]]:
    """在断点序列上生成线性段, 断点处允许跳变。

    groups: [(分量下标tuple, 容差tuple), ...], 每组分别合并(例如位置与角度),
    返回每组的 [(t0, t1, v0, v1)] (v为该组分量的tuple)。
    """
    # 先得到所有(未合并的)点: 每个区间内的采样点, 区间端点处分别记录"之后/之前"的值
    out: list[list[tuple]] = [[] for _ in groups]
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        if is_linear(a, b):
            ts = [a, b]
            vals = [sample(a, False), sample(b, True)]
        else:
            ts = _sample_points(a, b)
            vals = [sample(a, False)] + [sample(t, False) for t in ts[1:-1]] + [sample(b, True)]
        for g, (comps, tols) in enumerate(groups):
            gv = [tuple(v[c] for c in comps) for v in vals]
            idx = _simplify(ts, gv, tols) if len(ts) > 2 else [0, 1]
            for p, q in zip(idx, idx[1:]):
                out[g].append((ts[p], ts[q], gv[p], gv[q]))
    return [_merge_segments(segs, tols) for segs, (_, tols) in zip(out, groups)]


def _merge_segments(segs: list[tuple], tols: tuple) -> list[tuple]:
    """合并相邻且连续、可用一条直线表示的线段(例如只有角度在变化时, 位置事件无需跟着细分)"""
    if not segs:
        return segs
    merged = []
    run = [segs[0]]  # 当前可合并的一串连续线段

    def flush():
        t0, _, v0, _ = run[0]
        _, t1, _, v1 = run[-1]
        merged.append((t0, t1, v0, v1))

    for seg in segs[1:]:
        prev = run[-1]
        continuous = all(abs(a - b) <= 1e-9 for a, b in zip(prev[3], seg[2])) and prev[1] == seg[0]
        if continuous and len(run) < MAX_MERGE:
            t0, v0 = run[0][0], run[0][2]
            t1, v1 = seg[1], seg[3]
            ok = t1 > t0
            if ok:
                for s_ in run:  # 所有被吸收的端点都必须落在新直线的容差内
                    f = (s_[1] - t0) / (t1 - t0)
                    for c, tol in enumerate(tols):
                        if abs(v0[c] + (v1[c] - v0[c]) * f - s_[3][c]) > tol:
                            ok = False
                            break
                    if not ok:
                        break
            if ok:
                run.append(seg)
                continue
        flush()
        run = [seg]
    flush()
    return merged


def _round_time(t: float) -> float:
    """v3时间保留4位小数(0.0001个1/32拍), 整数时输出整数样式的浮点"""
    if t in (UNIT_SENTINEL, TAIL):
        return t
    return round(t, 4)


def rpe_to_official_v3(rpe: dict) -> tuple[dict, list[str]]:
    """把RPE谱面dict转换成官方v3结构dict。返回(结果, 警告列表)。"""
    warnings: list[str] = []
    meta = rpe.get('META', {}) or {}
    bpm = BpmList(rpe.get('BPMList', []) or [])
    v3_bpm = bpm.bpms[0]
    if not rpe.get('BPMList'):
        warnings.append('BPMList为空, 使用默认bpm 120')

    def v3t(sec: float) -> float:
        return sec * v3_bpm / 1.875

    lines_in = rpe['judgeLineList']
    motions = [LineMotion(line, bpm) for line in lines_in]

    # 父子线
    n_father = 0
    for li, line in enumerate(lines_in):
        fa = line.get('father', -1)
        if fa is None or not isinstance(fa, int) or fa < 0:
            continue
        if fa >= len(lines_in) or fa == li:
            warnings.append(f'判定线{li}: 父线编号{fa}无效, 已忽略')
            continue
        motions[li].father = motions[fa]
        n_father += 1
    for li, m in enumerate(motions):  # 环检测
        seen, cur = set(), m
        while cur is not None:
            if id(cur) in seen:
                warnings.append(f'判定线{li}: 父线关系成环, 已断开')
                m.father = None
                break
            seen.add(id(cur))
            cur = cur.father

    if len({round(b, 6) for b in bpm.bpms}) > 1:
        warnings.append(f'谱面包含{len(bpm.bpms)}段BPM, 已按变速换算为精确时间')
    factors = {line.get('bpmfactor', 1.0) for line in lines_in}
    if any(f not in (None, 1, 1.0) for f in factors):
        warnings.append('部分判定线的bpmfactor不为1, 已按1处理(与Phira一致)')
    n_layers = max((len([ly for ly in (line.get('eventLayers') or []) if ly]) for line in lines_in), default=0)

    lines_out = []
    skipped_fake = 0
    n_eased = 0

    for li, line in enumerate(lines_in):
        m = motions[li]
        layers = [ly for ly in (line.get('eventLayers') or []) if ly]

        # ---- 判定线位置 + 角度(同一组断点, 同时输出move与rotate)
        bps = sorted({0.0} | m.breakpoints())
        move_segs, rot_segs = _piecewise(bps, m.state, m.is_linear,
                                         [((0, 1), (TOL_POS, TOL_POS)), ((2,), (TOL_ROT,))])
        first = m.state(bps[0], False)

        def fx(v): return v / RPE_CANVAS_W + 0.5
        def fy(v): return v / RPE_CANVAS_H + 0.5

        move = [{'startTime': UNIT_SENTINEL, 'endTime': v3t(bps[0]),
                 'start': fx(first[0]), 'end': fx(first[0]), 'start2': fy(first[1]), 'end2': fy(first[1])}]
        rotate = [{'startTime': UNIT_SENTINEL, 'endTime': v3t(bps[0]), 'start': -first[2], 'end': -first[2]}]
        for t0, t1, v0, v1 in move_segs:
            move.append({'startTime': v3t(t0), 'endTime': v3t(t1),
                         'start': fx(v0[0]), 'end': fx(v1[0]), 'start2': fy(v0[1]), 'end2': fy(v1[1])})
        for t0, t1, v0, v1 in rot_segs:
            rotate.append({'startTime': v3t(t0), 'endTime': v3t(t1), 'start': -v0[0], 'end': -v1[0]})
        last = m.state(bps[-1], False)
        move.append({'startTime': v3t(bps[-1]), 'endTime': TAIL,
                     'start': fx(last[0]), 'end': fx(last[0]), 'start2': fy(last[1]), 'end2': fy(last[1])})
        rotate.append({'startTime': v3t(bps[-1]), 'endTime': TAIL, 'start': -last[2], 'end': -last[2]})
        if len(move_segs) + len(rot_segs) > 2 * len(bps):
            n_eased += 1

        # ---- 透明度(不影响自动打歌, 仅保持信息完整)
        abps = sorted({0.0} | m.alpha.breakpoints())
        (asegs,) = _piecewise(abps, lambda t, before: (m.alpha.value(t, before),), m.alpha.is_linear,
                              [((0,), (TOL_ALPHA,))])
        a0 = m.alpha.value(abps[0]) / 255.0 if m.alpha.layers else 1.0
        alpha = [{'startTime': UNIT_SENTINEL, 'endTime': v3t(abps[0]), 'start': a0, 'end': a0}]
        if m.alpha.layers:
            for t0, t1, v0, v1 in asegs:
                alpha.append({'startTime': v3t(t0), 'endTime': v3t(t1), 'start': v0[0] / 255.0, 'end': v1[0] / 255.0})
            al = m.alpha.value(abps[-1]) / 255.0
            alpha.append({'startTime': v3t(abps[-1]), 'endTime': TAIL, 'start': al, 'end': al})
        else:
            alpha[0]['endTime'] = TAIL

        # ---- 速度(各层相加, 每段取中点值; 只用于推导floorPosition)
        speed_ch = Channel(layers, 'speedEvents', bpm)
        sbps = sorted({0.0} | speed_ch.breakpoints())
        speed_raw = []
        cur = 0.0
        pts = sbps + [max(sbps[-1], 0.0) + 1.0]
        for a, b in zip(pts, pts[1:]):
            if b <= a:
                continue
            value = (speed_ch.value((a + b) / 2) if speed_ch.layers else 10.0) / SPEED_SCALE
            s_, e_ = v3t(a), v3t(b)
            speed_raw.append({'startTime': s_, 'endTime': e_, 'value': value, '_floor': cur})
            cur += 1.875 * (e_ - s_) * value / v3_bpm
        speed_raw[-1]['endTime'] = TAIL

        def floor_at(t: float) -> float:
            for e in speed_raw:
                if e['startTime'] <= t <= e['endTime']:
                    return e['_floor'] + (t - e['startTime']) * e['value'] * 1.875 / v3_bpm
            return 0.0

        # ---- 音符
        above, below = [], []
        for n in line.get('notes', []) or []:
            if n.get('isFake'):
                skipped_fake += 1
                continue
            if n['type'] not in TYPE_MAP:
                warnings.append(f'判定线{li}: 未知音符类型{n["type"]}已跳过')
                continue
            st = bpm.time(n['startTime'])
            en = bpm.time(n.get('endTime', n['startTime']))
            t = v3t(st)
            ti = int(round(t)) if abs(t - round(t)) < 1e-6 else t
            out = {
                'type': TYPE_MAP[n['type']],
                'time': ti,
                'positionX': float(n.get('positionX', 0.0)) / NOTE_X_SCALE,
                'holdTime': max(0.0, v3t(en) - v3t(st)) if n['type'] == 2 else 0.0,
                'speed': n.get('speed', 1.0),
                'floorPosition': floor_at(ti),
            }
            (above if n.get('above', 1) == 1 else below).append(out)

        for evs in (move, rotate, alpha):
            for e in evs:
                for k in e:
                    if k in ('startTime', 'endTime'):
                        e[k] = _round_time(e[k])
                    else:
                        e[k] = round(e[k], 7 if k in ('start', 'end', 'start2', 'end2') and evs is move else 5)

        lines_out.append({
            'bpm': v3_bpm,
            'notesAbove': above,
            'notesBelow': below,
            'speedEvents': [{k: v for k, v in e.items() if k != '_floor'} for e in speed_raw],
            'judgeLineMoveEvents': move,
            'judgeLineRotateEvents': rotate,
            'judgeLineDisappearEvents': alpha,
        })

    if n_layers > 1:
        warnings.append(f'事件层最多{n_layers}层, 已按Phira的规则逐层相加')
    if n_father:
        warnings.append(f'{n_father}条判定线绑定了父线, 已换算成屏幕上的实际位置')
    if n_eased:
        warnings.append(f'{n_eased}条判定线含缓动/曲线运动, 已细分为线性段')
    if skipped_fake:
        warnings.append(f'已跳过{skipped_fake}个fake音符(不参与判定, 自动打歌无需执行)')

    out = {
        'formatVersion': 3,
        # RPE的offset单位为毫秒(Phira: offset / 1000)
        'offset': float(meta.get('offset', 0.0) or 0.0) / 1000.0,
        'judgeLineList': lines_out,
        # 转换器版本(用于识别旧版本转换的谱面)
        'phisapRpeConverter': 2,
    }
    return out, warnings


__all__ = ['is_rpe', 'detect_kind', 'rpe_to_official_v3', 'rpe_beat', 'TYPE_MAP', 'event_tween', 'BpmList']
