from typing import Self
from bisect import bisect_left
from note import Note
import math


class _EventIndex:
    """按时间查找事件: 返回列表中第一个满足 start_time <= t <= end_time 的事件。

    事件按时间有序且互不重叠时(官谱与RPE转换谱均如此)用二分查找, 否则退回逐个查找,
    两种方式的结果完全相同(边界时刻属于前一个事件)。
    RPE缓动会被转换器采样成大量短事件, 逐个查找在规划时会非常慢。
    """

    __slots__ = ('events', 'starts', 'ends', 'ordered')

    def __init__(self, events: list) -> None:
        self.events = events
        self.starts = [e.start_time for e in events]
        self.ends = [e.end_time for e in events]
        self.ordered = all(s <= e for s, e in zip(self.starts, self.ends)) and all(
            self.ends[i] <= self.starts[i + 1] for i in range(len(events) - 1)
        )

    def find(self, t: float):
        if self.ordered:
            i = bisect_left(self.ends, t)
            if i < len(self.events) and self.starts[i] <= t:
                return self.events[i]
            return None
        for e in self.events:
            if e.start_time <= t <= e.end_time:
                return e
        return None


def _progress(e, t: float) -> float:
    """事件内的线性进度(0..1), 零长度事件取1(即直接取end)"""
    span = e.end_time - e.start_time
    return (t - e.start_time) / span if span else 1.0


class SpeedEvent:
    start_time: float
    end_time: float
    floor: float
    value: float

    def __init__(self, start_time: float, end_time: float, floor: float, value: float) -> None:
        self.start_time = start_time
        self.end_time = end_time
        self.floor = floor
        self.value = value

    @classmethod
    def from_dict(cls, d: dict) -> Self:
        return cls(d['startTime'], d['endTime'], d.get('floorPosition', 0.0), d['value'])

    def __repr__(self) -> str:
        return f'''SpeedEvent(start={self.start_time}, end={self.end_time}, floor={self.floor}, value={self.value})'''


