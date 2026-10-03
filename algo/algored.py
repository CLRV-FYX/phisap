"""噪点红场谱面的规划。

红场里的点击无效, 点进去会把后面的判定也带崩。判定只看触点在判定线方向上的投影,
所以一个音符可以点在它所在位置、垂直于判定线的任意坐标上。红场在动, 每一毫秒
的空位不一样: 这一下没缝, 就先在 Perfect(±40ms) 里等它让开, 还没有再放到 Good(±80ms)。
不把前后扫过的区域当成现在也不能点, 也不把坐标放进噪区。

扫屏(algo3/algo3f)的手指会扫过红场, 扫到的那一下是无效点击, 不能用来打这种谱。
"""
from __future__ import annotations

import math
from collections import defaultdict

from rich.console import Console
from rich.progress import track

from chart import Chart
from note import NoteType
from .algo_base import (
    TouchAction, VirtualTouchEvent, hold_point, flick_path, flick_time_shift, note_state,
    recalc_pos, warn_pause_presses, pause_presses, MAX_POINTERS,
    FLICK_START, FLICK_END, FLICK_RADIUS,
)
from .algo2 import Frames, PointerAllocator, PID_REUSE_COOLDOWN_MS
from .red_field import RedField
from .relay import HoldTrack, plan_hold_relays, pool_peak


def _red_ranges_ms(field: RedField) -> list[tuple[int, int]]:
    spans = []
    for b in field.blocks:
        # 只有 enable 区间挡点击。淡入淡出画出来了也不算死区。
        if b.disable <= b.enable:
            continue
        spans.append((int(b.enable * 1000) - 1, int(math.ceil(b.disable * 1000)) + 1))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _pin_hold_paths(events, holds, hold_pointer, field: RedField) -> None:
    """长条轨迹被精简后, 手指会在两次 MOVE 之间停住。红场若在这几毫秒里移过来, 停着的手指就进了红场。

    红场活动期间按规划好的(已经躲开红场的)轨迹逐毫秒补 MOVE, 手指才跟得上。
    """
    for tag, info in holds.items():
        pid = hold_pointer.get(tag)
        if pid is None:
            continue
        ms, hold_ms, path, head = info[2], info[3], info[4], info[5]
        for offset in range(0, hold_ms + 1):
            t_ms = ms + offset
            if not field.active(t_ms / 1000.0):
                continue
            pos = head if offset == 0 else path[offset - 1]
            evs = events.get(t_ms)
            if not evs:
                events[t_ms] = [VirtualTouchEvent(pos, TouchAction.MOVE, pid)]
                continue
            replaced = False
            for i, e in enumerate(evs):
                if e.pointer == pid and e.action == TouchAction.MOVE:
                    evs[i] = VirtualTouchEvent(pos, TouchAction.MOVE, pid)
                    replaced = True
                    break
            if not replaced and not any(e.pointer == pid and e.action == TouchAction.DOWN for e in evs):
                evs.append(VirtualTouchEvent(pos, TouchAction.MOVE, pid))


def _lift_red(events, field: RedField) -> int:
    """手指停在红场里时立刻抬起。红场会自己移过来, 所以不能只在有事件的那一毫秒查。"""
    if not events:
        return 0
    down: dict[int, tuple] = {}
    dead: set[int] = set()
    lifted = 0
    ranges = _red_ranges_ms(field)
    lo, hi = min(events), max(events)
    cursor = lo

    def apply(ms: int):
        nonlocal lifted
        batch = events.get(ms)
        # down 的键是触点 id, 不是毫秒。没有事件但还有手指按着时也要查, 红场会自己移过来。
        if not batch and not down:
            return
        kept = []
        t = ms / 1000.0
        for e in batch or []:
            pid = e.pointer
            if pid in dead:
                if e.action == TouchAction.DOWN:
                    dead.discard(pid)
                else:
                    continue
            if e.action == TouchAction.MOVE and pid in down and field.contains(e.pos[0], e.pos[1], t):
                kept.append(VirtualTouchEvent(down[pid], TouchAction.UP, pid))
                down.pop(pid, None)
                dead.add(pid)
                lifted += 1
                continue
            if e.action == TouchAction.DOWN and field.contains(e.pos[0], e.pos[1], t):
                # 按下的坐标已经在红场里。先按下再抬也是一次无效点击, 这一下不能发出去。
                dead.add(pid)
                lifted += 1
                continue
            kept.append(e)
            if e.action == TouchAction.DOWN:
                down[pid] = e.pos
            elif e.action == TouchAction.MOVE:
                down[pid] = e.pos
            elif e.action == TouchAction.UP:
                down.pop(pid, None)
        # 没有新事件、但红场移到了还按着的手指上
        if field.active(t):
            for pid, pos in list(down.items()):
                if field.contains(pos[0], pos[1], t):
                    kept.append(VirtualTouchEvent(pos, TouchAction.UP, pid))
                    down.pop(pid, None)
                    dead.add(pid)
                    lifted += 1
        if kept:
            events[ms] = kept
        elif ms in events:
            del events[ms]

    for a, b in ranges:
        a, b = max(a, lo), min(b, hi)
        if a > b:
            continue
        while cursor < a:
            if cursor in events:
                apply(cursor)
            cursor += 1
        for ms in range(a, b + 1):
            apply(ms)
        cursor = b + 1
    while cursor <= hi:
        if cursor in events:
            apply(cursor)
        cursor += 1
    return lifted


