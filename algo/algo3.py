# 扫屏算法: 专用触点在屏幕上不停地高速来回扫动, drag(黄键)和flick(红键)不再逐个规划;
# 其余触点按algo2的方式处理tap(蓝键)和hold。
#
# 两种布局:
# + algo3 : 4个(10触点时3个)触点分别在不同高度的"行"上左右扫, 只在drag/flick前后扫
# + algo3f: 像坐标轴一样, 1个触点在屏幕高度正中左右扫(横轴), 1个触点在屏幕宽度正中上下扫(纵轴),
#           从第一个音符之前一直扫到最后一个音符之后(见 algo3f.py)
#
# 原理(以Phira复刻的判定为准, 见 tools/judge_sim.py):
# + 判定只看触点在判定线方向上的投影, 范围约 ±151 像素(1280x720)。
# + drag: 判定时间 ±220ms 内任意一帧有任意触点在范围内即可(不需要按下)。
# + flick: 判定时间附近任意一帧有"正在快速移动"的触点在范围内即可; 每个触点每帧最多判定一个flick。
# + 游戏每帧只采样一次触点位置, 所以扫动速度不能太快: 相邻两帧(60fps)之间扫过的距离要小于判定宽度。
# + 只沿x方向扫动时, 竖直的判定线(投影方向是y)无法覆盖, 所以需要多行或者再加一个上下扫的触点。
# + 各触点的周期互不相同且与帧率不成整数比, 避免每帧都采样到同样的几个位置。
#
# 风险: 扫屏触点按下的瞬间会被当成一次点击, 可能抢走附近的tap/hold判定;
# 所以按下的时间和位置会避开前后 300ms 内的所有tap/hold, 并在按下后先静止 SETTLE_MS 再开始扫动。

import copy
import math
from collections import defaultdict
from typing import NamedTuple

from rich.console import Console

from chart import Chart
from note import NoteType
from . import algo2
from .algo_base import MAX_POINTERS, SWEEP_POINTER_BASE_MIN, TouchAction, VirtualTouchEvent

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
JUDGE_HALF_WIDTH = 151.2  # 判定宽度的一半(像素), 见 tools/judge_sim.py
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


def _without_sweep_notes(chart: Chart) -> Chart:
    """复制一份只保留tap/hold的谱面, 交给algo2规划"""
    lines = []
    for line in chart.judge_lines:
        new_line = copy.copy(line)
        new_line.notes_above = [n for n in line.notes_above if n.type in (NoteType.TAP, NoteType.HOLD)]
        new_line.notes_below = [n for n in line.notes_below if n.type in (NoteType.TAP, NoteType.HOLD)]
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


def solve_with(chart: Chart, console: Console, max_pointers: int, sweepers: list[Sweeper],
               intervals: list[list[int]]) -> defaultdict[int, list[VirtualTouchEvent]]:
    console.print(f'扫屏算法: {len(sweepers)}个触点扫屏(drag/flick), {max_pointers - len(sweepers)}个触点处理tap/hold')
    ans = algo2.solve(_without_sweep_notes(chart), console, max_pointers - len(sweepers))
    for ms, evs in plan_sweepers(chart, sweepers, intervals, console).items():
        ans[ms].extend(evs)
    return ans


def solve(chart: Chart, console: Console, max_pointers: int = MAX_POINTERS) -> defaultdict[int, list[VirtualTouchEvent]]:
    return solve_with(chart, console, max_pointers, row_sweepers(max_pointers), _active_intervals(chart))


__all__ = ['solve', 'solve_with', 'plan_sweepers', 'sweep_rows', 'row_sweepers', 'triangle', 'Sweeper',
           'whole_song_interval', 'SWEEP_POINTER_BASE']