class NormalEvent:
    start_time: float
    end_time: float
    start: float
    end: float
    start2: float
    end2: float

    def __init__(self, start_time: float, end_time: float, start: float, end: float, start2: float, end2: float) -> None:
        self.start_time = start_time
        self.end_time = end_time
        self.start = start
        self.end = end
        self.start2 = start2
        self.end2 = end2

    @classmethod
    def from_dict(cls, d: dict) -> Self:
        return cls(d['startTime'], d['endTime'], d['start'], d['end'], d.get('start2', 0.0), d.get('end2', 0.0))

    @classmethod
    def from_dict_v1(cls, d: dict) -> Self:
        start = d['start']
        end = d['end']
        return cls(
            d['startTime'],
            d['endTime'],
            (start // 1000) / 880,
            (end // 1000) / 880,
            (start % 1000) / 520,
            (end % 1000) / 520,
        )


class JudgeLine:
    notes_above: list[Note]
    notes_below: list[Note]
    bpm: float
    speed_events: list[SpeedEvent]
    disappear_events: list[NormalEvent]
    move_events: list[NormalEvent]
    rotate_events: list[NormalEvent]
    flip_y: bool

    def __init__(
        self,
        notes_above: list[Note],
        notes_below: list[Note],
        bpm: float,
        speed_events: list[SpeedEvent],
        disappear_events: list[NormalEvent],
        move_events: list[NormalEvent],
        rotate_events: list[NormalEvent],
        flip_y: bool = True,
    ) -> None:
        self.notes_above = notes_above
        self.notes_below = notes_below
        self.bpm = bpm
        self.speed_events = speed_events
        self.disappear_events = disappear_events
        self.move_events = move_events
        self.rotate_events = rotate_events
        # 官谱(formatVersion 1/3)的move事件y分量均以屏幕底部为0、向上为正, 需要翻转成
        # 屏幕坐标(0在顶部)。与Phira及原版phisap的解析一致。
        self.flip_y = flip_y
        self._speed_idx = _EventIndex(speed_events)
        self._alpha_idx = _EventIndex(disappear_events)
        self._move_idx = _EventIndex(move_events)
        self._rotate_idx = _EventIndex(rotate_events)

    @classmethod
    def from_dict(cls, d: dict):
        # speedEvents显式携带floorPosition的谱面(formatVersion 2, 仅phisap内部/旧工具使用)
        return cls(
            [*map(Note.load, d['notesAbove'])],
            [*map(Note.load, d['notesBelow'])],
            d['bpm'],
            [*map(SpeedEvent.from_dict, d['speedEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineDisappearEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineMoveEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineRotateEvents'])],
            flip_y=True,
        )

    @classmethod
    def from_dict_v3(cls, d: dict) -> Self:
        # formatVersion 3(v2.5.0及之后的官谱, 含3.20.0+):
        # speedEvents移除了floorPosition, 需从0开始按 1.875*value/bpm 逐段累积推导(推导方式同v1)。
        # move事件的y分量与之前完全相同: 0在屏幕底部、向上为正(见Phira pgr.rs与谱面格式文档),
        # 因此同样需要翻转。(曾误认为3.20.0改成了0在顶部, 导致判定线上下镜像、旋转线上的音符全部错位)
        speed_events = d['speedEvents']
        current_floor = 0.0
        for ev in speed_events:
            if 'floorPosition' not in ev:
                ev['floorPosition'] = current_floor
                current_floor += 1.875 * (ev['endTime'] - ev['startTime']) * ev['value'] / d['bpm']
        return cls(
            [*map(Note.load, d['notesAbove'])],
            [*map(Note.load, d['notesBelow'])],
            d['bpm'],
            [*map(SpeedEvent.from_dict, speed_events)],
            [*map(NormalEvent.from_dict, d['judgeLineDisappearEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineMoveEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineRotateEvents'])],
            flip_y=True,
        )

    @classmethod
    def from_dict_v1(cls, d: dict) -> Self:
        speed_events = d['speedEvents']
        current_floor = 0.0
        for ev in speed_events:
            if 'floorPosition' not in ev:
                ev['floorPosition'] = current_floor
                current_floor += 1.875 * (ev['endTime'] - ev['startTime']) * ev['value'] / d['bpm']

        return cls(
            [*map(Note.load, d['notesAbove'])],
            [*map(Note.load, d['notesBelow'])],
            d['bpm'],
            [*map(SpeedEvent.from_dict, d['speedEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineDisappearEvents'])],
            [*map(NormalEvent.from_dict_v1, d['judgeLineMoveEvents'])],
            [*map(NormalEvent.from_dict, d['judgeLineRotateEvents'])],
        )

    def floor(self, t: float) -> float:
        e = self._speed_idx.find(t)
        if e is not None:
            return self.seconds((t - e.start_time) * e.value) + e.floor
        # t超出最后一段speed事件的范围时(个别谱面末尾没有延伸到1e9)，
        # 按最后一段事件的速率继续外推(与Phira的解析行为一致)
        last = self.speed_events[-1]
        if t >= last.start_time:
            return self.seconds((t - last.start_time) * last.value) + last.floor
        raise RuntimeError(f'floorPosition not found: time = {t}')

    def seconds(self, t: float) -> float:
        return t * 1.875 / self.bpm

    def time(self, second: float) -> float:
        return second * self.bpm / 1.875

    def opacity(self, t: float) -> float:
        e = self._alpha_idx.find(t)
        if e is None:
            return 1.0
        return e.start + (e.end - e.start) * _progress(e, t)

    def pos(self, t: float) -> tuple[float, float]:
        e = self._move_idx.find(t)
        if e is None:
            return 0, 0
        f = _progress(e, t)
        x = (e.start + (e.end - e.start) * f) * 1280
        y_frac = e.start2 + (e.end2 - e.start2) * f
        return x, ((720 - y_frac * 720) if self.flip_y else y_frac * 720)

    def angle(self, t: float) -> float:
        e = self._rotate_idx.find(t)
        if e is None:
            return 0.0
        return e.start + (e.end - e.start) * _progress(e, t)

    @property
    def notes(self) -> list[Note]:
        return self.notes_above + self.notes_below

    def pos_of(self, note: Note, time: int | float | None = None) -> tuple[float, float]:
        t = time if time is not None else note.time
        off_x = note.x * 72
        x, y = self.pos(t)
        a = -self.angle(t) * math.pi / 180
        return x + off_x * math.cos(a), y + off_x * math.sin(a)