# Perfect 是 ±40ms, Good 到 ±80ms。先在 Perfect 里找垂线让开的时刻, 没有再放到 Good。
_SEEK_MS = 80
_SEEK_STEP = 4


def _seek_dts(limit: int = _SEEK_MS):
    yield 0
    k = _SEEK_STEP
    while k <= limit:
        yield k
        yield -k
        k += _SEEK_STEP


def _overlap(a, b):
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo > 1.0:
                out.append((lo, hi))
    return out


def _shift_flick(field: RedField, line, note, center_ms: int, path: tuple):
    """整段滑动加同一个垂直偏移, 投影和滑动形状都不变。

    整段都必须落在红场外。没有这样的偏移就放弃这次滑动, 不把手指扫进噪区。
    """
    if not path:
        return None
    common = None
    normals = []
    for i, p in enumerate(path):
        ms = center_ms + FLICK_START + i
        _, sa, ca = note_state(line, note, ms)
        nx, ny = -sa, ca
        nlen = math.hypot(nx, ny) or 1.0
        normals.append((nx / nlen, ny / nlen))
        ivs = field.safe_intervals(p[0], p[1], sa, ca, ms / 1000.0)
        common = ivs if common is None else _overlap(common, ivs)
        if not common:
            return None
    s = min((min(max(0.0, a), b) for a, b in common), key=abs)
    if abs(s) < 0.5:
        return path
    return tuple((p[0] + n[0] * s, p[1] + n[1] * s) for p, n in zip(path, normals))


def _follow_flick(field: RedField, line, note, center_ms: int, path: tuple):
    """整段对不齐同一个偏移时, 每一毫秒跟着红场让开的那一侧走。还是沿垂直线, 判定不变。"""
    if not path:
        return None
    prefer = 0.0
    out = []
    prev = None
    for i, p in enumerate(path):
        ms = center_ms + FLICK_START + i
        _, sa, ca = note_state(line, note, ms)
        hit = field.vertical_slot(p[0], p[1], sa, ca, ms / 1000.0, prefer)
        if hit is None:
            return None
        x, y, s = hit
        if prev is not None and math.hypot(x - prev[0], y - prev[1]) > 8 and field.segment_hits(prev, (x, y), ms / 1000.0):
            return None
        out.append((x, y))
        prev = (x, y)
        prefer = s
    if math.hypot(out[-1][0] - out[0][0], out[-1][1] - out[0][1]) < 60:
        return None  # 被挤成一个点, 甩不起来
    return tuple(out)


def _flick_outside(field: RedField, line, note, center_ms: int):
    shift = flick_time_shift(line, note, None)
    raw = tuple(flick_path(line, note, center_ms, FLICK_START, FLICK_END, FLICK_RADIUS, shift))
    path = _shift_flick(field, line, note, center_ms, raw)
    if path is None:
        path = _follow_flick(field, line, note, center_ms, raw)
    return raw, path


