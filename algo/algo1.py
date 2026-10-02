"""保守的指针规划算法"""

import math
from typing import NamedTuple
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from .algo_base import (TouchAction, VirtualTouchEvent, thin_path, distance_of, recalc_pos, _edge_safe,
                        MAX_POINTERS, note_point, hold_point, flick_path, flick_time_shift,
                        FLICK_START, FLICK_END, FLICK_RADIUS)
from .relay import HoldTrack, plan_hold_relays
from chart import Chart
from note import NoteType

from rich.console import Console
from rich.progress import track

# 蓝键(TAP)和长条(HOLD)结束后, 触点必须在此时间(ms)内释放, 避免触点占满导致后续音符漏判
MAX_RELEASE_MS = 5
# 指针UP之后, 必须至少等待这么多ms才能在新位置重新DOWN同一pid.
# 否则Android会把同一pointerId的远距离瞬移当作滑动/异常手势, 导致新位置的tap被吞掉
PID_REUSE_COOLDOWN_MS = 40


@dataclass
class Pointer:
    pid: int
    pos: tuple[float, float]
    timestamp: int
    occupied: int = 0
    release_deadline: int = 0  # 进入闲置状态后必须在此时间(ms)前释放


class FrameEventAction(Enum):
    TAP = 0
    DRAG = 1
    FLICK_START = 2
    FLICK = 3
    FLICK_END = 4
    HOLD_START = 5
    HOLD = 6
    HOLD_END = 7


class FrameEvent(NamedTuple):
    action: FrameEventAction
    point: tuple[float, float]
    id: int


