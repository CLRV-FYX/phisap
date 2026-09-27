"""判定模拟器: 把规划结果(触控事件)按Phira的判定逻辑回放, 统计每种音符的Perfect/Good/Bad/Miss。

判定规则照搬 Phira(TeamFlos/phira, prpr/src/judge.rs), Phira是对Phigros判定的复刻:
+ 按帧判定(默认60fps, 随机帧相位), 触点按"帧快照"参与判定, flick检测使用帧间的全部移动事件
+ 判定只看触点在判定线方向上的投影(与判定线的垂直距离无关), 判定宽度约±151像素(1280x720)
+ Tap: 需要新按下的触点, ±80ms Perfect / ±160ms Good / 更远Bad
+ Drag: 任意触点在范围内且时间差≤220ms
+ Flick: 快速移动中的触点(每判定一个flick后需重新"甩动"), 时间差≤160ms(迟到额外放宽70ms)
+ Hold: 头部同Tap, 之后每帧都需要有触点在范围内(允许断开50ms), 最后220ms自动判定

用法:
    python tools/judge_sim.py <谱面.json> [algo1|algo2|algo3|algo3f ...] [--fps 60] [--width 151] [--strict-flick]
    --strict-flick: 一次滑动只能判定一个flick(sim-phi的规则, 可能更接近真实的Phigros)
"""

from __future__ import annotations

import math
import os
import random
import sys
from bisect import bisect_left

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algo.algo_base import TouchAction  # noqa: E402
from note import NoteType  # noqa: E402

LIMIT_PERFECT = 0.08
LIMIT_GOOD = 0.16
LIMIT_BAD = 0.22
UP_TOLERANCE = 0.05
DIST_FACTOR = 0.2
EARLY_OFFSET = 0.07
NOTE_WIDTH_RATIO_BASE = 0.13175016
X_DIFF_MAX = 0.21 / (16 / 9) * 2          # 归一化单位(半屏宽 = 1)
FLICK_THRESHOLD = 0.8 * 275 / 386          # 归一化单位/秒
HALF_W = 640.0                             # 1280x720空间中 1个归一化单位 = 640像素

KIND = {NoteType.TAP: 'tap', NoteType.DRAG: 'drag', NoteType.HOLD: 'hold', NoteType.FLICK: 'flick'}


class FlickTracker:
    """Phira的flick规则: 判定一个flick后, 下一次足够快的移动就能再判定下一个"""

    def __init__(self, t, p):
        self.last_point = p
        self.last_delta = None
        self.last_time = t
        self.flicked = False
        self.stopped = True

    def push(self, t, p):
        dx, dy = p[0] - self.last_point[0], p[1] - self.last_point[1]
        self.last_point = p
        mag = math.hypot(dx, dy)
        if self.last_delta is not None:
            dt = t - self.last_time
            if dt > 0:
                speed = (dx * self.last_delta[0] + dy * self.last_delta[1]) / dt
                if speed < FLICK_THRESHOLD:
                    self.stopped = True
                if self.stopped and not self.flicked:
                    self.flicked = mag / dt >= FLICK_THRESHOLD * 2
        self.last_delta = (dx / mag, dy / mag) if mag > 0 else self.last_delta
        self.last_time = t

    def frame_move(self, p, dt_ms):
        pass

    def can_flick(self):
        return self.flicked

    def consume(self):
        self.flicked = False


