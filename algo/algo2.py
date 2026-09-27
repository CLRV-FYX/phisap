# 较为激进的规划算法

import math
from typing import Iterator, NamedTuple
from dataclasses import dataclass
from collections import defaultdict

from chart import Chart
from note import NoteType
from .algo_base import (TouchAction, VirtualTouchEvent, thin_path, recalc_pos, MAX_POINTERS, note_point,
                        flick_path, flick_time_shift, FLICK_START, FLICK_END, FLICK_RADIUS)


from rich.console import Console
from rich.progress import track


class PlainNote(NamedTuple):
    type: NoteType
    timestamp: int
    pos: tuple[float, float]
    angle: float
    # flick: 滑动轨迹(每毫秒一个点, 从pos所在时刻开始); hold: 按下之后每毫秒的位置
    path: tuple[tuple[float, float], ...] | None = None


class Frame:
    """一帧长度为10ms"""

    timestamp: int
    unallocated: defaultdict[NoteType, list[PlainNote]]

    def __init__(self, timestamp: int) -> None:
        self.timestamp = timestamp
        self.unallocated = defaultdict(list)

    def add(self, note_type: NoteType, pos: tuple[float, float], angle: float,
            path: tuple[tuple[float, float], ...] | None = None) -> None:
        pos = recalc_pos(pos, math.sin(angle), math.cos(angle))
        self.unallocated[note_type].append(PlainNote(note_type, self.timestamp, pos, angle, path))

    def holds(self) -> Iterator[PlainNote]:
        holds = self.unallocated[NoteType.HOLD]
        for hold in holds:
            yield hold
        holds.clear()

    def taps(self) -> Iterator[PlainNote]:
        taps = self.unallocated[NoteType.TAP]
        for tap in taps:
            yield tap
        taps.clear()

    def drags(self) -> Iterator[PlainNote]:
        drags = self.unallocated[NoteType.DRAG]
        for drag in drags:
            yield drag
        drags.clear()

    def flicks(self) -> Iterator[PlainNote]:
        flicks = self.unallocated[NoteType.FLICK]
        for flick in flicks:
            yield flick
        flicks.clear()


class Frames:
    frames: dict[int, Frame]

    def __init__(self) -> None:
        self.frames = {}

    def __getitem__(self, timestamp: int) -> Frame:
        if timestamp not in self.frames:
            self.frames[timestamp] = Frame(timestamp)
        return self.frames[timestamp]

    def __iter__(self) -> Iterator[Frame]:
        return iter(sorted(self.frames.values(), key=lambda f: f.timestamp))

    def __len__(self) -> int:
        return len(self.frames)


@dataclass
class Pointer:
    id: int  # 唯一id，用于发送指令
    note: PlainNote | None = None  # 和这个指针绑定的note
    age: int = 0  # 该指针在屏幕上存在的时间，负数表示该指针正在触发某个note


def distance_of(note1: PlainNote | None, note2: PlainNote | None) -> float:
    if note1 is None or note2 is None:
        return math.inf
    x1, y1 = note1.pos
    x2, y2 = note2.pos
    return (x2 - x1) ** 2 + (y2 - y1) ** 2


FLICK_DURATION = FLICK_END - FLICK_START


