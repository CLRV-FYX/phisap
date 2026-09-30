import socket
import struct
import subprocess
import threading
import time
import random
import os
import collections

from algo.algo_base import TouchAction
from server_patch import prepare_server, SCRCPY_MAX_POINTERS

# phisap 使用的 scrcpy-server 版本（协议与版本严格对应，server 会校验版本号）
# v4.1 支持 Android 5 ~ Android 16；旧的 v2.0 在 Android 14/15 上会因 SurfaceControl.createDisplay 被移除而崩溃
SCRCPY_VERSION = '4.1'
SCRCPY_SERVER_FILE = f'scrcpy-server-v{SCRCPY_VERSION}'
SCRCPY_SERVER_URL = f'https://github.com/Genymobile/scrcpy/releases/download/v{SCRCPY_VERSION}/{SCRCPY_SERVER_FILE}'

# 视频流帧头/会话包标志位（scrcpy v4 协议，见 scrcpy doc/develop.md "Video and audio"）
_PACKET_FLAG_SESSION = 1 << 63


class ServerDisconnected(ConnectionError):
    pass


def _recv_exact(skt: socket.socket, n: int) -> bytes:
    '''读满n字节；对端关闭时抛出ServerDisconnected（socket.recv可能只返回部分数据）'''
    buf = bytearray()
    while len(buf) < n:
        chunk = skt.recv(n - len(buf))
        if not chunk:
            raise ServerDisconnected('scrcpy-server 已断开连接')
        buf += chunk
    return bytes(buf)


def _skip_exact(skt: socket.socket, n: int) -> None:
    '''丢弃n字节'''
    while n > 0:
        chunk = skt.recv(min(n, 65536))
        if not chunk:
            raise ServerDisconnected('scrcpy-server 已断开连接')
        n -= len(chunk)


def server_file_path(server_dir: str = '.') -> str:
    return os.path.join(server_dir, SCRCPY_SERVER_FILE)


def max_touch_points(server_dir: str = '.') -> int:
    '''本机可用的最大同时触点数: 16(补丁版scrcpy-server, Android上限) 或 10(官方原版)'''
    server_file = server_file_path(server_dir)
    if not os.path.isfile(server_file):
        return SCRCPY_MAX_POINTERS
    return prepare_server(server_file)[1]