class StrictFlickTracker:
    """更严格的flick规则(与lchzh3473/sim-phi一致): 一次滑动只能判定一个flick。

    触点速度(沿上一次移动方向的分量)超过 START 像素/毫秒时进入"甩动"状态, 判定一个flick之后
    必须先减速到 STOP 以下(停下或者反向)才能再判定下一个。按帧更新(游戏每帧读取一次触点位置)。
    """
    START = 1.0   # 像素/毫秒(1280x720)
    STOP = 0.5

    def __init__(self, t, p):
        self.pos = p
        self.last = (0.0, 0.0)
        self.now = (0.0, 0.0)
        self.flicking = False
        self.flicked = False

    def frame_move(self, p, dt_ms):
        self.last = self.now
        self.now = (p[0] - self.pos[0], p[1] - self.pos[1])
        self.pos = p
        mag = math.hypot(*self.last)
        if mag == 0 or dt_ms <= 0:
            return
        speed = (self.now[0] * self.last[0] + self.now[1] * self.last[1]) / mag / dt_ms
        if self.flicking and speed < self.STOP:
            self.flicking = self.flicked = False
        elif not self.flicking and speed > self.START:
            self.flicking = True

    def can_flick(self):
        return self.flicking and not self.flicked

    def consume(self):
        self.flicked = True


class SimNote:
    __slots__ = ('line', 'kind', 't', 'end', 'x', 'status', 'result', 'hold_perfect', 'up_time', 'hold_pre', 'by')

    def __init__(self, line, kind, t, end, x):
        self.line, self.kind, self.t, self.end, self.x = line, kind, t, end, x
        self.status = 'none'   # none / pre / hold / done
        self.result = None
        self.hold_perfect = False
        self.up_time = math.inf
        self.hold_pre = False
        self.by = None  # (触点id, 判定时刻) 调试用


