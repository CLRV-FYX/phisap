"""RPE判定线运动的转换测试: 缓动、easingLeft/Right、贝塞尔、多事件层相加、父子线、BPM变化。

所有期望值都按Phira(prpr/src/parse/rpe.rs)的语义手工计算。
"""
import math
import unittest

from chart import Chart
from rpe import event_tween, rpe_to_official_v3, BpmList


def ev(start, end, b0, b1, easing=1, **kw):
    d = {'start': start, 'end': end, 'startTime': [b0, 0, 1], 'endTime': [b1, 0, 1], 'easingType': easing}
    d.update(kw)
    return d


def mk_line(layers=None, notes=None, **kw):
    d = {'bpmfactor': 1.0, 'eventLayers': layers or [], 'notes': notes or [], 'father': -1}
    d.update(kw)
    return d


def mk_rpe(lines, bpm_list=None):
    return {
        'BPMList': bpm_list or [{'bpm': 120.0, 'startTime': [0, 0, 1]}],
        'META': {'offset': 0, 'name': 't'},
        'judgeLineList': lines,
    }


def convert(rpe):
    out, warns = rpe_to_official_v3(rpe)
    return Chart.from_dict(out), out, warns


def world(line, sec):
    """转换后判定线在sec秒时的RPE画布坐标(y向上)与RPE角度(顺时针)"""
    t = line.time(sec)
    px, py = line.pos(t)
    x = (px / 1280 - 0.5) * 1350
    y = ((720 - py) / 720 - 0.5) * 900
    return x, y, -line.angle(t)


class TestEasing(unittest.TestCase):
    def test_endpoints(self):
        for et in range(1, 30):
            f = event_tween({'easingType': et}) or (lambda x: x)
            self.assertAlmostEqual(f(0.0), 0.0, delta=1e-3, msg=f'easingType {et}')
            self.assertAlmostEqual(f(1.0), 1.0, delta=1e-3, msg=f'easingType {et}')

    def test_known_values(self):
        self.assertIsNone(event_tween({'easingType': 1}))
        self.assertIsNone(event_tween({'easingType': 0}))
        self.assertIsNone(event_tween({'easingType': 99}))  # 超出范围按线性
        self.assertAlmostEqual(event_tween({'easingType': 2})(0.5), math.sin(math.pi / 4))  # outSine
        self.assertAlmostEqual(event_tween({'easingType': 5})(0.5), 0.25)                   # inQuad
        self.assertAlmostEqual(event_tween({'easingType': 4})(0.5), 0.75)                   # outQuad
        self.assertAlmostEqual(event_tween({'easingType': 7})(0.25), 0.125)                 # ioQuad
        self.assertAlmostEqual(event_tween({'easingType': 9})(0.5), 0.125)                  # inCubic
        self.assertAlmostEqual(event_tween({'easingType': 26})(0.5), 0.765625)              # outBounce

    def test_easing_left_right(self):
        f = event_tween({'easingType': 5, 'easingLeft': 0.5, 'easingRight': 1.0})
        self.assertAlmostEqual(f(0.0), 0.0)
        self.assertAlmostEqual(f(1.0), 1.0)
        self.assertAlmostEqual(f(0.5), (0.75 ** 2 - 0.25) / 0.75)

    def test_bezier(self):
        f = event_tween({'easingType': 1, 'bezier': 1, 'bezierPoints': [0.42, 0.0, 0.58, 1.0]})
        self.assertAlmostEqual(f(0.5), 0.5, places=5)
        self.assertLess(f(0.2), 0.2)
        g = event_tween({'easingType': 1, 'bezier': 1, 'bezierPoints': [0.0, 0.0, 1.0, 1.0]})
        for x in (0.1, 0.3, 0.7):
            self.assertAlmostEqual(g(x), x, places=5)


