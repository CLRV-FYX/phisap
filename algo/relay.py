"""长条瞬移接力(relay)。

问题
----
谱面常让判定线瞬移(moveX/moveY 零过渡时间跳变), 长条的判定区(沿判定线方向 ±151px 的条带)
跟着瞬移。一个长条只有一个手指时, 手指必须在瞬移的那一毫秒跳过去。可只要整条触控链路比音符
早/晚 d ms(手动同步的误差、注入延迟抖动、被卡住一下), 手指就会有 |d| ms 不在判定区里,
超过 UP_TOLERANCE(50ms) 长条就断。

Chart_AT.json 开头的8个长条每个节拍(约290ms)瞬移一次, 判定模拟器实测: 整体偏差在 ±40ms 以内
全中, 到 ±60ms 就断(第二波的瞬移最密、最先断), 也就是用户说的"第二波必断一个"。
以前的修复(触点预算、越界夹到屏幕边缘)都没动这个问题: 它只和"瞬移那一刻手指在哪"有关。

接力
----
对每一次瞬移(旧位置不在新判定区内), 用一个当时空闲的tap/hold触点做接力:

* 主手指在旧位置多留 RELAY_LAG_MS(落后);
* 接力触点提前 RELAY_LEAD_MS 静静按在新位置(领先), 直到主手指到位后再抬起。

整体偏差 d 落在 [-(LAG+50), +(LEAD+50)] 内时, 任何时刻都有手指在判定区里(多出来的50ms是
UP_TOLERANCE 本来就有的)。接力触点只占用tap/hold池里本来就空着的触点, 位置和时间都避开附近的
tap/hold(不会误触), 凑不齐就整簇放弃, 退回原来的"单手指逐毫秒跟随"。
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from typing import NamedTuple

from .algo_base import JUDGE_HALF_WIDTH, TouchAction, VirtualTouchEvent, _edge_safe, in_pause_box, note_state

RELAY_LEAD_MS = 45        # 接力触点比瞬移提前这么久按在新位置
RELAY_LAG_MS = 30         # 主手指在旧位置多留这么久
RELAY_TAIL_MS = 30        # 主手指到位之后接力触点再多留这么久(给相邻事件之间的抖动留余量)
TELEPORT_MIN = 60.0       # 相邻两毫秒位置变化超过这个距离, 才去检查是不是判定区瞬移(像素)
STRIP_MARGIN = 20.0       # 判定区内缩(像素): 擦着边的不算"在判定区里"
CLICK_GUARD_MS = 260      # 接力触点按下前后这么久内的tap/hold都不能被它误触
CLICK_MARGIN = 30.0
CLICK_SETTLE_MS = 40      # 按下这一帧可能比事件时间晚(30fps一帧33ms), 判定线在这段时间里还会继续瞬移, 都要避开
START_GUARD_MS = 10       # 接力触点至少在长条按下之后这么久才按下
SAMPLE_MS = 5             # 检查"静止在新位置是否一直在判定区内"的采样间隔
EDGE_INSET = 40.0         # 接力触点离屏幕边缘至少这么远(避开系统手势区); 按下再抬起不能落在暂停按钮上


class HoldTrack(NamedTuple):
    """一个已经分配到触点的长条"""
    pid: int                 # 执行这个长条的触点
    line: object
    note: object
    start: int               # 按下时刻(ms)
    length: int              # 持续时间(ms)
    path: tuple              # 按下之后第1..length毫秒的位置(hold_point)
    head: tuple              # 按下时的位置


class RelayStats(dict):
    """teleports: 判定区瞬移次数; helpers: 加了多少个接力触点; skipped_*: 放弃的原因"""

    def __missing__(self, key):
        return 0


def strip_offset(line, note, ms: float, pos: tuple[float, float]) -> float:
    """pos 相对于音符在 ms 时刻判定区中心的偏移(沿判定线方向, 像素)。判定只看这个量。"""
    (qx, qy), sa, ca = note_state(line, note, ms)
    return (pos[0] - qx) * ca + (pos[1] - qy) * sa


def in_strip(line, note, ms: float, pos: tuple[float, float], margin: float = STRIP_MARGIN) -> bool:
    return abs(strip_offset(line, note, ms, pos)) <= JUDGE_HALF_WIDTH - margin


def _room(pos: tuple[float, float], n: tuple[float, float], margin: float):
    """pos 沿方向 n 移动 s 之后仍在"屏幕内缩 margin"范围内的 s 的区间; 没有则 None"""
    lo, hi = -math.inf, math.inf
    for p, d, size in ((pos[0], n[0], 1280.0), (pos[1], n[1], 720.0)):
        if abs(d) < 1e-9:
            if not margin <= p <= size - margin:
                return None
            continue
        a, b = (margin - p) / d, (size - margin - p) / d
        lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
    return (lo, hi) if lo <= hi else None


def helper_position(line, note, ms: float, pos: tuple[float, float]) -> tuple[float, float] | None:
    """接力触点按下的位置: 把 pos 沿垂直于判定线的方向挪(不改变沿判定线方向的投影, 判定不受影响),
    挪到离屏幕边缘至少 EDGE_INSET 并避开左上角暂停按钮的地方(做不到就退一步: 只要在屏幕内);
    找不到避开暂停按钮的位置返回 None。

    接力触点是"按下再抬起"的一次点击: 落在暂停按钮上会让游戏暂停; 贴着屏幕边缘按下可能触发系统手势。"""
    _, sa, ca = note_state(line, note, ms)
    n = (-sa, ca)
    for margin in (EDGE_INSET, 1.0):
        room = _room(pos, n, margin)
        if room is None:
            continue
        lo, hi = room
        s0 = min(max(0.0, lo), hi)
        candidates = sorted((s0 + k * 10.0 for k in range(-72, 73) if lo <= s0 + k * 10.0 <= hi),
                            key=lambda v: abs(v - s0))
        for sv in candidates or [s0]:
            cand = (pos[0] + n[0] * sv, pos[1] + n[1] * sv)
            if not in_pause_box(cand):
                return cand
    return None if in_pause_box(pos) else pos


def find_teleports(track: HoldTrack) -> list[tuple[int, tuple, tuple]]:
    """[(时刻ms, 瞬移前的位置, 瞬移后的位置)]: 瞬移前的手指位置已不在新的判定区内。

    垂直于判定线方向的大位移(比如夹到屏幕边缘时换边)不改变判定区, 不算瞬移。"""
    out = []
    prev = track.head
    for i, p in enumerate(track.path):
        if math.hypot(p[0] - prev[0], p[1] - prev[1]) > TELEPORT_MIN:
            u = track.start + 1 + i
            if not in_strip(track.line, track.note, u, prev):
                out.append((u, prev, p))
        prev = p
    return out


def _busy_intervals(events, pool) -> dict[int, list[tuple[float, float]]]:
    busy = {pid: [] for pid in pool}
    opened: dict[int, int] = {}
    for ms in sorted(events):
        for e in events[ms]:
            if e.pointer not in busy:
                continue
            if e.action == TouchAction.DOWN:
                opened[e.pointer] = ms
            elif e.action == TouchAction.UP:
                busy[e.pointer].append((opened.pop(e.pointer, ms), ms))
    for pid, a in opened.items():
        busy[pid].append((a, math.inf))
    return busy


def pool_peak(events, pool, cooldown: int) -> int:
    """tap/hold池里同时"按着或在冷却"的触点数的峰值(这个池子至少要有这么多触点)"""
    busy = _busy_intervals(events, pool)
    delta: dict[float, int] = defaultdict(int)
    for spans in busy.values():
        for a, b in spans:
            delta[a] += 1
            delta[b + cooldown] -= 1   # b 为 inf 时 inf+cooldown 仍是 inf, 永远不减
    cur = peak = 0
    for t in sorted(delta):
        cur += delta[t]
        peak = max(peak, cur)
    return peak


class _ClickGuard:
    """接力触点按下(DOWN)也是一次"点击", 不能把附近的tap/hold偷走"""

    def __init__(self, heads):
        self.heads = sorted(heads, key=lambda h: h[0])
        self.times = [h[0] for h in self.heads]

    def safe(self, pos: tuple[float, float], ms: int, skip=None) -> bool:
        """按下后 CLICK_SETTLE_MS 内任何时刻, pos 都不在 ms±CLICK_GUARD_MS 内的tap/hold的判定范围里。
        (游戏按帧判定, 点击在"事件时间之后的那一帧"才生效, 那一刻的判定线位置才是准的。)"""
        lo = bisect.bisect_left(self.times, ms - CLICK_GUARD_MS)
        hi = bisect.bisect_right(self.times, ms + CLICK_GUARD_MS)
        for k in range(lo, hi):
            _, line, note = self.heads[k]
            if note is skip:
                continue
            for tt in range(ms, ms + CLICK_SETTLE_MS + 1, 10):
                lt = line.time(tt / 1000)
                cx, cy = line.pos(lt)
                a = -line.angle(lt) * math.pi / 180
                ca, sa = math.cos(a), math.sin(a)
                if abs((pos[0] - cx) * ca + (pos[1] - cy) * sa - note.x * 72) <= JUDGE_HALF_WIDTH + CLICK_MARGIN:
                    return False
        return True


def plan_hold_relays(tracks: list[HoldTrack], events, pool: list[int], heads, cooldown: int,
                     console=None) -> RelayStats:
    """给判定区瞬移的长条加接力触点, 就地修改 events。

    tracks:   已分配触点的长条
    events:   {ms: [VirtualTouchEvent]}, 触点分配完之后的完整事件(含最后的UP)
    pool:     tap/hold池的触点id(接力触点只从这里面找空闲的, 所以不会突破总触点预算)
    heads:    [(ms, line, note)], 所有tap/hold的判定时刻(避免接力触点按下时误触它们)
    cooldown: 同一个触点UP之后至少隔这么久才能再DOWN(algo2.PID_REUSE_COOLDOWN_MS)
    """
    stats = RelayStats()
    if not tracks:
        return stats
    busy = _busy_intervals(events, pool)
    guard = _ClickGuard(heads)
    gap = RELAY_LEAD_MS + RELAY_LAG_MS + RELAY_TAIL_MS

    def free(pid: int, t_dn: int, t_up: int) -> bool:
        return all(b <= t_dn - cooldown or a >= t_up + cooldown for a, b in busy[pid])

    added: list[tuple[int, VirtualTouchEvent]] = []
    freezes: list[tuple[HoldTrack, int, int]] = []
    for track in sorted(tracks, key=lambda t: t.start):
        end = track.start + track.length
        teleports = find_teleports(track)
        stats['teleports'] += len(teleports)
        clusters: list[list] = []
        for tp in teleports:
            if clusters and tp[0] - clusters[-1][-1][0] < gap:
                clusters[-1].append(tp)
            else:
                clusters.append([tp])
        for cluster in clusters:
            plan: list[tuple[int, int, int, tuple]] = []
            reason = None
            for k, (tj, _, s_far) in enumerate(cluster):
                # 这个接力触点负责 [tj, 下一次瞬移): 之后由下一个接力触点(或主手指)接手
                t_next = cluster[k + 1][0] if k + 1 < len(cluster) else None
                t_dn, t_up = tj - RELAY_LEAD_MS, tj + RELAY_LAG_MS + RELAY_TAIL_MS
                if t_next is not None:
                    t_up = max(t_up, t_next + RELAY_LAG_MS)
                cover_end = min(end, (t_next - 1) if t_next is not None else t_up)
                s_new = helper_position(track.line, track.note, tj, s_far)
                if t_dn < track.start + START_GUARD_MS:
                    reason = 'head'
                elif tj >= end - 1:
                    reason = 'end'
                elif s_new is None:
                    reason = 'unsafe'    # 只能落在暂停按钮上
                elif not all(in_strip(track.line, track.note, u, s_new)
                             for u in range(tj, cover_end + 1, SAMPLE_MS)):
                    reason = 'drift'     # 判定区很快又移走了, 静止的接力触点跟不上
                elif not guard.safe(s_new, t_dn, skip=track.note):
                    reason = 'click'
                else:
                    pid = next((q for q in pool if q != track.pid and free(q, t_dn, t_up)), None)
                    if pid is None:
                        reason = 'capacity'
                    else:
                        busy[pid].append((t_dn, t_up))     # 先占住, 同簇的下一个接力触点不能再用它
                        plan.append((pid, t_dn, t_up, s_new))
                if reason:
                    break
            if reason:
                for pid, t_dn, t_up, _ in plan:
                    busy[pid].remove((t_dn, t_up))
                stats['skipped_' + reason] += 1
                continue
            for pid, t_dn, t_up, s_new in plan:
                pos = _edge_safe(s_new)
                added.append((t_dn, VirtualTouchEvent(pos, TouchAction.DOWN, pid)))
                added.append((t_up, VirtualTouchEvent(pos, TouchAction.UP, pid)))
            stats['helpers'] += len(plan)
            stats['relayed'] += len(cluster)
            freezes.append((track, cluster[0][0], min(cluster[-1][0] + RELAY_LAG_MS, end)))

    # 主手指在 [a, b) 内不动(留在旧位置), b 时刻才跳到当时的位置
    for track, a, b in freezes:
        for t in range(a, b):
            evs = events.get(t)
            if evs:
                kept = [e for e in evs if not (e.pointer == track.pid and e.action == TouchAction.MOVE)]
                if kept:
                    events[t] = kept
                else:
                    del events[t]        # 不留空批次(播放器会白白多发一个空包)
        if b < track.start + track.length:
            pos = _edge_safe(track.path[b - track.start - 1])
            events.setdefault(b, []).append(VirtualTouchEvent(pos, TouchAction.MOVE, track.pid))
    for t, e in added:
        events.setdefault(t, []).append(e)

    if console is not None and stats['teleports']:
        msg = f'长条瞬移接力: {stats["teleports"]}处判定线瞬移, {stats["relayed"]}处加了接力触点(共{stats["helpers"]}个)'
        skipped = {k[8:]: v for k, v in stats.items() if k.startswith('skipped_')}
        if skipped:
            names = {'capacity': '触点不够', 'click': '会误触附近的tap/hold', 'drift': '判定区很快又移走',
                     'head': '离按下太近', 'end': '在长条末尾', 'unsafe': '只能落在暂停按钮上'}
            msg += '; 放弃' + ', '.join(f'{v}簇({names.get(k, k)})' for k, v in sorted(skipped.items()))
        console.print(msg)
    return stats


__all__ = ['HoldTrack', 'RelayStats', 'plan_hold_relays', 'find_teleports', 'pool_peak', 'strip_offset', 'in_strip',
           'helper_position',
           'RELAY_LEAD_MS', 'RELAY_LAG_MS']
