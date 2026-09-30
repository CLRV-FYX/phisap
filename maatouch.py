"""MaaTouch 触控后端 —— scrcpy 的替代方案。

背景(为什么要有第二个后端):
  phisap 原本只用 scrcpy-server 注入触控。用 scrcpy 时, 人手去点模拟器屏幕会**打断**
  自动演奏: scrcpy-server 和人手触摸最终都走 Android 的 InputManager.injectInputEvent,
  一旦有真实触点落到同一个窗口, InputDispatcher 会认为新手势开始, 给应用发 ACTION_CANCEL,
  已经按住的程序触点全部被取消, 谱面就断了。
  MaaTouch(MAA 明日方舟助手项目)是 minitouch 输入协议的安卓原生实现, 同样走
  InputManager.injectInputEvent, 但它是一个独立的 dex, 通过 adb shell 的 stdin/stdout
  收发指令, 不依赖视频流。它在 MuMu 等模拟器上和 scrcpy 行为不同, 所以作为可选项提供,
  哪个不被打断就用哪个。

与 scrcpy 后端的差异(用户需要知道的):
  1. 最多同时 **10** 个触点(scrcpy 补丁版是 16)。Phigros 绝大多数谱面用不到 10 指,
     但个别多押谱面会不够, 这时规划会自动限制在 10 指内。
  2. 没有视频流, 所以"视觉自动开始"不可用(会自动回退到计时器同步)。
  3. 指针 id 会被 MaaTouch 重映射成 0~9 再注入(和 scrcpy-server 一样),
     所以 phisap 里的 PID_OFFSET=20000 对应用实际看到的 pointerId 没有任何作用。

协议(minitouch 协议, 见 MaaAssistantArknights/MaaTouch):
  启动: adb shell -T "export CLASSPATH=/data/local/tmp/maatouch; app_process /data/local/tmp com.shxyke.MaaTouch.App"
  首行 banner: "^ 10 <屏宽> <屏高> 255"   然后 "$ 255"
  指令(每行一条):
    d <id> <x> <y> <pressure 0-255>   按下
    m <id> <x> <y> <pressure>         移动
    u <id>                            抬起
    c                                 提交(把累积的指令一次性下发)
    r                                 重置(抬起所有触点)
  坐标是显示器的**绝对像素**, 不需要缩放。
"""
from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import threading
import time

from algo.algo_base import TouchAction

# MaaTouch v1.1.0 发布物(13,775 字节的 dex)
MAATOUCH_VERSION = '1.1.0'
MAATOUCH_URL = (
    f'https://github.com/MaaAssistantArknights/MaaTouch/releases/download/'
    f'v{MAATOUCH_VERSION}/maatouch'
)
MAATOUCH_EXPECTED_SIZE = 13775
MAATOUCH_FILE = f'maatouch-v{MAATOUCH_VERSION}'
MAATOUCH_REMOTE_PATH = '/data/local/tmp/maatouch'
MAATOUCH_MAIN_CLASS = 'com.shxyke.MaaTouch.App'

# MaaTouch 的 PointersState.MAX_POINTERS = 10, 这是硬上限
MAATOUCH_MAX_POINTERS = 10

BANNER_TIMEOUT = 20.0


class MaaTouchError(RuntimeError):
    pass


def maatouch_file_path(server_dir: str = '.') -> str:
    return os.path.join(server_dir, MAATOUCH_FILE)


def download_maatouch(server_dir: str = '.', log=print) -> str:
    """确保本地有 maatouch dex(缺失时从 GitHub 下载), 返回路径。"""
    path = maatouch_file_path(server_dir)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(server_dir, exist_ok=True)
    log(f'[maatouch] 本地没有 {MAATOUCH_FILE}, 正在从 GitHub 下载 ...')
    tmp = path + '.tmp'
    # 用 curl 优先(Windows 上自带 curl.exe), 失败再退回 requests/urllib
    cmds = [
        ['curl', '-L', '--fail', '--silent', '--show-error', '-o', tmp, MAATOUCH_URL],
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
         f'[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;'
         f'$ProgressPreference=\'Silently\';'
         f'Invoke-WebRequest -UseBasicParsing -Uri \'{MAATOUCH_URL}\' -OutFile \'{tmp}\''],
    ]
    last_err = ''
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
            if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                break
            last_err = (r.stderr or r.stdout or '').strip()
        except FileNotFoundError as e:
            last_err = str(e)
        except Exception as e:
            last_err = str(e)
    else:
        raise MaaTouchError(
            f'下载 MaaTouch 失败: {last_err}\n'
            f'请手动下载 {MAATOUCH_URL}\n'
            f'并保存为 {MAATOUCH_FILE} 放到 main.py 同目录。'
        )
    size = os.path.getsize(tmp)
    if size != MAATOUCH_EXPECTED_SIZE:
        # 大小不符不阻塞使用(官方可能重新打包), 只警告
        log(f'[maatouch][warn] 下载得到 {size} 字节, 与预期的 {MAATOUCH_EXPECTED_SIZE} 不一致, 仍尝试使用')
    os.replace(tmp, path)
    log(f'[maatouch] 已就绪: {path} ({size} 字节)')
    return path


