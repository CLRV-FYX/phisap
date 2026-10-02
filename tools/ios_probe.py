"""iOS真机探针: 测出离线分析(tools/ios_sim.py)测不到的、决定方案成败的几个数字。

整套方案能不能成, 取决于WDA批量回放在真机上的这几件事(都没有公开数据, 必须实测, 见 docs/ios_feasibility.md):
  timing   同一次回放里, 事件之间的时间间隔准不准(抖动标准差/最大偏差)
  start    从发出请求到第一个触点出现的延迟及其波动(决定"起点同步"的难度)
  touches  同时能按住几个触点(iPhone系统对真手指上限是5, 对注入的触点是否一样?)
  contact  最短接触时间(按下到抬起多短会被丢掉)
  move     pointerMove 的 duration 内系统是否插值、插多密; 以及"停在原地再瞬移"能不能做到
  coords   坐标是否精确(点 vs 像素、安全区、横屏)
  repeat   同一个输入源里连续按两次(WDA源码里有一处可疑的写法)能不能都送达
  handoff  一个触点抬起的同一毫秒另一个按下(接力), 会不会被当成同时、会不会丢
  longhold 一个手指按住10秒, 配合手动 DELETE 会话, 测"能不能中止在播的记录"(要人配合, 不在 all 里)

做法: 电脑上起一个小HTTP服务, iPhone的Safari打开探针页(tools/ios_probe.html), 页面给每个触点事件
打页面时间戳并回传; 电脑通过WDA(http://127.0.0.1:8100, 用pymobiledevice3或go-ios转发)下发预排好的
W3C actions, 再对比"计划"与"页面实际收到"。页面时钟和电脑时钟的换算用往返ping(NTP式取最小RTT)。
Safari收到的触点和游戏(Unity)收到的触点走的是同一条系统通道(UIKit touches), 所以数字有参考价值,
但最终仍以Phigros里的实测为准。

用法:
    python tools/ios_probe.py run --wda http://127.0.0.1:8100            # 跑全部实验(约3分钟)
    python tools/ios_probe.py run --wda ... --experiments timing,touches   # 只跑其中几个
    python tools/ios_probe.py serve                                       # 只开页面服务(手动调试)
    python tools/ios_probe.py report probe_result.json                    # 重新生成报告
准备: iPhone 已装好并能跑 WebDriverAgent(详见 docs/ios_feasibility.md), 电脑和iPhone在同一个局域网,
iPhone设置里把自动锁定改成永不, 探针页保持在Safari前台。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import statistics
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(HERE, 'ios_probe.html')


def now_ms() -> float:
    return time.perf_counter() * 1000.0


# ---------------------------------------------------------------- 统计

def stats(xs: list[float]) -> dict:
    if not xs:
        return {'n': 0}
    xs = sorted(xs)

    def pct(p: float) -> float:
        return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]

    return {'n': len(xs), 'mean': statistics.fmean(xs), 'std': statistics.pstdev(xs) if len(xs) > 1 else 0.0,
            'min': xs[0], 'max': xs[-1], 'p50': pct(0.5), 'p95': pct(0.95), 'p99': pct(0.99)}


def fmt(d: dict, unit: str = 'ms') -> str:
    if not d.get('n'):
        return '无数据'
    return (f"n={d['n']} 均值{d['mean']:+.1f}{unit} 标准差{d['std']:.1f}{unit} 最小{d['min']:+.1f} 最大{d['max']:+.1f} "
            f"p95 {d['p95']:+.1f}")


# ---------------------------------------------------------------- 时钟换算

class ClockSync(NamedTuple):
    offset: float       # 电脑时间 = 页面时间 + offset (ms)
    rtt_min: float      # 最小往返(ms): 换算的不确定度约为 ±rtt_min/2
    spread: float       # 挑出的几个最小RTT样本之间偏移的极差(ms)
    n: int

    def host_time(self, page_ms: float) -> float:
        return page_ms + self.offset


def clock_offset(pings: list, best: int = 8) -> ClockSync | None:
    """pings: [(t1, t2, host)]: 页面发出/收到时刻(页面时钟)和服务器处理时刻(电脑时钟)。
    取往返最短的 best 个, 假设来回对称: offset = host - (t1+t2)/2, 取中位数。"""
    rows = [(t2 - t1, host - (t1 + t2) / 2) for t1, t2, host in pings if t2 >= t1]
    if not rows:
        return None
    rows.sort()
    pick = rows[:best]
    offs = [o for _, o in pick]
    return ClockSync(statistics.median(offs), rows[0][0], max(offs) - min(offs), len(rows))


# ---------------------------------------------------------------- 页面服务

class ProbeState:
    """页面上报的东西。events 里每条: {t, now, type(down/up/move/cancel), id, x, y, n, co, s}"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict] = []
        self.pings: list = []
        self.hello: dict | None = None
        self.hello_event = threading.Event()

    def add(self, payload: dict) -> None:
        with self._lock:
            if payload.get('hello'):
                self.hello = payload['hello']
                self.hello_event.set()
            self.events.extend(payload.get('events') or [])
            self.pings.extend(payload.get('pings') or [])

    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def since(self, mark: int) -> list[dict]:
        with self._lock:
            return list(self.events[mark:])

    def sync(self) -> ClockSync | None:
        with self._lock:
            return clock_offset(self.pings[-200:])