class PointerManager:
    max_pointer_id: int
    pointers: dict[int, Pointer]
    begin: int
    delta: int
    now: int

    recycled: set[int]
    unused: dict[int, Pointer]
    unused_now: dict[int, Pointer]
    mark_as_released: list[int]

    def __init__(self, begin: int, delta: int = 1, max_pointers: int = MAX_POINTERS) -> None:
        self.max_pointers = max_pointers
        self.forced = []          # 为腾出触点而提前抬起的指针: (pid, 抬起时刻, 位置)
        self.overflow = []        # 超出上限的时刻(无法腾出触点)
        self.begin = begin
        self.max_pointer_id = begin
        self.pointers = {}
        self.recycled = set()
        self.recycled_at: dict[int, int] = {}  # pid -> UP时间(ms), 用于冷却检查
        self.unused = {}
        self.delta = delta
        self.unused_now = {}
        self.mark_as_released = []

    def _new(self) -> int:
        # 优先从recycled中取出已过冷却期的pid
        now = self.now
        ready = [pid for pid in self.recycled if now - self.recycled_at.get(pid, 0) >= PID_REUSE_COOLDOWN_MS]
        if ready:
            pid = min(ready)
            self.recycled.remove(pid)
            self.recycled_at.pop(pid, None)
            return pid
        pid = self.max_pointer_id
        self.max_pointer_id += self.delta
        return pid

    def _del(self, pointer_id: int) -> None:
        self.recycled.add(pointer_id)
        self.recycled_at[pointer_id] = self.now
        if len(self.recycled) == (self.max_pointer_id - self.begin) / self.delta:
            self.max_pointer_id = self.begin
            self.recycled.clear()
            self.recycled_at.clear()

    # acquire()返回值: (pid, is_new)
    # is_new=True → 物理上需要新按下(DOWN); is_new=False → 指针已在屏幕上(MOVE即可)
    # 注意: 从recycled池取pid时is_new=True(已被UP过, 需要重新DOWN)
    #       从unused池复用pid时is_new=False(仍按着, MOVE即可)
    def acquire(self, event: FrameEvent, new: bool = True) -> tuple[int, bool, tuple[float,float] | None]:
        """返回 (pid, is_new, old_pos):
        is_new=True: 需要发DOWN; False: 指针已在屏幕上.
        old_pos: 仅当从unused复用且is_new=False时返回复用前的旧位置(需要先UP再DOWN时使用)
        """
        event_id = event.id
        if event_id in self.pointers:
            ptr = self.pointers[event_id]
            ptr.timestamp = self.now
            ptr.pos = event.point
            return ptr.pid, False, None
        if not new:
            # 1) 优先recycled(已物理抬起、且过了冷却期的pid, 需重新DOWN但不会引入误判)
            ready = [pid for pid in self.recycled
                     if self.now - self.recycled_at.get(pid, 0) >= PID_REUSE_COOLDOWN_MS]
            if ready:
                pid = min(ready)
                self.recycled.remove(pid)
                self.recycled_at.pop(pid, None)
                self.pointers[event_id] = Pointer(pid, event.point, self.now)
                return pid, True, None
            # 2) 再找unused(仍按着)
            nearest_distance = 200
            nearest_pid = None
            for pid, ptr in self.unused.items():
                if (d := distance_of(event.point, ptr.pos)) < nearest_distance:
                    nearest_pid = ptr.pid
                    nearest_distance = d
            if nearest_pid is not None:
                ptr = self.unused[nearest_pid]
                del self.unused[nearest_pid]
                old_pos = ptr.pos
                ptr.timestamp = self.now
                ptr.pos = event.point
                ptr.occupied = 0
                ptr.release_deadline = 0
                self.pointers[event_id] = ptr
                return ptr.pid, False, old_pos
        self._make_room()
        pid = self._new()
        self.pointers[event_id] = Pointer(pid, event.point, self.now)
        return pid, True, None

    def _on_screen(self) -> int:
        return len(self.pointers) + len(self.unused) + len(self.unused_now) - len(self.mark_as_released)

    def _make_room(self) -> None:
        """即将按下新指针: 触点已满时, 先抬起最久未使用的闲置指针(而不是让新触点被丢弃)"""
        while self._on_screen() >= self.max_pointers and self.unused:
            pid = min(self.unused, key=lambda k: self.unused[k].timestamp)
            ptr = self.unused.pop(pid)
            # 不立即回收pid(避免同一毫秒内同一pid先按下后抬起), 在recycle中回收
            ts = min(ptr.release_deadline, max(ptr.timestamp + 1, self.now))
            self.forced.append((ptr.pid, ts, ptr.pos))
        if self._on_screen() >= self.max_pointers:
            self.overflow.append(self.now)

    def release(self, event: FrameEvent) -> None:
        event_id = event.id
        if event_id in self.pointers:
            ptr = self.pointers[event_id]
            # 蓝键(TAP)/长条(HOLD)结束后必须在 MAX_RELEASE_MS ms 内释放, 避免占满触点
            # 其他音符(drag/flick)复用闲置触点时不会产生太多触点堆积, 释放时限可以放宽
            if event.action in (FrameEventAction.TAP, FrameEventAction.HOLD_END):
                ptr.release_deadline = self.now + MAX_RELEASE_MS
            else:
                ptr.release_deadline = self.now + 1
            self.unused_now[ptr.pid] = ptr
            self.mark_as_released.append(event_id)

    def recycle(self, is_keyframe: bool):
        for pid, ts, pos in self.forced:
            yield pid, ts, pos
            self._del(pid)
        self.forced = []
        marked = []
        for event_id in self.mark_as_released:
            del self.pointers[event_id]
        self.mark_as_released = []
        # 每帧检查所有闲置指针: 到达释放时限的必须释放(蓝键/长条最多5ms)
        for pid, ptr in list(self.unused.items()):
            if self.now >= ptr.release_deadline:
                yield pid, min(ptr.release_deadline, self.now), ptr.pos
                self._del(pid)
                marked.append(pid)
        for pid in marked:
            del self.unused[pid]
        marked = []
        self.unused |= self.unused_now
        self.unused_now = {}

        # 仍超出上限时(全部是正在使用的指针)只能提前抬起闲置指针, 无法更多
        while len(self.unused) + len(self.pointers) > self.max_pointers and self.unused:
            pid = min(self.unused, key=lambda k: self.unused[k].timestamp)
            ptr = self.unused.pop(pid)
            yield pid, min(ptr.release_deadline, max(ptr.timestamp + 1, self.now)), ptr.pos
            self._del(pid)

    def finish(self):
        for ptr in self.unused.values():
            yield ptr.pid, ptr.timestamp + 1, ptr.pos
        for ptr in self.unused_now.values():
            yield ptr.pid, ptr.timestamp + 1, ptr.pos
        for ptr in self.pointers.values():
            yield ptr.pid, ptr.timestamp + 1, ptr.pos


