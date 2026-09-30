'''打歌时的事件发送循环(不依赖tkinter, 便于测试)

+ 同一毫秒的所有触控事件合并成一次发送(sendall), 减少系统调用和adb转发的开销
+ 离下一批事件还早时短暂sleep(释放GIL), 避免和界面/接收线程抢占导致事件晚发
+ 打歌期间冻结垃圾回收(gc.freeze), 避免完整回收几十万个事件对象造成几十毫秒的卡顿
+ 统计每批事件实际发出的时刻比计划晚了多少, 结束后输出, 用于排查\"莫名其妙断连\"
'''
from __future__ import annotations

import gc
import os
import time
from typing import Callable, Iterator

from algo.algo_base import TouchAction

LATE_WARN_MS = 20      # 晚于计划这么多ms才记录(Perfect判定窗口为±80ms)
REPORT_LIMIT = 12      # 结束时最多列出这么多次延迟

# 距离下一批事件还有这么多ms以内就退出sleep改成忙等。
# 必须大于 time.sleep() 的实际粒度: Windows 上默认计时器精度是15.6ms,
# time.sleep(0.001) 实际会睡15.6ms(除非进程调用过 timeBeginPeriod),
# 阈值取3ms时每次都会被睡过头, 事件系统性晚发最多15ms。main.py 启动时会把
# 计时器精度调到1ms, 那时这个3ms阈值才真正生效。
BUSY_WAIT_MS = 3


def raise_timer_resolution() -> bool:
    '''把系统计时器精度提到1ms(仅Windows)。

    Windows 默认计时器粒度15.6ms, 且Python不会自动调用 timeBeginPeriod,
    于是 time.sleep(0.001) 实际睡15.6ms。打歌循环只提前 BUSY_WAIT_MS 退出sleep,
    不提高精度的话每一批事件都会被睡过头, 系统性晚发最多15ms —— 这正好卡在
    "20ms告警阈值"下面, 平时看不出来, 但确实在拖慢判定。
    返回True表示成功(或非Windows不需要)。
    '''
    if os.name != 'nt':
        return True
    try:
        import ctypes
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0   # TIMERR_NOERROR == 0
    except Exception:
        return False


class PlayStats:
    def __init__(self) -> None:
        self.batches = 0
        self.events = 0
        self.late: list[tuple[int, float, bool]] = []   # (计划时刻ms, 延迟ms, 是否含按下/抬起)
        self.max_late = 0.0
        self.max_late_at = 0

    def record(self, timestamp: int, late_ms: float, events: list) -> None:
        self.batches += 1
        self.events += len(events)
        if late_ms > self.max_late:
            self.max_late, self.max_late_at = late_ms, timestamp
        if late_ms > LATE_WARN_MS:
            key = any(e.action != TouchAction.MOVE for e in events)
            self.late.append((timestamp, late_ms, key))

    def summary(self, first_note_ms: int = 0) -> list[str]:
        def at(ms: int) -> str:
            s = (ms - first_note_ms) / 1000
            return f'{s:.2f}秒'

        lines = [f'发送统计: {self.batches}批/{self.events}个事件, 最大延迟{self.max_late:.0f}ms'
                 f'(第一个音符后{at(self.max_late_at)})']
        if self.late:
            keys = sum(1 for _, _, k in self.late if k)
            lines.append(f'[yellow]有{len(self.late)}批事件晚于计划超过{LATE_WARN_MS}ms'
                         f'(其中{keys}批含按下/抬起), 这些时刻附近可能断连:[/yellow]')
            worst = sorted(self.late, key=lambda x: -x[1])[:REPORT_LIMIT]
            lines.append('  ' + ', '.join(f'{at(t)}(+{d:.0f}ms{"" if k else ",仅移动"})' for t, d, k in sorted(worst)))
        else:
            lines.append(f'所有事件都在计划时刻{LATE_WARN_MS}ms内发出')
        return lines


def run_player(send: Callable[[list], None], ans_iter: Iterator[tuple[int, list]], start_time: Callable[[], float],
               running: Callable[[], bool], idle: Callable[[], None] | None = None,
               clock: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep,
               first_event: tuple[int, list] | None = None,
               should_continue: Callable[[], bool] | None = None) -> PlayStats:
    '''按时间表发送事件, 直到ans_iter耗尽或running()为False。

    send:            发送一批事件
    start_time:      返回当前的起始时刻(秒, 与clock同一时基; 微调时会变化)
    idle:            空闲时调用(延时模式下用于刷新界面, 只在离下一批事件还有8ms以上时调用)
    should_continue: 额外退出条件, 为False时立刻停止发送。
                      main.py 用它传"播放代际"检查: 用户点了停止或又重新开始时,
                      上一个还没退干净的worker会立即闭嘴, 不会继续往设备上戳。
    '''
    stats = PlayStats()
    gc.collect()
    gc.freeze()
    # 播放期间彻底关掉GC。理由: 这一轮里所有事件对象都被 plan 列表持有,
    # 播放过程中不产生任何垃圾(只有 struct.pack/join 的临时bytes, 由引用计数立即回收),
    # 所以gen-2回收在这里纯属白干——而它要扫掉整个堆(规划5万多个对象 + 整个Qt程序),
    # 实测在大谱面上能造成几十毫秒的停顿, 表现为"某一刻突然集体晚发"。
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        timestamp, events = first_event if first_event is not None else next(ans_iter)
        while running() and (should_continue is None or should_continue()):
            wait = timestamp - (clock() - start_time()) * 1000
            if wait > 0:
                if idle is not None and wait > 8:
                    idle()
                elif wait > BUSY_WAIT_MS:
                    sleep(0.001)
                continue
            send(events)
            stats.record(timestamp, -wait, events)
            timestamp, events = next(ans_iter)
    except StopIteration:
        pass
    finally:
        gc.unfreeze()
        if gc_was_enabled:
            gc.enable()
    return stats


__all__ = ['PlayStats', 'run_player', 'LATE_WARN_MS', 'BUSY_WAIT_MS', 'raise_timer_resolution']
