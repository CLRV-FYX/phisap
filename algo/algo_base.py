# 指针规划算法的基类和一些实用类型、函数
from typing import Self, IO
from enum import Enum
from typing import NamedTuple
import math
import json


# Android 系统允许同时存在的最大触点数(MotionEvent 硬上限)。
# phisap 会推送补丁版 scrcpy-server 以解除官方的10触点限制, 补丁失败时退回10。
MAX_POINTERS = 16

# 规划缓存文件后缀。规划逻辑或谱面解析有影响结果的修改时递增版本号,
# 旧版本生成的缓存会被自动忽略(需要重新规划)。
# v2: 修正官谱/RPE转换谱的y方向、RPE缓动/多层/父线、长按与滑键跟随判定线、16触点
# v3: 精简长按的MOVE事件; 新增algo3(扫屏)
# v4: flick滑动加快到3像素/毫秒; algo3/algo3f的flick改为单独规划
PLAN_CACHE_SUFFIX = '.ans.v6.json'


def distance_of(p1: tuple[float, float], p2: tuple[float, float]):
    p1x, p1y = p1
    p2x, p2y = p2
    return math.sqrt((p2x - p1x) ** 2 + (p2y - p1y) ** 2)


def div(x: float, y: float) -> float:
    """自动处理除零异常"""
    try:
        return x / y
    except ZeroDivisionError:
        return math.nan


def in_screen(pos: tuple[float, float]) -> bool:
    x, y = pos
    return (0 <= x <= 1280) and (0 <= y <= 720)


def recalc_pos(position: tuple[float, float], sa: float, ca: float) -> tuple[float, float]:
    """重新计算坐标
    一些情况下，note会在屏幕的外侧判定。点名批评Nhelv。
    也就是说，此时横坐标会在[0, 1280]的范围外，或者纵坐标会在[0, 720]的范围外。
    这是我们需要重新规划击打的位置，让该位置落在屏幕内。
    我们利用屁股肉的垂直判定区域特性来解决这个问题。
    也就是说，在高垂直于判定线且长度不限，同时宽平行且与note等长的矩形范围内点击任意位置均视为判定成功。
    为了简化这个问题，我们将矩形视作一条线，这条线过矩形的终点且与矩形的两高平行。
    这条线必与屏幕对应的矩形相交，且绝大部分情况下有两个交点。
    我们取这两个交点的中心点作为我们操作note的位置。
    :param position: 坐标
    :param sa: sin(angle) 判定线偏移角度的正弦值
    :param ca: cos(angle) 判定线偏移角度的余弦值
    :return: 重新计算后的坐标
    """
    if in_screen(position):
        return position

    # 重新计算note
    px, py = position
    sumx = sumy = 0
    x1 = px + py * div(sa, ca)
    y1 = py + px * div(ca, sa)
    x2 = px - (720 - py) * div(sa, ca)
    y2 = py - (1280 - px) * div(ca, sa)
    if 0 < x1 < 1280:
        sumx += x1
    if 0 < y1 < 720:
        sumy += y1
    if 0 < x2 < 1280:
        sumx += x2
        sumy += 720
    if 0 < y2 < 720:
        sumy += y2
        sumx += 1280
    return sumx / 2, sumy / 2


def clamp_to_screen(pos: tuple[float, float], margin: float = 1.0) -> tuple[float, float]:
    x, y = pos
    return min(max(x, margin), 1280 - margin), min(max(y, margin), 720 - margin)


def note_state(line, note, ms: float, time_shift: float = 0.0) -> tuple[tuple[float, float], float, float]:
    """音符在ms毫秒时(随判定线移动/旋转后)的位置, 以及该时刻判定线角度的sin/cos。
    time_shift: 额外的时间偏移(谱面时间单位)"""
    t = line.time(ms / 1000) + time_shift
    alpha = -line.angle(t) * math.pi / 180
    return line.pos_of(note, t), math.sin(alpha), math.cos(alpha)


def note_point(line, note, ms: float, time_shift: float = 0.0) -> tuple[float, float]:
    """音符在ms毫秒时的可点击位置(屏幕外时用该时刻的角度映射回屏幕内)"""
    pos, sa, ca = note_state(line, note, ms, time_shift)
    return recalc_pos(pos, sa, ca)


