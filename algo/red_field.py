"""Phigros 4.0.1 噪点红场(blockAreaList)。

几何按 4.0.1(versionCode 157) 的 PreviewBlockControl / JudgeControl 还原:

- 坐标是屏幕比例, 原点在左下, y 向上。phisap 的像素坐标原点在左上, y 向下。
- 挡点击的只有 enableTime <= t < disableTime。appear~disappear 是淡入淡出,
  enable==disable 的块整段都不挡点击。把淡入也算进死区, 垂线上明明有解也会被判成没位置。
- 缓动不是 RPE 那张表。0 线性; 1–3 二次 in/out/in-out; 4–6 三次; 7–9 四次;
  10–12 五次; 13 恒为 0(停在这一关键帧); 14 恒为 1(立刻跳到下一关键帧)。
  用的是当前关键帧的 ease, 不是上一个的。
- 变换顺序: 先绕各段缩放锚点改中心和尺寸(锚点不插值, 已完成的段用上一段的锚点),
  再绕旋转锚点转, 最后把中心平移到 moveEvents.endPosition 相对原中心的偏移。
- 普通块取并集, subtract 按覆盖次数的奇偶, 两者再异或。单独的 subtract 也挡点击;
  和普通块重叠的地方互相抵消, 所以铺满屏幕的 subtract 能把底下的红场挖掉。
  触控死区比填色矩形再缩进屏幕高度的 3%(每边最多缩掉边长的 1/4), 孔则外扩同样多,
  并且必须同时落在填色遮罩里。规划躲开的是填色矩形: 它包住死区, 贴着噪点边沿点下去
  取整之后还会进死区。
"""
from __future__ import annotations

import math
import os
import re
import struct
from bisect import bisect_right
from dataclasses import dataclass, field
from functools import lru_cache

from .algo_base import PAUSE_BUTTON_BOX, PAUSE_EXIT_MARGIN, _pause_cut, _perpendicular_room

SCREEN_W = 1280.0
SCREEN_H = 720.0
# 规划时再离开填色矩形这么多像素。贴着边落下, 缩放/取整之后还会进死区。
RED_MARGIN_PX = 6.0
RED_CLEARANCE_PX = 28.0
# 原生触控遮罩相对填色矩形的缩进: 屏幕高度的 3%, 每边最多缩掉该边边长的 1/4。
TOUCH_INSET_PX = SCREEN_H * 0.03
TOUCH_INSET_CAP = 0.25
TIME_PAD_SEC = 0.040
_SCREEN_INSET = 2.0


def _f32(value) -> float:
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


@lru_cache(maxsize=15)
def _ease_table(kind: int) -> tuple[float, ...]:
    # GetEase.Instantiation 的 101 个采样, GetEaseWithProgress 再线性插值。
    values = []
    for i in range(101):
        t = _f32(i / 100)
        if kind == 0:
            value = t
        elif kind == 13:
            value = 0.0
        elif kind == 14:
            value = 1.0
        else:
            power = (kind - 1) // 3 + 2
            direction = (kind - 1) % 3
            if direction == 0:
                value = _f32(t ** power)
            elif direction == 1:
                value = _f32(1 - _f32(_f32(1 - t) ** power))
            elif i < 50:
                value = _f32(_f32(_f32(2 * i / 100) ** power) * 0.5)
            else:
                value = _f32(_f32(_f32(1 - _f32(_f32(1 - _f32(2 * (i - 50) / 100)) ** power)) * 0.5) + 0.5)
        values.append(_f32(value))
    return tuple(values)


def _ease(progress: float, kind) -> float:
    try:
        kind = int(kind or 0)
    except (TypeError, ValueError):
        kind = 0
    if kind < 0 or kind > 14:
        kind = 0
    table = _ease_table(kind)
    position = _f32(_f32(progress) * 100)
    index = int(position)
    if index < 0:
        return table[0]
    if index >= 100:
        return table[100]
    return _f32(table[index] + _f32(_f32(position - index) * _f32(table[index + 1] - table[index])))