def simulate(chart, ans, fps: float = 60.0, phase: float | None = None, width_px: float | None = None,
             seed: int = 0, quantize_ms: float = 0.0, jitter_ms: float = 0.0, strict_flick: bool = False) -> dict:
    """回放规划结果, 返回 {'tap': {'perfect': n, ...}, ..., 'notes': [SimNote]}

    quantize_ms: 模拟发送端时钟精度(例如 Windows 上 Python 3.12 的 time.time() 精度约15.6ms,
                 事件会攒成一批一起发出)
    jitter_ms:   每批事件额外的随机延迟上限(模拟注入/传输延迟的抖动)
    strict_flick: 使用更严格的flick规则(一次滑动只能判定一个flick, 见StrictFlickTracker)
    """
    rng = random.Random(seed)
    xmax = X_DIFF_MAX if width_px is None else width_px / HALF_W
    notes = []
    for li, line in enumerate(chart.judge_lines):
        for n in line.notes:
            t = line.seconds(n.time)
            notes.append(SimNote(li, KIND[n.type], t, t + line.seconds(n.hold), n.x * 72 / HALF_W))
    notes.sort(key=lambda n: n.t)
    note_times = [n.t for n in notes]

    events = []
    q_phase = rng.random() * quantize_ms
    last_send = -math.inf
    for ms in sorted(ans):
        send = ms
        if quantize_ms:
            send = math.ceil((ms - q_phase) / quantize_ms) * quantize_ms + q_phase
        if jitter_ms:
            send += rng.random() * jitter_ms
        send = max(send, last_send)  # 发送顺序不变
        last_send = send
        for e in ans[ms]:
            events.append((send / 1000, e))
    if not notes:
        return {}
    frame = 1 / fps
    t = (notes[0].t - 1.0) + (rng.random() * frame if phase is None else phase)
    t_end = max(n.end for n in notes) + 1.0
    ei = 0
    down: dict[int, tuple] = {}
    trackers: dict = {}
    Tracker = StrictFlickTracker if strict_flick else FlickTracker
    last_t = t - frame

    def line_frame(li, cache):
        if li not in cache:
            line = chart.judge_lines[li]
            lt = line.time(t)
            cx, cy = line.pos(lt)
            a = -line.angle(lt) * math.pi / 180
            cache[li] = (cx, cy, math.cos(a), math.sin(a))
        return cache[li]

    def local_x(li, p, cache):
        cx, cy, ca, sa = line_frame(li, cache)
        return ((p[0] - cx) * ca + (p[1] - cy) * sa) / HALF_W

    while t <= t_end:
        # ---- 收集本帧的触控事件
        batch = []
        while ei < len(events) and events[ei][0] <= t:
            batch.append(events[ei][1])
            ei += 1
        touches: dict[int, list] = {pid: [p, 'moved'] for pid, p in down.items()}
        step = (t - last_t) / (len(batch) + 1)
        et = last_t
        for e in batch:
            et += step
            p = (float(e.pos[0]), float(e.pos[1]))
            np_ = ((p[0] - 640) / HALF_W, (p[1] - 360) / HALF_W)
            if e.action == TouchAction.DOWN:
                down[e.pointer] = p
                trackers[e.pointer] = Tracker(et, p if strict_flick else np_)
                touches[e.pointer] = [p, 'started']
            elif e.action == TouchAction.MOVE:
                if e.pointer in down:
                    down[e.pointer] = p
                    if not strict_flick and e.pointer in trackers:
                        trackers[e.pointer].push(et, np_)
                    if e.pointer in touches:
                        touches[e.pointer][0] = p
            elif e.action == TouchAction.UP:
                down.pop(e.pointer, None)
                trackers.pop(e.pointer, None)
                if e.pointer in touches and touches[e.pointer][1] != 'started':
                    touches[e.pointer][1] = 'ended'
        if strict_flick:
            for pid in {e.pointer for e in batch if e.action == TouchAction.MOVE}:
                if pid in trackers and pid in down:
                    trackers[pid].frame_move(down[pid], (t - last_t) * 1000)
        last_t = t

        cache: dict = {}
        lo = bisect_left(note_times, t - LIMIT_BAD - EARLY_OFFSET - 0.05)
        hi = bisect_left(note_times, t + LIMIT_BAD + 0.05)
        window = [n for n in notes[lo:hi] if n.status in ('none', 'pre')]

        # ---- 1. 点击 & flick
        for pid, (p, ph) in touches.items():
            click = ph == 'started'
            flick = ph == 'moved' and pid in trackers and trackers[pid].can_flick()
            if not (click or flick):
                continue
            best = (None, xmax, LIMIT_BAD, LIMIT_BAD + max(xmax / NOTE_WIDTH_RATIO_BASE - 1, 0) * DIST_FACTOR)
            for n in window:
                if n.status not in ('none', 'pre'):
                    continue
                if not click and n.kind in ('tap', 'hold'):
                    continue
                dt = n.t - t
                if dt >= best[3]:
                    continue
                dt = abs(min(dt + EARLY_OFFSET, 0)) if dt < 0 else dt
                dist = abs(n.x - local_x(n.line, p, cache))
                if dist > xmax:
                    continue
                if dt > (LIMIT_BAD - LIMIT_PERFECT * max(dist - 0.9, 0) if n.kind == 'tap' else LIMIT_GOOD):
                    continue
                if n.kind in ('flick', 'drag'):
                    dt += LIMIT_GOOD
                key = dt + max(dist / NOTE_WIDTH_RATIO_BASE - 1, 0) * DIST_FACTOR
                if key < best[3]:
                    best = (n, dist, dt, key)
            n, _, dt, _ = best
            if n is None or n.kind == 'drag':
                continue
            n.by = (pid, t)
            if click:
                if n.kind == 'flick':
                    continue
                if dt <= LIMIT_GOOD or n.kind == 'hold':
                    if n.kind == 'tap':
                        n.status, n.result = 'done', 'perfect' if dt <= LIMIT_PERFECT else 'good'
                    else:
                        n.status, n.hold_perfect, n.up_time = 'hold', dt <= LIMIT_PERFECT, math.inf
                elif n.status == 'none':
                    n.status, n.result = 'pre', 'bad'
            else:
                n.status = 'pre'
                trackers[pid].consume()

        # ---- 2. hold持续判定 / miss / drag
        for n in notes[bisect_left(note_times, t - 600):hi]:
            if n.status == 'hold':
                if n.end - t <= LIMIT_BAD:
                    n.hold_pre = True
                    continue
                if not any(abs(local_x(n.line, p, cache) - n.x) <= xmax for p, _ in touches.values()):
                    if t > n.up_time + UP_TOLERANCE:
                        n.status, n.result = 'done', 'miss'
                    elif math.isinf(n.up_time):
                        n.up_time = t
                else:
                    n.up_time = math.inf
                continue
            if n.status != 'none':
                continue
            dt = t - n.t
            if dt > LIMIT_BAD:
                n.status, n.result = 'done', 'miss'
                continue
            if -dt > LIMIT_BAD or n.kind != 'drag':
                continue
            dt = abs(dt)
            for p, _ in touches.values():
                dx = abs(local_x(n.line, p, cache) - n.x)
                if dx <= xmax and dt <= LIMIT_BAD - LIMIT_PERFECT * max(dx - 0.9, 0):
                    n.status = 'pre'
                    break

        # ---- 3. 预判定结算
        for n in notes[bisect_left(note_times, t - 600):hi]:
            if n.status == 'hold' and n.hold_pre and n.end <= t:
                n.status, n.result = 'done', 'perfect' if n.hold_perfect else 'good'
                continue
            if n.status != 'pre':
                continue
            if n.kind == 'tap':
                if t + LIMIT_GOOD >= n.t:
                    n.status = 'done'
            elif t >= n.t:
                n.status, n.result = 'done', 'perfect'
        t += frame

    stats: dict = {k: {'perfect': 0, 'good': 0, 'bad': 0, 'miss': 0} for k in ('tap', 'drag', 'hold', 'flick')}
    for n in notes:
        stats[n.kind][n.result or 'miss'] += 1
    stats['notes'] = notes
    return stats