class TestLineMotion(unittest.TestCase):
    def test_eased_rotation(self):
        # bpm120: 0..4拍 = 0..2秒, 角度 0→90 inQuad
        layer = {'rotateEvents': [ev(0.0, 90.0, 0, 4, easing=5)]}
        chart, out, _ = convert(mk_rpe([mk_line([layer])]))
        line = chart.judge_lines[0]
        for k in range(0, 201):
            sec = 2.0 * k / 200
            expected = 90.0 * (sec / 2.0) ** 2
            self.assertAlmostEqual(world(line, sec)[2], expected, delta=0.03)
        self.assertAlmostEqual(world(line, 5.0)[2], 90.0, delta=1e-6)  # 结束后保持
        # 线性事件不会被细分
        layer2 = {'rotateEvents': [ev(0.0, 90.0, 0, 4, easing=1)]}
        _, out2, _ = convert(mk_rpe([mk_line([layer2])]))
        self.assertLessEqual(len(out2['judgeLineList'][0]['judgeLineRotateEvents']), 4)
        # 缓动被细分, 但合并后远少于逐毫秒
        n = len(out['judgeLineList'][0]['judgeLineRotateEvents'])
        self.assertGreater(n, 10)
        self.assertLess(n, 480)

    def test_elastic_and_bounce_accuracy(self):
        for easing in (24, 25, 26, 29):
            layer = {'moveXEvents': [ev(-300.0, 300.0, 0, 2, easing=easing)]}
            chart, _, _ = convert(mk_rpe([mk_line([layer])]))
            line = chart.judge_lines[0]
            f = event_tween({'easingType': easing})
            for k in range(0, 101):
                sec = 1.0 * k / 100
                expected = -300.0 + 600.0 * f(sec / 1.0)
                self.assertAlmostEqual(world(line, sec)[0], expected, delta=1.0, msg=f'easing {easing} @ {sec}')

    def test_layers_are_summed(self):
        l0 = {'moveXEvents': [ev(100.0, 100.0, 0, 8)], 'rotateEvents': [ev(10.0, 10.0, 0, 8)]}
        l1 = {'moveXEvents': [ev(0.0, 200.0, 0, 4)], 'rotateEvents': [ev(0.0, 20.0, 0, 4)]}
        chart, _, warns = convert(mk_rpe([mk_line([l0, None, l1])]))
        line = chart.judge_lines[0]
        x, y, r = world(line, 1.0)   # 第2拍
        self.assertAlmostEqual(x, 200.0, delta=0.01)
        self.assertAlmostEqual(r, 20.0, delta=0.01)
        x, y, r = world(line, 3.0)   # 第6拍: 第二层已结束, 保持200
        self.assertAlmostEqual(x, 300.0, delta=0.01)
        self.assertAlmostEqual(r, 30.0, delta=0.01)
        self.assertTrue(any('相加' in w for w in warns))

    def test_gap_holds_previous_end(self):
        layer = {'moveYEvents': [ev(0.0, 10.0, 0, 1), ev(20.0, 30.0, 2, 3)]}
        chart, _, _ = convert(mk_rpe([mk_line([layer])]))
        line = chart.judge_lines[0]
        self.assertAlmostEqual(world(line, 0.75)[1], 10.0, delta=0.01)  # 第1.5拍: 空档
        self.assertAlmostEqual(world(line, 1.25)[1], 25.0, delta=0.01)  # 第2.5拍

    def test_father_static_rotation(self):
        # 父线在(100, 0), 顺时针转90度; 子线局部坐标(50, 0) → 世界坐标(100, -50)
        father = mk_line([{'moveXEvents': [ev(100.0, 100.0, 0, 8)], 'rotateEvents': [ev(90.0, 90.0, 0, 8)]}])
        child = mk_line([{'moveXEvents': [ev(50.0, 50.0, 0, 8)], 'rotateEvents': [ev(5.0, 5.0, 0, 8)]}], father=0)
        child_rot = mk_line([{'moveXEvents': [ev(50.0, 50.0, 0, 8)], 'rotateEvents': [ev(5.0, 5.0, 0, 8)]}],
                            father=0, rotateWithFather=True)
        chart, _, warns = convert(mk_rpe([father, child, child_rot]))
        x, y, r = world(chart.judge_lines[1], 1.0)
        self.assertAlmostEqual(x, 100.0, delta=0.01)
        self.assertAlmostEqual(y, -50.0, delta=0.01)
        self.assertAlmostEqual(r, 5.0, delta=0.01)
        x, y, r = world(chart.judge_lines[2], 1.0)
        self.assertAlmostEqual(r, 95.0, delta=0.01)
        self.assertTrue(any('父线' in w for w in warns))

    def test_father_rotating_moves_child_on_circle(self):
        father = mk_line([{'rotateEvents': [ev(0.0, 90.0, 0, 4)]}])
        child = mk_line([{'moveXEvents': [ev(200.0, 200.0, 0, 8)]}], father=0)
        chart, _, _ = convert(mk_rpe([father, child]))
        line = chart.judge_lines[1]
        for k in range(0, 41):
            sec = 2.0 * k / 40
            th = math.radians(90.0 * sec / 2.0)
            x, y, _ = world(line, sec)
            self.assertAlmostEqual(x, 200 * math.cos(th), delta=0.5)
            self.assertAlmostEqual(y, -200 * math.sin(th), delta=0.5)

    def test_bpm_changes(self):
        # 0..4拍 bpm120 (2秒), 之后 bpm60: 第6拍 = 2 + 2 = 4秒
        bpm_list = [{'bpm': 120.0, 'startTime': [0, 0, 1]}, {'bpm': 60.0, 'startTime': [4, 0, 1]}]
        self.assertAlmostEqual(BpmList(bpm_list).seconds(6.0), 4.0)
        notes = [{'above': 1, 'type': 1, 'startTime': [6, 0, 1], 'endTime': [6, 0, 1], 'positionX': 0.0},
                 {'above': 1, 'type': 2, 'startTime': [3, 0, 1], 'endTime': [5, 0, 1], 'positionX': 0.0}]
        layer = {'moveXEvents': [ev(0.0, 100.0, 4, 6)]}
        chart, _, _ = convert(mk_rpe([mk_line([layer], notes)], bpm_list))
        line = chart.judge_lines[0]
        tap = [n for n in line.notes if n.type.name == 'TAP'][0]
        hold = [n for n in line.notes if n.type.name == 'HOLD'][0]
        self.assertAlmostEqual(line.seconds(tap.time), 4.0)
        self.assertAlmostEqual(line.seconds(hold.time), 1.5)
        self.assertAlmostEqual(line.seconds(hold.hold), 1.5)   # 3拍→5拍 = 0.5秒 + 1秒
        # 事件在"秒"上线性插值(与Phira一致): 2秒→4秒, 3秒时为50
        self.assertAlmostEqual(world(line, 3.0)[0], 50.0, delta=0.01)

    def test_jump_at_boundary(self):
        layer = {'moveXEvents': [ev(0.0, 0.0, 0, 4), ev(300.0, 300.0, 4, 8, easing=5)]}
        chart, _, _ = convert(mk_rpe([mk_line([layer])]))
        line = chart.judge_lines[0]
        self.assertAlmostEqual(world(line, 1.999)[0], 0.0, delta=0.01)
        self.assertAlmostEqual(world(line, 2.001)[0], 300.0, delta=0.01)


if __name__ == '__main__':
    unittest.main()
