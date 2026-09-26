"""rpe.py (RPE→官方v3转换) 的单元测试。

用一个小规模合成RPE谱面覆盖关键换算:
时间[b,s,w]、音符x/75、类型映射、hold时长、速度/4.5、旋转取负、透明度/255、
判定线瞬移(jump)在边界的取值语义。
"""
import unittest

from chart import Chart
from rpe import detect_kind, rpe_to_official_v3


def _mk_rpe() -> dict:
    """1条判定线、bpm=100、含一个x方向瞬移的合成RPE谱"""
    return {
        'BPMList': [{'bpm': 100.0, 'startTime': [0, 0, 1]}],
        'META': {'offset': 2.5, 'name': 'test'},
        'judgeLineList': [{
            'bpmfactor': 1.0,
            'eventLayers': [{
                # x: 0..4拍恒0, 第4拍瞬移到300, 4..8拍保持300(线性不变)
                'moveXEvents': [
                    {'easingType': 1, 'start': 0.0, 'end': 0.0, 'startTime': [-4, 0, 1], 'endTime': [4, 0, 1]},
                    {'easingType': 1, 'start': 300.0, 'end': 300.0, 'startTime': [4, 0, 1], 'endTime': [8, 0, 1]},
                ],
                # y: 0..8拍恒0
                'moveYEvents': [
                    {'easingType': 1, 'start': 0.0, 'end': 0.0, 'startTime': [-4, 0, 1], 'endTime': [8, 0, 1]},
                ],
                'rotateEvents': [
                    {'easingType': 1, 'start': 0.0, 'end': 0.0, 'startTime': [-4, 0, 1], 'endTime': [8, 0, 1]},
                ],
                'alphaEvents': [
                    {'easingType': 1, 'start': 255.0, 'end': 255.0, 'startTime': [-4, 0, 1], 'endTime': [8, 0, 1]},
                ],
                # 速度: 0..4拍 value 4.5(→v3 1.0), 4..8拍 value 9.0(→v3 2.0)
                'speedEvents': [
                    {'start': 4.5, 'end': 4.5, 'startTime': [0, 0, 1], 'endTime': [4, 0, 1]},
                    {'start': 9.0, 'end': 9.0, 'startTime': [4, 0, 1], 'endTime': [8, 0, 1]},
                ],
            }],
            'notes': [
                # RPE type 1=tap, x=75px(→v3单位1.0), 第2拍
                {'above': 1, 'type': 1, 'startTime': [2, 0, 1], 'endTime': [2, 0, 1],
                 'positionX': 75.0, 'alpha': 255, 'speed': 1.0, 'isFake': 0,
                 'visibleTime': 999999.0, 'yOffset': 0.0, 'size': 1.0},
                # RPE type 2=hold(→v3 type 3), 第3拍持续1拍, 下方
                {'above': 0, 'type': 2, 'startTime': [3, 0, 1], 'endTime': [4, 0, 1],
                 'positionX': -150.0, 'alpha': 128, 'speed': 1.0, 'isFake': 0,
                 'visibleTime': 999999.0, 'yOffset': 0.0, 'size': 1.0},
                # RPE type 3=flick(→v3 type 4)
                {'above': 1, 'type': 3, 'startTime': [5, 0, 1], 'endTime': [5, 0, 1],
                 'positionX': 225.0, 'alpha': 255, 'speed': 1.0, 'isFake': 0,
                 'visibleTime': 999999.0, 'yOffset': 0.0, 'size': 1.0},
                # RPE type 4=drag(→v3 type 2)
                {'above': 0, 'type': 4, 'startTime': [6, 0, 1], 'endTime': [6, 0, 1],
                 'positionX': -75.0, 'alpha': 255, 'speed': 1.0, 'isFake': 0,
                 'visibleTime': 999999.0, 'yOffset': 0.0, 'size': 1.0},
                # fake音符应被跳过
                {'above': 1, 'type': 1, 'startTime': [7, 0, 1], 'endTime': [7, 0, 1],
                 'positionX': 0.0, 'alpha': 255, 'speed': 1.0, 'isFake': 1,
                 'visibleTime': 999999.0, 'yOffset': 0.0, 'size': 1.0},
            ],
            'numOfNotes': 5,
        }],
    }


