# 判定区半宽(像素, 1280x720)。触点沿判定线方向的投影落在音符位置 ±这个范围内即判定成功,
# 垂直方向不影响判定(见 tools/judge_sim.py 的 local_x / xmax)。
JUDGE_HALF_WIDTH = 151.2

# 指针规划算法的基类和一些实用类型、函数
from typing import Self, IO
from enum import Enum
from typing import NamedTuple
import math
import json


# 左上角暂停按钮的触发区(1280x720坐标)。在这里"点一下"(按下再抬起)会让游戏暂停,
# 所以任何触点都不能在这里按下/抬起: 扫屏/滑键触点的按下位置、长条接力触点是挑着位置放的;
# 蓝键/黄键/长条/红键本来按音符的位置点, 音符落在这个区域里时, 沿垂直于判定线的方向平移到区域外
# (垂直判定: 触点在判定线方向上的投影不变, 判定结果不变), 见 avoid_pause_button。
PAUSE_BUTTON_BOX = (160.0, 160.0)
# 平移出暂停键区域时多走这么远(像素), 抵消坐标取整/缩放带来的误差
PAUSE_EXIT_MARGIN = 8.0


# Android 系统允许同时存在的最大触点数(MotionEvent 硬上限)。
# phisap 会推送补丁版 scrcpy-server 以解除官方的10触点限制, 补丁失败时退回10。
MAX_POINTERS = 16

# 规划缓存文件后缀。规划逻辑或谱面解析有影响结果的修改时递增版本号,
# 旧版本生成的缓存会被自动忽略(需要重新规划)。
# v2: 修正官谱/RPE转换谱的y方向、RPE缓动/多层/父线、长按与滑键跟随判定线、16触点
# v3: 精简长按的MOVE事件; 新增algo3(扫屏)
# v4: flick滑动加快到3像素/毫秒; algo3/algo3f的flick改为单独规划
# v7: 蓝键(TAP)/长条(HOLD)必须在5ms内释放, 避免触点占满漏判
# v8: 同位置DRAG/TAP重复按下修复(DRAG复用同毫秒内已按下的触点, 避免挤占触点名额)
# v9: 修复指针预算被扫屏/滑键触点偷走(released_at记真实抬手时刻, 增长上限改为剩余名额)
# v10: 长条触点越界时沿垂直方向夹到屏幕边缘, 不再跳到"垂直弦中点"(手指位置连续)
# v11: 长条判定线瞬移时加接力触点(整体时间偏差容忍度从±40ms扩到±90ms, 见 algo/relay.py);
#      flick很密的谱面自动增加滑键触点, 排不下的flick推迟/短划(见 algo3.solve_with)
# v12: 音符落在左上角暂停键区域时, 触点沿垂直于判定线的方向平移到区域外(见 avoid_pause_button)
# v13: 规划时间加上谱面 offset; 缓存文件名带上算法和触点数; algored(噪点红场)
# v14: algored 按红块出现到消失的整段躲开, 在垂直线上选离红场足够远的点, 不再往红场里按下
# v15: 红场在动, 只躲这一毫秒的位置; 这一下没缝就在判定窗里等它让开
# v16: 红场按 4.0.1 原生判定几何(只在 enable 区间、正确缓动和缩放锚点);
#      规划窗优先 Perfect ±40ms, 最远 Good ±80ms
# v17: 长条在红场赶到之前先按在垂线空位上, 接住之后再松开原来的触点;
#      不再贴着红边, 也不把判定线瞬移当成扫过红场
# v18: 换手的新按下不再落进别的音符的判定窗(不抢判定、不打出 Bad);
#      长条头优先 ±40ms, 不贴 Good 外沿, 免得 60fps 一帧顶出 ±80ms
# v19: 换手至少重叠一帧再松原来的; 判定线瞬移时手指还在判定带里就停住, 不跳过红场
# v20: 长条坐进垂线空位中间, 不再贴着红边跟着挪; 红场期间不再逐毫秒补 MOVE
PLAN_CACHE_SUFFIX = '.ans.v20.json'


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