def flick_time_shift(line, note, console=None) -> float:
    """flick在判定时刻位于屏幕外时, 在±3个时间单位内寻找判定点位于屏幕内的时刻(见algo1中的说明)"""
    pos, _, _ = note_state(line, note, line.seconds(note.time) * 1000)
    if in_screen(pos):
        return 0.0
    for dt in range(-3, 4):
        new_pos = line.pos_of(note, note.time + dt)
        if in_screen(new_pos):
            if console is not None:
                console.print(f'[red]微调判定时间：flick(pos={pos}, time={note.time}) => flick(pos={new_pos}, time={note.time + dt})[/red]')
            return float(dt)
    return 0.0


# flick的滑动: 判定时刻前后各50ms, 沿垂直于判定线的方向滑过 2*FLICK_RADIUS 像素(1280x720)。
# 速度 2像素/毫秒(1080p屏幕上3像素/毫秒)。之前是60ms滑60像素(1像素/毫秒), 刚好卡在"甩动"判定的
# 速度门槛附近, 按"一次滑动只能判定一个flick"的规则(sim-phi)模拟时大量漏判。
FLICK_START = -50
FLICK_END = 50
FLICK_RADIUS = 100


def _perpendicular_room(bx: float, by: float, nx: float, ny: float) -> tuple[float, float]:
    """点(bx, by)沿方向(nx, ny)移动s时仍在屏幕内的s的范围"""
    lo, hi = -math.inf, math.inf
    for p, d, size in ((bx, nx, 1280.0), (by, ny, 720.0)):
        if abs(d) < 1e-9:
            if not 0 <= p <= size:
                return 0.0, 0.0
            continue
        a, b = (0 - p) / d, (size - p) / d
        lo, hi = max(lo, min(a, b)), min(hi, max(a, b))
    return (lo, hi) if lo <= hi else (0.0, 0.0)


def flick_path(line, note, center_ms: int, start: int, end: int, radius: float,
               time_shift: float = 0.0, reverse: bool = False) -> list[tuple[float, float]]:
    """flick的滑动轨迹: 返回 center_ms+start ... center_ms+end 每毫秒的位置。

    每一毫秒都按该时刻判定线的实际位置/角度计算(判定线在移动或旋转时手指跟着走),
    并沿垂直于判定线的方向滑过 2*radius 的距离(不改变在判定线方向上的投影, 所以不影响判定)。
    靠近屏幕边缘时, 整段轨迹沿垂直方向平移到屏幕内(而不是被截断在边缘上导致滑不动)。
    reverse: 反方向滑动
    """
    path = []
    duration = end - start
    for offset in range(start, end + 1):
        (x, y), sa, ca = note_state(line, note, center_ms + offset, time_shift)
        bx, by = recalc_pos((x, y), sa, ca)
        nx, ny = -sa, ca   # 垂直于判定线的方向
        lo, hi = _perpendicular_room(bx, by, nx, ny)
        lo, hi = lo + 1, hi - 1
        r = min(radius, max(0.0, (hi - lo) / 2))
        c = min(max(0.0, lo + r), hi - r) if hi - lo >= 2 * r else (lo + hi) / 2
        s = c + r * (1 - 2 * (offset - start) / duration) * (-1 if reverse else 1)
        path.append(clamp_to_screen((bx + nx * s, by + ny * s)))
    return path


class TouchAction(Enum):
    DOWN = 0
    UP = 1
    MOVE = 2
    CANCEL = 3
    OUTSIDE = 4
    POINTER_DOWN = 5
    POINTER_UP = 6
    HOVER_MOVE = 7


class TouchEvent(NamedTuple):
    pos: tuple[int, int]
    action: TouchAction
    pointer: int


class VirtualTouchEvent(NamedTuple):
    pos: tuple[float, float]
    action: TouchAction
    pointer: int

    def __str__(self) -> str:
        x, y = self.pos
        return f'''TouchEvent<{self.pointer} {self.action.name} @ ({x:4.2f}, {y:4.2f})>'''

    def to_serializable(self) -> dict:
        return {'pos': self.pos, 'action': self.action.value, 'pointer': self.pointer}

    @classmethod
    def from_serializable(cls, obj: dict) -> Self:
        return VirtualTouchEvent(obj['pos'], TouchAction(obj['action']), obj['pointer'])

    def map_to(self, x_offset: int, y_offset: int, x_scale: float, y_scale: float) -> TouchEvent:
        x_orig, y_orig = self.pos
        return TouchEvent(
            pos=(x_offset + round(x_orig * x_scale), y_offset + round(y_orig * y_scale)),
            action=self.action,
            pointer=self.pointer,
        )