class TestRpeImport(unittest.TestCase):
    def setUp(self):
        self.rpe = _mk_rpe()
        self.converted, self.warns = rpe_to_official_v3(self.rpe)
        self.chart = Chart.from_dict(self.converted)

    def test_detect_kind(self):
        self.assertEqual(detect_kind(self.rpe), 'rpe')
        self.assertEqual(detect_kind(self.converted), 'official')
        self.assertEqual(detect_kind({'foo': 1}), 'unknown')

    def test_meta(self):
        self.assertEqual(self.converted['formatVersion'], 3)
        # RPE的offset单位为毫秒(与Phira一致: offset / 1000)
        self.assertAlmostEqual(self.chart.offset, 0.0025)
        self.assertEqual(len(self.chart.judge_lines), 1)
        self.assertAlmostEqual(self.chart.judge_lines[0].bpm, 100.0)

    def test_notes(self):
        line = self.chart.judge_lines[0]
        notes = {n.time: n for n in line.notes}
        # fake音符被跳过, 剩4个
        self.assertEqual(len(notes), 4)
        # 时间: 拍*32; x: /75; 类型映射
        self.assertEqual(notes[64].type.name, 'TAP')
        self.assertAlmostEqual(notes[64].x, 1.0)
        self.assertEqual(notes[64].hold, 0.0)
        self.assertEqual(notes[96].type.name, 'HOLD')
        self.assertAlmostEqual(notes[96].x, -2.0)
        self.assertEqual(notes[96].hold, 32)  # 1拍
        self.assertEqual(notes[160].type.name, 'FLICK')
        self.assertAlmostEqual(notes[160].x, 3.0)
        self.assertEqual(notes[192].type.name, 'DRAG')
        self.assertAlmostEqual(notes[192].x, -1.0)
        # above/below
        self.assertEqual(len(line.notes_above), 2)
        self.assertEqual(len(line.notes_below), 2)

    def test_speed_and_floor(self):
        line = self.chart.judge_lines[0]
        # 0..4拍, 4..8拍, 8拍之后(保持最后一个事件的值)
        self.assertEqual(len(line.speed_events), 3)
        self.assertAlmostEqual(line.speed_events[0].value, 1.0)
        self.assertAlmostEqual(line.speed_events[1].value, 2.0)
        self.assertAlmostEqual(line.speed_events[2].value, 2.0)
        # floor推导: 0..4拍按1.0累积, 4拍处=128*1.0*1.875/100=2.4
        self.assertAlmostEqual(line.floor(4 * 32), 2.4, places=6)
        # 96时刻(第3拍, 第一段内): 96*1.0*1.875/100=1.8
        self.assertAlmostEqual(line.floor(96), 1.8, places=6)
        # hold音符的floorPosition应与推导一致
        all_notes = line.notes_above + line.notes_below
        hold_note = [n for n in all_notes if n.type.name == 'HOLD'][0]
        self.assertAlmostEqual(hold_note.floor, line.floor(96), places=6)

    def test_line_pos_and_jump(self):
        line = self.chart.judge_lines[0]
        # 瞬移前: x=0 → px 640; y=0 → py 360
        self.assertAlmostEqual(line.pos(64)[0], 640.0, places=3)
        self.assertAlmostEqual(line.pos(64)[1], 360.0, places=3)
        # 瞬移后: x=300 → frac 300/1350+0.5 → px (300/1350+0.5)*1280
        expected_x = (300.0 / 1350.0 + 0.5) * 1280
        self.assertAlmostEqual(line.pos(160)[0], expected_x, places=3)
        self.assertAlmostEqual(line.pos(160)[1], 360.0, places=3)
        # 边界处(第4拍=128)取瞬移前的值(游戏语义: 边界属于前一段)
        self.assertAlmostEqual(line.pos(128)[0], 640.0, places=3)

    def test_angle_opacity(self):
        line = self.chart.judge_lines[0]
        self.assertAlmostEqual(line.angle(64), 0.0)
        self.assertAlmostEqual(line.opacity(64), 1.0)

    def test_fake_warning(self):
        self.assertTrue(any('fake' in w for w in self.warns))


if __name__ == '__main__':
    unittest.main()
