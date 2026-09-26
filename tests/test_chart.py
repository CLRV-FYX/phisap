"""chart/judge_line/note 解析的单元测试。

覆盖三种 formatVersion：
+ v1: 旧版(1.x)，move事件为百分整数编码，speedEvents无floorPosition(累积推导)
+ v2: 2.x~3.1.x官谱，speedEvents显式携带floorPosition，move事件为0..1分数
+ v3: Phigros 3.20.0+新官谱，结构同v2，但speedEvents移除了floorPosition(需从0累积推导)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chart import Chart
from note import NoteType


def make_line(events, notes_above=None, notes_below=None, bpm=120.0, move=None,
              move_encode_v1=False, rotate=None, disappear=None):
    if move is None:
        move = [{'startTime': -999999.0, 'endTime': 1e9, 'start': 0.5, 'end': 0.5,
                 'start2': 0.5, 'end2': 0.5}]
    if move_encode_v1:
        # v1编码: start = x*880*1000 + y*520
        move = [{'startTime': e['startTime'], 'endTime': e['endTime'],
                 'start': e['start'] * 880 * 1000 + e['start2'] * 520,
                 'end': e['end'] * 880 * 1000 + e['end2'] * 520} for e in move]
    return {
        'bpm': bpm,
        'notesAbove': notes_above or [],
        'notesBelow': notes_below or [],
        'speedEvents': events,
        'judgeLineDisappearEvents': disappear or [{'startTime': -999999.0, 'endTime': 1e9,
                                                   'start': 1.0, 'end': 1.0}],
        'judgeLineMoveEvents': move,
        'judgeLineRotateEvents': rotate or [{'startTime': -999999.0, 'endTime': 1e9,
                                             'start': 0.0, 'end': 0.0}],
    }


def make_note(n_type, time, x, hold=0.0, floor=0.0, speed=1.0):
    return {'type': n_type, 'time': time, 'positionX': x, 'holdTime': hold,
            'speed': speed, 'floorPosition': floor}


def make_chart(version, lines, offset=0.0):
    return {'formatVersion': version, 'offset': offset, 'judgeLineList': lines}


class TestChartParsing(unittest.TestCase):
    def test_v2_explicit_floor_used(self):
        # v2: speedEvents显式携带floorPosition，必须原样使用
        events = [
            {'startTime': 0.0, 'endTime': 32.0, 'value': 2.0, 'floorPosition': 10.0},
            {'startTime': 32.0, 'endTime': 64.0, 'value': 1.0, 'floorPosition': 11.0},
        ]
        line = make_line(events)
        chart = Chart.from_dict(make_chart(2, [line]))
        self.assertAlmostEqual(chart.judge_lines[0].floor(16.0), 10.0 + 16 * 1.875 * 2 / 120)
        self.assertAlmostEqual(chart.judge_lines[0].floor(48.0), 11.0 + 16 * 1.875 * 1 / 120)

    def test_v3_cumulative_floor_from_zero(self):
        # v3: speedEvents无floorPosition，需从0累积
        # floor(16) = 16*1.875*2/120 = 0.5
        # floor(32) = 32*1.875*2/120 = 1.0
        # floor(48) = 1.0 + 16*1.875*1/120 = 1.25
        events = [
            {'startTime': 0.0, 'endTime': 32.0, 'value': 2.0},
            {'startTime': 32.0, 'endTime': 64.0, 'value': 1.0},
        ]
        line = make_line(events)
        chart = Chart.from_dict(make_chart(3, [line]))
        jl = chart.judge_lines[0]
        self.assertAlmostEqual(jl.floor(0.0), 0.0)
        self.assertAlmostEqual(jl.floor(16.0), 0.5)
        self.assertAlmostEqual(jl.floor(32.0), 1.0)
        self.assertAlmostEqual(jl.floor(48.0), 1.25)

    def test_v3_note_fields(self):
        # v3音符字段语义与v2一致: type编号(1tap 2drag 3hold 4flick)、time(1/32拍)、x(72px单位)
        notes = [
            make_note(1, 16, 1.0, floor=0.5),
            make_note(4, 32, -2.5, floor=1.0),
            make_note(3, 48, 0.0, hold=32.0, floor=1.5),
            make_note(2, 64, 3.0, floor=2.0),
        ]
        events = [
            {'startTime': 0.0, 'endTime': 1e9, 'value': 2.0},
        ]
        line = make_line(events, notes_above=notes)
        chart = Chart.from_dict(make_chart(3, [line]))
        jl = chart.judge_lines[0]
        self.assertEqual([n.type for n in jl.notes_above],
                         [NoteType.TAP, NoteType.FLICK, NoteType.HOLD, NoteType.DRAG])
        self.assertEqual([n.time for n in jl.notes_above], [16, 32, 48, 64])
        self.assertAlmostEqual(jl.notes_above[2].hold, 32.0)
        self.assertAlmostEqual(jl.notes_above[1].x, -2.5)
        # floor与speed事件一致
        for n in jl.notes_above:
            self.assertAlmostEqual(jl.floor(n.time), n.floor, places=3)

    def test_v3_note_pos(self):
        # x单位为72px: x=1.0 → 判定线中心(640)右侧72px
        notes = [make_note(1, 16, 1.0)]
        line = make_line([{'startTime': 0.0, 'endTime': 1e9, 'value': 1.0}], notes_above=notes)
        chart = Chart.from_dict(make_chart(3, [line]))
        n = chart.judge_lines[0].notes_above[0]
        x, y = chart.judge_lines[0].pos_of(n)
        self.assertAlmostEqual(x, 640 + 72)
        self.assertAlmostEqual(y, 360)

    def test_v1_unchanged(self):
        # v1路径保持原行为: move事件百分整数编码 + floor累积推导
        events = [
            {'startTime': 0.0, 'endTime': 32.0, 'value': 2.0},
            {'startTime': 32.0, 'endTime': 64.0, 'value': 1.0},
        ]
        line = make_line(events, move_encode_v1=True)
        chart = Chart.from_dict(make_chart(1, [line]))
        jl = chart.judge_lines[0]
        self.assertAlmostEqual(jl.floor(16.0), 0.5)
        self.assertAlmostEqual(jl.floor(48.0), 1.25)
        # v1 move解码: 0.5*880*1000 + 0.5*520 → (0.5*1280, 720-0.5*720)
        self.assertAlmostEqual(jl.pos(16.0)[0], 640.0)
        self.assertAlmostEqual(jl.pos(16.0)[1], 360.0)

    def test_floor_tail_extension(self):
        # 最后一段speed事件未覆盖到t时，按最后速率外推(与Phira行为一致)
        events = [{'startTime': 0.0, 'endTime': 32.0, 'value': 2.0}]
        line = make_line(events)
        chart = Chart.from_dict(make_chart(3, [line]))
        jl = chart.judge_lines[0]
        self.assertAlmostEqual(jl.floor(32.0), 1.0)
        self.assertAlmostEqual(jl.floor(48.0), 48 * 1.875 * 2 / 120)

    def test_floor_before_first_event_raises(self):
        events = [{'startTime': 16.0, 'endTime': 32.0, 'value': 2.0}]
        line = make_line(events)
        chart = Chart.from_dict(make_chart(3, [line]))
        with self.assertRaises(RuntimeError):
            chart.judge_lines[0].floor(8.0)

    def test_v2_move_fractions(self):
        # v2 move事件: start/end为1280x720空间的0..1分数
        line = make_line(
            [{'startTime': 0.0, 'endTime': 1e9, 'value': 1.0}],
            move=[{'startTime': 0.0, 'endTime': 100.0, 'start': 0.25, 'end': 0.75,
                   'start2': 0.5, 'end2': 0.5}],
        )
        chart = Chart.from_dict(make_chart(2, [line]))
        self.assertAlmostEqual(chart.judge_lines[0].pos(0.0)[0], 0.25 * 1280)
        self.assertAlmostEqual(chart.judge_lines[0].pos(100.0)[0], 0.75 * 1280)
        self.assertAlmostEqual(chart.judge_lines[0].pos(50.0)[0], 0.5 * 1280)

    def test_move_y_convention_v2_vs_v3(self):
        # 官谱formatVersion 1/3(含3.20.0+新官谱)的move事件y分量都以屏幕底部为0、向上为正
        # → 屏幕y = 720 - frac*720 (与Phira pgr.rs、原版phisap、谱面格式文档一致)
        move = [{'startTime': 0.0, 'endTime': 1e9, 'start': 0.5, 'end': 0.5,
                 'start2': 0.25, 'end2': 0.25}]
        events = [{'startTime': 0.0, 'endTime': 1e9, 'value': 1.0}]
        c2 = Chart.from_dict(make_chart(2, [make_line(events, move=move)]))
        c3 = Chart.from_dict(make_chart(3, [make_line(events, move=move)]))
        self.assertAlmostEqual(c2.judge_lines[0].pos(0.0)[1], 720 - 0.25 * 720)
        self.assertAlmostEqual(c3.judge_lines[0].pos(0.0)[1], 720 - 0.25 * 720)

    def test_rotated_line_note_position(self):
        # 判定线在屏幕下方(y分数0.25)并逆时针旋转90度: x=+1的音符应在线中心的正上方72像素
        move = [{'startTime': 0.0, 'endTime': 1e9, 'start': 0.5, 'end': 0.5,
                 'start2': 0.25, 'end2': 0.25}]
        rot = [{'startTime': 0.0, 'endTime': 1e9, 'start': 90.0, 'end': 90.0}]
        events = [{'startTime': 0.0, 'endTime': 1e9, 'value': 1.0}]
        notes = [make_note(1, 16, 1.0)]
        c3 = Chart.from_dict(make_chart(3, [make_line(events, notes_above=notes, move=move, rotate=rot)]))
        line = c3.judge_lines[0]
        x, y = line.pos_of(line.notes[0])
        self.assertAlmostEqual(x, 640.0, places=6)
        self.assertAlmostEqual(y, 540.0 - 72.0, places=6)

    def test_event_lookup_bisect_matches_linear(self):
        # 二分查找与逐个查找的结果一致, 包括边界时刻(属于前一个事件)与零长度事件
        rot = [
            {'startTime': -999999.0, 'endTime': 0.0, 'start': 0.0, 'end': 0.0},
            {'startTime': 0.0, 'endTime': 10.0, 'start': 0.0, 'end': 10.0},
            {'startTime': 10.0, 'endTime': 10.0, 'start': 50.0, 'end': 60.0},
            {'startTime': 10.0, 'endTime': 20.0, 'start': 30.0, 'end': 40.0},
            {'startTime': 20.0, 'endTime': 1e9, 'start': 40.0, 'end': 40.0},
        ]
        events = [{'startTime': 0.0, 'endTime': 1e9, 'value': 1.0}]
        line = Chart.from_dict(make_chart(3, [make_line(events, rotate=rot)])).judge_lines[0]
        self.assertTrue(line._rotate_idx.ordered)
        self.assertAlmostEqual(line.angle(5.0), 5.0)
        self.assertAlmostEqual(line.angle(10.0), 10.0)   # 边界属于前一个事件
        self.assertAlmostEqual(line.angle(10.5), 30.5)
        self.assertAlmostEqual(line.angle(25.0), 40.0)
        line._rotate_idx.ordered = False
        for t in (-5.0, 0.0, 5.0, 10.0, 10.5, 20.0, 25.0):
            linear = line.angle(t)
            line._rotate_idx.ordered = True
            self.assertAlmostEqual(line.angle(t), linear)
            line._rotate_idx.ordered = False

    def test_v2_v3_equivalent_when_floor_present(self):
        # 同一份谱面数据，仅v3去掉floorPosition，两者解析结果应一致
        events_v2 = [
            {'startTime': 0.0, 'endTime': 32.0, 'value': 2.0, 'floorPosition': 0.0},
            {'startTime': 32.0, 'endTime': 64.0, 'value': 1.0, 'floorPosition': 1.0},
        ]
        events_v3 = [
            {'startTime': 0.0, 'endTime': 32.0, 'value': 2.0},
            {'startTime': 32.0, 'endTime': 64.0, 'value': 1.0},
        ]
        notes = [make_note(1, 16, 1.0, floor=0.5)]
        c2 = Chart.from_dict(make_chart(2, [make_line(events_v2, notes_above=notes)]))
        c3 = Chart.from_dict(make_chart(3, [make_line(events_v3, notes_above=notes)]))
        for t in (0.0, 16.0, 32.0, 48.0, 64.0):
            self.assertAlmostEqual(c2.judge_lines[0].floor(t), c3.judge_lines[0].floor(t))


if __name__ == '__main__':
    unittest.main()
