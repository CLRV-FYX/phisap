"""电脑版导出的计划，手机版要能按同一格式读回来。"""
import io
import json
import unittest

from algo.algo_base import TouchAction, VirtualTouchEvent
from device_plan import load_device_plan, export_device_plan


class DevicePlanTest(unittest.TestCase):
    def test_roundtrip_keeps_order_and_actions(self):
        ans = {
            20: [VirtualTouchEvent((100.4, 200.6), TouchAction.MOVE, 1001)],
            10: [VirtualTouchEvent((640, 360), TouchAction.DOWN, 1000)],
        }
        buf = io.StringIO()
        export_device_plan(ans, buf, name='Chart_AT')
        obj = json.loads(buf.getvalue())
        self.assertEqual(obj['format'], 1)
        self.assertEqual(obj['width'], 1280)
        self.assertEqual(obj['height'], 720)
        self.assertEqual(obj['name'], 'Chart_AT')
        self.assertEqual(obj['events'], [
            [10, TouchAction.DOWN.value, 1000, 640, 360],
            [20, TouchAction.MOVE.value, 1001, 100, 201],
        ])
        again = load_device_plan(io.StringIO(buf.getvalue()))
        self.assertEqual(again['events'], obj['events'])

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            load_device_plan(io.StringIO('{"format": 9, "width": 1280, "height": 720, "events": []}'))


if __name__ == '__main__':
    unittest.main()