def solve(chart: Chart, console: Console, max_pointers: int = MAX_POINTERS, stats: dict | None = None,
          relay: bool = True, warn_pause: bool = True) -> defaultdict[int, list[VirtualTouchEvent]]:
    field = RedField.from_chart(chart)
    if not field:
        console.print('谱面没有噪点红场, algored 按 algo2 规划')
        from . import algo2
        return algo2.solve(chart, console, max_pointers, stats=stats, relay=relay, warn_pause=warn_pause)

    frames = Frames()
    holds: dict[tuple[int, int], tuple] = {}
    heads: list[tuple[int, object, object]] = []
    shifted = 0
    skipped = 0
    cuts = 0
    handoffs = 0
    waited = 0

    def place(pos, sa, ca, ms, prefer=0.0):
        nonlocal shifted
        hit = field.vertical_slot(pos[0], pos[1], sa, ca, ms / 1000.0, prefer)
        if hit is None:
            return None
        x, y, s = hit
        if abs(s) >= 1.0:
            shifted += 1
        return (x, y), s

    def seek_click(ms0: int, limit: int = _SEEK_MS):
        """红场这一毫秒盖住整条垂线时, 在判定窗里找它让开的最近一毫秒。"""
        for dt in _seek_dts(limit):
            t_ms = ms0 + dt
            raw, sa_i, ca_i = note_state(line, note, t_ms)
            if note.type == NoteType.HOLD:
                raw = hold_point(line, note, t_ms)
            else:
                raw = recalc_pos(raw, sa_i, ca_i)
            placed = place(raw, sa_i, ca_i, t_ms)
            if placed is not None:
                return t_ms, placed, sa_i, ca_i
        return None

    for li, line in enumerate(track(chart.judge_lines, description='统计操作帧(红场)...', console=console)):
        for ni, note in enumerate(line.notes_above + line.notes_below):
            ms = round(line.seconds(note.time) * 1000)
            alpha = -line.angle(note.time) * math.pi / 180
            if note.type == NoteType.HOLD:
                hold_ms = math.ceil(line.seconds(note.hold) * 1000)
                found = seek_click(ms)
                if found is None:
                    skipped += 1
                    continue
                click_ms, (head, prefer), _, _ = found
                if click_ms != ms:
                    waited += 1
                end_ms = ms + hold_ms
                # 一段手指扫不过红场时换一只手, 不把整条长条提前掐掉。
                segments = []
                seg_start, seg_head, seg_pts = click_ms, head, []
                prev_pos = head
                prev_raw = hold_point(line, note, click_ms)
                t_ms = click_ms + 1
                while t_ms <= end_ms:
                    _, sa_i, ca_i = note_state(line, note, t_ms)
                    raw = hold_point(line, note, t_ms)
                    if math.hypot(raw[0] - prev_raw[0], raw[1] - prev_raw[1]) > 80:
                        prefer = 0.0  # 判定线瞬移, 不要把上一侧的偏移带过去
                    placed = place(raw, sa_i, ca_i, t_ms, prefer)
                    if placed is None:
                        # 这一毫秒整条垂线都在红场里。红场在动, 最多等 45ms(判定允许断开 50ms)。
                        resumed_at = None
                        for wait in range(1, 46):
                            u = t_ms + wait
                            if u > end_ms:
                                break
                            _, sa_u, ca_u = note_state(line, note, u)
                            raw_u = hold_point(line, note, u)
                            resumed = place(raw_u, sa_u, ca_u, u, prefer)
                            if resumed is None or field.segment_hits(prev_pos, resumed[0], u / 1000.0):
                                continue
                            if any(field.contains(prev_pos[0], prev_pos[1], (t_ms + k) / 1000.0) for k in range(wait)):
                                continue  # 停在原地会进红场, 这一下接不上
                            resumed_at = (u, resumed, raw_u)
                            break
                        if resumed_at is None:
                            cuts += 1
                            break
                        u, resumed, raw_u = resumed_at
                        p, prefer = resumed
                        seg_pts.extend([prev_pos] * (u - t_ms))
                        seg_pts.append(p)
                        prev_pos = p
                        prev_raw = raw_u
                        t_ms = u + 1
                        continue
                    p, prefer = placed
                    if math.hypot(p[0] - prev_pos[0], p[1] - prev_pos[1]) > 8 and field.segment_hits(prev_pos, p, t_ms / 1000.0):
                        # 空位在红场另一侧。滑过去会进噪区, 换一只手按在那一侧。
                        segments.append((seg_start, seg_head, seg_pts))
                        handoffs += 1
                        seg_start, seg_head, seg_pts = t_ms, p, []
                        prev_pos = p
                        prev_raw = raw
                        t_ms += 1
                        continue
                    seg_pts.append(p)
                    prev_pos = p
                    prev_raw = raw
                    t_ms += 1
                segments.append((seg_start, seg_head, seg_pts))
                for i, (st, hd, pts) in enumerate(segments):
                    path = tuple(pts)
                    tag = (li, ni) if i == 0 else (li, ni, st)
                    holds[tag] = (line, note, st, len(path), path, hd)
                    _, sa_h, ca_h = note_state(line, note, st)
                    frames[st].add(NoteType.HOLD, hd, math.atan2(sa_h, ca_h), path, tag=tag)
                heads.append((click_ms, line, note))
            elif note.type == NoteType.FLICK:
                emitted = False
                for dt in _seek_dts():
                    raw, path = _flick_outside(field, line, note, ms + dt)
                    if path is None:
                        continue
                    if path != raw or dt:
                        shifted += 1
                    if dt:
                        waited += 1
                    frames[ms + dt + FLICK_START].add(NoteType.FLICK, path[0], alpha, path)
                    emitted = True
                    break
                if not emitted:
                    skipped += 1
            else:
                found = seek_click(ms)
                if found is None:
                    skipped += 1
                    continue
                click_ms, (p, _), sa_i, ca_i = found
                if click_ms != ms:
                    waited += 1
                alpha_i = math.atan2(sa_i, ca_i)
                frames[click_ms].add(note.type, p, alpha_i)
                if note.type == NoteType.TAP:
                    heads.append((click_ms, line, note))

    console.print(f'统计完毕，当前谱面共计{len(frames)}帧, 红场块{len(field.blocks)}个, 垂直挪出红场{shifted}处')
    if waited:
        console.print(f'红场在动, {waited}个音符改到垂线让开的时刻按下')
    if skipped:
        console.print(f'[yellow]有{skipped}个音符在 Perfect/Good 窗口内垂线一直被红场盖住, 没有按下[/yellow]')
    if handoffs:
        console.print(f'红场挡住滑动, {handoffs}次长条换到垂线另一侧继续按')
    if cuts:
        console.print(f'[yellow]有{cuts}条长条在红场盖住之前抬起, 没有扫进噪区[/yellow]')

    allocator = PointerAllocator(max_pointers)
    for frame in track(frames, description='规划触控事件...', console=console):
        allocator.allocate(frame)
    if allocator.dropped:
        console.print(f'[yellow]警告: 有{len(allocator.dropped)}个音符因同时需要超过{max_pointers}个触点而无法执行'
                      f'(首次出现在{allocator.dropped[0][0]}ms)[/yellow]')
    events = allocator.done()
    _pin_hold_paths(events, holds, allocator.hold_pointer, field)
    pool = [p.id for p in allocator.pointers]
    if stats is not None:
        stats['dropped'] = len(allocator.dropped)
        stats['pool_peak'] = pool_peak(events, pool, PID_REUSE_COOLDOWN_MS)
    if relay:
        tracks = [HoldTrack(allocator.hold_pointer[tag], *info[:2], info[2], info[3], info[4], info[5])
                  for tag, info in holds.items() if tag in allocator.hold_pointer]
        plan_hold_relays(tracks, events, pool, heads, PID_REUSE_COOLDOWN_MS, console,
                         blocked=lambda pos, ms: field.dead(pos[0], pos[1], ms / 1000.0))
    lifted = _lift_red(events, field)
    if lifted:
        console.print(f'[yellow]有{lifted}次触点没法完全躲开红场, 没有留在红场里[/yellow]')
    if warn_pause:
        warn_pause_presses(events, console)
    if stats is not None:
        stats['pause_presses'] = len(pause_presses(events))
        stats['red_lifts'] = lifted
        stats['red_shifts'] = shifted
        stats['red_skips'] = skipped
        stats['red_cuts'] = cuts
        stats['red_waits'] = waited
        stats['red_handoffs'] = handoffs
    return events


__all__ = ['solve']
