# algo3f: "坐标轴"扫屏
#
# 像坐标系的两条轴一样:
# + 横轴: 1个触点在屏幕高度正中(y=360), 从左到右、从右到左不停地扫过整个屏幕宽度
# + 纵轴: 1个触点在屏幕宽度正中(x=640), 从上到下、从下到上不停地扫过整个屏幕高度
# 从第一个音符之前按下, 一直扫到最后一个音符之后才抬起, drag(黄键)完全不规划;
# flick(红键)由滑键触点负责(16触点时4个, 10触点时2个, 只滑动不重新按下, 见algo3.py开头的说明),
# 其余触点(10个/6个)按algo2的方式处理tap(蓝键)和hold。
#
# 判定只看触点在判定线方向上的投影: 判定线接近水平时由横轴触点负责, 接近竖直时由纵轴触点负责。
# 扫动速度受游戏帧率限制: 游戏每帧只采样一次触点位置, 相邻两帧之间扫过的距离最好小于判定宽度(约302像素),
# 否则可能某一帧在音符左边、下一帧已经到了右边。两个触点的周期与常见帧率都不成整数比, 采样位置会不断错开。

from collections import defaultdict

from rich.console import Console

from chart import Chart
from .algo3 import (SWEEP_X_MAX, SWEEP_X_MIN, SWEEP_Y_MAX, SWEEP_Y_MIN, Sweeper, solve_with,
                    whole_song_interval)
from .algo_base import MAX_POINTERS, VirtualTouchEvent

AXIS_POINTER_BASE = 2100
H_HALF_PERIOD = 103   # 横轴单程(1240像素)时间ms, 约12000像素/秒, 60帧下每帧约200像素
V_HALF_PERIOD = 71    # 纵轴单程(680像素)时间ms, 约9600像素/秒, 60帧下每帧约160像素
START_BEFORE = 1000   # 第一个音符之前多久按下(ms)
END_AFTER = 500       # 最后一个音符之后多久抬起(ms)


def axis_sweepers() -> list[Sweeper]:
    return [
        Sweeper(AXIS_POINTER_BASE, True, 360.0, SWEEP_X_MIN, SWEEP_X_MAX, H_HALF_PERIOD, 0.5, True, 0),
        Sweeper(AXIS_POINTER_BASE + 1, False, 640.0, SWEEP_Y_MIN, SWEEP_Y_MAX, V_HALF_PERIOD, 0.5, True, 3),
    ]


def solve(chart: Chart, console: Console, max_pointers: int = MAX_POINTERS) -> defaultdict[int, list[VirtualTouchEvent]]:
    return solve_with(chart, console, max_pointers, axis_sweepers(),
                      whole_song_interval(chart, START_BEFORE, END_AFTER))


__all__ = ['solve', 'axis_sweepers']
