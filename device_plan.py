"""电脑版和手机版共用的计划文件。

电脑版负责规划，也可以继续用 ADB 演奏。手机版只导入这份文件，
在游戏进程里按同一套 1280×720 坐标播放，再到真机屏幕上补黑边。
"""
from __future__ import annotations

import json
from typing import IO

from algo.algo_base import TouchAction, VirtualTouchEvent

DEVICE_PLAN_FORMAT = 1
DEVICE_PLAN_WIDTH = 1280
DEVICE_PLAN_HEIGHT = 720


def export_device_plan(ans: dict[int, list[VirtualTouchEvent]], out_file: IO, name: str = '') -> None:
    events = []
    for ts in sorted(ans):
        for event in ans[ts]:
            x, y = event.pos
            events.append([
                int(ts), int(event.action.value), int(event.pointer),
                int(round(x)), int(round(y)),
            ])
    json.dump({
        'format': DEVICE_PLAN_FORMAT,
        'name': name,
        'width': DEVICE_PLAN_WIDTH,
        'height': DEVICE_PLAN_HEIGHT,
        'events': events,
    }, out_file, ensure_ascii=False, separators=(',', ':'))


def load_device_plan(in_file: IO) -> dict:
    obj = json.load(in_file)
    if obj.get('format') != DEVICE_PLAN_FORMAT:
        raise ValueError(f'不认识的手机计划版本: {obj.get("format")}')
    if obj.get('width') != DEVICE_PLAN_WIDTH or obj.get('height') != DEVICE_PLAN_HEIGHT:
        raise ValueError('手机计划必须是 1280×720，适配留到播放时做')
    events = obj.get('events')
    if not isinstance(events, list):
        raise ValueError('手机计划没有事件')
    for row in events:
        if len(row) != 5:
            raise ValueError('事件必须是 [毫秒, 动作, 触点, x, y]')
        TouchAction(row[1])
    return obj
