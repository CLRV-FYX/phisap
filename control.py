import socket
import struct
import subprocess
import threading
import time
import random
import os

import av

from algo.algo_base import TouchAction


class DeviceController:
    serial: str | None
    session_id: str
    video_socket: socket.socket
    control_socket: socket.socket
    server_process: subprocess.Popen
    streaming_collector: threading.Thread
    control_collector: threading.Thread
    device_width: int
    device_height: int
    collector_running: bool

    def __init__(self, serial: str | None = None, port: int = 27188, push_server: bool = True, server_dir: str = '.') -> None:
        self.serial = serial
        adb = ('adb',) if serial is None else ('adb', '-s', serial)
        self.session_id = format(random.randint(0, 0x7FFFFFFF), '08x')
        # phisap的协议实现仅与scrcpy 2.0兼容，严格选择v2.0（目录中可能同时存在其他版本）
        candidates = [p for p in os.listdir(server_dir) if p.startswith('scrcpy-server-v')]
        if 'scrcpy-server-v2.0' not in candidates:
            raise FileNotFoundError(
                f'未找到scrcpy-server-v2.0（phisap目前仅支持scrcpy 2.0协议）。\n'
                f'当前目录中的scrcpy-server文件: {candidates or "无"}\n'
                f'请从 https://github.com/Genymobile/scrcpy/releases/tag/v2.0 下载'
            )
        server_file = os.path.join(server_dir, 'scrcpy-server-v2.0')
        server_version = '2.0'
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

        if push_server:
            adb_run('push', server_file, '/data/local/tmp/scrcpy-server.jar')
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
            server_version,
            f'scid={self.session_id}',
            'log_level=info',
            'audio=false',
            'clipboard_autosync=false',
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
        self.video_socket.settimeout(None)
        self.control_socket.settimeout(None)
        subprocess.run(
            [*adb, 'reverse', '--remove', f'localabstract:scrcpy_{self.session_id}'], capture_output=True
        )  # 移除创建的adb tunnel，我们不再需要它了

        self.collector_running = True

        def streaming_decoder():
            '''解码手机端传回的视频数据，得到视频的尺寸'''
            codec = av.CodecContext.create('h264', 'r')
            try:
                while self.collector_running:
                    _pts = self.video_socket.recv(8)  # unused
                    size = int.from_bytes(self.video_socket.recv(4), 'big')
                    packets = codec.parse(self.video_socket.recv(size))
                    for packet in packets:
                        frames = codec.decode(packet)
                        for frame in frames:
                            if self.device_width != frame.width or self.device_height != frame.height:
                                print('[client]', f'device_size: {self.device_width}x{self.device_height} -> {frame.width}x{frame.height}')
                                self.device_width = frame.width
                                self.device_height = frame.height
                            break
                        break
            except Exception as e:
                if self.collector_running:  # 主动close()时不打印
                    print(e.with_traceback(None))
                self.collector_running = False

        def ctrlmsg_receiver():
            '''另一个垃圾收集器
            收集的是scrcpy-server传来的控制事件的信息，
            比如屏幕旋转事件等'''
            try:
                while self.collector_running:
                    _msg_type = self.control_socket.recv(1)
                    size = int.from_bytes(self.control_socket.recv(4), 'big')
                    self.control_socket.recv(size)
            except Exception as e:
                if self.collector_running:  # 主动close()时不打印
                    print(e.with_traceback(None))
                self.collector_running = False

        _device_name = self.video_socket.recv(64)  # sendDeviceMeta

        # streamer.writeVideoHeader(device.getScreenInfo().getVideoSize())
        codec_id = self.video_socket.recv(4).decode()
        self.device_width = int.from_bytes(self.video_socket.recv(4), 'big')
        self.device_height = int.from_bytes(self.video_socket.recv(4), 'big')

        print('[client]', f'device_size = {self.device_width}x{self.device_height}, codec_id = {codec_id}')

        self.streaming_collector = threading.Thread(target=streaming_decoder, daemon=True)
        self.streaming_collector.start()

        self.control_collector = threading.Thread(target=ctrlmsg_receiver, daemon=True)
        self.control_collector.start()

    def touch(self, x: int, y: int, action: TouchAction, pointer_id: int) -> None:
        self.control_socket.send(
            struct.pack(
                '!bbQiiHHHII',
                2,  # type: SC_CONTROL_MSG_TYPE_INJECT_TOUCH_EVENT
                action.value,
                pointer_id,
                x,
                y,
                self.device_width,
                self.device_height,
                0xFFFF,  # pressure
                1,  # action_button: AMOTION_EVENT_BUTTON_PRIMARY
                1,  # buttons: AMOTION_EVENT_BUTTON_PRIMARY
            )
        )

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
