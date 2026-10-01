# 扫屏算法: 专用触点在屏幕上不停地高速来回扫动, drag(黄键)不再逐个规划;
# flick(红键)由几个"滑键触点"负责: 它们在第一个音符之前按下(避开所有tap/hold), 直到最后一个音符之后才抬起,
# 只是滑到每个flick的位置快速划一下, 再滑到下一个; 其余触点按algo2的方式处理tap(蓝键)和hold。
#
# 为什么flick不能只靠扫屏: Phigros中"一根手指向一个方向滑动仅能判定一次"(见萌娘百科/sim-phi),
# 触点必须先减速或换方向才能判定下一个flick。扫屏触点每次单程(约0.1秒)最多接一个flick,
# 扫得再快也接不住成批的flick; 而游戏每帧只读一次触点位置, 扫得太快反而会跳过音符。
# 滑键触点每划一个flick都换一次方向(滑向下一个flick的方向与上一次划的方向错开), 保证每次都是"新的一划"。
# 滑键触点全曲只按下一次, flick完全靠滑动判定, 不会重新按下(也就不会误触附近的tap/hold)。
#
# 两种布局:
# + algo3 : 4个(10触点时3个)触点分别在不同高度的"行"上左右扫, 只在drag/flick前后扫
# + algo3f: 像坐标轴一样, 1个触点在屏幕高度正中左右扫(横轴), 1个触点在屏幕宽度正中上下扫(纵轴),
#           从第一个音符之前一直扫到最后一个音符之后(见 algo3f.py)
#
# 原理(见 tools/judge_sim.py):
# + 判定只看触点在判定线方向上的投影, 范围约 ±151 像素(1280x720)。
# + drag: 判定时间附近任意一帧有任意触点在范围内即可(不需要按下)。
# + flick: 判定时间附近任意一帧有"正在快速滑动"的触点在范围内即可; 一次滑动只能判定一个。
# + 游戏每帧只采样一次触点位置, 所以扫动速度不能太快: 相邻两帧(60fps)之间扫过的距离要小于判定宽度。
# + 只沿x方向扫动时, 竖直的判定线(投影方向是y)无法覆盖, 所以需要多行或者再加一个上下扫的触点。
# + 各触点的周期互不相同且与帧率不成整数比, 避免每帧都采样到同样的几个位置。
#
# 风险: 扫屏/滑键触点按下的瞬间会被当成一次点击, 可能抢走附近的tap/hold判定;
# 所以按下的时间和位置会避开前后 300ms 内的所有tap/hold, 并在按下后先静止 SETTLE_MS 再开始移动。

import copy
import math
from collections import defaultdict
from typing import NamedTuple

from rich.console import Console

from chart import Chart
from note import NoteType
from . import algo2
from .algo_base import (FLICK_END, FLICK_RADIUS, FLICK_START, JUDGE_HALF_WIDTH, MAX_POINTERS,
                        SWEEP_POINTER_BASE_MIN, TouchAction, VirtualTouchEvent, flick_path, flick_time_shift)

SWEEP_X_MIN = 20.0
SWEEP_X_MAX = 1260.0
SWEEP_Y_MIN = 20.0
SWEEP_Y_MAX = 700.0
SWEEP_ROWS_16 = (90.0, 270.0, 450.0, 630.0)   # 行距180 < 判定宽度302
SWEEP_ROWS_10 = (120.0, 360.0, 600.0)          # 行距240 < 判定宽度302
SWEEP_HALF_PERIODS = (97, 103, 89, 109)        # 单程时间(ms), 约12000px/s, 60fps下每帧约210像素
SWEEP_UPDATE_MS = 6                            # 每个扫屏触点每6ms更新一次位置
SWEEP_POINTER_BASE = SWEEP_POINTER_BASE_MIN

ACTIVE_BEFORE = 400      # drag/flick 判定前后保持扫屏的时间(ms)
ACTIVE_AFTER = 400
MERGE_GAP = 1500         # 两段扫屏间隔小于此值时合并, 避免频繁按下/抬起
DOWN_GUARD = 300         # 扫屏触点按下时, 前后这么多ms内的tap/hold需避开
DOWN_SEARCH = 2000       # 找不到安全的按下时机时, 最多提前这么多ms
SAFE_MARGIN = 30.0
SETTLE_MS = 40           # 按下后先静止这么久(超过30fps的一帧)再开始扫动, 保证"按下"这一帧的位置是安全的
PAUSE_BUTTON_BOX = (160.0, 160.0)  # 左上角暂停按钮附近不作为按下位置