class MaaTouchController:
    """与 DeviceController 同接口的 MaaTouch 后端(供 main.py 无缝切换)。"""

    # 类级默认值, 与 DeviceController 对齐(便于 getattr 检查/未初始化时也不炸)
    serial: str | None = None
    max_pointers: int = MAATOUCH_MAX_POINTERS
    session_id: str = ''
    device_width: int = 0
    device_height: int = 0

    def __init__(self, serial: str | None = None, push: bool = True, server_dir: str = '.',
                 log=print) -> None:
        self.serial = serial
        self.log = log
        adb = ('adb',) if serial is None else ('adb', '-s', serial)
        self._adb = adb
        self.max_pointers = MAATOUCH_MAX_POINTERS
        self.session_id = f'maatouch-{os.getpid()}'
        self.device_width = 0
        self.device_height = 0
        self._proc: subprocess.Popen | None = None
        self._stdin = None
        self._stdout = None
        self._closed = False
        self._write_lock = threading.Lock()

        def adb_run(*args: str) -> subprocess.CompletedProcess:
            r = subprocess.run([*adb, *args], capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or '').strip()
                hint = ''
                if 'more than one device' in detail:
                    hint = '\n\n检测到多个设备/模拟器, 请先在界面的"设备Serial"中选择要使用的设备。'
                elif 'not found' in detail or 'offline' in detail or 'no devices' in detail:
                    hint = '\n\n设备未连接或已离线, 请检查USB调试/模拟器adb连接后点"刷新"。'
                raise MaaTouchError(f'adb 命令执行失败: adb {" ".join(args)}\n{detail}{hint}')
            return r

        self._adb_run = adb_run

        local = download_maatouch(server_dir, log=log)
        if push:
            adb_run('push', local, MAATOUCH_REMOTE_PATH)

        # -T: 不要分配 PTY。PTY 会把 \n 转成 \r\n 并回显输入, 直接毁掉协议。
        cmd = [*adb, 'shell', '-T',
               f'export CLASSPATH={MAATOUCH_REMOTE_PATH}; '
               f'app_process /data/local/tmp {MAATOUCH_MAIN_CLASS}']
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
        except FileNotFoundError as e:
            raise MaaTouchError(f'找不到 adb, 请把 adb 加入 PATH: {e}') from e
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout
        try:
            self._read_banner()
        except Exception:
            self.close()
            raise

    # ---- banner / 屏幕尺寸 ----
    def _read_banner(self) -> None:
        """读 "^ 10 <w> <h> 255" 和 "$ 255" 两行, 拿到屏幕尺寸。"""
        deadline = time.monotonic() + BANNER_TIMEOUT
        w = h = 0
        got_caret = False
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise MaaTouchError(
                    'MaaTouch 进程启动后立即退出了。\n'
                    '常见原因: 设备不支持 app_process / SELinux 禁止了注入 / 设备未解锁。'
                )
            line = self._readline(deadline)
            if line is None:
                continue
            line = line.strip()
            if not line:
                continue
            if line.startswith('^'):
                parts = line[1:].split()
                # "^ <max-contacts> <max-x> <max-y> <max-pressure>"
                if len(parts) >= 4:
                    try:
                        self.max_pointers = min(int(parts[0]), MAATOUCH_MAX_POINTERS)
                        w, h = int(parts[1]), int(parts[2])
                    except ValueError:
                        pass
                got_caret = True
            elif line.startswith('$'):
                if got_caret:
                    break
        if not got_caret or w <= 0 or h <= 0:
            raise MaaTouchError(
                f'没有收到 MaaTouch 的 banner(等待 {BANNER_TIMEOUT:.0f} 秒超时)。\n'
                '请确认设备已解锁、adb 连接正常, 然后重试。'
            )
        self.device_width, self.device_height = w, h
        self.log(f'[maatouch] 已连接, 屏幕 {w}x{h}, 最多同时 {self.max_pointers} 个触点')

    def _readline(self, deadline: float) -> str | None:
        """带超时读一行(用 select 避免永久阻塞)。"""
        import select
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            r, _, _ = select.select([self._stdout], [], [], min(remaining, 0.5))
        except (OSError, ValueError):
            return None
        if not r:
            return None
        try:
            raw = self._stdout.readline()
        except (OSError, ValueError):
            return None
        if not raw:
            return None
        return raw.decode('utf-8', 'replace')

    # ---- 触控 ----
    def _write(self, text: str) -> None:
        if self._closed or self._stdin is None:
            return
        data = text.encode('ascii', 'replace')
        with self._write_lock:
            try:
                self._stdin.write(data)
                self._stdin.flush()
            except (OSError, ValueError):
                self._closed = True

    def touch_many(self, events) -> None:
        """把同一时刻的多个事件合并成一次下发(和 DeviceController.touch_many 同语义)。"""
        if not events:
            return
        buf: list[str] = []
        for e in events:
            x, y = e.pos
            xi, yi = int(round(x)), int(round(y))
            if e.action is TouchAction.DOWN:
                buf.append(f'd {e.pointer} {xi} {yi} 255\n')
            elif e.action is TouchAction.UP:
                buf.append(f'u {e.pointer}\n')
            else:
                buf.append(f'm {e.pointer} {xi} {yi} 255\n')
        buf.append('c\n')   # 提交: MaaTouch 收到 c 才真正把这一批事件发出去
        self._write(''.join(buf))

    def touch(self, x: int, y: int, action: TouchAction, pointer_id: int = 1000) -> None:
        if action is TouchAction.DOWN:
            self._write(f'd {pointer_id} {int(x)} {int(y)} 255\nc\n')
        elif action is TouchAction.UP:
            self._write(f'u {pointer_id}\nc\n')
        else:
            self._write(f'm {pointer_id} {int(x)} {int(y)} 255\nc\n')

    def tap(self, x: int, y: int, pointer_id: int = 1000, delay: float = 0.1) -> None:
        self.touch(x, y, TouchAction.DOWN, pointer_id)
        time.sleep(delay)
        self.touch(x, y, TouchAction.UP, pointer_id)

    def release_pointers(self, pointer_ids) -> None:
        """抬起指定的程序触点(停止演奏时用, 防止手指卡在屏幕上)。"""
        pids = [p for p in (pointer_ids or []) if p is not None]
        if not pids:
            return
        self._write(''.join(f'u {p}\n' for p in pids) + 'c\n')

    def reset_all(self) -> None:
        """MaaTouch 的 r 指令: 抬起它记录的所有触点。"""
        self._write('r\n')

    # ---- 视觉自动开始: MaaTouch 没有视频流, 不支持 ----
    @property
    def supports_visual_watch(self) -> bool:
        return False

    def start_activity_watch(self, cooldown: float = 2.0):
        raise NotImplementedError('MaaTouch 后端没有视频流, 不支持视觉自动开始')

    def stop_activity_watch(self) -> None:
        pass

    # ---- 生命周期 ----
    @property
    def collector_running(self) -> bool:
        """进程还活着就返回 True(main.py 停止时会检查这个)。"""
        if self._closed or self._proc is None:
            return False
        return self._proc.poll() is None

    def abort(self) -> None:
        '''强制中断当前传输: 关掉stdin让阻塞中的 write/flush 立刻抛异常,
        播放线程从而能走到 finally 里释放触点。之后想继续用要重新连接设备。'''
        self._closed = True
        try:
            if self._stdin is not None:
                self._stdin.close()
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.kill()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # 先抬起所有触点, 避免手指卡在屏幕上
        try:
            self.reset_all()
            time.sleep(0.02)
        except Exception:
            pass
        try:
            if self._stdin is not None:
                self._stdin.close()
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.kill()
        except Exception:
            pass
        # 杀掉 adb 客户端不一定会杀掉设备端的 app_process, 这里补一刀
        try:
            subprocess.run([*self._adb, 'shell', 'pkill', '-f', MAATOUCH_MAIN_CLASS],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    @staticmethod
    def get_devices() -> list[str]:
        from control import DeviceController
        return DeviceController.get_devices()


__all__ = ['MaaTouchController', 'MaaTouchError', 'download_maatouch',
           'maatouch_file_path', 'MAATOUCH_MAX_POINTERS', 'MAATOUCH_VERSION']
