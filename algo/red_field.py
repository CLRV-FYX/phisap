"""Phigros 4.0.1 噪点红场(blockAreaList)。

谱面里没有单独的说明, 几何和时间窗口是从 Chart_AT.json 对出来的, 并被测试钉死:

- 坐标是屏幕比例, 原点在左下, y 向上。phisap 的像素坐标原点在左上, y 向下。
- 只有 enableTime~disableTime(秒, 和音符的 line.seconds 同一时间轴)内挡点击。
  appear/disappear 只是淡入淡出, 不挡点击。
- 矩形先按 bottomLeft/topRight 定大小, 再绕锚点缩放、旋转, 最后把中心挪到
  moveEvents.endPosition。缓动用起始关键帧的 ease(outgoing), 索引和 rpe.py 的 RPE 表一致。
- 同一时刻多个块重叠时, 列表里靠后的块说了算。isSubtract 为真表示这块是挖孔
  (从红场里减掉), 不是另一块红场。Chart_AT 在 64s 附近有一块铺满屏幕的普通块,
  紧跟着一块同样铺满屏幕的 subtract 块, 同时还有音符: 如果 subtract 也挡点击,
  这一段就没法打。所以 subtract 必须是孔。
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from rpe import _EASINGS, _linear

from .algo_base import in_pause_box

SCREEN_W = 1280.0
SCREEN_H = 720.0
# 矩形外再扩这么多像素才算出红场, 抵消取整和缓动插值的误差
RED_MARGIN_PX = 6.0
# 沿垂直方向往外探路的步长
_STEP = 6.0
_TIME_EPS = 5e-4  # 半毫秒, 盖住音符时间取整


def _ease(et, u: float) -> float:
    try:
        et = int(et or 0)
    except (TypeError, ValueError):
        et = 0
    if et <= 1:
        return u
    f = _EASINGS[et] if 0 <= et < len(_EASINGS) else _linear
    try:
        return float(f(u))
    except (TypeError, ValueError):
        return u


def _lerp(a: float, b: float, u: float) -> float:
    return a + (b - a) * u


def _collapse(events: list) -> list:
    out = []
    for e in sorted(events or [], key=lambda ev: float(ev.get('time', 0) or 0)):
        if out and abs(float(e.get('time', 0) or 0) - float(out[-1].get('time', 0) or 0)) < 1e-9:
            out[-1] = e
        else:
            out.append(e)
    return out


def _sample_xy(events, t: float, xy_of, ease_x, ease_y, default):
    events = _collapse(events)
    if not events:
        return default
    if t <= float(events[0].get('time', 0) or 0):
        return xy_of(events[0])
    if t >= float(events[-1].get('time', 0) or 0):
        return xy_of(events[-1])
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        ta, tb = float(a.get('time', 0) or 0), float(b.get('time', 0) or 0)
        if ta <= t <= tb:
            span = tb - ta
            u = 0.0 if span <= 0 else (t - ta) / span
            ax, ay = xy_of(a)
            bx, by = xy_of(b)
            return (_lerp(ax, bx, _ease(ease_x(a), u)),
                    _lerp(ay, by, _ease(ease_y(a), u)))
    return xy_of(events[-1])


def _sample_scalar(events, t: float, val_of, ease_of, default: float) -> float:
    events = _collapse(events)
    if not events:
        return default
    if t <= float(events[0].get('time', 0) or 0):
        return val_of(events[0])
    if t >= float(events[-1].get('time', 0) or 0):
        return val_of(events[-1])
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        ta, tb = float(a.get('time', 0) or 0), float(b.get('time', 0) or 0)
        if ta <= t <= tb:
            span = tb - ta
            u = 0.0 if span <= 0 else (t - ta) / span
            return _lerp(val_of(a), val_of(b), _ease(ease_of(a), u))
    return val_of(events[-1])


def _xy(d, default=(0.0, 0.0)):
    if not isinstance(d, dict):
        return default
    return float(d.get('x', default[0]) or 0), float(d.get('y', default[1]) or 0)


def _rot(dx, dy, deg: float):
    a = deg * math.pi / 180.0
    c, s = math.cos(a), math.sin(a)
    return dx * c - dy * s, dx * s + dy * c


@dataclass
class _Block:
    subtract: bool
    enable: float
    disable: float
    cx: float
    cy: float
    hx: float
    hy: float
    moves: list
    scales: list
    rots: list

    def covers(self, t: float) -> bool:
        return self.enable - _TIME_EPS <= t <= self.disable + _TIME_EPS

    def _pose(self, t: float):
        mx, my = _sample_xy(
            self.moves, t,
            lambda e: _xy(e.get('endPosition'), (self.cx, self.cy)),
            lambda e: e.get('easeTypeX', e.get('easeType', 0)),
            lambda e: e.get('easeTypeY', e.get('easeType', 0)),
            (self.cx, self.cy))
        sx, sy = _sample_xy(
            self.scales, t,
            lambda e: _xy(e.get('scale'), (1.0, 1.0)),
            lambda e: e.get('easeTypeX', e.get('easeType', 0)),
            lambda e: e.get('easeTypeY', e.get('easeType', 0)),
            (1.0, 1.0))
        ax, ay = _sample_xy(
            self.scales, t,
            lambda e: _xy(e.get('anchor'), (self.cx, self.cy)),
            lambda e: e.get('easeTypeX', 0),
            lambda e: e.get('easeTypeY', 0),
            (self.cx, self.cy))
        rot = _sample_scalar(
            self.rots, t,
            lambda e: float(e.get('rotation', 0) or 0),
            lambda e: e.get('easeType', 0),
            0.0)
        rx, ry = _sample_xy(
            self.rots, t,
            lambda e: _xy(e.get('anchor'), (self.cx, self.cy)),
            lambda e: 0,
            lambda e: 0,
            (self.cx, self.cy))
        # 锚点写在未平移的谱面坐标里, 跟着中心一起挪
        return (mx, my), (sx, sy), rot, (ax - self.cx, ay - self.cy), (rx - self.cx, ry - self.cy)

    def contains_norm(self, x: float, y: float, t: float, margin_x: float, margin_y: float) -> bool:
        (mx, my), (sx, sy), rot, (asx, asy), (arx, ary) = self._pose(t)
        if abs(sx) < 1e-3 or abs(sy) < 1e-3:
            return False  # 缩没了, 没有可点区域
        # 逆变换: 平移回中心, 绕旋转锚点转回去, 再绕缩放锚点缩回去
        lx, ly = x - mx, y - my
        dx, dy = lx - arx, ly - ary
        ux, uy = _rot(dx, dy, -rot)
        ux, uy = ux + arx, uy + ary
        lx = asx + (ux - asx) / sx
        ly = asy + (uy - asy) / sy
        return abs(lx) <= self.hx + margin_x and abs(ly) <= self.hy + margin_y


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
    if disable < enable:
        enable, disable = disable, enable
    return _Block(
        subtract=bool(raw.get('isSubtract')),
        enable=enable, disable=disable,
        cx=(x0 + x1) / 2, cy=(y0 + y1) / 2,
        hx=abs(x1 - x0) / 2, hy=abs(y1 - y0) / 2,
        moves=list(raw.get('moveEvents') or []),
        scales=list(raw.get('scaleEvents') or []),
        rots=list(raw.get('rotateEvents') or []),
    )


class RedField:
    """某一时刻屏幕上的红场。没有块时 contains 恒为 False。"""

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
        # 规划只有 1ms 分辨率。用四舍五入后的毫秒再换算回去, 避免 102.9696 和 102.970
        # 共用一个缓存键却算出两套不同的活动块。
        key = int(round(t * 1000))
        hit = self._active_cache.get(key)
        if hit is None:
            tq = key / 1000.0
            hit = [b for b in self.blocks if b.covers(tq)]
            self._active_cache[key] = hit
        return hit

    def contains(self, x_px: float, y_px: float, t_sec: float, margin_px: float = RED_MARGIN_PX) -> bool:
        """像素坐标(y 向下)在 t_sec 是否落在红场里。

        重叠时列表靠后的块说了算: 普通块是红场, isSubtract 是从红场里挖掉的孔。
        """
        t_sec = round(t_sec * 1000) / 1000.0
        active = self.active(t_sec)
        if not active:
            return False
        nx = x_px / SCREEN_W
        ny = 1.0 - y_px / SCREEN_H
        mx = margin_px / SCREEN_W
        my = margin_px / SCREEN_H
        last = None
        for b in active:
            if b.contains_norm(nx, ny, t_sec, mx, my):
                last = b
        return last is not None and not last.subtract

    def _safe(self, x: float, y: float, t: float) -> bool:
        return (1.0 <= x <= SCREEN_W - 1 and 1.0 <= y <= SCREEN_H - 1
                and not in_pause_box((x, y))
                and not self.contains(x, y, t))

    def escape(self, x: float, y: float, sa: float, ca: float, t_sec: float,
               prefer_s: float = 0.0) -> tuple[float, float, float]:
        """沿垂直于判定线的方向把点挪出红场。返回 (x, y, s)。

        s 是沿 (-sa, ca) 走的距离。投影(判定)不变。挪不出去就原样返回, s=0,
        调用方必须把这根手指立刻抬起。prefer_s 非 0 时优先留在同一侧, 长条才不会来回跳。
        """
        nx, ny = -sa, ca
        if nx * nx + ny * ny < 1e-8:
            nx, ny = 0.0, 1.0
        if self._safe(x, y, t_sec):
            return x, y, 0.0

        def at(s: float):
            return x + nx * s, y + ny * s

        def search(sign: float):
            s = 0.0
            while abs(s) < 1700.0:
                s += sign * _STEP
                px, py = at(s)
                if not (0.0 <= px <= SCREEN_W and 0.0 <= py <= SCREEN_H):
                    return None
                if not self._safe(px, py, t_sec):
                    continue
                lo, hi = s - sign * _STEP, s
                for _ in range(8):
                    mid = (lo + hi) / 2
                    if self._safe(*at(mid), t_sec):
                        hi = mid
                    else:
                        lo = mid
                extra = hi + sign * 4.0
                if self._safe(*at(extra), t_sec):
                    px, py = at(extra)
                    return px, py, extra
                px, py = at(hi)
                return px, py, hi
            return None

        if abs(prefer_s) >= 1.0:
            sign = 1.0 if prefer_s > 0 else -1.0
            hit = search(sign)
            if hit is not None:
                return hit
            hit = search(-sign)
            return hit if hit is not None else (x, y, 0.0)
        a, b = search(1.0), search(-1.0)
        cands = [c for c in (a, b) if c is not None]
        if not cands:
            return x, y, 0.0
        return min(cands, key=lambda c: abs(c[2]))


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


__all__ = ['RedField', 'RED_MARGIN_PX', 'chart_has_block_areas', 'chart_file_has_block_areas']