class Sweeper(NamedTuple):
    """一个扫屏触点: horizontal=True 时在 y=fixed 处沿x往返, 否则在 x=fixed 处沿y往返"""
    pointer: int
    horizontal: bool
    fixed: float
    lo: float
    hi: float
    half_period: float   # 单程时间(ms)
    phase: float = 0.0   # 首选的起始位置(0~1)
    forward: bool = True
    tick: int = 0        # 在第几个毫秒更新(错开多个触点的发送时间)

    def point(self, v: float) -> tuple[float, float]:
        return (v, self.fixed) if self.horizontal else (self.fixed, v)


def sweep_rows(max_pointers: int) -> tuple[float, ...]:
    return SWEEP_ROWS_16 if max_pointers >= 16 else SWEEP_ROWS_10


def row_sweepers(max_pointers: int) -> list[Sweeper]:
    """algo3的布局: 每行一个左右扫的触点"""
    return [Sweeper(SWEEP_POINTER_BASE + k, True, y, SWEEP_X_MIN, SWEEP_X_MAX,
                    SWEEP_HALF_PERIODS[k % len(SWEEP_HALF_PERIODS)], (k * 0.37) % 1, k % 2 == 0, k)
            for k, y in enumerate(sweep_rows(max_pointers))]


def triangle(t: float, t0: float, x0: float, half_period: float, forward: bool,
             lo: float = SWEEP_X_MIN, hi: float = SWEEP_X_MAX) -> float:
    """从t0时刻位于x0(方向forward)的三角波在t时刻的坐标, 在[lo, hi]之间往返"""
    span = hi - lo
    # 把(x0, 方向)换算成周期内的相位 u ∈ [0, 2)
    u0 = (x0 - lo) / span
    if not forward:
        u0 = 2 - u0
    u = (u0 + (t - t0) / half_period) % 2
    return lo + span * (u if u <= 1 else 2 - u)


def _note_times(chart: Chart, types=None) -> list[tuple[int, int]]:
    """(判定时间ms, 结束时间ms)"""
    out = []
    for line in chart.judge_lines:
        for note in line.notes:
            if types is None or note.type in types:
                t = round(line.seconds(note.time) * 1000)
                out.append((t, t + round(line.seconds(note.hold) * 1000)))
    out.sort()
    return out


def _active_intervals(chart: Chart) -> list[list[int]]:
    """需要扫屏的时间段: 所有drag/flick判定时间前后各 ACTIVE_BEFORE/AFTER ms, 相近的段合并"""
    intervals: list[list[int]] = []
    for t, _ in _note_times(chart, (NoteType.DRAG, NoteType.FLICK)):
        if intervals and t - ACTIVE_BEFORE <= intervals[-1][1] + MERGE_GAP:
            intervals[-1][1] = max(intervals[-1][1], t + ACTIVE_AFTER)
        else:
            intervals.append([t - ACTIVE_BEFORE, t + ACTIVE_AFTER])
    return intervals


def whole_song_interval(chart: Chart, before: int = 1000, after: int = 500) -> list[list[int]]:
    """从第一个音符之前一直到最后一个音符(含hold尾)之后"""
    times = _note_times(chart)
    if not times:
        return []
    return [[times[0][0] - before, max(e for _, e in times) + after]]


def _heads(chart: Chart) -> list[tuple[int, object, object]]:
    """所有tap/hold头部: (判定时间ms, 判定线, 音符)"""
    heads = [
        (round(line.seconds(note.time) * 1000), line, note)
        for line in chart.judge_lines
        for note in line.notes
        if note.type in (NoteType.TAP, NoteType.HOLD)
    ]
    heads.sort(key=lambda h: h[0])
    return heads