def recalc_pos(position: tuple[float, float], sa: float, ca: float, avoid_pause: bool = True) -> tuple[float, float]:
    """重新计算坐标
    一些情况下，note会在屏幕的外侧判定。点名批评Nhelv。
    也就是说，此时横坐标会在[0, 1280]的范围外，或者纵坐标会在[0, 720]的范围外。
    这是我们需要重新规划击打的位置，让该位置落在屏幕内。
    我们利用屁股肉的垂直判定区域特性来解决这个问题。
    也就是说，在高垂直于判定线且长度不限，同时宽平行且与note等长的矩形范围内点击任意位置均视为判定成功。
    为了简化这个问题，我们将矩形视作一条线，这条线过矩形的终点且与矩形的两高平行。
    这条线必与屏幕对应的矩形相交，且绝大部分情况下有两个交点。
    我们取这两个交点的中心点作为我们操作note的位置。
    结果落在左上角暂停键区域里时, 再沿垂直于判定线的方向平移到区域外(avoid_pause_button)。
    :param position: 坐标
    :param sa: sin(angle) 判定线偏移角度的正弦值
    :param ca: cos(angle) 判定线偏移角度的余弦值
    :param avoid_pause: 是否避开暂停键(调用方自己处理暂停键时传False)
    :return: 重新计算后的坐标
    """
    pos = _recalc_on_screen(position, sa, ca)
    return avoid_pause_button(pos, sa, ca) if avoid_pause else pos


def _recalc_on_screen(position: tuple[float, float], sa: float, ca: float) -> tuple[float, float]:
    """recalc_pos 的屏幕外映射部分(不考虑暂停键)"""
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


def _edge_safe(pos: tuple[float, float]) -> tuple[float, float]:
    """把x>=1280或y>=720的边界点内缩到(1279,719),其余点不变。
    y=720/x=1280这类点经scrcpy缩放后会映射到物理坐标==device_size,
    超出Android合法范围[0,size-1],被InputManager静默丢弃,
    表现为屏幕底边缘(y=720)/右边缘(x=1280)的TAP/HOLD全部漏点。
    y=0/x=0是合法坐标(0∈[0,size-1])无需处理。
    1px内缩沿屏幕轴, 对Phigros ±80px垂直判定完全无影响,
    且在algo生成事件时统一处理(端点和所有MOVE点都过这里),路径连续不会跳变。"""
    x, y = pos
    if x >= 1280:
        x = 1279.0
    if y >= 720:
        y = 719.0
    return x, y


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


def hold_point(line, note, ms: float, time_shift: float = 0.0) -> tuple[float, float]:
    """长条(HOLD)触点的位置: 音符在屏幕内时取音符本身; 越界时沿垂直于判定线的方向
    夹回屏幕内, 取最靠近音符的那个屏幕内的点。

    为什么不能沿用 recalc_pos: recalc_pos 在越界时返回"垂直弦的中点", 这和屏幕内的
    表示(音符本身)根本不是一套坐标。谱面让判定线瞬移出屏幕再回来时, 手指会被要求在
    1ms 内从 (768, 2.6) 跳到 (768, 360) 再跳回 (192, 720) —— Chart_AT 实测单次瞬移
    357px / 679px, 全程最大 1603px。这种瞬移只要被注入延迟吃掉一部分, 长条就会因为
    "触点离开判定区超过 UP_TOLERANCE(50ms)"而断。

    沿垂直方向夹到屏幕边缘则完全不同: 音符滑出屏幕上沿时, 手指连续地滑到 y=1 并一直
    按在屏幕边缘上, 不再有几百像素的跳变; 判定线瞬移回来时, 只有沿判定线方向的分量
    需要跟随(那本来就是必须跟的)。沿判定线方向的投影始终精确等于音符的位置, 判定不受影响。
    """
    pos, sa, ca = note_state(line, note, ms, time_shift)
    x, y = pos
    if in_screen(pos):
        return avoid_pause_button(pos, sa, ca)
    lo, hi = _perpendicular_room(x, y, -sa, ca)
    lo, hi = lo + 1, hi - 1
    if lo > hi:
        # 垂直方向在屏幕内没有余量(几乎擦着角落过去), 退回原来的弦中点
        return recalc_pos(pos, sa, ca)
    s = min(max(0.0, lo), hi)
    return avoid_pause_button(clamp_to_screen((x - sa * s, y + ca * s)), sa, ca)


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


def in_pause_box(pos: tuple[float, float]) -> bool:
    """pos 是否落在左上角暂停键的触发区(PAUSE_BUTTON_BOX)里"""
    return pos[0] < PAUSE_BUTTON_BOX[0] and pos[1] < PAUSE_BUTTON_BOX[1]