def thin_path(points, start: tuple[float, float] | None = None, min_step: float = 1.0, max_lag: float = 6.0,
              min_interval: int = 4) -> list[int]:
    """精简长按的逐毫秒轨迹, 返回需要发送MOVE的下标(轨迹中第i个点对应按下后第i+1毫秒)。

    长按跟随判定线时原本每毫秒发送一个MOVE, 判定线静止时全是重复的位置, 会给scrcpy/模拟器
    的注入造成不必要的压力。这里: 与上次发送的位置相差不到 min_step 像素的点不发送(静止的长按
    完全不发送MOVE); 相差不到 max_lag 像素且距上次发送不足 min_interval 毫秒的点也不发送;
    最后一个点总是发送。位置误差最多 max_lag 像素(1280x720), 远小于判定宽度。
    start: 按下时的位置(默认为轨迹的第一个点)
    """
    keep: list[int] = []
    if not points:
        return keep
    last_i, (lx, ly) = -1, (start if start is not None else points[0])
    for i, (x, y) in enumerate(points):
        d = math.hypot(x - lx, y - ly)
        if d < min_step or (d < max_lag and i - last_i < min_interval):
            continue
        keep.append(i)
        last_i, lx, ly = i, x, y
    if keep[-1:] != [len(points) - 1]:
        keep.append(len(points) - 1)
    return keep


# 扫屏触点(algo3/algo3f)使用的指针id下限, 普通触点的id都小于它
SWEEP_POINTER_BASE_MIN = 2000


def first_note_ms(chart) -> int | None:
    """谱面第一个音符的判定时间(ms)"""
    times = [round(line.seconds(n.time) * 1000) for line in chart.judge_lines for n in line.notes]
    return min(times) if times else None


def manual_start_plan(plan: list, first_note: int | None, sweep_pointer_base: int = SWEEP_POINTER_BASE_MIN) -> list:
    """手动开始模式下的事件序列: 以"第一个音符"和"第一个非扫屏触点的事件"中较早者作为按下按钮的时刻。

    algo1/algo2没有扫屏触点, 对齐点就是第一个事件(与之前的行为相同);
    algo3/algo3f的扫屏触点在第一个音符之前就会按下, 这些事件在按下按钮时立即发送,
    其中的MOVE只保留每个触点最后的位置(避免一次性发送几百个过时的MOVE)。
    plan: [(时间ms, [事件, ...]), ...], 按时间排序; 事件需有 action / pointer 属性
    """
    if not plan:
        return plan
    first_normal = next((ts for ts, evs in plan if any(e.pointer < sweep_pointer_base for e in evs)), None)
    candidates = [t for t in (first_normal, first_note) if t is not None]
    align = min(candidates) if candidates else plan[0][0]
    head, rest = [], []
    last_move: dict = {}
    for ts, evs in plan:
        if ts >= align:
            rest.append((ts, evs))
            continue
        for e in evs:
            if e.action == TouchAction.MOVE:
                last_move[e.pointer] = e
            else:
                last_move.pop(e.pointer, None)
                head.append(e)
    head.extend(last_move.values())
    if not head:
        return rest
    if rest and rest[0][0] == align:
        return [(align, head + rest[0][1])] + rest[1:]
    return [(align, head)] + rest


def export_to_json(ans: dict[int, list[VirtualTouchEvent]], out_file: IO):
    json.dump(
        {timestamp: [event.to_serializable() for event in events] for timestamp, events in ans.items()},
        out_file,
    )


def load_from_json(in_file: IO) -> dict[int, list[VirtualTouchEvent]]:
    return {
        int(ts): [VirtualTouchEvent.from_serializable(event) for event in events]
        for ts, events in json.load(in_file).items()
    }


__all__ = ['TouchAction', 'VirtualTouchEvent', 'TouchEvent', 'distance_of', 'recalc_pos', 'in_screen',
           'MAX_POINTERS', 'PLAN_CACHE_SUFFIX', 'FLICK_START', 'FLICK_END', 'FLICK_RADIUS', 'thin_path', 'first_note_ms', 'manual_start_plan', 'note_state', 'note_point', 'flick_path', 'flick_time_shift',
           'clamp_to_screen']