class PointerAllocator:
    pointers: list[Pointer]
    events: defaultdict[int, list[VirtualTouchEvent]]
    last_timestamp: int | None
    now: int

    def __init__(self, max_pointers_count: int = MAX_POINTERS, begin_at: int = 1000):
        self.pointers = [Pointer(i + begin_at) for i in range(max_pointers_count)]
        self.events = defaultdict(list)
        self.last_timestamp = None
        self.dropped: list[tuple[int, NoteType]] = []  # 触点不足而无法执行的音符

    def _find_available_pointers(self, note: PlainNote) -> Pointer | None:
        """查找当前屏幕上可以直接拿来用的指针
        查找条件：距离目标点100个单位之内的、已经被废弃的指针
        """
        ox, oy = note.pos
        ca, sa = math.cos(note.angle), math.sin(note.angle)
        for pointer in self.pointers:
            if pointer.note is None or pointer.age <= 0:  # 忽略闲置指针和正在FLICK的指针
                continue
            px, py = pointer.note.pos
            if abs((px - ox) * ca + (py - oy) * sa) < 100:
                return pointer
        return None

    def _alloc(self, note: PlainNote) -> Pointer | None:
        available_pointers = [p for p in self.pointers if p.note is None or p.age > 0]
        if not available_pointers:
            # 所有触点都在按住/滑动中(超过触点上限), 只能放弃这个音符
            self.dropped.append((self.now, note.type))
            return None
        return min(available_pointers, key=lambda p: distance_of(p.note, note))  # 优先使用废弃的Pointer

    def _insert(self, timestamp: int, event: VirtualTouchEvent) -> None:
        self.events[timestamp].append(event)

    def _tap(self, pointer: Pointer, note: PlainNote) -> None:
        if pointer.note is not None:
            # 如果分配的是"旧"指针，先抬起，再落下
            self._insert(self.now - pointer.age + 1, VirtualTouchEvent(pointer.note.pos, TouchAction.UP, pointer.id))
        pointer.note = note
        pointer.age = 0
        self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.DOWN, pointer.id))

    def _hold(self, pointer: Pointer, note: PlainNote) -> None:
        """按下后同一个指针逐毫秒跟随判定线移动, 直到hold结束; 期间该指针不会被其他音符占用"""
        self._tap(pointer, note)
        path = note.path or ()
        for i in thin_path(path, note.pos):  # 位置不变或变化很小的毫秒不发送MOVE
            self._insert(self.now + i + 1, VirtualTouchEvent(path[i], TouchAction.MOVE, pointer.id))
        if path:
            pointer.note = note._replace(pos=path[-1])
        pointer.age = -len(path)

    def _flick(self, pointer: Pointer, note: PlainNote) -> None:
        if note.path:
            path = note.path
        else:
            alpha = note.angle
            sa, ca = math.sin(alpha), math.cos(alpha)
            # 对于flick，需先判断是否在屏幕内判定，否则之后生成的一系列滑动事件将会被recalc_pos给映射到同一点，使得flick漏判
            x, y = recalc_pos(note.pos, sa, ca)
            path = tuple(
                (x - (1 - 2 * d / FLICK_DURATION) * FLICK_RADIUS * sa, y + (1 - 2 * d / FLICK_DURATION) * FLICK_RADIUS * ca)
                for d in range(FLICK_DURATION)
            )
        # flick总是重新按下: 复用旧触点时它会先"瞬移"到起点, 这一下会被当成一次滑动,
        # 按"一次滑动只能判定一个flick"的规则要多等一两帧才能再次判定
        if pointer.note is not None:
            self._insert(self.now - pointer.age + 1, VirtualTouchEvent(pointer.note.pos, TouchAction.UP, pointer.id))
        self._insert(self.now, VirtualTouchEvent(path[0], TouchAction.DOWN, pointer.id))
        for delta, pos in enumerate(path):
            self._insert(self.now + delta, VirtualTouchEvent(pos, TouchAction.MOVE, pointer.id))
        pointer.note = note._replace(pos=path[-1])
        pointer.age = -(len(path) - 1)

    def _drag(self, pointer: Pointer, note: PlainNote) -> None:
        if pointer.note is None:
            self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.DOWN, pointer.id))
        else:
            self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.MOVE, pointer.id))
        self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.MOVE, pointer.id))
        pointer.note = note
        pointer.age = 0

    def allocate(self, frame: Frame) -> None:
        # 更新pointer age
        self.now = frame.timestamp
        if self.last_timestamp is not None:
            for pointer in self.pointers:
                pointer.age += self.now - self.last_timestamp

        # 步骤1：分配tap与hold(hold按下后由同一个指针跟随到结束)
        for note in frame.taps():
            pointer = self._alloc(note)
            if pointer is not None:
                self._tap(pointer, note)
        for note in frame.holds():
            pointer = self._alloc(note)
            if pointer is not None:
                self._hold(pointer, note)

        # 步骤2: 分配flick
        for note in frame.flicks():
            pointer = self._alloc(note)
            if pointer is not None:
                self._flick(pointer, note)

        # 步骤3：分配drag
        for note in frame.drags():
            pointer = self._find_available_pointers(note)
            if pointer:
                pointer.age = 0
                continue
            pointer = self._alloc(note)
            if pointer is not None:
                self._drag(pointer, note)

        self.last_timestamp = frame.timestamp

    def withdraw(self) -> None:
        """收回在屏幕上的所有pointer"""
        if self.last_timestamp is None:
            return

        for pointer in self.pointers:
            if pointer.note:
                # 仍在按住/滑动中的指针(age < 0)要等其移动事件结束后再抬起
                final = self.last_timestamp + max(0, -pointer.age) + 1
                self._insert(final, VirtualTouchEvent(pointer.note.pos, TouchAction.UP, pointer.id))

    def done(self) -> defaultdict[int, list[VirtualTouchEvent]]:
        self.withdraw()
        return self.events


def solve(chart: Chart, console: Console, max_pointers: int = MAX_POINTERS) -> defaultdict[int, list[VirtualTouchEvent]]:
    frames = Frames()

    # 统计frames
    for line in track(chart.judge_lines, description='统计操作帧...', console=console):
        for note in line.notes_above + line.notes_below:
            ms = round(line.seconds(note.time) * 1000)
            off_x = note.x * 72
            x, y = line.pos(note.time)
            alpha = -line.angle(note.time) * math.pi / 180
            pos = x + off_x * math.cos(alpha), y + off_x * math.sin(alpha)
            match note.type:
                case NoteType.HOLD:
                    # 按下之后每毫秒的位置(随判定线移动/旋转), 由同一个指针执行
                    hold_ms = math.ceil(line.seconds(note.hold) * 1000)
                    path = tuple(note_point(line, note, ms + offset) for offset in range(1, hold_ms + 1))
                    frames[ms].add(NoteType.HOLD, pos, alpha, path)
                case NoteType.FLICK:
                    # 判定点在屏幕外时的时间微调(说明见algo1.py); 滑动轨迹逐毫秒跟随判定线
                    shift = flick_time_shift(line, note, console)
                    path = tuple(flick_path(line, note, ms, FLICK_START, FLICK_END, FLICK_RADIUS, shift))
                    frames[ms + FLICK_START].add(NoteType.FLICK, path[0], alpha, path)
                case _:
                    frames[ms].add(note.type, pos, alpha)

    console.print(f'统计完毕，当前谱面共计{len(frames)}帧')

    allocator = PointerAllocator(max_pointers)

    for frame in track(frames, description='规划触控事件...', console=console):
        allocator.allocate(frame)

    if allocator.dropped:
        console.print(f'[yellow]警告: 有{len(allocator.dropped)}个音符因同时需要超过{max_pointers}个触点而无法执行'
                      f'(首次出现在{allocator.dropped[0][0]}ms)[/yellow]')
    return allocator.done()


__all__ = ['solve']
