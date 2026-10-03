"""噪点红场谱面的规划。

红场里的点击无效, 点进去会把后面的判定也带崩。判定只看触点在判定线方向上的投影,
所以一个音符可以点在它所在位置、垂直于判定线的任意坐标上。红场在动, 每一毫秒
的空位不一样: 这一下没缝, 就先在 Perfect(±40ms) 里等它让开, 还没有再放到 Good(±80ms)。
长条不能等红场盖到手指上再换: 红区从中间(或略偏两侧)长出来时, 要在它赶到之前
先按住垂线上的空位, 新触点不松, 然后再松开原来的。同一侧还连得上的, 就提前挪过去,
不扫过红场, 也不把判定线瞬移当成换边。

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
# 不能等红场已经盖住再换手。
_PRESS_LEAD_MS = 48
_LIFT_EARLY_MS = 48
_LOOKAHEAD_MS = 112
_SLIDE_MAX_PX = 70.0
_TELEPORT_PX = 80.0
_STEP_PX = 16.0


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


def _plan_hold(field: RedField, line, note, click_ms: int, end_ms: int, occupied=None):
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

    def blocked(ms: int, s: float) -> bool:
        if occupied is None:
            return False
        return occupied(ms, pos_of(ms, s))

    def refuge(danger: int, avoid: float, now: int):
        """danger 时还在的空位。优先现在就能按、而且能多留一会儿的, 不选马上又消失的窄缝。

        空位如果会误触别的音符, 先换一个点; 全都挡了也要按, 进红场比误触更糟。
        """
        best = None
        for a, b in geom(min(danger, end_ms))[3]:
            if b - a < 8.0:
                continue
            for s in ((a + b) / 2, a + min(RED_CLEARANCE_PX, (b - a) / 3), b - min(RED_CLEARANCE_PX, (b - a) / 3)):
                if abs(s - avoid) < 10.0 and a - 2 <= avoid <= b + 2 and b - a < 120:
                    continue
                first_safe = first_free = None
                for t in range(max(now, click_ms), danger):
                    if not safe_s(t, s):
                        continue
                    if first_safe is None:
                        first_safe = t
                    if first_free is None and not blocked(t, s):
                        first_free = t
                        break
                if first_safe is None:
                    continue
                # 提前量不够就进红场, 比误触别的音符更糟。先保证在红场前按住。
                options = [(first_safe, True)]
                if first_free is not None:
                    options.append((first_free, False))
                lived = survival(s, danger)
                for press, hits_other in options:
                    lead = danger - press
                    key = (0 if lead >= _PRESS_LEAD_MS else 1, 1 if hits_other else 0,
                           -lead, -lived, -(b - a), abs(s - avoid))
                    slot = (key, s, press)
                    if best is None or key < best[0]:
                        best = slot
        return None if best is None else (best[1], best[2])

    def slide_clear(now: int, s: float, target: float, arrive: int) -> bool:
        if abs(target - s) > _SLIDE_MAX_PX or arrive <= now:
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

    prepress = 0

    def cover(start: int, s: float, stop: int, depth: int = 0):
        nonlocal prepress
        if start > stop or depth > 48:
            return []
        s0 = pick(start, s)
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
        u = start + 1
        while u <= stop:
            raw = geom(u)[0]
            if math.hypot(raw[0] - prev_raw[0], raw[1] - prev_raw[1]) > _TELEPORT_PX:
                ns = pick(u, 0.0)
                if ns is None or field.contains(*pos_of(u, ns), u / 1000.0):
                    segs = [(start, head, pts)]
                    segs.extend(cover(u, 0.0, stop, depth + 1))
                    return segs
                cur = ns
                want = None
                p = pos_of(u, cur)
                pts.append(p)
                prev, prev_raw = p, raw
                u += 1
                continue
            danger = None
            if (u & 7) == 0 or u == start + 1:
                danger = forecast(u - 1, cur, u - 1 + _LOOKAHEAD_MS)
            if danger is not None and danger <= stop:
                found = refuge(danger, cur, u - 1)
                if found is not None:
                    ref_s, press = found
                    # 危险时刻两边已经隔着红场。现在滑过去会穿过它要出现的位置, 只能先按住再松原来的。
                    separated = _home(geom(min(danger, end_ms))[3], cur) is None
                    if (not separated and abs(ref_s - cur) <= _SLIDE_MAX_PX
                            and slide_clear(u - 1, cur, ref_s, min(max(u, danger - _LIFT_EARLY_MS), stop))):
                        want = ref_s
                    elif not (press <= start and abs(ref_s - cur) < 8.0):
                        press = max(press, start)
                        lift = min(stop, max(press, danger - _LIFT_EARLY_MS))
                        while u <= lift:
                            stepped = step(u, cur, want)
                            if stepped is None:
                                break
                            p = pos_of(u, stepped)
                            if field.segment_hits(prev, p, u / 1000.0) or field.contains(p[0], p[1], u / 1000.0):
                                break
                            pts.append(p)
                            prev, prev_raw, cur = p, geom(u)[0], stepped
                            u += 1
                        segs = [(start, head, pts)]
                        prepress += 1
                        segs.extend(cover(press, ref_s, stop, depth + 1))
                        return segs
            stepped = step(u, cur, want)
            if stepped is None:
                found = refuge(min(u + 1, stop), cur, u)
                segs = [(start, head, pts)]
                if found is not None:
                    prepress += 1
                    segs.extend(cover(max(found[1], u), found[0], stop, depth + 1))
                else:
                    segs.extend(cover(u + 1, cur, stop, depth + 1))
                return segs
            p = pos_of(u, stepped)
            if field.segment_hits(prev, p, u / 1000.0) or field.contains(p[0], p[1], u / 1000.0):
                found = refuge(min(u + 8, stop), cur, u)
                segs = [(start, head, pts)]
                if found is not None and abs(found[0] - cur) >= 8.0:
                    prepress += 1
                    segs.extend(cover(max(found[1], u), found[0], stop, depth + 1))
                return segs
            pts.append(p)
            prev, prev_raw, cur = p, raw, stepped
            if want is not None and abs(cur - want) < 4.0:
                want = None
            u += 1
        return [(start, head, pts)]

    s = pick(click_ms, 0.0)
    if s is None:
        return None
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
    """新按下的长条触点不要落进别的音符的判定带。同一条垂线上投影不变, 挡的是别的判定线。"""
    items = []
    for line in chart.judge_lines:
        for n in line.notes_above + line.notes_below:
            items.append((round(line.seconds(n.time) * 1000), line, n))
    items.sort(key=lambda it: it[0])
    times = [t for t, _, _ in items]

    def occupied(ms: int, pos, skip) -> bool:
        lo = bisect_left(times, ms - 220)
        hi = bisect_right(times, ms + 80)
        for _, ol, on in items[lo:hi]:
            if on is skip:
                continue
            raw, sa, ca = note_state(ol, on, ms)
            along = (pos[0] - raw[0]) * ca + (pos[1] - raw[1]) * sa
            if abs(along) <= JUDGE_HALF_WIDTH:
                return True
        return False

    return occupied


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
                click_ms, _, _, _ = found
                if click_ms != ms:
                    waited += 1
                planned = _plan_hold(field, line, note, click_ms, ms + hold_ms,
                                     occupied=lambda t, pos, _n=note: occupied(t, pos, _n))
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