def _pause_cut(bx: float, by: float, nx: float, ny: float) -> tuple[float, float] | None:
    """直线 (bx, by) + s*(nx, ny) 落在【暂停键区域外扩 PAUSE_EXIT_MARGIN】里的 s 区间(开区间), 不经过则返回 None"""
    lo, hi = -math.inf, math.inf
    for p, d, limit in ((bx, nx, PAUSE_BUTTON_BOX[0] + PAUSE_EXIT_MARGIN),
                        (by, ny, PAUSE_BUTTON_BOX[1] + PAUSE_EXIT_MARGIN)):
        if abs(d) < 1e-9:
            if p >= limit:
                return None
            continue
        t = (limit - p) / d
        if d > 0:
            hi = min(hi, t)
        else:
            lo = max(lo, t)
    return (lo, hi) if lo < hi else None


def pause_free_intervals(bx: float, by: float, nx: float, ny: float, lo: float, hi: float
                         ) -> list[tuple[float, float, str]]:
    """[lo, hi] 里不碰暂停键区域的 s 区间: [(下界, 上界, 标签)]。

    垂直线经过暂停键区域时会被切成两段: 'low'(s较小的一侧)和'high'(s较大的一侧), 两侧都没有余量
    (整条线都在暂停键区域里)则返回空表; 不经过暂停键区域时只有一段, 标签'all'。"""
    cut = _pause_cut(bx, by, nx, ny)
    if cut is None:
        return [(lo, hi, 'all')]
    a, b = cut
    out = []
    if lo <= a:
        out.append((lo, min(a, hi), 'low'))
    if b <= hi:
        out.append((max(b, lo), hi, 'high'))
    return out


def avoid_pause_button(pos: tuple[float, float], sa: float, ca: float) -> tuple[float, float]:
    """触点位置 pos 落在左上角暂停键区域时, 沿垂直于判定线的方向平移到区域外, 否则原样返回。

    垂直判定: 判定只看触点在判定线方向上的投影, 与垂直方向的高度无关(见 tools/judge_sim.py 的
    local_x), 所以沿垂直方向平移不改变投影、不影响判定。平移取最近的出口(两个方向里短的那个),
    并且保持在屏幕内; 判定线擦着屏幕角落、垂直线整段都在暂停键区域里(躲不开)时, 取离屏幕角落最远的一端。
    sa/ca: 该时刻判定线角度的sin/cos(与 note_state 的返回值一致), 垂直方向是 (-sa, ca)。"""
    if not in_pause_box(pos):
        return pos
    x, y = pos
    nx, ny = -sa, ca
    room_lo, room_hi = _perpendicular_room(x, y, nx, ny)
    lo, hi = room_lo + 1, room_hi - 1
    if lo > hi:
        return pos
    free = pause_free_intervals(x, y, nx, ny, lo, hi)
    if free:
        s = min((min(max(0.0, l), h) for l, h, _ in free), key=abs)
    else:
        # 躲不开: 取弦的两端(屏幕边缘)里离屏幕角落远的一端, clamp_to_screen 负责留出边距
        s = max((room_lo, room_hi), key=lambda v: (x + nx * v) ** 2 + (y + ny * v) ** 2)
    return clamp_to_screen((x + nx * s, y + ny * s))


def _flick_side(lo: float, hi: float, bx: float, by: float, nx: float, ny: float, radius: float) -> str | None:
    """整段滑动让出暂停键的哪一侧: 优先能滑满 radius 的一侧, 其次是离音符本身更近的一侧"""
    free = pause_free_intervals(bx, by, nx, ny, lo, hi)
    if len(free) <= 1:
        return free[0][2] if free else None

    def key(f):
        l, h, _ = f
        r = min(radius, max(0.0, (h - l) / 2))
        c = min(max(0.0, l + r), h - r) if h - l >= 2 * r else (l + h) / 2
        return -r, abs(c)

    return min(free, key=key)[2]


def _flick_interval(lo: float, hi: float, bx: float, by: float, nx: float, ny: float,
                    side: str | None) -> tuple[float, float]:
    """这一毫秒滑动可用的 s 区间: 去掉暂停键区域后取 side 那一侧(没有就取最长的一段);
    整条垂直线都在暂停键区域里(躲不开)时不管暂停键"""
    if lo > hi:
        return lo, hi
    free = pause_free_intervals(bx, by, nx, ny, lo, hi)
    if not free:
        return lo, hi
    for l, h, tag in free:
        if tag == side:
            return l, h
    l, h, _ = max(free, key=lambda f: f[1] - f[0])
    return l, h