def solve(chart: Chart, console: Console, max_pointers: int = MAX_POINTERS) -> dict[int, list[VirtualTouchEvent]]:

    frames: defaultdict[int, list[FrameEvent]] = defaultdict(list)

    def add_frame_event(milliseconds: int, action: FrameEventAction, point: tuple[float, float], id: int):
        frames[milliseconds].append(FrameEvent(action, point, id))

    current_event_id = 0
    holds_meta: dict[int, tuple] = {}
    hold_pids: dict[int, int] = {}
    heads: list[tuple[int, object, object]] = []

    console.print('开始规划')

    # 统计frames
    for line in track(chart.judge_lines, description='正在统计帧...', console=console):
        for event in line.notes_above + line.notes_below:
            ms = round(line.seconds(event.time) * 1000)
            off_x = event.x * 72
            x, y = line.pos(event.time)
            alpha = -line.angle(event.time) * math.pi / 180
            sa = math.sin(alpha)
            ca = math.cos(alpha)
            px, py = x + off_x * ca, y + off_x * sa

            match event.type:
                case NoteType.TAP:
                    heads.append((ms, line, event))
                    add_frame_event(ms, FrameEventAction.TAP, recalc_pos((px, py), sa, ca), current_event_id)
                case NoteType.DRAG:
                    add_frame_event(ms, FrameEventAction.DRAG, recalc_pos((px, py), sa, ca), current_event_id)
                case NoteType.FLICK:
                    # flick在判定时刻位于屏幕外时的时间微调(给DESTRUCTION 3,2,1打的补丁):
                    # 这首歌IN难度的最后一个flick在屏幕外判定(26752时刻判定线位于(w/2, -h/2)),
                    # 但它在(w/2, h/2)闪了几下, 按Phigros的判定机制可以在那里判定。
                    # 因此在±3个时间单位内寻找判定点位于屏幕内的时刻; 找不到时用recalc_pos映射。
                    shift = flick_time_shift(line, event, console)
                    # 滑动轨迹逐毫秒跟随判定线的移动/旋转(旧版本整个滑动过程固定在判定时刻的位置)
                    path = flick_path(line, event, ms, FLICK_START, FLICK_END, FLICK_RADIUS, shift)
                    add_frame_event(ms + FLICK_START, FrameEventAction.FLICK_START, path[0], current_event_id)
                    for offset in range(FLICK_START + 1, FLICK_END):
                        add_frame_event(ms + offset, FrameEventAction.FLICK, path[offset - FLICK_START], current_event_id)
                    add_frame_event(ms + FLICK_END, FrameEventAction.FLICK_END, path[-1], current_event_id)
                case NoteType.HOLD:
                    hold_ms = math.ceil(line.seconds(event.hold) * 1000)
                    # hold_point: 音符越界时沿垂直方向夹到屏幕边缘, 手指位置连续,
                    # 不会在判定线瞬移出屏幕时被要求1ms内跳几百像素(见 algo_base.hold_point)
                    head = hold_point(line, event, ms)
                    heads.append((ms, line, event))
                    holds_meta[current_event_id] = (
                        line, event, ms, hold_ms,
                        tuple(hold_point(line, event, ms + offset) for offset in range(1, max(hold_ms, 0) + 1)),
                        head)
                    add_frame_event(ms, FrameEventAction.HOLD_START, head, current_event_id)
                    # 按住期间每毫秒跟随判定线(屏幕外映射时使用该时刻的角度, 而不是按下时的角度)
                    # (位置不变或变化很小的毫秒不发送MOVE, 见thin_path)
                    hold_path = [hold_point(line, event, ms + offset) for offset in range(1, hold_ms)]
                    for i in thin_path(hold_path, head):
                        add_frame_event(ms + i + 1, FrameEventAction.HOLD, hold_path[i], current_event_id)
                    add_frame_event(ms + hold_ms, FrameEventAction.HOLD_END, hold_point(line, event, ms + hold_ms),
                                    current_event_id)
            current_event_id += 1

    console.print(f'统计完毕，当前谱面共计{len(frames)}帧')

    pointers = PointerManager(1000, max_pointers=max_pointers)

    result: defaultdict[int, list[VirtualTouchEvent]] = defaultdict(list)

    def add_touch_event(milliseconds: int, pos: tuple[float, float], action: TouchAction, pointer_id: int):
        pos = _edge_safe(pos)  # 避免y>=720/x>=1280边界点被Android丢弃
        if action == TouchAction.UP:
            # 同一毫秒内先抬起再按下, 避免瞬间超出触点上限
            result[milliseconds].insert(0, VirtualTouchEvent(pos, action, pointer_id))
        else:
            result[milliseconds].append(VirtualTouchEvent(pos, action, pointer_id))

    for ms, frame in track(sorted(frames.items()), description='正在规划触控事件...', console=console):
        pointers.now = ms
        is_keyframe = False
        # 本毫秒内已经按下(由TAP/HOLD_START产生)的触点位置->pid, DRAG遇到同位置时直接复用发MOVE
        down_positions: dict[tuple[float, float], int] = {}
        for event in frame:
            match event.action:
                case FrameEventAction.TAP:
                    # new=False: 优先复用recycled已抬起的pid(避免高速纵连时pid递增耗尽触点)
                    pid, is_new, old_pos = pointers.acquire(event, new=False)
                    if old_pos is not None:
                        # 复用了unused里仍按着的指针: 必须先在旧位置UP, 再DOWN到新位置才能产生新点击
                        add_touch_event(ms, old_pos, TouchAction.UP, pid)
                        is_new = True
                    if is_new:
                        add_touch_event(ms, event.point, TouchAction.DOWN, pid)
                        down_positions[event.point] = pid
                    pointers.release(event)
                    is_keyframe = True
                case FrameEventAction.DRAG:
                    # 找同毫秒内已按下的触点, 位置<5像素时复用(发MOVE), 不重复按下
                    shared_pid = None
                    for (px, py), spid in down_positions.items():
                        if abs(px - event.point[0]) < 5 and abs(py - event.point[1]) < 5:
                            shared_pid = spid
                            break
                    if shared_pid is not None:
                        add_touch_event(ms, event.point, TouchAction.MOVE, shared_pid)
                        continue
                    pid, is_new, old_pos = pointers.acquire(event, new=False)
                    if old_pos is not None:
                        # 复用unused里近距指针只发MOVE即可(DRAG不需要DOWN触发)
                        pass
                    act = TouchAction.DOWN if is_new else TouchAction.MOVE
                    add_touch_event(ms, event.point, act, pid)
                    if is_new:
                        down_positions[event.point] = pid
                    pointers.release(event)
                    # is_keyframe = True
                case FrameEventAction.FLICK_START:
                    pid, is_new, old_pos = pointers.acquire(event, new=False)
                    act = TouchAction.DOWN if is_new else TouchAction.MOVE
                    add_touch_event(ms, event.point, act, pid)
                case FrameEventAction.FLICK | FrameEventAction.HOLD:
                    add_touch_event(ms, event.point, TouchAction.MOVE, pointers.acquire(event)[0])
                case FrameEventAction.FLICK_END | FrameEventAction.HOLD_END:
                    add_touch_event(ms, event.point, TouchAction.MOVE, pointers.acquire(event)[0])
                    pointers.release(event)
                case FrameEventAction.HOLD_START:
                    pid, _, _ = pointers.acquire(event)
                    add_touch_event(ms, event.point, TouchAction.DOWN, pid)
                    hold_pids[event.id] = pid
                    down_positions[event.point] = pid
                    is_keyframe = True

        for pid, ts, pos in pointers.recycle(is_keyframe):
            add_touch_event(ts, pos, TouchAction.UP, pid)

    for pid, ts, pos in pointers.finish():
        add_touch_event(ts, pos, TouchAction.UP, pid)
    if pointers.overflow:
        console.print(f'[yellow]警告: 有{len(pointers.overflow)}个时刻需要同时按下超过{max_pointers}个触点'
                      f'(首次出现在{pointers.overflow[0]}ms), 超出部分可能漏判[/yellow]')
    tracks = [HoldTrack(hold_pids[eid], *meta) for eid, meta in holds_meta.items() if eid in hold_pids]
    if tracks:
        # 只从预算内的 id 里找空闲手指, 接力不会把同时按下的触点数顶过上限
        plan_hold_relays(tracks, result, list(range(1000, 1000 + max_pointers)), heads,
                         PID_REUSE_COOLDOWN_MS, console)
    console.print('规划完毕.')
    return result