def _safe_values(heads, t: int, sw: Sweeper) -> list[float]:
    """t时刻按下(并静止SETTLE_MS)时不会误触任何tap/hold的位置(沿扫动方向的坐标)"""
    near = [h for h in heads if t - DOWN_GUARD <= h[0] <= t + SETTLE_MS + DOWN_GUARD]
    frames = []
    for _, line, note in near:
        for tt in range(t, t + SETTLE_MS + 1, 10):
            lt = line.time(tt / 1000)
            cx, cy = line.pos(lt)
            a = -line.angle(lt) * math.pi / 180
            frames.append((cx, cy, math.cos(a), math.sin(a), note.x * 72))
    out = []
    for i in range(63):
        v = sw.lo + (sw.hi - sw.lo) * i / 62
        x, y = sw.point(v)
        if x < PAUSE_BUTTON_BOX[0] and y < PAUSE_BUTTON_BOX[1]:
            continue
        if all(abs((x - cx) * ca + (y - cy) * sa - nx) > JUDGE_HALF_WIDTH + SAFE_MARGIN
               for cx, cy, ca, sa, nx in frames):
            out.append(v)
    return out


def _choose_down(heads, start: int, sw: Sweeper, preferred: float) -> tuple[int, float, bool]:
    """在start之前寻找安全的按下时间和位置: 返回 (时间, 坐标, 是否安全)"""
    for dt in range(0, DOWN_SEARCH + 1, 10):
        t = start - dt
        vs = _safe_values(heads, t, sw)
        if vs:
            return t, min(vs, key=lambda v: abs(v - preferred)), True
    return start, preferred, False


POINTER_NOTE_TYPES = (NoteType.TAP, NoteType.HOLD)


def _without_sweep_notes(chart: Chart) -> Chart:
    """复制一份只保留tap/hold的谱面, 交给algo2规划"""
    lines = []
    for line in chart.judge_lines:
        new_line = copy.copy(line)
        new_line.notes_above = [n for n in line.notes_above if n.type in POINTER_NOTE_TYPES]
        new_line.notes_below = [n for n in line.notes_below if n.type in POINTER_NOTE_TYPES]
        lines.append(new_line)
    new_chart = copy.copy(chart)
    new_chart.judge_lines = lines
    return new_chart


def plan_sweepers(chart: Chart, sweepers: list[Sweeper], intervals: list[list[int]],
                  console: Console | None = None) -> defaultdict[int, list[VirtualTouchEvent]]:
    events: defaultdict[int, list[VirtualTouchEvent]] = defaultdict(list)
    heads = _heads(chart)
    unsafe = 0
    for sw in sweepers:
        pid = sw.pointer
        prev_end = None
        for start, end in intervals:
            preferred = sw.lo + (sw.hi - sw.lo) * sw.phase
            down_t, v0, safe = _choose_down(heads, start - SETTLE_MS, sw, preferred)
            if prev_end is not None and down_t <= prev_end:
                down_t = prev_end + 1
            if not safe:
                unsafe += 1
            events[down_t].append(VirtualTouchEvent(sw.point(v0), TouchAction.DOWN, pid))
            move_t0 = down_t + SETTLE_MS
            t = move_t0 + 1 + sw.tick  # 不同触点在不同的毫秒更新, 分散发送压力
            while t < end:
                v = triangle(t, move_t0, v0, sw.half_period, sw.forward, sw.lo, sw.hi)
                events[t].append(VirtualTouchEvent(sw.point(v), TouchAction.MOVE, pid))
                t += SWEEP_UPDATE_MS
            v = triangle(end, move_t0, v0, sw.half_period, sw.forward, sw.lo, sw.hi)
            events[end].append(VirtualTouchEvent(sw.point(v), TouchAction.UP, pid))
            prev_end = end
    if console is not None:
        total = sum(e - s for s, e in intervals) / 1000
        console.print(f'扫屏: {len(sweepers)}个触点, {len(intervals)}段, 共{total:.1f}秒')
        if unsafe:
            console.print(f'[yellow]警告: 有{unsafe}次扫屏触点按下时无法完全避开附近的tap/hold, 可能导致个别tap提前判定[/yellow]')
    return events