class DeviceController:
    # 给标量属性类级默认值, 让两个触控后端(scrcpy / MaaTouch)在类层面就可互换:
    # main.py 里 getattr(controller, 'max_pointers', None) 之类不依赖实例已初始化完成。
    serial: str | None = None
    max_pointers: int = 10
    session_id: str = ''
    device_width: int = 0
    device_height: int = 0
    collector_running: bool = False
    video_socket: socket.socket
    control_socket: socket.socket
    server_process: subprocess.Popen
    streaming_collector: threading.Thread
    control_collector: threading.Thread

    def __init__(self, serial: str | None = None, port: int = 27188, push_server: bool = True, server_dir: str = '.') -> None:
        self.serial = serial
        adb = ('adb',) if serial is None else ('adb', '-s', serial)
        self.session_id = format(random.randint(0, 0x7FFFFFFF), '08x')
        server_file = server_file_path(server_dir)
        if not os.path.isfile(server_file) or os.path.getsize(server_file) == 0:
            raise FileNotFoundError(
                f'未找到 {SCRCPY_SERVER_FILE}（phisap 需要与之严格对应的 scrcpy {SCRCPY_VERSION} 服务端）。\n'
                f'请运行 start.cmd 自动下载，或手动下载后放到 main.py 同目录:\n{SCRCPY_SERVER_URL}'
            )
        def adb_run(*args: str) -> None:
            r = subprocess.run([*adb, *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or '').strip()
                hint = ''
                if 'more than one device' in detail:
                    hint = '\n\n检测到多个设备/模拟器，请先在界面的"设备Serial"中选择要使用的设备。'
                elif 'not found' in detail or 'offline' in detail or 'no devices' in detail:
                    hint = '\n\n设备未连接或已离线，请检查USB调试/模拟器adb连接后点"刷新"。'
                raise RuntimeError(f'adb 命令执行失败: adb {" ".join(args)}\n{detail}{hint}')

        # 推送16触点补丁版(补丁失败时自动退回官方原版/10触点)
        push_file, self.max_pointers = prepare_server(server_file)
        print('[client]', f'scrcpy-server: {os.path.basename(push_file)}, 最多同时 {self.max_pointers} 个触点')
        if push_server:
            adb_run('push', push_file, '/data/local/tmp/scrcpy-server.jar')
        adb_run('reverse', f'localabstract:scrcpy_{self.session_id}', f'tcp:{port}')
        skt = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        skt.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        skt.bind(('localhost', port))
        skt.listen(1)
        command_line = [
            *adb,
            'shell',
            'CLASSPATH=/data/local/tmp/scrcpy-server.jar',
            'app_process',
            '/',
            'com.genymobile.scrcpy.Server',
            SCRCPY_VERSION,
            f'scid={self.session_id}',
            'log_level=info',
            'audio=false',
            'clipboard_autosync=false',
            # phisap 只需要视频流中的屏幕尺寸信息，不需要画面：
            # 降低帧率和码率以减轻设备/模拟器的编码负担，避免影响打歌时序
            'max_fps=20',
            'video_bit_rate=900000',
        ]
        self.server_process = subprocess.Popen(command_line)
        # 由于我们指定了audio=false，所以这只有两个socket
        # 其实本来audio streaming可以用于对齐时钟，不过可惜只支持Android 11及以上
        # 设置超时：scrcpy-server启动失败时不再无限等待（否则界面会卡死）
        skt.settimeout(15)
        try:
            self.video_socket, _ = skt.accept()
            self.control_socket, _ = skt.accept()
        except socket.timeout:
            self.server_process.kill()
            subprocess.run([*adb, 'reverse', '--remove', f'localabstract:scrcpy_{self.session_id}'], capture_output=True)
            raise RuntimeError(
                '等待设备端scrcpy-server连接超时(15秒)。\n'
                '请查看控制台中scrcpy-server的输出信息，并确认设备已解锁、已开启USB调试。'
            )
        finally:
            skt.close()
        # 关闭Nagle算法: 触控消息都很小(32字节), 默认会被攒包/等待ACK后才发出,
        # 可能让个别按下/抬起延迟几十毫秒以上
        try:
            self.control_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.video_socket.settimeout(None)
        self.control_socket.settimeout(None)
        subprocess.run(
            [*adb, 'reverse', '--remove', f'localabstract:scrcpy_{self.session_id}'], capture_output=True
        )  # 移除创建的adb tunnel，我们不再需要它了

        self.collector_running = True
        self.device_width = 0
        self.device_height = 0
        self._size_changed = threading.Event()

        def server_error_hint() -> str:
            time.sleep(0.5)
            code = self.server_process.poll()
            state = f'(scrcpy-server 进程已退出，返回值 {code})' if code is not None else ''
            return (
                f'与设备端 scrcpy-server 的连接中断{state}。\n'
                '请查看控制台中以 [server] 开头的报错信息。\n'
                '常见原因: 设备/模拟器的 Android 版本不受支持、设备锁屏、或同时运行了其他 scrcpy 实例。'
            )

        # ---- 握手：device meta(64字节设备名) + codec id(4字节) + 首个会话包(视频尺寸) ----
        self.video_socket.settimeout(15)
        try:
            device_name = _recv_exact(self.video_socket, 64).rstrip(b'\0').decode('utf-8', 'replace')
            codec_id = _recv_exact(self.video_socket, 4).lstrip(b'\0').decode('ascii', 'replace')
            while True:
                header = _recv_exact(self.video_socket, 12)
                flags = int.from_bytes(header[:8], 'big')
                if flags & _PACKET_FLAG_SESSION:
                    self.device_width = int.from_bytes(header[4:8], 'big')
                    self.device_height = int.from_bytes(header[8:12], 'big')
                    break
                _skip_exact(self.video_socket, int.from_bytes(header[8:12], 'big'))  # 会话包之前的数据包，丢弃
        except (ServerDisconnected, OSError) as e:
            self.close()
            raise RuntimeError(server_error_hint()) from e
        self.video_socket.settimeout(None)

        if not (self.device_width > 0 and self.device_height > 0):
            self.close()
            raise RuntimeError(f'从 scrcpy-server 获取到的屏幕尺寸无效: {self.device_width}x{self.device_height}')

        print('[client]', f'设备: {device_name}, 视频尺寸 = {self.device_width}x{self.device_height}, codec = {codec_id}')

        def video_receiver():
            '''持续读取视频流（必须读走，否则设备端会阻塞），只解析会话包中的尺寸，画面数据直接丢弃。
            设备旋转时会收到新的会话包。'''
            try:
                while self.collector_running:
                    header = _recv_exact(self.video_socket, 12)
                    flags = int.from_bytes(header[:8], 'big')
                    if flags & _PACKET_FLAG_SESSION:
                        w = int.from_bytes(header[4:8], 'big')
                        h = int.from_bytes(header[8:12], 'big')
                        if (w, h) != (self.device_width, self.device_height):
                            print('[client]', f'视频尺寸变化(设备旋转?): {self.device_width}x{self.device_height} -> {w}x{h}')
                            self.device_width, self.device_height = w, h
                            self._size_changed.set()
                    else:
                        plen = int.from_bytes(header[8:12], 'big')
                        self._activity_feed(plen)
                        _skip_exact(self.video_socket, plen)
            except Exception as e:
                if self.collector_running:  # 主动close()时不打印
                    print('[client]', f'视频流中断: {e}')
                self.collector_running = False

        def ctrlmsg_receiver():
            '''读走 scrcpy-server 通过控制通道发来的消息（剪贴板等），phisap 不需要，直接丢弃'''
            try:
                while self.collector_running:
                    if not self.control_socket.recv(4096):
                        break
            except Exception as e:
                if self.collector_running:
                    print('[client]', f'控制通道中断: {e}')
            self.collector_running = False

        self.streaming_collector = threading.Thread(target=video_receiver, daemon=True)
        self.streaming_collector.start()

        self.control_collector = threading.Thread(target=ctrlmsg_receiver, daemon=True)
        self.control_collector.start()

    def touch(self, x: float, y: float, action: TouchAction, pointer_id: int) -> None:
        self.control_socket.sendall(self._pack_touch(x, y, action, pointer_id))

    # ---- 屏幕活动度监视（视觉自动开始）：不看画面内容，只统计每帧字节数的突变 ----
    # Phigros 选曲/准备界面是静态 UI，H.264 编码后帧很小；进入演奏瞬间音符下落+背景流动，
    # 帧体积显著跳增。用 payload 长度的移动平均突变做启发式检测：零解码依赖、CPU 极低、
    # 不占用打歌时的发送时序（只读帧头，不缓冲画面）。
    _ACTIVITY_WIN = 24       # 移动平均窗口(帧数)
    _ACTIVITY_RATIO = 1.8    # 当前帧长 / 近期平均 超过此倍数即视为"界面跳变"
    _ACTIVITY_MIN_BYTES = 3000   # 帧至少这么大才参与判定，避免空帧/掉线误触发

    def start_activity_watch(self, cooldown: float = 2.0) -> threading.Event:
        '''开启"界面跳变"检测，返回一个 Event，检测到跳变时被 set()。
        调用方需自行 wait()。'''
        self._activity_watching = True
        self._activity_cooldown = float(cooldown)
        self._activity_last_fire = 0.0
        self._activity_win = collections.deque(maxlen=self._ACTIVITY_WIN)
        self._activity_event = threading.Event()
        return self._activity_event

    def stop_activity_watch(self) -> None:
        self._activity_watching = False

    def _activity_feed(self, payload_len: int) -> None:
        '''由视频接收线程调用：喂入一帧的字节数，突变则置位事件。'''
        if not getattr(self, '_activity_watching', False):
            return
        now = time.perf_counter()
        if now - self._activity_last_fire < self._activity_cooldown:
            return
        win = self._activity_win
        if payload_len < self._ACTIVITY_MIN_BYTES:
            return
        if len(win) < self._ACTIVITY_WIN // 2:
            win.append(payload_len)
            return
        avg = sum(win) / len(win)
        if avg > 0 and payload_len > avg * self._ACTIVITY_RATIO:
            self._activity_last_fire = now
            print('[client]', f'[视觉触发] 检测到界面跳变: 本帧 {payload_len}B vs 近期均值 {avg:.0f}B')
            self._activity_event.set()
        win.append(payload_len)

    def touch_many(self, events) -> None:
        '''同一时刻的多个事件(需有pos/action/pointer属性)合并成一次发送'''
        if events:
            self.control_socket.sendall(b''.join(self._pack_touch(*e.pos, e.action, e.pointer) for e in events))

    def _pack_touch(self, x: float, y: float, action: TouchAction, pointer_id: int) -> bytes:
        # 坐标系为当前视频尺寸(device_width x device_height)，scrcpy-server 会映射到实际屏幕；
        # 尺寸与 server 当前视频尺寸不一致的事件会被 server 丢弃
        return (
            struct.pack(
                '!bbQiiHHHII',
                2,  # type: SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT
                action.value,
                pointer_id,
                int(round(x)),
                int(round(y)),
                self.device_width,
                self.device_height,
                0xFFFF,  # pressure
                1,  # action_button: AMOTION_EVENT_BUTTON_PRIMARY
                1,  # buttons: AMOTION_EVENT_BUTTON_PRIMARY
            )
        )

    def release_pointers(self, pointer_ids) -> None:
        '''抬起指定的程序触点(停止演奏时用, 防止手指卡在屏幕上)。

        抬起位置用屏幕中心即可: Android 按 pointerId 匹配, 坐标不影响抬起语义。'''
        pids = [p for p in (pointer_ids or []) if p is not None]
        if not pids:
            return
        dw, dh = self.device_width, self.device_height
        pkts = [
            self._pack_touch(dw >> 1, dh >> 1, TouchAction.UP, pid)
            for pid in pids
        ]
        self.control_socket.sendall(b''.join(pkts))

    def reset_all(self) -> None:
        '''scrcpy 后端没有"重置所有触点"的原语, 由调用方维护 active 集合后调 release_pointers。'''
        raise NotImplementedError('scrcpy 后端请使用 release_pointers(active_pids)')

    def tap(self, x: int, y: int, pointer_id: int = 1000, delay: float = 0.1) -> None:
        self.touch(x, y, TouchAction.DOWN, pointer_id)
        time.sleep(delay)
        self.touch(x, y, TouchAction.UP, pointer_id)

    def close(self) -> None:
        '''断开与设备的连接（切换设备时使用）'''
        self.collector_running = False
        for skt in (getattr(self, 'video_socket', None), getattr(self, 'control_socket', None)):
            try:
                skt and skt.close()
            except OSError:
                pass
        try:
            self.server_process.kill()
        except Exception:
            pass

    @staticmethod
    def get_devices() -> list[str]:
        ret, output = subprocess.getstatusoutput('adb devices')
        if ret != 0:
            return []
        devices = []
        for line in output.splitlines():
            # 跳过 '* daemon started' 、'List of devices attached' 、adb版本警告等非设备行
            parts = line.strip().split('\t')
            if len(parts) == 2 and parts[1].strip() == 'device':
                devices.append(parts[0].strip())
        return devices


if __name__ == '__main__':
    print(DeviceController.get_devices())
    controller = DeviceController()
    device_width = controller.device_width
    device_height = controller.device_height

    controller.tap(device_width >> 1, device_height >> 1)
