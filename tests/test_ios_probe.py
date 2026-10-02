"""iOS真机探针(tools/ios_probe.py)的主机端逻辑测试。

探针要到真机上才能出数字, 但"怎么从页面上报的事件算出结论"这部分必须先测对, 否则到了真机上
分不清是设备的问题还是探针算错了。这里用一个"假设备"代替iPhone: 它按 algo/ios_actions.replay_actions
记录的WDA语义回放actions, 并且能注入已知的抖动/起点延迟/触点上限/最短接触/坐标偏差, 把页面事件直接写进
ProbeState; 再检查探针算出来的数字是否等于注入的值。另外起一个假WDA HTTP服务测 Wda 客户端。
页面JS用node做语法检查(没有node就跳过)。
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import ios_probe as P  # noqa: E402
from algo.algo_base import TouchAction  # noqa: E402
from algo.ios_actions import replay_actions  # noqa: E402

HOST_PAGE_OFFSET = 5000.0          # 电脑时钟 = 页面时钟 + 5000ms(假设备用)
HELLO = {'w': 852.0, 'h': 393.0, 'dpr': 3.0, 'sw': 393, 'sh': 852, 'orientation': 'landscape-primary',
         'insets': {'top': 0, 'right': 47, 'bottom': 21, 'left': 47}, 'ua': 'fake'}


class FakeDevice:
    """按已知规则回放actions的假iPhone。事件直接写进 state(就像页面上报的)。"""

    def __init__(self, state, jitter_sd=0.0, start_latency=120.0, start_sd=0.0, max_touches=99, min_contact=0.0,
                 bias=(0.0, 0.0), seed=1, abort_at_ms=None):
        self.state = state
        self.abort_at_ms = abort_at_ms             # 设备在回放开始后这么久被中止: 之后的事件没有, 按着的触点在此刻抬起
        self.jitter_sd, self.start_latency, self.start_sd = jitter_sd, start_latency, start_sd
        self.max_touches, self.min_contact, self.bias = max_touches, min_contact, bias
        self.rng = random.Random(seed)
        self.seq = 0
        self.pointer_id = 100
        self.session = 'S'

    # ---- 假页面的ping: 往返10ms、来回对称
    def _pings(self):
        out = []
        for _ in range(20):
            host = P.now_ms()
            mid = host - HOST_PAGE_OFFSET
            out.append([mid - 5.0, mid + 5.0, host])
        self.state.add({'pings': out})

    def actions(self, body):
        t_send = P.now_ms()
        self._pings()
        rel = replay_actions(body)                         # {ms: [VirtualTouchEvent]} (pointer=源序号)
        lives, cur = {}, {}
        for ms in sorted(rel):
            for e in rel[ms]:
                if e.action == TouchAction.DOWN:
                    cur[e.pointer] = {'down': ms, 'up': None, 'ev': [(ms, 'down', e.pos)]}
                    lives[(e.pointer, ms)] = cur[e.pointer]
                elif e.action == TouchAction.MOVE and e.pointer in cur:
                    cur[e.pointer]['ev'].append((ms, 'move', e.pos))
                elif e.action == TouchAction.UP and e.pointer in cur:
                    cur[e.pointer]['ev'].append((ms, 'up', e.pos))
                    cur[e.pointer]['up'] = ms
                    del cur[e.pointer]
        if self.abort_at_ms is not None:
            cut = {}
            for key, lf in lives.items():
                if lf['down'] > self.abort_at_ms:
                    continue
                ev = [e for e in lf['ev'] if e[0] <= self.abort_at_ms]
                if lf['up'] is None or lf['up'] > self.abort_at_ms:
                    ev = [e for e in ev if e[1] != 'up'] + [(self.abort_at_ms, 'up', ev[-1][2])]
                    lf = dict(lf, up=self.abort_at_ms)
                cut[key] = dict(lf, ev=ev)
            lives = cut
        kept, active_until = [], []
        for lf in sorted(lives.values(), key=lambda v: v['down']):
            up = lf['up'] if lf['up'] is not None else lf['down']
            if up - lf['down'] < self.min_contact:
                continue                                   # 接触太短: 整条丢掉
            active_until = [u for u in active_until if u > lf['down']]
            if len(active_until) >= self.max_touches:
                continue                                   # 超过触点上限: 这个触点没有
            active_until.append(up)
            kept.append(lf)
        start = self.start_latency + self.rng.gauss(0, self.start_sd)
        raw = []
        for lf in kept:
            self.pointer_id += 1
            for ms, kind, pos in lf['ev']:
                raw.append((ms + start + self.rng.gauss(0, self.jitter_sd), kind, pos, self.pointer_id))
        raw.sort(key=lambda r: r[0])
        events, active = [], set()
        for t, kind, pos, pid in raw:
            if kind == 'down':
                active.add(pid)
            elif kind == 'up':
                active.discard(pid)
            page_t = t_send + t - HOST_PAGE_OFFSET
            events.append({'t': page_t, 'now': page_t + 0.3, 'type': kind, 'id': pid, 'x': pos[0] + self.bias[0],
                           'y': pos[1] + self.bias[1], 'n': len(active), 'co': 1, 's': self.seq})
            self.seq += 1
        self.state.add({'events': events})
        dur = max((ms for ms in rel), default=0)
        return t_send, t_send + start + dur + 5.0, {'value': None}

    def window_size(self):
        return {'width': HELLO['w'], 'height': HELLO['h']}


def make_runner(**kw):
    state = P.ProbeState()
    state.add({'hello': HELLO})
    dev = FakeDevice(state, **kw)
    return P.Runner(dev, state, flush_wait=0.0, settle_scale=0.0), state, dev


class StatsTest(unittest.TestCase):
    def test_stats_and_fit(self):
        s = P.stats([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual((s['n'], s['min'], s['max'], s['p50']), (5, 1.0, 100.0, 3.0))
        self.assertAlmostEqual(s['mean'], 22.0)
        self.assertEqual(P.stats([]), {'n': 0})
        a, b = P.linear_fit([0, 1, 2, 3], [5, 7, 9, 11])
        self.assertAlmostEqual(a, 2.0)
        self.assertAlmostEqual(b, 5.0)
        self.assertEqual(P.linear_fit([1.0], [2.0]), (1.0, 0.0))

    def test_clock_offset_uses_the_shortest_round_trips(self):
        rng = random.Random(3)
        pings = []
        for _ in range(100):
            out_d, back_d = rng.uniform(2, 40), rng.uniform(2, 40)           # 来回延迟不对称且抖动
            host = 1000.0 + out_d                                              # 服务器处理时刻(电脑时钟)
            t1 = host - HOST_PAGE_OFFSET - out_d
            pings.append((t1, t1 + out_d + back_d, host))
        sync = P.clock_offset(pings)
        self.assertLess(abs(sync.offset - HOST_PAGE_OFFSET), sync.rtt_min / 2 + 6.0)   # 误差不超过 ±RTT/2 量级
        self.assertLess(sync.rtt_min, 15.0)
        self.assertIsNone(P.clock_offset([]))

    def test_event_time_falls_back_to_now_when_timestamp_is_absurd(self):
        self.assertEqual(P.event_time({'t': 123.0, 'now': 125.0}), 123.0)
        self.assertEqual(P.event_time({'t': 1.7e12, 'now': 125.0}), 125.0)          # 老WebKit给的是epoch时间
        self.assertEqual(P.event_time({'now': 9.0}), 9.0)

    def test_pairs_and_concurrency(self):
        ev = [{'t': 0, 'now': 0, 'type': 'down', 'id': 1}, {'t': 5, 'now': 5, 'type': 'down', 'id': 2},
              {'t': 10, 'now': 10, 'type': 'up', 'id': 1}, {'t': 12, 'now': 12, 'type': 'cancel', 'id': 2},
              {'t': 20, 'now': 20, 'type': 'down', 'id': 3}]
        pairs = P.pairs_by_pointer(ev)
        self.assertEqual([(d['id'], None if u is None else u['type']) for d, u in pairs],
                         [(1, 'up'), (2, 'cancel'), (3, None)])
        self.assertEqual(P.max_concurrent(ev), 2)


class ExperimentTest(unittest.TestCase):
    def test_timing_reports_the_injected_jitter(self):
        r, _, _ = make_runner(jitter_sd=0.0)
        clean = P.exp_timing(r)
        self.assertEqual(clean['delivered'], clean['planned'])
        self.assertLess(clean['err']['std'], 0.7)               # 回放不加抖动: 误差只有毫秒取整
        self.assertAlmostEqual(clean['slope'], 1.0, places=3)
        r, _, _ = make_runner(jitter_sd=4.0)
        noisy = P.exp_timing(r, n=120)
        self.assertGreater(noisy['err']['std'], 3.0)             # 两个事件相减, 标准差约 4*sqrt(2)
        self.assertLess(noisy['err']['std'], 9.0)

    def test_start_latency_matches_the_injected_value(self):
        r, _, _ = make_runner(start_latency=120.0, start_sd=8.0)
        res = P.exp_start(r, repeats=30, gap_s=0.0)
        lat = res['latency']
        self.assertEqual(lat['n'], 30)
        self.assertLess(abs(lat['mean'] - 120.0), 8.0)
        self.assertGreater(lat['std'], 3.0)
        self.assertLess(lat['std'], 14.0)
        self.assertLess(res['sync']['rtt_min'], 11.0)

    def test_start_with_a_big_request_body(self):
        r, _, _ = make_runner(start_latency=90.0)
        res = P.exp_start(r, repeats=3, big=50, gap_s=0.0)
        self.assertEqual(res['big'], 50)
        self.assertLess(abs(res['latency']['mean'] - 90.0), 6.0)

    def test_touch_limit_is_found(self):
        r, _, _ = make_runner(max_touches=5)
        rows = {row['fingers']: row for row in P.exp_touches(r)['rows']}
        for m in (1, 2, 3, 4, 5):
            self.assertEqual((rows[m]['downs'], rows[m]['max_concurrent']), (m, m))
        for m in (6, 8, 12):
            self.assertEqual((rows[m]['downs'], rows[m]['max_concurrent']), (5, 5))
        r, _, _ = make_runner(max_touches=99)
        self.assertEqual({row['fingers']: row['max_concurrent'] for row in P.exp_touches(r)['rows']}[12], 12)

    def test_minimum_contact_is_found(self):
        r, _, _ = make_runner(min_contact=20.0)
        rows = {row['contact_ms']: row for row in P.exp_contact(r)['rows']}
        for t in (0, 2, 5, 10):
            self.assertEqual(rows[t]['delivered'], 0)
        for t in (20, 40, 80, 125):
            self.assertEqual(rows[t]['delivered'], rows[t]['planned'])
            self.assertAlmostEqual(rows[t]['observed']['mean'], t, delta=1.5)

    def test_move_interpolation_and_dwell_then_jump(self):
        r, _, _ = make_runner()
        res = P.exp_move(r)
        self.assertGreater(res['glide']['moves'], 400)                         # 500ms的滑动, 每ms一个点(假设备按线性插值)
        self.assertLess(res['glide']['last_x'] - res['glide']['first_x'], 852.0)
        j = res['dwell_jump']
        self.assertEqual(j['moves_in_dwell'], 0)                                # 停留期不动
        self.assertEqual(j['drift_during_dwell'], 0.0)
        self.assertAlmostEqual(j['jump_dx'], 852.0 - 200.0, delta=0.2)
        self.assertAlmostEqual(j['jump_at_ms'], 300.0, delta=3.0)               # 跳变发生在按下后300ms

    def test_coordinates_bias_is_measured(self):
        r, _, _ = make_runner(bias=(2.0, -1.0))
        res = P.exp_coords(r)
        self.assertEqual(res['delivered'], res['planned'])
        self.assertAlmostEqual(res['dx']['mean'], 2.0, places=3)
        self.assertAlmostEqual(res['dy']['mean'], -1.0, places=3)
        self.assertAlmostEqual(res['max_abs_err'], 2.0, places=3)
        self.assertEqual(res['wda_window'], {'width': 852.0, 'height': 393.0})

    def test_repeat_and_handoff_on_the_reference_semantics(self):
        r, _, _ = make_runner()
        rep = P.exp_repeat(r)
        self.assertEqual((rep['A']['downs'], rep['A']['ups'], rep['B']['downs'], rep['B']['ups']), (2, 2, 2, 2))
        self.assertAlmostEqual(rep['B']['second_x'], 852.0 - 100.0, delta=0.1)
        hand = P.exp_handoff(r)
        self.assertEqual((hand['downs'], hand['ups'], hand['max_concurrent']), (2, 2, 1))     # 同一毫秒接力 = 不同时

    def test_longhold_detects_an_abort(self):
        r, _, _ = make_runner()
        res = P.exp_longhold(r, seconds=10.0)
        self.assertAlmostEqual(res['held_ms'], 10000.0, delta=2.0)
        self.assertFalse(res['aborted_early'])
        r, _, _ = make_runner(abort_at_ms=3000)                  # 设备在3秒时被中止
        res = P.exp_longhold(r, seconds=10.0)
        self.assertAlmostEqual(res['held_ms'], 3000.0, delta=2.0)
        self.assertTrue(res['aborted_early'])
        self.assertNotIn('longhold', P.DEFAULT_EXPERIMENTS)      # 要人配合, 不跑在 all 里
        self.assertIn('longhold', P.EXPERIMENTS)

    def test_report_mentions_every_experiment(self):
        r, state, _ = make_runner(max_touches=5, min_contact=10.0, jitter_sd=2.0)
        res = P.run_experiments(r, list(P.EXPERIMENTS), repeats=3, big=20, log=lambda *_: None)
        text = P.format_report(res, state.hello)
        for key in ('timing', 'start', 'touches', 'contact', 'move', 'coords', 'repeat', 'handoff', 'longhold', '时钟换算'):
            self.assertIn(key, text)
        self.assertIn('实测同时触点上限: **5**', text)
        json.dumps(res)                         # 结果可以直接存成JSON


class FakeWdaServer:
    """假WDA的HTTP服务: 只实现探针用到的几个接口, /actions 交给 FakeDevice"""

    def __init__(self, device):
        outer = self
        self.calls = []

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.calls.append(('GET', self.path))
                if self.path == '/status':
                    self._json(200, {'value': {'ready': True}})
                elif self.path.endswith('/window/size'):
                    self._json(200, {'value': device.window_size()})
                else:
                    self._json(404, {'value': {'error': 'unknown command'}})

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'{}')
                outer.calls.append(('POST', self.path))
                if self.path == '/session':
                    self._json(200, {'value': {'sessionId': 'S'}, 'sessionId': 'S'})
                elif self.path == '/session/S/appium/settings':
                    outer.settings = body['settings']
                    self._json(200, {'value': None})
                elif self.path == '/session/S/actions':
                    device.actions(body)
                    self._json(200, {'value': None})
                else:
                    self._json(404, {'value': {'error': 'unknown command', 'message': 'nope'}})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class WdaClientTest(unittest.TestCase):
    def test_client_talks_to_a_wda_like_server(self):
        state = P.ProbeState()
        state.add({'hello': HELLO})
        srv = FakeWdaServer(FakeDevice(state, start_latency=100.0))
        try:
            wda = P.Wda(srv.url, timeout=10.0)
            self.assertEqual(wda.status()['value']['ready'], True)
            sid = wda.new_session()
            self.assertEqual(sid, 'S')
            wda.settings(waitForIdleTimeout=0, animationCoolOffTimeout=0)
            self.assertEqual(srv.settings, {'waitForIdleTimeout': 0, 'animationCoolOffTimeout': 0})
            self.assertEqual(wda.window_size(), {'width': 852.0, 'height': 393.0})
            res = P.exp_touches(P.Runner(wda, state, flush_wait=0.0, settle_scale=0.0), counts=(2, 7))
            self.assertEqual([row['downs'] for row in res['rows']], [2, 7])        # 假设备没有上限
            with self.assertRaises(P.WdaError) as cm:
                wda.request('POST', '/session/S/nope', {})
            self.assertIn('404', str(cm.exception))
        finally:
            srv.close()

    def test_unreachable_wda_gives_a_helpful_error(self):
        with self.assertRaises(P.WdaError) as cm:
            P.Wda('http://127.0.0.1:9', timeout=1.0).status()
        self.assertIn('WDA', str(cm.exception))


class PageServerTest(unittest.TestCase):
    def setUp(self):
        self.state = P.ProbeState()
        self.server = P.start_server(self.state, 0)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_serves_the_page_pings_and_collects_logs(self):
        page = urllib.request.urlopen(self.base + '/').read().decode('utf-8')
        self.assertIn('phisap iOS probe', page)
        t0 = P.now_ms()
        host = json.loads(urllib.request.urlopen(self.base + '/ping').read())['host']
        self.assertGreaterEqual(host, t0 - 1)
        body = json.dumps({'hello': HELLO, 'events': [{'t': 1.0, 'now': 1.0, 'type': 'down', 'id': 1}],
                           'pings': [[1.0, 2.0, 3.0]]}).encode()
        req = urllib.request.Request(self.base + '/log', data=body, method='POST')
        self.assertEqual(urllib.request.urlopen(req).status, 200)
        self.assertTrue(self.state.hello_event.is_set())
        self.assertEqual(self.state.hello['w'], 852.0)
        self.assertEqual(len(self.state.events), 1)
        self.assertEqual(self.state.mark(), 1)
        self.assertEqual(self.state.since(0)[0]['type'], 'down')

    def test_bad_json_is_rejected(self):
        req = urllib.request.Request(self.base + '/log', data=b'{not json', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)


class PageScriptTest(unittest.TestCase):
    def setUp(self):
        with open(P.PAGE_PATH, encoding='utf-8') as f:
            self.html = f.read()

    def test_page_uses_the_endpoints_the_server_serves(self):
        for needle in ("'/log'", "'/ping", 'pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'timeStamp'):
            self.assertIn(needle, self.html)

    @unittest.skipUnless(shutil.which('node'), '没有node')
    def test_page_script_has_valid_syntax(self):
        js = re.search(r'<script>(.*?)</script>', self.html, re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'page.js')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(js)
            out = subprocess.run(['node', '--check', path], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == '__main__':
    unittest.main()
