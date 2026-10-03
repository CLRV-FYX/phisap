"""噪点红场谱面的规划。

红场里的点击无效, 点进去会把后面的判定也带崩。判定只看触点在判定线方向上的投影,
所以一个音符可以点在它所在位置、垂直于判定线的任意坐标上。红场在动, 每一毫秒
的空位不一样: 这一下没缝, 就先在 Perfect(±40ms) 里等它让开, 还没有再放到 Good(±80ms)。
长条不能等红场盖到手指上再换: 红区从中间(或略偏两侧)长出来时, 要在它赶到之前
先按住垂线上的空位, 新触点不松, 然后再松开原来的。这个新按下不能落进别的音符的
判定窗: ±80ms 会把那个音符抢走, 再早到 180ms 是 Bad。同一侧还连得上的, 就提前挪过去,
不扫过红场。判定线瞬移时, 手指还在判定带里就停在原地, 不跟着跳过红场。
换手要先按住新的, 至少重叠一帧, 再松开原来的; 同一毫秒一按一松, 长条会断。
长条头优先落在原时刻的近处, 不贴 Good 外沿。

扫屏(algo3/algo3f)的手指会扫过红场, 扫到的那一下是无效点击, 不能用来打这种谱。
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict

from rich.console import Console
from rich.progress import track

from chart import Chart
from note import NoteType
from .algo_base import (
    TouchAction, VirtualTouchEvent, hold_point, flick_path, flick_time_shift, note_state,
    recalc_pos, warn_pause_presses, pause_presses, MAX_POINTERS, JUDGE_HALF_WIDTH,
    FLICK_START, FLICK_END, FLICK_RADIUS,
)
from .algo2 import Frames, PointerAllocator, PID_REUSE_COOLDOWN_MS
from .red_field import RED_CLEARANCE_PX, RedField
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


# 新触点要比红场赶到早这么久按下, 旧触点再早这么久抬起。给注入延迟和一帧采样留空档,
# 不能等红场已经盖住再换手。重叠不够一帧就不要换: 同一毫秒按下又松开, 游戏先处理抬起,
# 长条会断。
_PRESS_LEAD_MS = 48
_LIFT_EARLY_MS = 48
_OVERLAP_MS = 80
_MIN_OVERLAP_MS = 32
# 要提前看到红场: 别的音符的 Bad 窗有 180ms, 等到只剩 112ms 再换手, 新按下已经落在窗里。
_LOOKAHEAD_MS = 300
_SLIDE_MAX_PX = 70.0
_SLIDE_FALLBACK_PX = 1200.0
_TELEPORT_PX = 80.0
_STEP_PX = 16.0
# 用户判定窗: Perfect ±40, Good ±80。提前按下到 180ms 是 Bad, 不是“没打到”。
# 迟到超过 Good 的点击也躲开; 实在来不及才允许, 但绝不能落进 ±80(会抢走那个音符)。
_STEAL_MS = 80
_BAD_EARLY_MS = 180
_BAD_LATE_MS = 180
_GUARD_MARGIN_PX = 8.0
# Good 外沿再晚一帧(60fps)就出了 ±80, 变成 Bad。长条头不要贴这条边。
_SEEK_COMFORT_MS = 64


def _normal(sa: float, ca: float) -> tuple[float, float]:
    # 与 red_field.safe_intervals 同一个方向: 沿 (-sa, ca), 投影不变。
    nx, ny = -sa, ca
    nlen = math.hypot(nx, ny)
    if nlen < 1e-8:
        return 0.0, 1.0
    return nx / nlen, ny / nlen


def _home(ivs, s: float):
    best = None
    for a, b in ivs:
        if a - 2.0 <= s <= b + 2.0 and (best is None or b - a > best[1] - best[0]):
            best = (a, b)
    return best


def _sample_s(iv, step: float = 24.0) -> list[float]:
    """区间里多取几个垂线偏移。只取中心和两个唇, 会正好落进别的音符的判定带。"""
    a, b = iv
    span = b - a
    if span < 8.0:
        return []
    guard = min(RED_CLEARANCE_PX, span / 3)
    out = [(a + b) / 2, a + guard, b - guard]
    s = a + guard
    while s <= b - guard + 0.1:
        out.append(s)
        s += step
    return out


def _inset(iv, prefer: float) -> float:
    """落在区间内部, 不贴红边。prefer 已经在区间里就留在附近; 在外面就坐到中间, 不贴最近的唇。"""
    a, b = iv
    span = b - a
    if span <= 2.0:
        return (a + b) / 2
    guard = min(RED_CLEARANCE_PX, span / 2 - 1.0)
    if a <= prefer <= b:
        return min(max(prefer, a + guard), b - guard)
    return (a + b) / 2


def _plan_hold(field: RedField, line, note, click_ms: int, end_ms: int, occupied=None, prefer_s: float = 0.0):
    """规划一条长条的手指。

    返回 (segments, prepress)。segments 是 [(按下时刻, 按下坐标, 之后每毫秒的坐标)]。
    红场要盖住当前触点、又不能沿垂线滑过去时, 先在空位上按住新的, 再松开原来的。
    判定线瞬移不算换边, 留给接力去重叠。
    """
    if end_ms < click_ms:
        return None
    cache: dict[int, tuple] = {}

    def geom(ms: int):
        hit = cache.get(ms)
        if hit is None:
            raw = hold_point(line, note, ms)
            _, sa, ca = note_state(line, note, ms)
            nx, ny = _normal(sa, ca)
            t = ms / 1000.0
            ivs = field.safe_intervals(raw[0], raw[1], sa, ca, t, RED_CLEARANCE_PX)
            if not ivs:
                ivs = field.safe_intervals(raw[0], raw[1], sa, ca, t, 0.0)
            hit = (raw, nx, ny, ivs)
            cache[ms] = hit
        return hit

    def pos_of(ms: int, s: float):
        raw, nx, ny, _ = geom(ms)
        return raw[0] + nx * s, raw[1] + ny * s

    def safe_s(ms: int, s: float) -> bool:
        if ms < click_ms or ms > end_ms + _LOOKAHEAD_MS:
            return False
        return _home(geom(ms)[3], s) is not None

    def pick(ms: int, prefer: float):
        ivs = geom(ms)[3]
        if not ivs:
            return None
        def key(iv):
            a, b = iv
            inside = a <= prefer <= b
            c = min(max(prefer, a), b)
            return (0 if inside else 1, abs(c - prefer), -(b - a))
        return _inset(min(ivs, key=key), prefer)

    def step(ms: int, s: float, toward: float | None = None):
        home = _home(geom(ms)[3], s)
        if home is None:
            return None
        target = _inset(home, s if toward is None else toward)
        if toward is not None:
            target = min(max(target, home[0]), home[1])
        if abs(target - s) > _STEP_PX:
            target = s + math.copysign(_STEP_PX, target - s)
        return target

    def forecast(ms: int, s: float, limit: int):
        """当前偏移再往前, 最早哪一毫秒会掉出空位。中间遇到判定线瞬移就停, 那不是红场换边。"""
        cur = s
        last = min(limit, end_ms)
        for u in range(ms + 1, last + 1):
            raw, prev = geom(u)[0], geom(u - 1)[0]
            if math.hypot(raw[0] - prev[0], raw[1] - prev[1]) > _TELEPORT_PX:
                return None
            home = _home(geom(u)[3], cur)
            if home is None:
                return u
            nxt = _inset(home, cur)
            if abs(nxt - cur) > _STEP_PX:
                nxt = cur + math.copysign(_STEP_PX, nxt - cur)
            if not (home[0] <= nxt <= home[1]):
                return u
            cur = nxt
        return None

    def survival(s: float, ms: int) -> int:
        lived = 0
        for k in range(0, 180, 8):
            u = ms + k
            if u > end_ms or not safe_s(u, s):
                break
            lived += 8
        return lived

    def guard_level(ms: int, s: float) -> int:
        """0 干净, 1 只擦到已经过了 Good 的音符, 2 会抢走或打出 Bad。没有检查函数时当干净。"""
        if occupied is None:
            return 0
        return occupied(ms, pos_of(ms, s))

    def refuge(danger: int, avoid: float, now: int):
        """danger 时还在的空位, 而且这一下不能抢走别的音符、也不能提前打出 Bad。

        提前量够不够, 排在“不误触”后面。±80ms 里的点击会把那个音符判走;
        再早到 180ms 是 Bad。全都挡了就不要按, 不能用一次 Bad 去换提前量。
        只擦到已经过了 Good 的音符可以退而求其次, 但仍然比进红场好。
        """
        best = None
        for a, b in geom(min(danger, end_ms))[3]:
            if b - a < 8.0:
                continue
            for s in _sample_s((a, b)):
                if abs(s - avoid) < 10.0 and a - 2 <= avoid <= b + 2 and b - a < 120:
                    continue
                found_at = {}
                # 发现得晚也要往回找。等到红场贴到手指上才搜, 搜索区间是空的, 只能和抬起挤在同一毫秒。
                earliest = max(click_ms, danger - _LOOKAHEAD_MS)
                for t in range(earliest, danger, 4):
                    if not safe_s(t, s) or field.contains(*pos_of(t, s), t / 1000.0):
                        continue
                    got = guard_level(t, s)
                    if got >= 2 or got in found_at:
                        continue
                    found_at[got] = t
                    if 0 in found_at:
                        break
                if not found_at:
                    continue
                lived = survival(s, danger)
                for lvl, press in found_at.items():
                    lead = danger - press
                    # 重叠不够就换不了手: 有提前量的按下(哪怕只擦到已经判过的音符)优先于
                    # 刚好踩在危险时刻的干净按下。干净仍然优先于擦边, 但都要来得及重叠。
                    key = (0 if lead >= _OVERLAP_MS else 1, lvl, -lead, -lived, -(b - a), abs(s - avoid))
                    if best is None or key < best[0]:
                        best = (key, s, press)
        return None if best is None else (best[1], best[2])

    def slide_clear(now: int, s: float, target: float, arrive: int, max_px: float = _SLIDE_MAX_PX) -> bool:
        if abs(target - s) > max_px or arrive <= now:
            return False
        steps = arrive - now
        prev = pos_of(now, s)
        for i in range(1, steps + 1):
            u = now + i
            ss = s + (target - s) * i / steps
            if not safe_s(u, ss):
                return False
            p = pos_of(u, ss)
            if field.segment_hits(prev, p, u / 1000.0) or field.contains(p[0], p[1], u / 1000.0):
                return False
            prev = p
        return True

    def slide_target(now: int, s: float, danger: int):
        """没有不误触的新按下时, 同一只手指能不过红场滑到空位, 就滑。滑不过去也不许打出 Bad。"""
        best = None
        arrive = min(max(now + 1, danger - _LIFT_EARLY_MS), end_ms)
        for a, b in geom(min(danger, end_ms))[3]:
            if b - a < 8.0:
                continue
            target = (a + b) / 2
            if abs(target - s) < 8.0:
                continue
            if not slide_clear(now, s, target, arrive, _SLIDE_FALLBACK_PX):
                continue
            key = (abs(target - s), -(b - a))
            if best is None or key < best[0]:
                best = (key, target)
        return None if best is None else best[1]

    def still_holding(pos, ms: int) -> bool:
        """这个屏幕坐标此刻还判得中这条长条, 而且没进红场。"""
        if ms < click_ms or ms > end_ms + 8:
            return False
        raw, nx, ny, _ = geom(ms)
        along = (pos[0] - raw[0]) * ny - (pos[1] - raw[1]) * nx
        if abs(along) > JUDGE_HALF_WIDTH - 8.0:
            return False
        return not field.contains(pos[0], pos[1], ms / 1000.0)

    def screen_s(ms: int, pos) -> float:
        raw, nx, ny, _ = geom(ms)
        return (pos[0] - raw[0]) * nx + (pos[1] - raw[1]) * ny

    def red_covers(pos, ms: int, horizon: int = 160):
        """手指停在 pos, 最早哪一毫秒会被红场盖住。这段里没有就 None。"""
        last = min(end_ms, ms + horizon)
        for t in range(ms, last + 1, 4):
            if field.contains(pos[0], pos[1], t / 1000.0):
                return t
        return None

    prepress = 0

    def cover(start: int, s: float, stop: int, depth: int = 0):
        nonlocal prepress
        if start > stop or depth > 48:
            return []
        s0 = pick(start, s)
        if s0 is not None and occupied is not None and safe_s(start, s):
            # 收到区间内部有时会收进别的音符的判定带。传进来的点更干净就留在那儿。
            if occupied(start, pos_of(start, s)) < occupied(start, pos_of(start, s0)):
                s0 = s
        if s0 is None:
            nxt = None
            for t in range(start + 1, min(stop, start + 50) + 1):
                got = pick(t, s)
                if got is not None and not field.contains(*pos_of(t, got), t / 1000.0):
                    nxt = (t, got)
                    break
            if nxt is None:
                return []
            return cover(nxt[0], nxt[1], stop, depth + 1)
        head = pos_of(start, s0)
        if field.contains(head[0], head[1], start / 1000.0):
            return []
        pts = []
        prev = head
        prev_raw = geom(start)[0]
        cur = s0
        want = None
        tried = None
        u = start + 1

        def stay():
            nonlocal u, cur, prev, prev_raw, want
            raw = geom(u)[0]
            cur = screen_s(u, prev)
            pts.append(prev)
            prev_raw = raw
            want = None
            u += 1

        def commit_handoff(press, ref_s, leave_at):
            """新手指在 press 按下, 旧手指在红场盖到之前抬起。重叠不够就不要换。"""
            nonlocal prepress, u, cur, prev, prev_raw, want
            press = max(int(press), start)
            if press >= stop or (press <= start and abs(ref_s - cur) < 8.0):
                return None
            lift = min(stop, leave_at - _LIFT_EARLY_MS)
            # 空位消失往往还没进红场。旧手指抬得太早, 新的只重叠了几毫秒, 一帧就断。
            # 红场真正盖上来之前能多留, 就留到重叠够。
            want_lift = press + _OVERLAP_MS
            if lift < want_lift:
                red_t = red_covers(prev, max(start, u - 1), max(8, want_lift - start + _LIFT_EARLY_MS))
                cap = stop if red_t is None else red_t - _LIFT_EARLY_MS
                lift = min(stop, max(lift, min(want_lift, cap)))
            if lift < press + _MIN_OVERLAP_MS:
                if leave_at - 4 >= press + _MIN_OVERLAP_MS:
                    lift = press + _MIN_OVERLAP_MS
                else:
                    return None
            saved = (list(pts), u, cur, prev, prev_raw, want)
            while u <= lift and u <= stop:
                raw = geom(u)[0]
                if (math.hypot(raw[0] - prev_raw[0], raw[1] - prev_raw[1]) > _TELEPORT_PX
                        and still_holding(prev, u)):
                    pts.append(prev)
                    prev_raw = raw
                    cur = screen_s(u, prev)
                    u += 1
                    continue
                stepped = step(u, cur, None)
                if stepped is None:
                    if still_holding(prev, u):
                        pts.append(prev)
                        prev_raw = raw
                        cur = screen_s(u, prev)
                        u += 1
                        continue
                    break
                p = pos_of(u, stepped)
                if (field.segment_hits(prev, p, u / 1000.0) or field.contains(p[0], p[1], u / 1000.0)
                        or math.hypot(p[0] - prev[0], p[1] - prev[1]) > _TELEPORT_PX):
                    if still_holding(prev, u):
                        pts.append(prev)
                        prev_raw = raw
                        cur = screen_s(u, prev)
                        u += 1
                        continue
                    break
                pts.append(p)
                prev, prev_raw, cur = p, raw, stepped
                u += 1
            # 已经规划过危险时刻的, 把尾巴剪到抬起时刻。否则旧手指要在红场里多留一帧。
            keep = max(0, min(len(pts), lift - start))
            del pts[keep:]
            if start + len(pts) < press + _MIN_OVERLAP_MS:
                pts[:], u, cur, prev, prev_raw, want = saved
                return None
            prepress += 1
            rest = cover(press, ref_s, stop, depth + 1)
            if not rest:
                prepress -= 1
                pts[:], u, cur, prev, prev_raw, want = saved
                return None
            return [(start, head, pts)] + rest

        while u <= stop:
            raw = geom(u)[0]
            # 判定线瞬移, 但手指还在判定带里、也没进红场。跟着跳会扫过红场, 拆成两段又没有重叠。停住。
            if (math.hypot(raw[0] - prev_raw[0], raw[1] - prev_raw[1]) > _TELEPORT_PX
                    and still_holding(prev, u)):
                stay()
                continue
            danger = None
            if (u & 7) == 0 or u == start + 1:
                danger = forecast(u - 1, cur, u - 1 + _LOOKAHEAD_MS)
                if danger is None:
                    danger = red_covers(prev, u)
            if danger is not None and danger <= stop and want is None and tried != danger:
                found = refuge(danger, cur, u - 1)
                if found is not None:
                    ref_s, press = found
                    separated = _home(geom(min(danger, end_ms))[3], cur) is None
                    if (not separated and abs(ref_s - cur) <= _SLIDE_MAX_PX
                            and slide_clear(u - 1, cur, ref_s, min(max(u, danger - _LIFT_EARLY_MS), stop))):
                        want = ref_s
                    else:
                        handed = commit_handoff(press, ref_s, danger)
                        if handed is not None:
                            return handed
                        tried = danger
                else:
                    slid = slide_target(u - 1, cur, danger)
                    if slid is None:
                        tried = danger
                    else:
                        want = slid
            stepped = step(u, cur, want)
            if stepped is None:
                if still_holding(prev, u):
                    stay()
                    continue
                found = refuge(min(u + _LIFT_EARLY_MS, stop), cur, u)
                if found is not None:
                    handed = commit_handoff(found[1], found[0], u)
                    if handed is not None:
                        return handed
                # 不能换手, 也不进红场。停在还能判的位置; 已经进了就到此为止。
                if not field.contains(prev[0], prev[1], u / 1000.0):
                    stay()
                    continue
                return [(start, head, pts)]
            p = pos_of(u, stepped)
            far = math.hypot(p[0] - prev[0], p[1] - prev[1]) > _TELEPORT_PX
            blocked = field.segment_hits(prev, p, u / 1000.0) or field.contains(p[0], p[1], u / 1000.0)
            if (far or blocked) and still_holding(prev, u):
                soon = red_covers(prev, u, _LIFT_EARLY_MS + _OVERLAP_MS)
                if soon is not None and tried != soon:
                    found = refuge(soon, cur, u)
                    if found is not None:
                        handed = commit_handoff(found[1], found[0], soon)
                        if handed is not None:
                            return handed
                    tried = soon
                stay()
                continue
            if blocked:
                found = refuge(min(u + _LIFT_EARLY_MS, stop), cur, u)
                if found is not None:
                    handed = commit_handoff(found[1], found[0], u)
                    if handed is not None:
                        return handed
                return [(start, head, pts)]
            pts.append(p)
            prev, prev_raw, cur = p, raw, stepped
            if want is not None and abs(cur - want) < 4.0:
                want = None
            u += 1
        return [(start, head, pts)]

    s = pick(click_ms, prefer_s)
    if s is None:
        return None
    if occupied is not None and safe_s(click_ms, prefer_s):
        # pick 会把点收进区间内部, 有时正好收进别的音符的判定带。干净的那个点还在就留在那儿。
        if occupied(click_ms, pos_of(click_ms, prefer_s)) < occupied(click_ms, pos_of(click_ms, s)):
            s = prefer_s
    early = forecast(click_ms, s, click_ms + _LOOKAHEAD_MS)
    if early is not None:
        found = refuge(early, s, click_ms)
        if found is not None and found[1] == click_ms and abs(found[0] - s) >= 8.0:
            s = found[0]
    segments = [(st, hd, pts) for st, hd, pts in cover(click_ms, s, end_ms) if hd is not None and st <= end_ms]
    if not segments:
        return None
    return segments, prepress


def _note_guard(chart: Chart):
    """新按下会不会打到别的音符。

    返回 0 / 1 / 2。2 是会抢走(±80ms)或提前打出 Bad(再早到 180ms);
    1 是只擦到已经过了 Good 的音符; 0 是干净。
    同一条垂线上投影不变, 换偏移躲不开同一条判定线上的音符, 只能换按下时刻。
    按下之后一两帧判定线还在动, 那两帧也要干净, 不能只看事件时间。
    """
    items = []
    for line in chart.judge_lines:
        for n in line.notes_above + line.notes_below:
            if n.type == NoteType.FLICK:
                continue
            items.append((round(line.seconds(n.time) * 1000), line, n))
    items.sort(key=lambda it: it[0])
    times = [t for t, _, _ in items]
    width = JUDGE_HALF_WIDTH + _GUARD_MARGIN_PX

    def level(ms: int, pos, skip) -> int:
        worst = 0
        px, py = pos
        for sample in (ms, ms + 16, ms + 32):
            lo = bisect_left(times, sample - _BAD_LATE_MS)
            hi = bisect_right(times, sample + _BAD_EARLY_MS)
            for nms, ol, on in items[lo:hi]:
                if on is skip:
                    continue
                dt = nms - sample
                raw, sa, ca = note_state(ol, on, sample)
                along = (px - raw[0]) * ca + (py - raw[1]) * sa
                if abs(along) > width:
                    continue
                if abs(dt) <= _STEAL_MS or _STEAL_MS < dt <= _BAD_EARLY_MS:
                    return 2
                if -_BAD_LATE_MS <= dt < -_STEAL_MS:
                    worst = 1
        return worst

    return level


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
    prepress = 0
    waited = 0
    occupied = _note_guard(chart)

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

    def choose_hold_click(ms0: int, end_ms: int, guard):
        """长条头的按下时刻和垂线偏移。

        先在 Perfect(±40ms) 里找不误触的点。没有, 再放到 ±64ms, 不贴 Good 外沿
        (60fps 一帧会把 ±80 顶出 Good, 变成 Bad)。外沿只在垂线更早一直被盖住时才用。
        按下不能晚于长条结束。窗口里每个点都会误触, 就按原时刻按下, 不跳过。
        """
        def slots(t_ms):
            raw, sa_i, ca_i = note_state(line, note, t_ms)
            raw = hold_point(line, note, t_ms)
            nx, ny = _normal(sa_i, ca_i)
            out = []
            hit = field.vertical_slot(raw[0], raw[1], sa_i, ca_i, t_ms / 1000.0, 0.0)
            if hit is not None:
                out.append((hit[2], (hit[0], hit[1])))
            for clearance in (RED_CLEARANCE_PX, 0.0):
                ivs = field.safe_intervals(raw[0], raw[1], sa_i, ca_i, t_ms / 1000.0, clearance)
                for iv in ivs:
                    for s in _sample_s(iv):
                        pos = (raw[0] + nx * s, raw[1] + ny * s)
                        if not field.contains(pos[0], pos[1], t_ms / 1000.0):
                            out.append((s, pos))
                if out:
                    break
            return out

        raw0, sa0, ca0 = note_state(line, note, ms0)
        raw0 = hold_point(line, note, ms0)
        hit0 = field.vertical_slot(raw0[0], raw0[1], sa0, ca0, ms0 / 1000.0, 0.0)
        if hit0 is not None and ms0 <= end_ms and guard(ms0, (hit0[0], hit0[1]), note) == 0:
            return ms0, hit0[2]
        near = slots(ms0)
        clean = [s for s, pos in near if guard(ms0, pos, note) == 0]
        if clean:
            return ms0, min(clean, key=abs)
        # 原时刻没有完全干净的点, 但有不打出 Bad 的近点。挪到稍后去换一个很远的偏移,
        # 判定线一转, 一帧延迟就把触点推出判定带, 这条长条根本没头。原时刻按下。
        soft = [s for s, pos in near if guard(ms0, pos, note) <= 1]
        if soft:
            return ms0, min(soft, key=abs)
        best = None
        bands = ((0, 0, 40), (1, 44, _SEEK_COMFORT_MS), (2, _SEEK_COMFORT_MS + 4, _SEEK_MS))
        for band, lo, hi in bands:
            for dt in _seek_dts(hi):
                ad = abs(dt)
                if ad < lo or ad > hi:
                    continue
                t_ms = ms0 + dt
                if t_ms > end_ms:
                    continue
                for s, pos in slots(t_ms):
                    lvl = guard(t_ms, pos, note)
                    key = (band, lvl, ad, abs(s))
                    if best is None or key < best[0]:
                        best = (key, t_ms, s)
            if best is not None and best[0][0] == band and best[0][1] == 0:
                break
        if best is None:
            if hit0 is not None and ms0 <= end_ms:
                return ms0, hit0[2]
            return None
        return best[1], best[2]

    for li, line in enumerate(track(chart.judge_lines, description='统计操作帧(红场)...', console=console)):
        for ni, note in enumerate(line.notes_above + line.notes_below):
            ms = round(line.seconds(note.time) * 1000)
            alpha = -line.angle(note.time) * math.pi / 180
            if note.type == NoteType.HOLD:
                hold_ms = math.ceil(line.seconds(note.hold) * 1000)
                end_ms = ms + hold_ms
                chosen = choose_hold_click(ms, end_ms, occupied)
                if chosen is None:
                    skipped += 1
                    continue
                click_ms, prefer_s = chosen
                guard = lambda t, pos, _n=note: occupied(t, pos, _n)
                planned = _plan_hold(field, line, note, click_ms, end_ms, occupied=guard, prefer_s=prefer_s)
                if not planned and click_ms != ms:
                    # 为了躲开别的音符挪了时刻, 但这条长条已经结束。退回原时刻, 不能因此跳过。
                    click_ms, prefer_s = ms, 0.0
                    planned = _plan_hold(field, line, note, click_ms, end_ms, occupied=guard, prefer_s=prefer_s)
                if click_ms != ms:
                    waited += 1
                if not planned:
                    skipped += 1
                    continue
                segments, n_pre = planned
                prepress += n_pre
                for i, (st, hd, pts) in enumerate(segments):
                    path = tuple(pts)
                    tag = (li, ni) if i == 0 else (li, ni, st)
                    holds[tag] = (line, note, st, len(path), path, hd)
                    _, sa_h, ca_h = note_state(line, note, st)
                    frames[st].add(NoteType.HOLD, hd, math.atan2(sa_h, ca_h), path, tag=tag)
                    heads.append((st, line, note))
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
    if prepress:
        console.print(f'红场赶来之前, {prepress}次先按在垂线空位上, 再松开原来的触点')
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
        stats['red_prepress'] = prepress
    return events


__all__ = ['solve']
