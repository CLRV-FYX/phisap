"""保守的指针规划算法"""

import math
from typing import NamedTuple
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from .algo_base import (TouchAction, VirtualTouchEvent, thin_path, distance_of, recalc_pos,
                        MAX_POINTERS, note_point, flick_path, flick_time_shift,
                        FLICK_START, FLICK_END, FLICK_RADIUS)
from chart import Chart
from note import NoteType

from rich.console import Console
from rich.progress import track

@dataclass
class Pointer:
    pid: int
    pos: tuple[float, float]
    timestamp: int
    occupied: int = 0


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
        self.unused = {}
        self.delta = delta
        self.unused_now = {}
        self.mark_as_released = []

    def _new(self) -> int:
        if not self.recycled:
            pid = self.max_pointer_id
            self.max_pointer_id += self.delta
            return pid
        return self.recycled.pop()

    def _del(self, pointer_id: int) -> None:
        self.recycled.add(pointer_id)
        if len(self.recycled) == (self.max_pointer_id - self.begin) / self.delta:
            self.max_pointer_id = self.begin
            self.recycled.clear()

    def acquire(self, event: FrameEvent, new: bool = True) -> tuple[int, bool]:
        event_id = event.id
        if event_id in self.pointers:
            ptr = self.pointers[event_id]
            ptr.timestamp = self.now
            ptr.pos = event.point
            return ptr.pid, False
        if not new:
            nearest_distance = 200
            nearest_pid = None
            for pid, ptr in self.unused.items():
                if (d := distance_of(event.point, ptr.pos)) < nearest_distance:
                    nearest_pid = ptr.pid
                    nearest_distance = d
            if nearest_pid is not None:
                ptr = self.unused[nearest_pid]
                del self.unused[nearest_pid]
                ptr.timestamp = self.now
                ptr.pos = event.point
                ptr.occupied = 0
                self.pointers[event_id] = ptr
                return ptr.pid, False
        self._make_room()
        pid = self._new()
        self.pointers[event_id] = Pointer(pid, event.point, self.now)
        return pid, True

    def _on_screen(self) -> int:
        return len(self.pointers) + len(self.unused) + len(self.unused_now) - len(self.mark_as_released)

    def _make_room(self) -> None:
        """即将按下新指针: 触点已满时, 先抬起最久未使用的闲置指针(而不是让新触点被丢弃)"""
        while self._on_screen() >= self.max_pointers and self.unused:
            pid = min(self.unused, key=lambda k: self.unused[k].timestamp)
            ptr = self.unused.pop(pid)
            # 不立即回收pid(避免同一毫秒内同一pid先按下后抬起), 在recycle中回收
            self.forced.append((ptr.pid, min(ptr.timestamp + 1, self.now), ptr.pos))
        if self._on_screen() >= self.max_pointers:
            self.overflow.append(self.now)

    def release(self, event: FrameEvent) -> None:
        event_id = event.id
        if event_id in self.pointers:
            ptr = self.pointers[event_id]
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
        if is_keyframe:
            for ptr in self.unused.values():
                ptr.occupied += 1
                if ptr.occupied > 0:
                    yield ptr.pid, ptr.timestamp + 1, ptr.pos
                    self._del(ptr.pid)
                    marked.append(ptr.pid)
        for pid in marked:
            del self.unused[pid]
        self.unused |= self.unused_now
        self.unused_now = {}

        # 仍超出上限时(全部是正在使用的指针)只能提前抬起闲置指针, 无法更多
        while len(self.unused) + len(self.pointers) > self.max_pointers and self.unused:
            pid = min(self.unused, key=lambda k: self.unused[k].timestamp)
            ptr = self.unused.pop(pid)
            yield ptr.pid, ptr.timestamp + 1, ptr.pos
            self._del(ptr.pid)

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
                    head = recalc_pos((px, py), sa, ca)
                    add_frame_event(ms, FrameEventAction.HOLD_START, head, current_event_id)
                    # 按住期间每毫秒跟随判定线(屏幕外映射时使用该时刻的角度, 而不是按下时的角度)
                    # (位置不变或变化很小的毫秒不发送MOVE, 见thin_path)
                    hold_path = [note_point(line, event, ms + offset) for offset in range(1, hold_ms)]
                    for i in thin_path(hold_path, head):
                        add_frame_event(ms + i + 1, FrameEventAction.HOLD, hold_path[i], current_event_id)
                    add_frame_event(ms + hold_ms, FrameEventAction.HOLD_END, note_point(line, event, ms + hold_ms),
                                    current_event_id)
            current_event_id += 1

    console.print(f'统计完毕，当前谱面共计{len(frames)}帧')

    pointers = PointerManager(1000, max_pointers=max_pointers)

    result: defaultdict[int, list[VirtualTouchEvent]] = defaultdict(list)

    def add_touch_event(milliseconds: int, pos: tuple[float, float], action: TouchAction, pointer_id: int):
        if action == TouchAction.UP:
            # 同一毫秒内先抬起再按下, 避免瞬间超出触点上限
            result[milliseconds].insert(0, VirtualTouchEvent(pos, action, pointer_id))
        else:
            result[milliseconds].append(VirtualTouchEvent(pos, action, pointer_id))

    for ms, frame in track(sorted(frames.items()), description='正在规划触控事件...', console=console):
        pointers.now = ms
        is_keyframe = False
        for event in frame:
            match event.action:
                case FrameEventAction.TAP:
                    add_touch_event(ms, event.point, TouchAction.DOWN, pointers.acquire(event)[0])
                    pointers.release(event)
                    is_keyframe = True
                case FrameEventAction.DRAG:
                    pid, new = pointers.acquire(event, new=False)
                    act = TouchAction.DOWN if new else TouchAction.MOVE
                    add_touch_event(ms, event.point, act, pid)
                    pointers.release(event)
                    # is_keyframe = True
                case FrameEventAction.FLICK_START:
                    pid, new = pointers.acquire(event, new=False)
                    act = TouchAction.DOWN if new else TouchAction.MOVE
                    add_touch_event(ms, event.point, act, pid)
                case FrameEventAction.FLICK | FrameEventAction.HOLD:
                    add_touch_event(ms, event.point, TouchAction.MOVE, pointers.acquire(event)[0])
                case FrameEventAction.FLICK_END | FrameEventAction.HOLD_END:
                    add_touch_event(ms, event.point, TouchAction.MOVE, pointers.acquire(event)[0])
                    pointers.release(event)
                case FrameEventAction.HOLD_START:
                    add_touch_event(ms, event.point, TouchAction.DOWN, pointers.acquire(event)[0])
                    is_keyframe = True

        for pid, ts, pos in pointers.recycle(is_keyframe):
            add_touch_event(ts, pos, TouchAction.UP, pid)

    for pid, ts, pos in pointers.finish():
        add_touch_event(ts, pos, TouchAction.UP, pid)
    if pointers.overflow:
        console.print(f'[yellow]警告: 有{len(pointers.overflow)}个时刻需要同时按下超过{max_pointers}个触点'
                      f'(首次出现在{pointers.overflow[0]}ms), 超出部分可能漏判[/yellow]')
    console.print('规划完毕.')
    return result