def _xy(d, default=(0.0, 0.0)) -> tuple[float, float]:
    if not isinstance(d, dict):
        return default
    return _f32(d.get('x', default[0]) or 0), _f32(d.get('y', default[1]) or 0)


def _world(p: tuple[float, float]) -> tuple[float, float]:
    return ((p[0] - 0.5) * SCREEN_W, (p[1] - 0.5) * SCREEN_H)


def _safe_div(a: float, b: float) -> float:
    # Mathf.Approximately(b, 0) 才当除零。返回 1 表示这一步不挪中心。
    return 1.0 if abs(b) < 1e-12 else a / b


def _merge(ivs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ivs = sorted((a, b) for a, b in ivs if b > a + 1e-6)
    if not ivs:
        return []
    out = [list(ivs[0])]
    for a, b in ivs[1:]:
        if a <= out[-1][1] + 1e-4:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _subtract_one(room: tuple[float, float], blocked: list[tuple[float, float]]) -> list[tuple[float, float]]:
    lo, hi = room
    if hi <= lo:
        return []
    out = []
    cursor = lo
    for a, b in blocked:
        if b <= cursor:
            continue
        if a >= hi:
            break
        if a > cursor:
            out.append((cursor, min(a, hi)))
        cursor = max(cursor, b)
        if cursor >= hi:
            break
    if cursor < hi:
        out.append((cursor, hi))
    return [(a, b) for a, b in out if b > a + 1e-3]


def _xor_mask(spans: list[tuple[float, float, bool]]) -> list[tuple[float, float]]:
    """spans: (s0, s1, subtract)。普通块取并集, subtract 按奇偶, 两者再异或。"""
    if not spans:
        return []
    events = []
    for s0, s1, sub in spans:
        events.append((s0, 1, sub))
        events.append((s1, -1, sub))
    events.sort(key=lambda e: (e[0], -e[1]))
    normal = subtract = 0
    red = []
    prev = None

    def is_red() -> bool:
        return (normal > 0) ^ (subtract % 2 == 1)

    for s, delta, sub in events:
        if prev is not None and s > prev + 1e-9 and is_red():
            red.append((prev, s))
        if sub:
            subtract += delta
        else:
            normal += delta
        prev = s
    return _merge(red)


def _slab(x, y, dx, dy, minx, maxx, miny, maxy):
    lo, hi = -math.inf, math.inf
    for p, d, a, b in ((x, dx, minx, maxx), (y, dy, miny, maxy)):
        if abs(d) < 1e-12:
            if p < a - 1e-9 or p > b + 1e-9:
                return None
            continue
        t0, t1 = (a - p) / d, (b - p) / d
        if t0 > t1:
            t0, t1 = t1, t0
        lo = max(lo, t0)
        hi = min(hi, t1)
        if lo > hi:
            return None
    return lo, hi


def _touch_half(full: float, inset: float, subtract: bool) -> float:
    if full < 1e-4:
        return 0.0
    sign = 1.0 if subtract else -1.0
    return full * (0.5 + sign * min(TOUCH_INSET_CAP, inset / full))


@dataclass
class _Block:
    subtract: bool
    enable: float
    disable: float
    appear: float
    disappear: float
    bl: tuple[float, float]
    tr: tuple[float, float]
    scales: list
    rots: list
    moves: list
    _rc: dict = field(default_factory=dict, repr=False, compare=False)

    def covers(self, t: float) -> bool:
        return self.enable <= t < self.disable

    def rect_at(self, t: float) -> tuple[float, float, float, float, float]:
        """左上角像素坐标下的 (cx, cy, width, height, angle_rad)。angle 是图表角度取负。"""
        key = int(round(t * 1000))
        hit = self._rc.get(key)
        if hit is not None:
            return hit
        hit = self._rect(key / 1000.0)
        self._rc[key] = hit
        if len(self._rc) > 160:
            self._rc.pop(next(iter(self._rc)))
        return hit

    def _event(self, items: list, t: float, kind: str):
        if not items:
            return -1, None
        times = [e['time'] for e in items]
        index = bisect_right(times, t) - 1
        if index < 0:
            return index, None
        current = items[index]
        if index == len(items) - 1:
            return index, current
        nxt = items[index + 1]
        duration = nxt['time'] - current['time']
        u0 = 0.0 if duration <= 0 else _f32(_f32(t - current['time']) / duration)
        if kind == 'rot':
            u = max(0.0, min(1.0, _ease(u0, current['ease'])))
            return index, {
                'time': current['time'], 'anchor': current['anchor'], 'ease': current['ease'],
                'rotation': current['rotation'] + (nxt['rotation'] - current['rotation']) * u,
            }
        axes = []
        for axis in range(2):
            u = max(0.0, min(1.0, _ease(u0, current['ease'][axis])))
            a, b = current['value'][axis], nxt['value'][axis]
            axes.append(a + (b - a) * u)
        return index, {
            'time': current['time'], 'anchor': current['anchor'], 'ease': current['ease'],
            'value': (axes[0], axes[1]), 'value0': current['value'],
        }

    def _rect(self, t: float) -> tuple[float, float, float, float, float]:
        lo, hi = _world(self.bl), _world(self.tr)
        original = ((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5)
        center = original
        size = (hi[0] - lo[0], hi[1] - lo[1])
        index, current = self._event(self.scales, t, 'scale')
        if current is not None:
            steps = [(self.scales[i], self.scales[i + 1]['value']) for i in range(index)]
            if index < len(self.scales) - 1:
                steps.append((self.scales[index], current['value']))
            for event, value in steps:
                anchor = _world(event['anchor'])
                ratio = (_safe_div(value[0], event['value'][0]), _safe_div(value[1], event['value'][1]))
                center = (anchor[0] + ratio[0] * (center[0] - anchor[0]),
                          anchor[1] + ratio[1] * (center[1] - anchor[1]))
            size = (abs(size[0] * current['value'][0]), abs(size[1] * current['value'][1]))
        index, current = self._event(self.rots, t, 'rot')
        angle = 0.0
        if current is not None:
            steps = [(self.rots[i], self.rots[i + 1]['rotation']) for i in range(index)]
            if index < len(self.rots) - 1:
                steps.append((self.rots[index], current['rotation']))
            for event, value in steps:
                anchor = _world(event['anchor'])
                delta = math.radians(value - event['rotation'])
                c, s = math.cos(delta), math.sin(delta)
                dx, dy = center[0] - anchor[0], center[1] - anchor[1]
                center = (anchor[0] + c * dx - s * dy, anchor[1] + s * dx + c * dy)
            angle = current['rotation']
        _, current = self._event(self.moves, t, 'move')
        if current is not None:
            pos = _world(current['value'])
            center = (center[0] + pos[0] - original[0], center[1] + pos[1] - original[1])
        return (center[0] + SCREEN_W * 0.5, SCREEN_H * 0.5 - center[1],
                abs(size[0]), abs(size[1]), -math.radians(angle))

    def _frame(self, t: float, margin_px: float, touch: bool):
        cx, cy, width, height, angle = self.rect_at(t)
        if min(width, height) < 1e-4:
            return None
        if touch:
            hx = _touch_half(width, TOUCH_INSET_PX, self.subtract) + margin_px
            hy = _touch_half(height, TOUCH_INSET_PX, self.subtract) + margin_px
        else:
            hx = width * 0.5 + margin_px
            hy = height * 0.5 + margin_px
        if hx <= 0 or hy <= 0:
            return None
        c, s = math.cos(angle), math.sin(angle)
        return cx, cy, hx, hy, c, s

    def contains_px(self, x: float, y: float, t: float, margin_px: float = 0.0, touch: bool = False) -> bool:
        frame = self._frame(t, margin_px, touch)
        if frame is None:
            return False
        cx, cy, hx, hy, c, s = frame
        dx, dy = x - cx, y - cy
        return abs(dx * c + dy * s) <= hx and abs(-dx * s + dy * c) <= hy

    def intersect_s(self, x_px: float, y_px: float, nx: float, ny: float, t: float,
                    margin_px: float, touch: bool = False) -> tuple[float, float] | None:
        """点 (x_px, y_px) + s*(nx, ny) 落在这块(外扩 margin_px)里的 s 区间。"""
        frame = self._frame(t, margin_px, touch)
        if frame is None:
            return None
        cx, cy, hx, hy, c, s = frame
        dx, dy = x_px - cx, y_px - cy
        return _slab(dx * c + dy * s, -dx * s + dy * c, nx * c + ny * s, -nx * s + ny * c,
                     -hx, hx, -hy, hy)


def _norm_scale(raw: dict, cx: float, cy: float) -> dict:
    return {
        'time': _f32(raw.get('time', 0) or 0),
        'anchor': _xy(raw.get('anchor'), (cx, cy)),
        'ease': (int(raw.get('easeTypeX', raw.get('easeType', 0)) or 0),
                 int(raw.get('easeTypeY', raw.get('easeType', 0)) or 0)),
        'value': _xy(raw.get('scale'), (1.0, 1.0)),
    }


def _norm_move(raw: dict, cx: float, cy: float) -> dict:
    return {
        'time': _f32(raw.get('time', 0) or 0),
        'anchor': (cx, cy),
        'ease': (int(raw.get('easeTypeX', raw.get('easeType', 0)) or 0),
                 int(raw.get('easeTypeY', raw.get('easeType', 0)) or 0)),
        'value': _xy(raw.get('endPosition'), (cx, cy)),
    }


def _norm_rot(raw: dict, cx: float, cy: float) -> dict:
    return {
        'time': _f32(raw.get('time', 0) or 0),
        'anchor': _xy(raw.get('anchor'), (cx, cy)),
        'ease': int(raw.get('easeType', 0) or 0),
        'rotation': _f32(raw.get('rotation', 0) or 0),
    }


def _sorted_events(events: list) -> list:
    return sorted(events, key=lambda e: e['time'])


def _parse_block(raw: dict) -> _Block | None:
    if not isinstance(raw, dict):
        return None
    try:
        bl, tr = raw.get('bottomLeftPercentage') or {}, raw.get('topRightPercentage') or {}
        x0, y0 = float(bl.get('x', 0) or 0), float(bl.get('y', 0) or 0)
        x1, y1 = float(tr.get('x', 0) or 0), float(tr.get('y', 0) or 0)
    except (TypeError, ValueError):
        return None
    enable = float(raw.get('enableTime', raw.get('appearTime', 0)) or 0)
    disable = float(raw.get('disableTime', raw.get('disappearTime', enable)) or enable)
    appear = float(raw.get('appearTime', enable) or 0)
    disappear = float(raw.get('disappearTime', disable) or 0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return _Block(
        subtract=bool(raw.get('isSubtract')),
        enable=enable, disable=disable,
        appear=appear, disappear=disappear,
        bl=(x0, y0), tr=(x1, y1),
        scales=_sorted_events(_norm_scale(e, cx, cy) for e in (raw.get('scaleEvents') or [])),
        rots=_sorted_events(_norm_rot(e, cx, cy) for e in (raw.get('rotateEvents') or [])),
        moves=_sorted_events(_norm_move(e, cx, cy) for e in (raw.get('moveEvents') or [])),
    )


def _unit(sa: float, ca: float) -> tuple[float, float]:
    nx, ny = -sa, ca
    nlen = math.hypot(nx, ny)
    if nlen < 1e-8:
        return 0.0, 1.0
    return nx / nlen, ny / nlen


class RedField:
    """某一时刻屏幕上的红场。没有块, 或不在 enable 区间时, contains 恒为 False。"""

    def __init__(self, blocks: list[_Block]):
        self.blocks = blocks
        self._active_cache: dict[int, list[_Block]] = {}

    @classmethod
    def from_chart(cls, chart) -> 'RedField':
        raw = getattr(chart, 'block_areas', None) or []
        blocks = [b for b in (_parse_block(x) for x in raw) if b is not None]
        return cls(blocks)

    def __bool__(self) -> bool:
        return bool(self.blocks)

    def active(self, t: float) -> list[_Block]:
        # 规划只有 1ms 分辨率。用四舍五入后的毫秒再换算回去, 避免同一毫秒算出两套活动块。
        key = int(round(t * 1000))
        hit = self._active_cache.get(key)
        if hit is None:
            tq = key / 1000.0
            hit = [b for b in self.blocks if b.covers(tq)]
            self._active_cache[key] = hit
        return hit

    def _masked(self, x_px: float, y_px: float, t_sec: float, margin_px: float, touch: bool) -> bool:
        t_sec = round(t_sec * 1000) / 1000.0
        active = self.active(t_sec)
        if not active:
            return False
        normal = False
        subtract = 0
        for b in active:
            if b.contains_px(x_px, y_px, t_sec, margin_px, touch):
                if b.subtract:
                    subtract ^= 1
                else:
                    normal = True
        return normal ^ bool(subtract)

    def contains(self, x_px: float, y_px: float, t_sec: float, margin_px: float = 0.0) -> bool:
        """像素坐标(y 向下)在 t_sec 是否落在挡点击的填色遮罩里。

        只在 enable 区间。普通块取并集, subtract 按奇偶, 两者再异或。
        margin_px 表示离这张精确遮罩不到这么多像素也算进红场。不能先把每块矩形外扩再异或,
        否则外扩的普通块会和 subtract 抵消, 死区中间被判成空地。
        """
        if self._masked(x_px, y_px, t_sec, 0.0, touch=False):
            return True
        if margin_px <= 0:
            return False
        # 四向采样是遮罩外扩的下界, 用来提前离开边沿, 不会把异或结果翻过来。
        m = margin_px
        return any(self._masked(x_px + dx, y_px + dy, t_sec, 0.0, touch=False)
                   for dx, dy in ((m, 0.0), (-m, 0.0), (0.0, m), (0.0, -m)))

    def touch_blocked(self, x_px: float, y_px: float, t_sec: float) -> bool:
        """原生触控死区: 填色遮罩和缩进/外扩后的遮罩同时盖住才算。"""
        return (self._masked(x_px, y_px, t_sec, 0.0, touch=False)
                and self._masked(x_px, y_px, t_sec, 0.0, touch=True))

    def dead(self, x: float, y: float, t: float, clearance: float = RED_CLEARANCE_PX) -> bool:
        """这一毫秒、这个点在边距内是不是红场。红场在动, 不把前后扫过的区域并进来。"""
        return self.contains(x, y, t, RED_MARGIN_PX + clearance)

    def _red_at(self, x: float, y: float, nx: float, ny: float, t: float,
                margin_px: float) -> list[tuple[float, float]]:
        active = self.active(t)
        if not active:
            return []
        spans = []
        for b in active:
            iv = b.intersect_s(x, y, nx, ny, t, 0.0, touch=False)
            if iv is None or iv[1] <= iv[0] + 1e-4:
                continue
            spans.append((iv[0], iv[1], b.subtract))
        # 先异或, 再把得到的死区向外扩。外扩输入矩形会把重叠处的异或结果翻掉。
        red = _xor_mask(spans)
        if margin_px > 0.5:
            red = _merge([(a - margin_px, b + margin_px) for a, b in red])
        return red

    def safe_intervals(self, x: float, y: float, sa: float, ca: float, t: float,
                       clearance: float = RED_CLEARANCE_PX) -> list[tuple[float, float]]:
        """这一毫秒, 沿垂直于判定线的方向, 屏幕内、不碰暂停键、不进红场的 s 区间。

        s 是沿 (-sa, ca) 走的像素。投影(判定)不变。红场在动, 只看这一下的位置。
        """
        nx, ny = _unit(sa, ca)
        room_lo, room_hi = _perpendicular_room(x, y, nx, ny)
        lo, hi = room_lo + _SCREEN_INSET, room_hi - _SCREEN_INSET
        red = self._red_at(x, y, nx, ny, t, max(0.0, RED_MARGIN_PX + clearance))
        pause = _pause_cut(x, y, nx, ny)
        if pause is not None:
            red = _merge(red + [pause])
        if hi > lo:
            found = _subtract_one((lo, hi), red)
            if found:
                return found
        # 2 像素内缩会把贴边的唯一空缝丢掉，整条垂线就被判成没位置。
        # 内缩找不到时才用到屏幕边缘，仍然减去红场，不把触点放进噪区。
        if room_hi <= room_lo:
            return []
        return _subtract_one((room_lo, room_hi), red)

    def _pick(self, x: float, y: float, nx: float, ny: float, ivs, prefer_s: float):
        best = None
        best_key = None
        for a, b in ivs:
            if b - a < 1.0:
                continue
            s = min(max(prefer_s, a), b)
            key = (0 if a - 1e-3 <= prefer_s <= b + 1e-3 else 1, abs(s - prefer_s), abs(s))
            if best_key is None or key < best_key:
                best_key = key
                best = s
        if best is None:
            return None
        return x + nx * best, y + ny * best, best

    def _clear_ahead(self, x: float, y: float, t: float, pad: float = 0.024) -> bool:
        """这个屏幕点在接下来 pad 秒里都不进红场。注入晚一帧时, 只看这一毫秒的点会进死区。"""
        step = 0.008
        u = t
        end = t + pad
        while u <= end + 1e-9:
            if self.contains(x, y, u):
                return False
            u += step
        return True

    def _pick_stable(self, x: float, y: float, nx: float, ny: float, ivs, prefer_s: float, t: float):
        """同一条垂线上, 优先选晚一帧也不会进红场的点。没有这样的点就交给精确时刻的选择。"""
        best = None
        best_key = None
        for a, b in ivs:
            if b - a < 1.0:
                continue
            span = b - a
            lip = min(8.0, span / 2)
            samples = (
                min(max(prefer_s, a), b),
                (a + b) / 2,
                a + lip,
                b - lip,
            )
            for s in samples:
                px, py = x + nx * s, y + ny * s
                if not self._clear_ahead(px, py, t):
                    continue
                key = (abs(s - prefer_s), abs(s))
                if best_key is None or key < best_key:
                    best_key = key
                    best = (px, py, s)
        return best

    def vertical_slot(self, x: float, y: float, sa: float, ca: float, t: float,
                      prefer_s: float = 0.0) -> tuple[float, float, float] | None:
        """在音符的垂直线上选一个这一毫秒安全的点。这一下整条线都在红场里时返回 None。

        优先留在 prefer_s 那一侧, 并且尽量离填色远一点。晚一帧仍在红场外的点优先;
        整条线都只在这一毫秒安全时, 退回精确时刻的点, 不因此判成没位置。
        只有贴着边才有缝时, 退到仍在矩形外的位置, 也不把触点放进噪区。
        """
        nx, ny = _unit(sa, ca)
        exact = None
        for clearance in (RED_CLEARANCE_PX, 8.0, 0.0, -RED_MARGIN_PX):
            ivs = self.safe_intervals(x, y, sa, ca, t, clearance)
            stable = self._pick_stable(x, y, nx, ny, ivs, prefer_s, t)
            if stable is not None:
                return stable
            if exact is None:
                exact = self._pick(x, y, nx, ny, ivs, prefer_s)
        return exact

    def segment_hits(self, p0: tuple[float, float], p1: tuple[float, float], t: float) -> bool:
        """从 p0 滑到 p1 会不会穿过当前红场。长条换边之前先查这个, 避免手指扫进噪区。"""
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        dist = math.hypot(dx, dy)
        if dist < 1.0:
            return self.contains(p0[0], p0[1], t, RED_MARGIN_PX)
        nx, ny = dx / dist, dy / dist
        red = self._red_at(p0[0], p0[1], nx, ny, t, RED_MARGIN_PX)
        for a, b in red:
            if a < dist - 0.5 and b > 0.5:
                return True
        return False


def chart_has_block_areas(path: str) -> bool:
    """谱面文件里有没有非空的 blockAreaList。只扫字节, 不整份解析。"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return False
    return bool(re.search(br'"blockAreaList"\s*:\s*\[\s*\{', data))


def chart_file_has_block_areas(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and chart_has_block_areas(path)


__all__ = ['RedField', 'RED_MARGIN_PX', 'RED_CLEARANCE_PX', 'TIME_PAD_SEC',
           'TOUCH_INSET_PX', 'chart_has_block_areas', 'chart_file_has_block_areas']