FLICK_FINGER_BASE = 2200
FLICK_GROUP_GAP = 1500   # 相邻flick间隔小于此值时算作同一段(只用于统计)
GLIDE_MS = 24            # 滑到下一个flick起点所用的时间(ms)
FLICK_UPDATE_MS = 4      # 滑键触点的位置更新间隔(ms)
FLICK_RELEASE_AFTER = 40
WHOLE_SONG_BEFORE = 1000   # 滑键触点在第一个音符之前多久按下(ms)
WHOLE_SONG_AFTER = 500     # 最后一个音符(含hold尾)之后多久抬起(ms)


def flick_finger_count(max_pointers: int, sweepers: int = 0) -> int:
    """滑键触点个数: 16触点时4个; 10触点时尽量3个, 但至少给tap/hold留5个"""
    if max_pointers >= 16:
        return 4
    return max(2, min(3, max_pointers - sweepers - 5))


def _safe_points(heads, t: int, candidates: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """t时刻按下(并静止SETTLE_MS)时不会误触任何tap/hold的位置"""
    near = [h for h in heads if t - DOWN_GUARD <= h[0] <= t + SETTLE_MS + DOWN_GUARD]
    frames = []
    for _, line, note in near:
        for tt in range(t, t + SETTLE_MS + 1, 10):
            lt = line.time(tt / 1000)
            cx, cy = line.pos(lt)
            a = -line.angle(lt) * math.pi / 180
            frames.append((cx, cy, math.cos(a), math.sin(a), note.x * 72))
    return [(x, y) for x, y in candidates
            if not (x < PAUSE_BUTTON_BOX[0] and y < PAUSE_BUTTON_BOX[1])
            and all(abs((x - cx) * ca + (y - cy) * sa - nx) > JUDGE_HALF_WIDTH + SAFE_MARGIN
                    for cx, cy, ca, sa, nx in frames)]


_GRID = [(80.0 + 1120.0 * i / 14, 60.0 + 600.0 * j / 6) for i in range(15) for j in range(7)]


class _Finger:
    def __init__(self, pid: int):
        self.pid = pid
        self.pos: tuple[float, float] = (640.0, 360.0)
        self.free = -math.inf           # 从这个时刻起可以开始滑向下一个flick
        self.last_dir: tuple[float, float] | None = None
        self.down = False


def plan_flick_fingers(chart: Chart, count: int, console: Console | None = None
                       ) -> tuple[defaultdict[int, list[VirtualTouchEvent]], int]:
    """滑键触点: 返回 (事件, 没有分配到滑键触点的flick数)"""
    events: defaultdict[int, list[VirtualTouchEvent]] = defaultdict(list)
    heads = _heads(chart)
    items = []
    for line in chart.judge_lines:
        for note in line.notes:
            if note.type == NoteType.FLICK:
                items.append((round(line.seconds(note.time) * 1000), line, note))
    items.sort(key=lambda it: it[0])
    groups: list[list] = []
    for it in items:
        if groups and it[0] - groups[-1][-1][0] < FLICK_GROUP_GAP:
            groups[-1].append(it)
        else:
            groups.append([it])

    unassigned = unsafe = 0
    if not groups:
        return events, 0
    # 滑键触点全曲只按下一次: 第一个音符之前按下, 最后一个音符之后才抬起, 中间只滑动不抬起
    times = _note_times(chart)
    first_start = min(times[0][0] - WHOLE_SONG_BEFORE, groups[0][0][0] + FLICK_START - GLIDE_MS - SETTLE_MS)
    fingers = [_Finger(FLICK_FINGER_BASE + k) for k in range(count)]
    taken: list[tuple[float, float]] = []
    for k, f in enumerate(fingers):
        # 在不会误触tap/hold的时间和位置按下(各触点错开位置)
        down_t, pos, safe = first_start, _GRID[(k * 37) % len(_GRID)], False
        for dt in range(0, DOWN_SEARCH + 1, 10):
            pts = [p for p in _safe_points(heads, first_start - dt, _GRID) if p not in taken]
            if pts:
                down_t = first_start - dt
                pos = min(pts, key=lambda p: math.hypot(p[0] - 640, p[1] - 360) + 97 * k * (p[0] < 640))
                safe = True
                break
        unsafe += not safe
        taken.append(pos)
        f.pos, f.free, f.down = pos, down_t + SETTLE_MS, True
        events[down_t].append(VirtualTouchEvent(pos, TouchAction.DOWN, f.pid))

    for group in groups:
        for t_ms, line, note in group:
            shift = flick_time_shift(line, note)
            s0 = t_ms + FLICK_START
            ready = [f for f in fingers if f.free <= s0 - 8]
            if not ready:
                unassigned += 1
                continue
            best = None
            for f in ready:
                for rev in (False, True):
                    path = flick_path(line, note, t_ms, FLICK_START, FLICK_END, FLICK_RADIUS, shift, reverse=rev)
                    gx, gy = path[0][0] - f.pos[0], path[0][1] - f.pos[1]
                    dx, dy = path[-1][0] - path[0][0], path[-1][1] - path[0][1]
                    dist = math.hypot(gx, gy)
                    ref = (gx / dist, gy / dist) if dist > 20 else f.last_dir
                    dn = math.hypot(dx, dy) or 1.0
                    # 这一划的方向要和之前的移动方向错开(点积<=0), 才算"新的一划"
                    turn = 0.0 if ref is None else (ref[0] * dx + ref[1] * dy) / dn
                    score = (turn > 0.1, dist)
                    if best is None or score < best[0]:
                        best = (score, f, path, (dx / dn, dy / dn))
            _, f, path, direction = best
            # 滑向起点
            g0 = max(f.free, s0 - GLIDE_MS)
            x0, y0 = f.pos
            for t in range(g0 + FLICK_UPDATE_MS, s0, FLICK_UPDATE_MS):
                r = (t - g0) / (s0 - g0)
                events[t].append(VirtualTouchEvent((x0 + (path[0][0] - x0) * r, y0 + (path[0][1] - y0) * r),
                                                   TouchAction.MOVE, f.pid))
            # 快速划过
            for i in list(range(0, len(path) - 1, FLICK_UPDATE_MS)) + [len(path) - 1]:
                events[s0 + i].append(VirtualTouchEvent(path[i], TouchAction.MOVE, f.pid))
            f.pos, f.free, f.last_dir = path[-1], s0 + len(path), direction

    end = max(max(f.free for f in fingers) + FLICK_RELEASE_AFTER, max(e for _, e in times) + WHOLE_SONG_AFTER)
    for f in fingers:
        events[end].append(VirtualTouchEvent(f.pos, TouchAction.UP, f.pid))

    if console is not None:
        console.print(f'滑键触点: {count}个, {len(groups)}段, 共{len(items)}个flick')
        if unassigned:
            console.print(f'[yellow]警告: 有{unassigned}个flick同时出现得太密, 没有空闲的滑键触点(扫屏触点可能接到)[/yellow]')
        if unsafe:
            console.print(f'[yellow]警告: 有{unsafe}次滑键触点按下时无法完全避开附近的tap/hold[/yellow]')
    return events, unassigned


def solve_with(chart: Chart, console: Console, max_pointers: int, sweepers: list[Sweeper],
               intervals: list[list[int]]) -> defaultdict[int, list[VirtualTouchEvent]]:
    fingers = flick_finger_count(max_pointers, len(sweepers))
    rest = max_pointers - len(sweepers) - fingers
    console.print(f'扫屏算法: {len(sweepers)}个触点扫屏(drag), {fingers}个滑键触点(flick), {rest}个触点处理tap/hold')
    ans = algo2.solve(_without_sweep_notes(chart), console, rest)
    for ms, evs in plan_sweepers(chart, sweepers, intervals, console).items():
        ans[ms].extend(evs)
    for ms, evs in plan_flick_fingers(chart, fingers, console)[0].items():
        ans[ms].extend(evs)
    return ans


def solve(chart: Chart, console: Console, max_pointers: int = MAX_POINTERS) -> defaultdict[int, list[VirtualTouchEvent]]:
    return solve_with(chart, console, max_pointers, row_sweepers(max_pointers), _active_intervals(chart))


__all__ = ['solve', 'solve_with', 'plan_sweepers', 'plan_flick_fingers', 'flick_finger_count', 'sweep_rows', 'row_sweepers', 'triangle', 'Sweeper',
           'whole_song_interval', 'SWEEP_POINTER_BASE']
