# 较为激进的规划算法

import math
from typing import Iterator, NamedTuple
from dataclasses import dataclass
from collections import defaultdict

from chart import Chart
from note import NoteType
from .algo_base import (TouchAction, VirtualTouchEvent, thin_path, recalc_pos, _edge_safe, MAX_POINTERS, note_point,
                        hold_point, flick_path, flick_time_shift,
                        FLICK_START, FLICK_END, FLICK_RADIUS)

# 蓝键(TAP)和长条(HOLD)结束后, 触点必须在此时间(ms)内释放, 避免触点占满导致后续音符漏判
MAX_RELEASE_MS = 5
# 指针UP之后, 必须至少等待这么多ms才能在新位置重新DOWN同一pid(避免Android把瞬移误判为滑动)
PID_REUSE_COOLDOWN_MS = 40


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
    release_deadline: int = 0  # 变为闲置状态后必须在此时间(ms)前抬起


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
        self.max_pointers_count = max_pointers_count
        self.next_pid = begin_at + max_pointers_count
        self.pointers = [Pointer(i + begin_at) for i in range(max_pointers_count)]
        self.events = defaultdict(list)
        self.last_timestamp = None
        self.dropped: list[tuple[int, NoteType]] = []  # 触点不足而无法执行的音符
        self.released_at: dict[int, int] = {}  # pid -> UP的时刻(用于冷却检查)

    def _find_available_pointers(self, note: PlainNote) -> Pointer | None:
        """查找当前屏幕上可以直接拿来用的指针
        查找条件：距离目标点100个单位之内的、已经被废弃的指针;
        另外: 本帧内刚刚按下(TAP/HOLD, age==0)且位置非常近(<5像素)的触点也直接复用,
              避免同位置的DRAG在TAP之外再额外按下一个触点(挤占触点名额)
        """
        ox, oy = note.pos
        ca, sa = math.cos(note.angle), math.sin(note.angle)
        # 先找本帧内刚按下的触点(age==0即刚按下这一帧, 没有释放), 位置<5像素直接复用
        for pointer in self.pointers:
            if pointer.note is None or pointer.age != 0:
                continue
            px, py = pointer.note.pos
            if math.hypot(px - ox, py - oy) < 5:
                return pointer
        for pointer in self.pointers:
            if pointer.note is None or pointer.age <= 0:  # 忽略闲置指针和正在FLICK的指针
                continue
            px, py = pointer.note.pos
            if abs((px - ox) * ca + (py - oy) * sa) < 100:
                return pointer
        return None

    def _alloc(self, note: PlainNote) -> Pointer | None:
        # idle指针: note is None; aging指针: age > 0(FLICK刚结束但仍可复用)
        idle_pointers = [p for p in self.pointers if p.note is None]
        aging_pointers = [p for p in self.pointers if p.note is not None and p.age > 0]

        # 检查已过冷却期的闲置指针
        cool_idle = [p for p in idle_pointers
                     if self.now - self.released_at.get(p.id, -10**9) >= PID_REUSE_COOLDOWN_MS]

        if cool_idle:
            return min(cool_idle, key=lambda p: distance_of(p.note, note))
        if aging_pointers:
            # 老化指针仍在屏幕上(未UP), 无冷却限制, 可以MOVE使用; 但尽量选距离近的
            return min(aging_pointers, key=lambda p: distance_of(p.note, note))
        if idle_pointers and len(self.pointers) < self.max_pointers_count:
            # 分配新pid。上限是 max_pointers_count(调用方solve_with扣掉扫屏/滑键触点后
            # 剩下的名额), 不是 MAX_POINTERS: 后者是Android的硬上限, 而这里再涨就会
            # 突破当前后端(scrcpy-server 官方10/补丁16, MaaTouch 10)的同时触点上限,
            # 多出来的DOWN被服务端**静默丢弃**, 表现就是"某一波长条莫名断一个"。
            pid = self.next_pid
            self.next_pid += 1
            p = Pointer(pid)
            self.pointers.append(p)
            return p
        # 所有触点都在按住/滑动中(超过触点上限或都在冷却中), 只能放弃这个音符
        self.dropped.append((self.now, note.type))
        return None

    def _insert(self, timestamp: int, event: VirtualTouchEvent) -> None:
        # 所有发出的坐标经过_edge_safe,避免x>=1280/y>=720边界点映射到物理屏幕外被Android丢弃
        event = VirtualTouchEvent(_edge_safe(event.pos), event.action, event.pointer)
        self.events[timestamp].append(event)

    def _tap(self, pointer: Pointer, note: PlainNote) -> None:
        if pointer.note is not None:
            # 如果分配的是"旧"指针，先抬起，再落下
            up_t = self.now - pointer.age + 1
            self._insert(up_t, VirtualTouchEvent(pointer.note.pos, TouchAction.UP, pointer.id))
            self.released_at[pointer.id] = up_t
        pointer.note = note
        pointer.age = 0
        pointer.release_deadline = self.now + MAX_RELEASE_MS  # tap 按下后最迟 MAX_RELEASE_MS 释放
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
        # hold 结束(age 从负数变为0时)后最迟 MAX_RELEASE_MS 释放
        pointer.release_deadline = self.now + len(path) + MAX_RELEASE_MS

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
        if pointer.note is None:
            self._insert(self.now, VirtualTouchEvent(path[0], TouchAction.DOWN, pointer.id))
        else:
            self._insert(self.now, VirtualTouchEvent(path[0], TouchAction.MOVE, pointer.id))
        for delta, pos in enumerate(path):
            self._insert(self.now + delta, VirtualTouchEvent(pos, TouchAction.MOVE, pointer.id))
        pointer.note = note._replace(pos=path[-1])
        pointer.age = -(len(path) - 1)
        pointer.release_deadline = self.now + len(path) - 1 + 1  # flick 结束1ms后释放

    def _drag(self, pointer: Pointer, note: PlainNote) -> None:
        if pointer.note is None:
            self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.DOWN, pointer.id))
        else:
            self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.MOVE, pointer.id))
        self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.MOVE, pointer.id))
        pointer.note = note
        pointer.age = 0
        pointer.release_deadline = self.now + 1  # drag 不要求严格, 1ms后即可释放

    def allocate(self, frame: Frame) -> None:
        # 更新pointer age
        self.now = frame.timestamp
        if self.last_timestamp is not None:
            for pointer in self.pointers:
                pointer.age += self.now - self.last_timestamp

        # 步骤0: 释放已到达deadline的闲置指针(age>=0, 即不在flick/hold的滑动过程中)
        # 蓝键(TAP)/长条(HOLD)必须在 MAX_RELEASE_MS ms 内释放, 避免占满触点导致后续音符漏判
        for pointer in self.pointers:
            if pointer.note is not None and pointer.age >= 0 and self.now >= pointer.release_deadline:
                # UP事件的时刻就是 release_deadline(这一帧来得晚也一样按到期时刻发出),
                # released_at 记的也必须是这个真实抬起时刻, 不能记 self.now:
                # 谱面稀疏时(两个密集段之间几百毫秒没有任何tap/hold), self.now 会比真实
                # 抬起时刻晚一大截, PID_REUSE_COOLDOWN_MS 被凭空拉长, 刚刚能用的触点
                # 被当成"还在冷却"而无谓占住 —— Chart_AT 第二波长条的第一个hold
                # 因此拿不到刚释放的pid, 只能另开新pid, 把触点预算吃光。
                up_t = min(pointer.release_deadline, self.now)
                self._insert(up_t, VirtualTouchEvent(pointer.note.pos, TouchAction.UP, pointer.id))
                self.released_at[pointer.id] = up_t
                pointer.note = None
                pointer.age = 0

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
                # 复用已有触点(同位置刚按下的TAP/HOLD或邻近的闲置触点): 发一个MOVE到drag位置
                self._insert(self.now, VirtualTouchEvent(note.pos, TouchAction.MOVE, pointer.id))
                pointer.note = note
                pointer.age = 0
                pointer.release_deadline = self.now + 1
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
                    # 按下之后每毫秒的位置(随判定线移动/旋转), 由同一个指针执行。
                    # 用 hold_point 而不是 note_point: 音符越界时沿垂直方向夹到屏幕边缘,
                    # 手指位置连续, 不会在判定线瞬移出屏幕时被要求1ms内跳几百像素(见 algo_base.hold_point)
                    hold_ms = math.ceil(line.seconds(note.hold) * 1000)
                    pos = hold_point(line, note, ms)
                    path = tuple(hold_point(line, note, ms + offset) for offset in range(1, hold_ms + 1))
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