def make_handler(state: ProbeState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        disable_nagle_algorithm = True

        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str = 'application/json') -> None:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split('?')[0]
            if path in ('/', '/index.html'):
                with open(PAGE_PATH, 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            elif path == '/ping':
                self._send(200, json.dumps({'host': now_ms()}).encode())
            else:
                self._send(404, b'{}')

        def do_POST(self) -> None:
            if self.path != '/log':
                self._send(404, b'{}')
                return
            n = int(self.headers.get('Content-Length') or 0)
            try:
                state.add(json.loads(self.rfile.read(n) or b'{}'))
            except ValueError:
                self._send(400, b'{}')
                return
            self._send(200, b'{}')

    return Handler


def start_server(state: ProbeState, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(('0.0.0.0', port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


# ---------------------------------------------------------------- WDA 客户端(只用标准库)

class WdaError(RuntimeError):
    pass


class Wda:
    def __init__(self, base: str = 'http://127.0.0.1:8100', timeout: float = 120.0) -> None:
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.session: str | None = None

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[dict, float, float]:
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        t_send = now_ms()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:300]
            raise WdaError(f'WDA {method} {path} -> HTTP {e.code}: {detail}') from e
        except (urllib.error.URLError, OSError) as e:
            raise WdaError(f'连不上 WDA {self.base}: {e}。先确认 WDA 已启动、端口已转发(见 docs/ios_feasibility.md)') from e
        return out, t_send, now_ms()

    def status(self) -> dict:
        return self.request('GET', '/status')[0]

    def new_session(self, bundle_id: str | None = None) -> str:
        caps = {'bundleId': bundle_id} if bundle_id else {}
        out, _, _ = self.request('POST', '/session', {'capabilities': {'alwaysMatch': caps}, 'desiredCapabilities': caps})
        sid = out.get('sessionId') or (out.get('value') or {}).get('sessionId')
        if not sid:
            raise WdaError(f'WDA没有返回sessionId: {out}')
        self.session = sid
        return sid

    def settings(self, **kw) -> None:
        self.request('POST', f'/session/{self.session}/appium/settings', {'settings': kw})

    def window_size(self) -> dict:
        return self.request('GET', f'/session/{self.session}/window/size')[0].get('value', {})

    def open_url(self, url: str) -> None:
        self.request('POST', f'/session/{self.session}/url', {'url': url})

    def actions(self, body: dict) -> tuple[float, float, dict]:
        out, t_send, t_resp = self.request('POST', f'/session/{self.session}/actions', body)
        return t_send, t_resp, out


# ---------------------------------------------------------------- 实验用的 actions 构造

def tap_source(sid: str, t_ms: int, x: float, y: float, contact_ms: int = 40) -> dict:
    """t_ms 毫秒后在 (x,y) 按下, 按住 contact_ms 后抬起"""
    items: list[dict] = []
    if t_ms > 0:
        items.append({'type': 'pause', 'duration': int(t_ms)})
    items.append({'type': 'pointerMove', 'duration': 0, 'x': round(x, 1), 'y': round(y, 1), 'origin': 'viewport'})
    items.append({'type': 'pointerDown', 'button': 0})
    if contact_ms > 0:
        items.append({'type': 'pause', 'duration': int(contact_ms)})
    items.append({'type': 'pointerUp', 'button': 0})
    return {'type': 'pointer', 'id': sid, 'parameters': {'pointerType': 'touch'}, 'actions': items}


def grid_points(w: float, h: float, n: int, margin: float = 60.0) -> list[tuple[float, float]]:
    cols = 6
    rows = max(1, math.ceil(n / cols))
    return [(margin + (i % cols) * (w - 2 * margin) / max(cols - 1, 1),
             margin + (i // cols % rows) * (h - 2 * margin) / max(rows - 1, 1) if rows > 1 else h / 2)
            for i in range(n)]


def event_time(e: dict) -> float:
    """页面时间戳: 优先用事件自带的 timeStamp(接近系统收到触点的时刻), 异常时退回处理函数里的 now"""
    t, now = e.get('t'), e.get('now')
    if t is None or (now is not None and abs(t - now) > 5000):
        return float(now if now is not None else 0.0)
    return float(t)


def pairs_by_pointer(events: list[dict]) -> list[tuple[dict, dict | None]]:
    """按 pointer id 把 down 和随后的 up/cancel 配对"""
    downs: dict = {}
    out: list[tuple[dict, dict | None]] = []
    for e in sorted(events, key=event_time):
        if e['type'] == 'down':
            downs[e['id']] = e
            out.append((e, None))
        elif e['type'] in ('up', 'cancel') and e['id'] in downs:
            d = downs.pop(e['id'])
            for i in range(len(out) - 1, -1, -1):
                if out[i][0] is d:
                    out[i] = (d, e)
                    break
    return out


def max_concurrent(events: list[dict]) -> int:
    cur, peak = set(), 0
    for e in sorted(events, key=event_time):
        if e['type'] == 'down':
            cur.add(e['id'])
        elif e['type'] in ('up', 'cancel'):
            cur.discard(e['id'])
        peak = max(peak, len(cur))
    return peak


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """最小二乘 y = a*x + b, 返回 (a, b); 样本太少返回 (1, 0)"""
    n = len(xs)
    if n < 3:
        return 1.0, 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 1.0, 0.0
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return a, my - a * mx


# ---------------------------------------------------------------- 实验

class Runner:
    def __init__(self, wda, state: ProbeState, flush_wait: float = 0.7, settle_scale: float = 1.0) -> None:
        self.wda, self.state, self.flush_wait, self.settle_scale = wda, state, flush_wait, settle_scale

    def settle(self, seconds: float) -> None:
        """两次回放之间等一等, 让设备/页面静下来(测试里 settle_scale=0 跳过)"""
        time.sleep(seconds * self.settle_scale)

    def play(self, sources: list[dict]):
        """下发一次actions(同步阻塞到回放结束), 返回 (t_send, t_resp, 页面事件)"""
        mark = self.state.mark()
        t_send, t_resp, _ = self.wda.actions({'actions': sources})
        time.sleep(self.flush_wait)               # 等页面把最后一批事件回传
        return t_send, t_resp, self.state.since(mark)

    def viewport(self) -> tuple[float, float]:
        h = self.state.hello or {}
        return float(h.get('w') or 800), float(h.get('h') or 400)


def exp_timing(r: Runner, n: int = 40, spacing: int = 100, contact: int = 40) -> dict:
    """一次回放里 n 个tap, 计划间隔 spacing ms: 观察间隔误差"""
    w, h = r.viewport()
    pts = grid_points(w, h, n)
    srcs = [tap_source(f't{i}', 300 + i * spacing, x, y, contact) for i, (x, y) in enumerate(pts)]
    _, _, ev = r.play(srcs)
    downs = sorted((e for e in ev if e['type'] == 'down'), key=event_time)
    obs = [event_time(e) for e in downs]
    res = {'planned': n, 'delivered': len(downs)}
    if len(obs) >= 2:
        rel = [o - obs[0] for o in obs]
        plan = [i * spacing for i in range(len(obs))]
        errs = [a - b for a, b in zip(rel, plan)]
        slope, _ = linear_fit(plan, rel)
        res.update(err=stats(errs), slope=slope,
                   gaps=stats([b - a for a, b in zip(obs, obs[1:])]))
    return res


def exp_start(r: Runner, repeats: int = 20, big: int = 0, gap_s: float = 1.2) -> dict:
    """请求发出 -> 第一个触点出现 的延迟(需要页面时钟换算, 不确定度见 sync)。big>0: 请求里带 big 个别的tap(请求体变大)"""
    lat, rtt = [], []
    w, h = r.viewport()
    for k in range(repeats):
        srcs = [tap_source('first', 0, w / 2, h / 2, 40)]
        if big:
            pts = grid_points(w, h, big)
            srcs += [tap_source(f'b{i}', 300 + i * 4, x, y, 20) for i, (x, y) in enumerate(pts)]
        t_send, t_resp, ev = r.play(srcs)
        downs = sorted((e for e in ev if e['type'] == 'down'), key=event_time)
        sync = r.state.sync()
        if downs and sync:
            lat.append(sync.host_time(event_time(downs[0])) - t_send)
        rtt.append(t_resp - t_send)
        r.settle(gap_s)
    sync = r.state.sync()
    return {'repeats': repeats, 'big': big, 'latency': stats(lat), 'call_rtt': stats(rtt),
            'sync': None if sync is None else {'rtt_min': sync.rtt_min, 'spread': sync.spread, 'n': sync.n}}


def exp_touches(r: Runner, counts=(1, 2, 3, 4, 5, 6, 7, 8, 10, 12), hold_ms: int = 700) -> dict:
    w, h = r.viewport()
    rows = []
    for m in counts:
        pts = grid_points(w, h, m)
        srcs = [tap_source(f'f{i}', 150, x, y, hold_ms) for i, (x, y) in enumerate(pts)]
        _, _, ev = r.play(srcs)
        rows.append({'fingers': m, 'downs': sum(e['type'] == 'down' for e in ev),
                     'ups': sum(e['type'] == 'up' for e in ev), 'cancels': sum(e['type'] == 'cancel' for e in ev),
                     'max_concurrent': max_concurrent(ev)})
        r.settle(0.8)
    return {'rows': rows}


def exp_contact(r: Runner, floors=(0, 2, 5, 10, 20, 40, 80, 125), n: int = 20, spacing: int = 160) -> dict:
    w, h = r.viewport()
    rows = []
    for t in floors:
        pts = grid_points(w, h, n)
        srcs = [tap_source(f'c{i}', 200 + i * spacing, x, y, t) for i, (x, y) in enumerate(pts)]
        _, _, ev = r.play(srcs)
        pr = pairs_by_pointer(ev)
        done = [(d, u) for d, u in pr if u is not None and u['type'] == 'up']
        durs = [event_time(u) - event_time(d) for d, u in done]
        rows.append({'contact_ms': t, 'planned': n, 'downs': len(pr), 'delivered': len(done),
                     'observed': stats(durs)})
        r.settle(0.6)
    return {'rows': rows}


def exp_move(r: Runner) -> dict:
    """A: 单次 pointerMove(duration=500) 的插值; B: 先停300ms再1ms跳过去"""
    w, h = r.viewport()
    y = h / 2
    a = {'type': 'pointer', 'id': 'm', 'parameters': {'pointerType': 'touch'}, 'actions': [
        {'type': 'pointerMove', 'duration': 0, 'x': 80.0, 'y': y, 'origin': 'viewport'},
        {'type': 'pointerDown', 'button': 0},
        {'type': 'pointerMove', 'duration': 500, 'x': round(w - 80, 1), 'y': y, 'origin': 'viewport'},
        {'type': 'pointerUp', 'button': 0}]}
    _, _, ev = r.play([a])
    moves = sorted((e for e in ev if e['type'] == 'move'), key=event_time)
    ts = [event_time(e) for e in moves]
    glide = {'moves': len(moves), 'coalesced': sum(e.get('co', 1) for e in moves),
             'gap': stats([b - a_ for a_, b in zip(ts, ts[1:])]),
             'first_x': moves[0]['x'] if moves else None, 'last_x': moves[-1]['x'] if moves else None}
    r.settle(0.8)
    b = {'type': 'pointer', 'id': 'j', 'parameters': {'pointerType': 'touch'}, 'actions': [
        {'type': 'pointerMove', 'duration': 0, 'x': 100.0, 'y': y, 'origin': 'viewport'},
        {'type': 'pointerDown', 'button': 0},
        {'type': 'pause', 'duration': 300},
        {'type': 'pointerMove', 'duration': 1, 'x': round(w - 100, 1), 'y': y, 'origin': 'viewport'},
        {'type': 'pause', 'duration': 200},
        {'type': 'pointerUp', 'button': 0}]}
    _, _, ev = r.play([b])
    down = next((e for e in ev if e['type'] == 'down'), None)
    jump = {'moves': 0, 'drift_during_dwell': None, 'jump_dx': None}
    if down:
        t0 = event_time(down)
        mv = sorted((e for e in ev if e['type'] == 'move'), key=event_time)
        dwell = [e for e in mv if event_time(e) - t0 < 270]
        jump = {'moves': len(mv), 'moves_in_dwell': len(dwell),
                'drift_during_dwell': max((abs(e['x'] - down['x']) for e in dwell), default=0.0),
                'jump_dx': (mv[-1]['x'] - down['x']) if mv else None,
                'jump_at_ms': (event_time(mv[-1]) - t0) if mv else None}
    return {'glide': glide, 'dwell_jump': jump}


def exp_coords(r: Runner) -> dict:
    w, h = r.viewport()
    pts = [(x, y) for y in (60.0, h / 2, h - 60.0) for x in (60.0, w / 4, w / 2, 3 * w / 4, w - 60.0)]
    srcs = [tap_source(f'g{i}', 200 + i * 150, x, y, 40) for i, (x, y) in enumerate(pts)]
    _, _, ev = r.play(srcs)
    downs = sorted((e for e in ev if e['type'] == 'down'), key=event_time)
    dx = [d['x'] - p[0] for d, p in zip(downs, pts)]
    dy = [d['y'] - p[1] for d, p in zip(downs, pts)]
    ws = {}
    try:
        ws = r.wda.window_size()
    except Exception:       # noqa: BLE001  (WdaError 或假设备没有这个接口)
        pass
    return {'planned': len(pts), 'delivered': len(downs), 'dx': stats(dx), 'dy': stats(dy),
            'max_abs_err': max([abs(v) for v in dx + dy], default=None), 'wda_window': ws,
            'page': {k: (r.state.hello or {}).get(k) for k in ('w', 'h', 'dpr', 'sw', 'sh', 'orientation', 'insets')}}


def exp_repeat(r: Runner) -> dict:
    """同一个输入源连续按两次: A=pause后直接pointerDown(不带pointerMove); B=中间带一个pointerMove(WDA源码里可疑的写法)"""
    w, h = r.viewport()

    def src(variant: str) -> dict:
        items = [{'type': 'pointerMove', 'duration': 0, 'x': 100.0, 'y': h / 2, 'origin': 'viewport'},
                 {'type': 'pointerDown', 'button': 0}, {'type': 'pause', 'duration': 40}, {'type': 'pointerUp', 'button': 0},
                 {'type': 'pause', 'duration': 150}]
        if variant == 'B':
            items.append({'type': 'pointerMove', 'duration': 0, 'x': w - 100.0, 'y': h / 2, 'origin': 'viewport'})
        items += [{'type': 'pointerDown', 'button': 0}, {'type': 'pause', 'duration': 40}, {'type': 'pointerUp', 'button': 0}]
        return {'type': 'pointer', 'id': 'r', 'parameters': {'pointerType': 'touch'}, 'actions': items}

    out = {}
    for variant in ('A', 'B'):
        _, _, ev = r.play([src(variant)])
        downs = sorted((e for e in ev if e['type'] == 'down'), key=event_time)
        out[variant] = {'downs': len(downs), 'ups': sum(e['type'] == 'up' for e in ev),
                        'second_x': downs[1]['x'] if len(downs) > 1 else None}
        r.settle(0.8)
    return out


def exp_handoff(r: Runner) -> dict:
    """触点A在 t=300 抬起、触点B同一毫秒按下"""
    w, h = r.viewport()
    a = tap_source('a', 200, w / 3, h / 2, 100)
    b = tap_source('b', 300, 2 * w / 3, h / 2, 100)
    _, _, ev = r.play([a, b])
    order = [(e['type'], round(event_time(e), 1)) for e in sorted(ev, key=event_time) if e['type'] in ('down', 'up')]
    return {'downs': sum(e['type'] == 'down' for e in ev), 'ups': sum(e['type'] == 'up' for e in ev),
            'max_concurrent': max_concurrent(ev), 'order': order}


def exp_longhold(r: Runner, seconds: float = 10.0) -> dict:
    """一个手指按住 seconds 秒。要手动配合: 按住期间在另一个终端执行
    curl -X DELETE <WDA>/session/<会话id>, 看手指是不是提前抬起、这次请求是不是提前返回(测"能不能中止")"""
    w, h = r.viewport()
    sid = getattr(r.wda, 'session', None)
    print(f'[probe] longhold: 手指将按住 {seconds:.0f} 秒。要测中止, 现在在另一个终端执行: '
          f'curl -X DELETE {getattr(r.wda, "base", "http://127.0.0.1:8100")}/session/{sid}', flush=True)
    t_send, t_resp, ev = r.play([tap_source('long', 0, w / 2, h / 2, int(seconds * 1000))])
    pr = pairs_by_pointer(ev)
    held = [event_time(u) - event_time(d) for d, u in pr if u is not None]
    return {'planned_ms': seconds * 1000, 'session': sid, 'call_ms': t_resp - t_send,
            'downs': len(pr), 'held_ms': held[0] if held else None,
            'aborted_early': bool(held) and held[0] < seconds * 1000 - 500}


EXPERIMENTS: dict[str, Callable] = {'timing': exp_timing, 'start': exp_start, 'touches': exp_touches,
                                    'contact': exp_contact, 'move': exp_move, 'coords': exp_coords,
                                    'repeat': exp_repeat, 'handoff': exp_handoff, 'longhold': exp_longhold}
DEFAULT_EXPERIMENTS = [k for k in EXPERIMENTS if k != 'longhold']       # longhold 要人配合, 不放进 all


def run_experiments(r: Runner, names: list[str], repeats: int = 20, big: int = 400, log=print) -> dict:
    out: dict = {}
    for name in names:
        log(f'[probe] 实验 {name} ...')
        if name == 'start':
            out['start'] = exp_start(r, repeats)
            out['start_big'] = exp_start(r, max(5, repeats // 2), big=big)
        else:
            out[name] = EXPERIMENTS[name](r)
    out['sync'] = None if r.state.sync() is None else dict(r.state.sync()._asdict())
    return out


# ---------------------------------------------------------------- 报告

def format_report(res: dict, hello: dict | None = None) -> str:
    L: list[str] = ['# iOS 真机探针报告', '']
    if hello:
        L.append(f"设备页面: {hello.get('w')}x{hello.get('h')} pt @{hello.get('dpr')}x, 方向 {hello.get('orientation')}, "
                 f"安全区 {hello.get('insets')}, UA: {hello.get('ua', '')[:90]}")
        L.append('')
    s = res.get('sync')
    if s:
        L.append(f"时钟换算: 往返最小 {s['rtt_min']:.1f}ms(换算不确定度约 ±{s['rtt_min'] / 2:.1f}ms), "
                 f"样本数 {s['n']}, 最小RTT样本间偏移极差 {s['spread']:.1f}ms")
        L.append('')
    if 'timing' in res:
        t = res['timing']
        L += ['## timing: 一次回放里的事件间隔', f"- 送达 {t['delivered']}/{t['planned']}"]
        if 'err' in t:
            L += [f"- 间隔误差(观察-计划): {fmt(t['err'])}", f"- 时间尺度(斜率, 应≈1): {t['slope']:.5f}",
                  f"- 相邻事件间隔: {fmt(t['gaps'])}"]
        L.append('')
    for key, title in (('start', 'start: 请求->第一个触点(小请求体)'), ('start_big', 'start: 请求->第一个触点(大请求体)')):
        if key in res:
            t = res[key]
            L += [f'## {title}', f"- 延迟: {fmt(t['latency'])}", f"- 整个调用耗时(含回放): {fmt(t['call_rtt'])}", '']
    if 'touches' in res:
        L += ['## touches: 同时触点数', '| 手指数 | 按下 | 抬起 | 取消 | 页面看到的同时触点峰值 |', '|---|---|---|---|---|']
        for row in res['touches']['rows']:
            L.append(f"| {row['fingers']} | {row['downs']} | {row['ups']} | {row['cancels']} | {row['max_concurrent']} |")
        peak = max((row['max_concurrent'] for row in res['touches']['rows']), default=0)
        L += [f'- 实测同时触点上限: **{peak}**', '']
    if 'contact' in res:
        L += ['## contact: 最短接触时间', '| 计划接触ms | 计划次数 | 送达(按下+抬起) | 实测接触时长 |', '|---|---|---|---|']
        for row in res['contact']['rows']:
            o = row['observed']
            L.append(f"| {row['contact_ms']} | {row['planned']} | {row['delivered']} | "
                     + (f"均值{o['mean']:.1f} 最小{o['min']:.1f} 最大{o['max']:.1f}" if o.get('n') else '无') + ' |')
        L.append('')
    if 'move' in res:
        g, j = res['move']['glide'], res['move']['dwell_jump']
        L += ['## move', f"- pointerMove(duration=500): 页面收到 {g['moves']} 个move(合并前 {g['coalesced']}), 间隔 {fmt(g['gap'])}",
              f"- 先停300ms再1ms跳: 停留期内move数 {j.get('moves_in_dwell')}, 停留期漂移 {j.get('drift_during_dwell')}, "
              f"跳变 dx={j.get('jump_dx')} 发生在按下后 {j.get('jump_at_ms')}ms", '']
    if 'coords' in res:
        c = res['coords']
        L += ['## coords', f"- 送达 {c['delivered']}/{c['planned']}, x误差 {fmt(c['dx'], 'pt')}, y误差 {fmt(c['dy'], 'pt')}, "
              f"最大绝对误差 {c['max_abs_err']}", f"- WDA窗口尺寸 {c['wda_window']}, 页面 {c['page']}", '']
    if 'repeat' in res:
        L += ['## repeat: 同一输入源按两次',
              f"- A(中间只有pause): 按下 {res['repeat']['A']['downs']} 次, 抬起 {res['repeat']['A']['ups']} 次",
              f"- B(中间带pointerMove): 按下 {res['repeat']['B']['downs']} 次, 抬起 {res['repeat']['B']['ups']} 次, "
              f"第二次x={res['repeat']['B']['second_x']}", '']
    if 'longhold' in res:
        lh = res['longhold']
        L += ['## longhold: 能不能中止在播的记录',
              f"- 计划按住 {lh['planned_ms'] / 1000:.0f}s, 实际按了 {None if lh['held_ms'] is None else round(lh['held_ms'] / 1000, 2)}s, "
              f"这次请求耗时 {lh['call_ms'] / 1000:.1f}s", f"- 提前抬起(中止生效): **{'是' if lh['aborted_early'] else '否/没有配合操作'}**", '']
    if 'handoff' in res:
        h = res['handoff']
        L += ['## handoff: 同一毫秒一个抬起一个按下',
              f"- 按下 {h['downs']} 抬起 {h['ups']}, 同时触点峰值 {h['max_concurrent']}(1=干净接力, 2=被当成同时)", f"- 顺序 {h['order']}", '']
    return '\n'.join(L)


# ---------------------------------------------------------------- 命令行

def cmd_serve(a) -> None:
    state = ProbeState()
    start_server(state, a.port)
    print(f'探针页: http://{lan_ip()}:{a.port}/  (在iPhone的Safari里打开)')
    try:
        while True:
            time.sleep(2)
            h = state.hello
            print(f'  页面{"已连接" if h else "未连接"}, 事件 {len(state.events)}, ping {len(state.pings)}')
    except KeyboardInterrupt:
        pass


def cmd_run(a) -> None:
    state = ProbeState()
    start_server(state, a.port)
    url = f'http://{lan_ip()}:{a.port}/'
    wda = Wda(a.wda)
    print(f'[probe] WDA状态: {json.dumps(wda.status().get("value", {}), ensure_ascii=False)[:200]}')
    wda.new_session()
    try:
        wda.open_url(url)
    except WdaError:
        pass
    print(f'[probe] 请在iPhone的Safari里打开(并保持在前台): {url}')
    if not state.hello_event.wait(a.wait):
        raise SystemExit('等不到探针页连接: 确认iPhone和电脑在同一局域网、电脑防火墙放行端口, 页面在前台')
    wda.settings(waitForIdleTimeout=0, animationCoolOffTimeout=0)
    time.sleep(2.0)               # 让页面做完第一轮ping
    names = list(DEFAULT_EXPERIMENTS) if a.experiments == 'all' else a.experiments.split(',')
    runner = Runner(wda, state)
    res = run_experiments(runner, names, a.repeat, a.big)
    res['hello'] = state.hello
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(format_report(res, state.hello))
    print(f'\n[probe] 原始结果已保存到 {a.out}')


def cmd_report(a) -> None:
    with open(a.file, encoding='utf-8') as f:
        res = json.load(f)
    print(format_report(res, res.get('hello')))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sp = sub.add_parser('serve')
    sp.add_argument('--port', type=int, default=8777)
    sp = sub.add_parser('run')
    sp.add_argument('--wda', default='http://127.0.0.1:8100')
    sp.add_argument('--port', type=int, default=8777)
    sp.add_argument('--experiments', default='all',
                    help='all(不含longhold) 或逗号分隔: ' + ','.join(EXPERIMENTS))
    sp.add_argument('--repeat', type=int, default=20, help='start实验的重复次数')
    sp.add_argument('--big', type=int, default=400, help='start实验"大请求体"里额外的tap数')
    sp.add_argument('--wait', type=float, default=180.0, help='等探针页连接的秒数')
    sp.add_argument('--out', default='probe_result.json')
    sp = sub.add_parser('report')
    sp.add_argument('file')
    a = ap.parse_args(argv)
    {'serve': cmd_serve, 'run': cmd_run, 'report': cmd_report}[a.cmd](a)


if __name__ == '__main__':
    main()