def summary(stats: dict) -> str:
    parts = []
    tot = {'perfect': 0, 'good': 0, 'bad': 0, 'miss': 0}
    for k in ('tap', 'drag', 'hold', 'flick'):
        s = stats[k]
        for r in tot:
            tot[r] += s[r]
        n = sum(s.values())
        if n:
            parts.append(f'{k}: {s["perfect"]}/{n}' + (f' (good {s["good"]}, bad {s["bad"]}, miss {s["miss"]})'
                                                      if s['perfect'] != n else ''))
    n = sum(tot.values())
    return f'Perfect {tot["perfect"]}/{n}  Good {tot["good"]}  Bad {tot["bad"]}  Miss {tot["miss"]} | ' + ', '.join(parts)


def main():
    import io
    import json
    from rich.console import Console
    from chart import Chart
    from rpe import detect_kind, rpe_to_official_v3
    import algo.algo1
    import algo.algo2
    import algo.algo3
    import algo.algo3f

    args = sys.argv[1:]
    fps, width = 60.0, None
    if '--fps' in args:
        i = args.index('--fps'); fps = float(args[i + 1]); del args[i:i + 2]
    strict = '--strict-flick' in args
    if strict:
        args.remove('--strict-flick')
    if '--width' in args:
        i = args.index('--width'); width = float(args[i + 1]); del args[i:i + 2]
    path, algos = args[0], args[1:] or ['algo1', 'algo2', 'algo3', 'algo3f']
    with open(path, encoding='utf-8-sig') as f:
        d = json.load(f)
    if detect_kind(d) == 'rpe':
        d, _ = rpe_to_official_v3(d)
    chart = Chart.from_dict(d)
    solvers = {'algo1': algo.algo1.solve, 'algo2': algo.algo2.solve, 'algo3': algo.algo3.solve,
               'algo3f': algo.algo3f.solve}
    for name in algos:
        ans = solvers[name](chart, Console(file=io.StringIO()), 16)
        print(f'{name}: {summary(simulate(chart, ans, fps=fps, width_px=width, strict_flick=strict))}')


if __name__ == '__main__':
    main()