def flick_path(line, note, center_ms: int, start: int, end: int, radius: float,
               time_shift: float = 0.0, reverse: bool = False) -> list[tuple[float, float]]:
    """flick的滑动轨迹: 返回 center_ms+start ... center_ms+end 每毫秒的位置。

    每一毫秒都按该时刻判定线的实际位置/角度计算(判定线在移动或旋转时手指跟着走),
    并沿垂直于判定线的方向滑过 2*radius 的距离(不改变在判定线方向上的投影, 所以不影响判定)。
    靠近屏幕边缘时, 整段轨迹沿垂直方向平移到屏幕内(而不是被截断在边缘上导致滑不动)。
    靠近左上角时, 整段轨迹同样沿垂直方向平移到暂停键区域外(整段只选暂停键的一侧, 不会中途换边)。
    reverse: 反方向滑动
    """
    duration = end - start
    samples = []
    for offset in range(start, end + 1):
        (x, y), sa, ca = note_state(line, note, center_ms + offset, time_shift)
        bx, by = recalc_pos((x, y), sa, ca, avoid_pause=False)
        nx, ny = -sa, ca   # 垂直于判定线的方向
        lo, hi = _perpendicular_room(bx, by, nx, ny)
        samples.append((bx, by, nx, ny, lo + 1, hi - 1))
    bx, by, nx, ny, lo, hi = samples[len(samples) // 2]
    side = None if lo > hi else _flick_side(lo, hi, bx, by, nx, ny, radius)
    path = []
    for i, (bx, by, nx, ny, lo, hi) in enumerate(samples):
        lo, hi = _flick_interval(lo, hi, bx, by, nx, ny, side)
        r = min(radius, max(0.0, (hi - lo) / 2))
        c = min(max(0.0, lo + r), hi - r) if hi - lo >= 2 * r else (lo + hi) / 2
        s = c + r * (1 - 2 * i / duration) * (-1 if reverse else 1)
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


def pause_presses(events) -> list[tuple[int, VirtualTouchEvent]]:
    """规划结果里落在暂停键区域里的按下/抬起事件: [(ms, 事件)]。

    只数DOWN和UP(会触发暂停的是按在暂停键上); 扫屏触点只是从暂停键上面划过去(MOVE)不算。
    avoid_pause_button 躲得开的情况下结果应该为空, 它也是测试和警告用的独立检查。"""
    return [(ms, e) for ms in sorted(events) for e in events[ms]
            if e.action != TouchAction.MOVE and in_pause_box(e.pos)]


def warn_pause_presses(events, console) -> int:
    """规划结果里有触点按在/抬在暂停键区域里时输出警告, 返回次数"""
    hits = pause_presses(events)
    if hits:
        console.print(f'[yellow]警告: 有{len(hits)}次按下/抬起落在左上角暂停键区域里(首次在{hits[0][0]}ms): '
                      f'判定线贴着屏幕左上角, 沿垂直方向也躲不开, 这些时刻可能会暂停游戏[/yellow]')
    return len(hits)


# 扫屏触点(algo3/algo3f)使用的指针id下限, 普通触点的id都小于它
SWEEP_POINTER_BASE_MIN = 2000


def first_note_ms(chart) -> int | None:
    """谱面第一个音符的判定时间(ms)。不含 chart.offset, 和 solve() 的时间轴一致。

    播放时的时间轴要再加上 offset(见 chart_offset_ms / shift_plan): offset>=0 时
    音乐先响, 谱面晚这么多秒。手动对齐用的是「第一个音符」, 两边一起加, 差值不变。
    """
    times = [round(line.seconds(n.time) * 1000) for line in chart.judge_lines for n in line.notes]
    return min(times) if times else None


def chart_offset_ms(chart) -> int:
    """谱面 offset 换算成毫秒。非数字或缺失按 0。"""
    try:
        return int(round(float(getattr(chart, 'offset', 0) or 0) * 1000))
    except (TypeError, ValueError):
        return 0


def shift_plan(events, chart):
    """把规划整体平移 chart.offset 秒, 让事件时间和音乐对齐。offset 为 0 时原样返回。"""
    shift = chart_offset_ms(chart)
    if not shift:
        return events
    from collections import defaultdict
    out = defaultdict(list)
    for ts, evs in events.items():
        out[ts + shift].extend(evs)
    return out


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
           'MAX_POINTERS', 'PLAN_CACHE_SUFFIX', 'FLICK_START', 'FLICK_END', 'FLICK_RADIUS', 'thin_path', 'first_note_ms', 'chart_offset_ms', 'shift_plan', 'manual_start_plan', 'note_state', 'note_point', 'flick_path', 'flick_time_shift',
           'clamp_to_screen', 'hold_point', 'JUDGE_HALF_WIDTH', 'PAUSE_BUTTON_BOX', 'PAUSE_EXIT_MARGIN', 'in_pause_box',
           'avoid_pause_button', 'pause_free_intervals', 'pause_presses', 'warn_pause_presses']
