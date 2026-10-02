"""把规划结果(触控事件)转成 WebDriverAgent 的 W3C actions —— iOS方案的第一块砖(见 docs/ios_feasibility.md)。

为什么是“整首歌预排好再一次性交给设备”, 而不是像Android那样边播边发:
WDA 的 POST /session/<id>/actions 同步阻塞到手势放完, 单次请求的固定开销就是几十~几百毫秒(解析、
查前台应用、XCTest事件合成), 按事件逐个发根本跟不上; 但一次请求里每个输入源(手指)都是一条
带绝对时间偏移的独立时间线, 设备端按偏移自己回放。所以这里把计划拆成“每次按下~抬起=一个输入源”。

WDA 源码里的语义(appium/WebDriverAgent FBW3CActionsSynthesizer.m, 16.x), 本模块按它生成:
+ 每个输入源是独立时间线, 偏移 = 该源此前所有条目的 duration 之和(不做跨源的tick对齐);
+ 链里第一个真正的动作如果是 pointerMove, 它在 偏移+duration 处创建触点(XCPointerEventPath);
  紧跟着的 pointerDown 不会再另起一个触点; pointerUp 在当前偏移抬起;
+ 每个XCPointerEventPath只能“按下~抬起”一次(Appium文档): 所以同一个源不重复使用, 每次按下新开一个源;
+ pointerMove 的 duration 是“从上一个点移到这个点用的时间”, 中间的点由系统插值(猜测, 需真机确认);
+ 只支持 pointerType=touch; 不要带 pressure(没有3D Touch的设备会报错)。
坐标是屏幕点(point, 不是像素), 原点在左上角, 随设备方向变化(横屏时宽>高)。

本模块不做任何网络/设备操作, 只做数据转换, 可以在没有iPhone的机器上测试。
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, NamedTuple

from .algo_base import TouchAction, VirtualTouchEvent

# 触点出现在屏幕边缘外的坐标会被WDA拒绝或夹到边缘, 统一收进屏幕内留一点余量
EDGE_MARGIN_PT = 0.5
# 规划里的MOVE事件是“采样保持”: 手指停在上一个位置, 到下一个事件的时刻才跳过去(判定线瞬移时长条就是这样
# 停一阵再跳)。但密集采样(扫屏每6ms、红键每1~4ms、跟随移动判定线的长条)按连续移动处理更接近真实的手指。
# 相邻事件间隔不超过这个值的当连续移动(线性插值), 超过的当作“停在原地, 最后1ms跳过去”。
GAP_LINEAR_MS = 8


class Life(NamedTuple):
    """一次按下~抬起: pointer 的 DOWN..UP"""
    pointer: int
    t0: int                                       # 按下时刻(ms)
    t1: int                                       # 抬起时刻(ms)
    points: tuple[tuple[int, tuple[float, float]], ...]   # (ms, 位置): 第一个是按下点, 最后一个是抬起点


class Mapping(NamedTuple):
    """规划坐标(1280x720虚拟屏幕) -> 设备屏幕点: 和 main.py 的 _build_adapted 一样, 等比例缩放并居中"""
    x_offset: float
    y_offset: float
    scale: float
    width: float
    height: float

    def apply(self, pos: tuple[float, float]) -> tuple[float, float]:
        x = self.x_offset + pos[0] * self.scale
        y = self.y_offset + pos[1] * self.scale
        return (min(max(x, EDGE_MARGIN_PT), self.width - EDGE_MARGIN_PT),
                min(max(y, EDGE_MARGIN_PT), self.height - EDGE_MARGIN_PT))


def fit_mapping(width_pt: float, height_pt: float) -> Mapping:
    """横屏时的屏幕点尺寸(宽>高)。Phigros在刘海屏上的实际画面区域/安全区是否和这个一致, 需要真机确认"""
    s = min(width_pt / 1280, height_pt / 720)
    return Mapping((width_pt - 1280 * s) / 2, (height_pt - 720 * s) / 2, s, width_pt, height_pt)


def split_lives(ans: dict[int, list[VirtualTouchEvent]]) -> list[Life]:
    """把按时间排好的事件拆成一次次“按下~抬起”。

    同一毫秒同一个触点的多个MOVE只保留最后一个位置(设备端一毫秒内也只会有一个落点);
    没有按下就出现的MOVE/抬起、抬起之后遗留的MOVE忽略; 到结尾还没抬起的触点在最后一个事件处补一个抬起。"""
    lives: list[Life] = []
    open_: dict[int, list] = {}      # pointer -> [t0, [(ms, pos)...]]
    last_ms = 0
    for ms in sorted(ans):
        last_ms = ms
        for e in ans[ms]:
            if e.action == TouchAction.DOWN:
                if e.pointer in open_:                       # 没抬起就又按下: 先结束上一次
                    t0, pts = open_.pop(e.pointer)
                    lives.append(Life(e.pointer, t0, ms, tuple(pts)))
                open_[e.pointer] = [ms, [(ms, e.pos)]]
            elif e.action == TouchAction.MOVE:
                cur = open_.get(e.pointer)
                if cur is None:
                    continue
                pts = cur[1]
                if pts[-1][0] == ms:
                    pts[-1] = (ms, e.pos)
                else:
                    pts.append((ms, e.pos))
            elif e.action == TouchAction.UP:
                cur = open_.pop(e.pointer, None)
                if cur is None:
                    continue
                t0, pts = cur
                if pts[-1][0] == ms:
                    pts[-1] = (ms, e.pos if len(pts) > 1 else pts[-1][1])
                else:
                    pts.append((ms, e.pos))
                lives.append(Life(e.pointer, t0, ms, tuple(pts)))
    for pointer, (t0, pts) in open_.items():                 # 兜底: 没抬起的在最后补一个抬起
        lives.append(Life(pointer, t0, max(last_ms, pts[-1][0]), tuple(pts)))
    lives.sort(key=lambda lf: (lf.t0, lf.pointer))
    return lives


def peak_pointers(lives: Iterable[Life]) -> int:
    """同时按在屏幕上的触点数的峰值。区间取左闭右开: 同一毫秒一个抬起、一个按下算接力, 不算同时。"""
    edges = []
    for lf in lives:
        edges.append((lf.t0, 1))
        edges.append((lf.t1, -1))
    edges.sort(key=lambda e: (e[0], e[1]))       # 同一时刻先抬起(-1)后按下(+1)
    cur = peak = 0
    for _, d in edges:
        cur += d
        peak = max(peak, cur)
    return peak


def simplify(points: tuple[tuple[int, tuple[float, float]], ...], tolerance: float
             ) -> tuple[tuple[int, tuple[float, float]], ...]:
    """精简轨迹: 保留的点之间按时间线性插值, 原来每个点的位置误差都不超过 tolerance 像素(同步欧氏距离)。

    扫屏触点的三角波、红键的直线滑动、跟随静止判定线的长条都能压成寥寥几个点; 判定线瞬移留下的“跳变”
    (相邻1ms相差几百像素)会被保留。tolerance<=0 不精简。"""
    n = len(points)
    if tolerance <= 0 or n <= 2:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ta, (xa, ya) = points[a]
        tb, (xb, yb) = points[b]
        worst, idx = -1.0, -1
        span = tb - ta
        for i in range(a + 1, b):
            t, (x, y) = points[i]
            f = (t - ta) / span if span else 0.0
            d = math.hypot(x - (xa + (xb - xa) * f), y - (ya + (yb - ya) * f))
            if d > worst:
                worst, idx = d, i
        if worst > tolerance:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return tuple(p for p, k in zip(points, keep) if k)


def hold_polyline(points: tuple[tuple[int, tuple[float, float]], ...], gap: int = GAP_LINEAR_MS
                  ) -> tuple[tuple[int, tuple[float, float]], ...]:
    """把“采样保持”的事件序列变成按时间线性的折线: 间隔超过 gap 的两个事件之间, 在后一个事件前1ms
    补一个“还停在原位置”的点, 这样线性插值之后手指仍然是停到最后1ms才跳过去, 不会缓慢漂移。"""
    out = [points[0]]
    for (ta, pa), (tb, pb) in zip(points, points[1:]):
        if tb - ta > gap:
            out.append((tb - 1, pa))
        out.append((tb, pb))
    return tuple(out)


def _coord(v: float) -> float:
    return round(v, 1)


def _merge_pauses(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for it in items:
        if it['type'] == 'pause' and out and out[-1]['type'] == 'pause':
            out[-1] = {'type': 'pause', 'duration': out[-1]['duration'] + it['duration']}
        else:
            out.append(it)
    return out


def life_to_source(life: Life, source_id: str, start_ms: int, mapping: Mapping,
                   min_contact_ms: int = 0, tolerance: float = 3.0) -> dict:
    """一次按下~抬起 -> 一个W3C输入源。start_ms: 整个actions的时间零点"""
    t0, t1 = life.t0 - start_ms, max(life.t1, life.t0 + min_contact_ms) - start_ms
    pts = simplify(hold_polyline(life.points), tolerance)
    items: list[dict] = []
    if t0 > 0:
        items.append({'type': 'pause', 'duration': t0})
    x, y = mapping.apply(pts[0][1])
    cur_pos = (_coord(x), _coord(y))
    items.append({'type': 'pointerMove', 'duration': 0, 'x': cur_pos[0], 'y': cur_pos[1], 'origin': 'viewport'})
    items.append({'type': 'pointerDown', 'button': 0})
    cur = pts[0][0]
    for ms, pos in pts[1:]:
        if ms <= cur:
            continue
        x, y = mapping.apply(pos)
        nxt = (_coord(x), _coord(y))
        if nxt == cur_pos:
            items.append({'type': 'pause', 'duration': ms - cur})        # 原地不动: 用pause, 不让系统白插值
        else:
            items.append({'type': 'pointerMove', 'duration': ms - cur, 'x': nxt[0], 'y': nxt[1],
                          'origin': 'viewport'})
            cur_pos = nxt
        cur = ms
    end = t1 + start_ms
    if end > cur:
        items.append({'type': 'pause', 'duration': end - cur})
    items.append({'type': 'pointerUp', 'button': 0})
    return {'type': 'pointer', 'id': source_id, 'parameters': {'pointerType': 'touch'},
            'actions': _merge_pauses(items)}


def build_actions(ans: dict[int, list[VirtualTouchEvent]], mapping: Mapping, start_ms: int | None = None,
                  min_contact_ms: int = 0, tolerance: float = 3.0,
                  start_tap: tuple[float, float] | None = None, start_delay_ms: int = 0,
                  start_contact_ms: int = 60) -> dict:
    """整张规划 -> POST /session/<id>/actions 的请求体 {'actions': [...]}。

    start_ms: 时间零点(默认是第一个事件的时刻)。设备收到请求、开始回放的那一刻对应这个零点。
    min_contact_ms: 每次接触至少持续这么久(iOS可能丢弃过短的接触, 要真机测, 见docs); 0=不加长。
    tolerance: 轨迹精简误差(像素, 规划坐标系), 0=逐毫秒全发。
    start_tap: 规划坐标系里的一个点(Phigros的开始键, Android的"计时器同步"点的是屏幕中心)。给了就在记录的零点
        先点一下这里, 整首歌从偏移 start_delay_ms 起开始(规划里的时刻0 = 记录偏移 start_delay_ms)。
        点开始键和第一个音符之间的间隔完全由记录内部的偏移决定, 不受"请求发出 -> 第一个事件"的延迟波动影响,
        这和 Android 的"点一下屏幕, 再等固定的开始延迟"是同一个做法。此时 start_ms 参数无效。
    start_delay_ms: 见 start_tap; 必须不小于规划里最早事件的负时刻(有些谱面第一个音符之前就有扫屏触点按下)。"""
    lives = split_lives(ans)
    if start_tap is None:
        if not lives:
            return {'actions': []}
        base = lives[0].t0 if start_ms is None else start_ms
        return {'actions': [life_to_source(lf, f'p{i}', base, mapping, min_contact_ms, tolerance)
                            for i, lf in enumerate(lives)]}
    base = -int(start_delay_ms)
    if lives and lives[0].t0 < base:
        raise ValueError(f'start_delay_ms={start_delay_ms} 太小: 规划里最早的事件在 {lives[0].t0}ms, '
                         f'需要 start_delay_ms >= {-lives[0].t0}')
    tap = Life(-1, base, base + max(int(start_contact_ms), 0), ((base, start_tap),))
    sources = [life_to_source(tap, 'start', base, mapping, 0, 0.0)]
    sources += [life_to_source(lf, f'p{i}', base, mapping, min_contact_ms, tolerance) for i, lf in enumerate(lives)]
    return {'actions': sources}


def count_items(body: dict) -> int:
    return sum(len(src['actions']) for src in body['actions'])


def replay_actions(body: dict, step_ms: int = 1) -> dict[int, list[VirtualTouchEvent]]:
    """按上面记录的WDA语义把 actions 还原成触控事件(参考解释器, 不是真机行为):
    pointerMove 的 duration 内按线性插值, 每 step_ms 一个MOVE。坐标是设备屏幕点, 触点id按源顺序编号。
    用来验证转换前后判定一致(tools/ios_sim.py)、以及做单元测试。"""
    out: dict[int, list[VirtualTouchEvent]] = defaultdict(list)
    for pid, src in enumerate(body['actions']):
        t = 0.0
        pos: tuple[float, float] | None = None
        last_sent: tuple[float, float] | None = None
        down = False
        for it in src['actions']:
            kind = it['type']
            d = it.get('duration', 0)
            if kind == 'pause':
                t += d
            elif kind == 'pointerMove':
                dst = (float(it['x']), float(it['y']))
                if down and pos is not None and d > 0:
                    n = max(1, int(d // step_ms))
                    for k in range(1, n + 1):
                        f = k * step_ms / d if k < n else 1.0
                        p = (pos[0] + (dst[0] - pos[0]) * f, pos[1] + (dst[1] - pos[1]) * f)
                        if p != last_sent:
                            out[int(round(t + (d * f)))].append(VirtualTouchEvent(p, TouchAction.MOVE, pid))
                            last_sent = p
                t += d
                pos = dst
            elif kind == 'pointerDown':
                down = True
                last_sent = pos
                out[int(round(t))].append(VirtualTouchEvent(pos, TouchAction.DOWN, pid))
            elif kind == 'pointerUp':
                down = False
                out[int(round(t))].append(VirtualTouchEvent(pos, TouchAction.UP, pid))
    return out


__all__ = ['Life', 'Mapping', 'fit_mapping', 'split_lives', 'peak_pointers', 'simplify', 'life_to_source',
           'build_actions', 'count_items', 'replay_actions', 'hold_polyline', 'EDGE_MARGIN_PT', 'GAP_LINEAR_MS']
