"""噪点红场谱面的规划。

红场里的点击无效。判定只看触点在判定线方向上的投影, 所以每个触点都沿垂直方向
挪到红场外(和躲开暂停键是同一条路)。扫屏(algo3/algo3f)的手指会扫过红场, 扫到的
那一下是无效点击, 不能用来打这种谱。

挪不出去的手指(整条垂直线都在红场里)按下后立刻抬起, 不留在红场里占着手指,
也不拿去点后面的音符。
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
    spans = [(int(b.enable * 1000) - 1, int(math.ceil(b.disable * 1000)) + 1) for b in field.blocks]
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
                kept.append(e)
                kept.append(VirtualTouchEvent(e.pos, TouchAction.UP, pid))
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


def _shift_flick(field: RedField, line, note, center_ms: int, path: tuple) -> tuple:
    """整段滑动加同一个垂直偏移, 投影和滑动形状都不变。挪不干净就尽量多躲开。"""
    if not path:
        return path
    normals = []
    times = []
    for i in range(len(path)):
        ms = center_ms + FLICK_START + i
        _, sa, ca = note_state(line, note, ms)
        normals.append((-sa, ca))
        times.append(ms / 1000.0)

    def moved(s: float):
        return tuple((p[0] + n[0] * s, p[1] + n[1] * s) for p, n in zip(path, normals))

    def score(s: float) -> int:
        pts = moved(s)
        ok = 0
        for p, t in zip(pts, times):
            if field._safe(p[0], p[1], t):
                ok += 1
        return ok

    if score(0.0) == len(path):
        return path
    candidates = {0.0}
    prefer = 0.0
    for p, n, t in zip(path, normals, times):
        # n = (-sa, ca), 所以 sa = -n[0], ca = n[1]
        _, _, s = field.escape(p[0], p[1], -n[0], n[1], t, prefer)
        candidates.add(s)
        prefer = s
    best_s, best = 0.0, -1
    for s in candidates:
        sc = score(s)
        if sc > best or (sc == best and abs(s) < abs(best_s)):
            best, best_s = sc, s
    if best == len(path):
        return moved(best_s)
    # 没有一个偏移能让整段都出去: 再按 16px 扫一遍, 取躲开最多的
    for s in range(-720, 721, 16):
        sc = score(float(s))
        if sc > best or (sc == best and abs(s) < abs(best_s)):
            best, best_s = sc, float(s)
    return moved(best_s)


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

    def place(pos, sa, ca, ms, prefer=0.0):
        nonlocal shifted
        x, y, s = field.escape(pos[0], pos[1], sa, ca, ms / 1000.0, prefer)
        if abs(s) >= 1.0:
            shifted += 1
        return (x, y), s

    for li, line in enumerate(track(chart.judge_lines, description='统计操作帧(红场)...', console=console)):
        for ni, note in enumerate(line.notes_above + line.notes_below):
            ms = round(line.seconds(note.time) * 1000)
            off_x = note.x * 72
            x, y = line.pos(note.time)
            alpha = -line.angle(note.time) * math.pi / 180
            sa, ca = math.sin(alpha), math.cos(alpha)
            pos = (x + off_x * ca, y + off_x * sa)
            if note.type in (NoteType.TAP, NoteType.HOLD):
                heads.append((ms, line, note))
            if note.type == NoteType.HOLD:
                hold_ms = math.ceil(line.seconds(note.hold) * 1000)
                head, s = place(hold_point(line, note, ms), sa, ca, ms)
                path_pts = []
                prefer = s
                prev = head
                for offset in range(1, hold_ms + 1):
                    t_ms = ms + offset
                    _, sa_i, ca_i = note_state(line, note, t_ms)
                    raw = hold_point(line, note, t_ms)
                    if math.hypot(raw[0] - prev[0], raw[1] - prev[1]) > 80:
                        prefer = 0.0  # 判定线瞬移, 不要把上一侧的偏移带过去
                    p, prefer = place(raw, sa_i, ca_i, t_ms, prefer)
                    path_pts.append(p)
                    prev = raw
                path = tuple(path_pts)
                holds[(li, ni)] = (line, note, ms, hold_ms, path, head)
                frames[ms].add(NoteType.HOLD, head, alpha, path, tag=(li, ni))
            elif note.type == NoteType.FLICK:
                shift = flick_time_shift(line, note, console)
                raw = tuple(flick_path(line, note, ms, FLICK_START, FLICK_END, FLICK_RADIUS, shift))
                path = _shift_flick(field, line, note, ms, raw)
                if path != raw:
                    shifted += 1
                frames[ms + FLICK_START].add(NoteType.FLICK, path[0], alpha, path)
            else:
                # 屏幕外的音符先沿垂直方向收到屏幕里(和 algo2 一样), 再躲开红场
                pos = recalc_pos(pos, sa, ca)
                p, _ = place(pos, sa, ca, ms)
                frames[ms].add(note.type, p, alpha)

    console.print(f'统计完毕，当前谱面共计{len(frames)}帧, 红场块{len(field.blocks)}个, 垂直挪出红场{shifted}处')

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
                         blocked=lambda pos, ms: field.contains(pos[0], pos[1], ms / 1000.0))
    lifted = _lift_red(events, field)
    if lifted:
        console.print(f'[yellow]有{lifted}次触点没法完全躲开红场, 按下后已立刻抬起[/yellow]')
    if warn_pause:
        warn_pause_presses(events, console)
    if stats is not None:
        stats['pause_presses'] = len(pause_presses(events))
        stats['red_lifts'] = lifted
        stats['red_shifts'] = shifted
    return events


__all__ = ['solve']
