"""从安卓设备提取 Phigros 安装包(APK / OBB), 并把里面的谱面解包进谱面库。

两件事, 都带进度:
  1. adb 提取: 找到已安装的 Phigros, 把 APK(含所有分包)和 OBB 数据包拉到 ./Assets/APK/<包名>/
  2. 解包:     读 APK/OBB(都是 zip)里的 assets/aa/catalog.json, 找出谱面所在的 bundle,
               解出里面的 TextAsset, 写成 ./Assets/Tracks/<歌曲>/Chart_<难度>.json

为什么要拉 OBB: Google Play 版把资源(含谱面)放在 OBB 里, APK 本身只有一点点; TapTap 版则把资源
直接打进 APK(两三个 GB)。所以提取要把 APK 全部分包和 OBB 都拉下来, 解包要能同时读多个文件
(catalog 在这个文件里、bundle 在另一个文件里也没关系)。

整个模块不依赖 Qt: 进度走回调 progress(done, total, text), 取消用 threading.Event。
GUI 线程、命令行(python apk_tools.py --help)和单元测试用的是同一套代码。
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Sequence

PHIGROS_PACKAGE = 'com.PigeonGames.Phigros'
DEFAULT_APK_DIR = './Assets/APK'
DEFAULT_TRACKS_DIR = './Assets/Tracks'
# /sdcard 在个别设备上不是 /storage/emulated/0 的别名, 两个都试
OBB_ROOTS = ('/sdcard/Android/obb', '/storage/emulated/0/Android/obb')
DISK_MARGIN = 256 * 1024 * 1024  # 磁盘空间检查时额外留的余量
CATALOG_MEMBER = 'assets/aa/catalog.json'
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # Windows 下别弹黑框; 其它系统恒为 0

ProgressFn = Callable[[int, int, str], None]
LogFn = Callable[[str], None]


def _noop(*_a, **_k) -> None:
    pass


# ---------------------------------------------------------------- 异常

class ApkToolError(RuntimeError):
    """可预期的失败。message 已经是给用户看的中文说明, 界面直接显示, 不用打堆栈"""


class AdbError(ApkToolError):
    pass


class PackageError(ApkToolError):
    pass


class Cancelled(Exception):
    """用户点了取消。不是错误"""


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# ---------------------------------------------------------------- 进度

def format_size(n: float) -> str:
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'  # 走不到, 给类型检查器看的


def format_duration(seconds: float) -> str:
    s = int(round(max(seconds, 0)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


class Meter:
    """进度汇报器: 节流(默认最多每 50ms 汇报一次)、done 只增不减、顺带估算速度和剩余时间。

    progress(done, total, text): total=0 表示总量未知(界面应该只显示文字)。
    unit='bytes' 时文字里带 已传/总量 和速度; 别的 unit 只带百分比和剩余时间,
    具体内容(第几个/哪个文件)由调用方通过 detail 给。
    """

    def __init__(self, progress: ProgressFn | None, total: int, *, label: str = '', unit: str = 'bytes',
                 min_interval: float = 0.05, window: float = 3.0, clock: Callable[[], float] = time.monotonic):
        self._progress = progress
        self.total = max(int(total), 0)
        self.label = label
        self.unit = unit
        self._min_interval = min_interval
        self._window = window
        self._clock = clock
        self._last_emit: float | None = None
        self.done = 0
        self._samples: deque[tuple[float, int]] = deque()
        self.started = clock()

    def rate(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        return (d1 - d0) / dt if dt >= 0.25 else 0.0

    def update(self, done: int, detail: str = '', *, force: bool = False) -> None:
        now = self._clock()
        done = int(done)
        if self.total:
            done = min(done, self.total)
        done = max(done, self.done)
        self.done = done
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        if self._progress is None:
            return
        final = bool(self.total) and done >= self.total
        if not (force or final or self._last_emit is None or now - self._last_emit >= self._min_interval):
            return
        self._last_emit = now
        self._progress(done, self.total, self._text(done, detail))

    def _text(self, done: int, detail: str) -> str:
        parts = []
        if self.label:
            parts.append(self.label)
        if detail:
            parts.append(detail)
        if self.total:
            parts.append(f'{done * 100 // self.total}%')
        if self.unit == 'bytes':
            parts.append(f'{format_size(done)} / {format_size(self.total)}' if self.total else format_size(done))
        rate = self.rate()
        if rate > 0:
            if self.unit == 'bytes':
                parts.append(f'{format_size(rate)}/s')
            if self.total and done < self.total:
                parts.append(f'剩余 {format_duration((self.total - done) / rate)}')
        return ' · '.join(parts)


# ---------------------------------------------------------------- adb

ADB_NOT_FOUND = ('找不到 adb。请安装 Android platform-tools 并把它加入 PATH, '
                 '或者把 platform-tools 文件夹放到 phisap 目录下; '
                 '也可以设置环境变量 PHISAP_ADB 指向 adb 可执行文件。')


def find_adb() -> str:
    """优先级: 环境变量 PHISAP_ADB > PATH > ./platform-tools > ./adb。都没有就返回 'adb', 运行时再报错"""
    env = os.environ.get('PHISAP_ADB')
    if env:
        return env
    found = shutil.which('adb')
    if found:
        return found
    for local in ('platform-tools/adb.exe', 'platform-tools/adb', 'adb.exe', 'adb'):
        if os.path.isfile(local):
            return os.path.abspath(local)
    return 'adb'


def _explain(args: Sequence[str], returncode: int, detail: str) -> str:
    detail = (detail or '').strip()
    hint = ''
    low = detail.lower()
    if 'more than one device' in low:
        hint = '检测到多台设备/模拟器, 请先在界面里选择要使用的设备。'
    elif 'unauthorized' in low:
        hint = '设备还没有授权这台电脑: 请解锁手机, 在弹出的"允许 USB 调试"对话框里点"允许"。'
    elif 'offline' in low:
        hint = '设备处于离线状态: 重新插拔 USB, 或者执行 adb kill-server 后再试。'
    elif 'no devices' in low or 'not found' in low:
        hint = '没有找到设备: 请检查 USB 调试/模拟器的 adb 连接, 然后点"刷新"。'
    elif 'insufficient permissions' in low or 'no permissions' in low:
        hint = '没有访问 USB 设备的权限(Linux 需要配置 udev 规则, 或用 root 启动 adb)。'
    msg = f'adb {" ".join(map(str, args))} 失败(退出码 {returncode})'
    if detail:
        msg += f'\n{detail}'
    if hint:
        msg += f'\n\n{hint}'
    return msg


class Adb:
    """adb 命令行的薄封装。exe 可以是路径, 也可以是命令前缀列表(测试里用 [python, fake_adb.py])"""

    def __init__(self, serial: str | None = None, exe: str | Sequence[str] | None = None, timeout: float = 30.0):
        self.serial = serial or None
        if exe is None:
            exe = find_adb()
        self.exe = list(exe) if isinstance(exe, (list, tuple)) else [str(exe)]
        self.timeout = timeout

    def argv(self, *args: str) -> list[str]:
        cmd = list(self.exe)
        if self.serial:
            cmd += ['-s', self.serial]
        return cmd + [str(a) for a in args]

    def run(self, *args: str, timeout: float | None = None) -> str:
        try:
            r = subprocess.run(self.argv(*args), capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout or self.timeout, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            raise AdbError(ADB_NOT_FOUND) from None
        except subprocess.TimeoutExpired:
            raise AdbError(f'adb {" ".join(map(str, args))} 超时(设备没有响应?)') from None
        except OSError as e:
            raise AdbError(f'无法运行 adb: {e}') from None
        if r.returncode != 0:
            raise AdbError(_explain(args, r.returncode, r.stderr or r.stdout))
        return r.stdout

    def shell(self, command: str, timeout: float | None = None) -> str:
        return self.run('shell', command, timeout=timeout)


@dataclass(frozen=True)
class Device:
    serial: str
    state: str

    @property
    def ready(self) -> bool:
        return self.state == 'device'


def parse_devices(output: str) -> list[Device]:
    """解析 adb devices 的输出。跳过 '* daemon started'、表头、版本警告等没有制表符的行"""
    devices = []
    for line in output.splitlines():
        if '\t' not in line:
            continue
        serial, _, state = line.partition('\t')
        if serial.strip():
            devices.append(Device(serial.strip(), state.strip()))
    return devices


def list_devices(adb: Adb | None = None) -> list[Device]:
    adb = adb or Adb()
    base = Adb(None, adb.exe, adb.timeout)  # adb devices 不能带 -s
    return parse_devices(base.run('devices'))


def pick_serial(devices: Sequence[Device], preferred: str | None = None) -> str:
    """选出要操作的设备序列号; 选不出来就抛带指引的 AdbError"""
    ready = [d for d in devices if d.ready]
    if preferred:
        if any(d.serial == preferred for d in ready):
            return preferred
        state = next((d.state for d in devices if d.serial == preferred), None)
        if state is None:
            raise AdbError(f'设备 {preferred} 没有连接。请检查连接后点"刷新"。')
        raise AdbError(_device_state_hint(preferred, state))
    if len(ready) == 1:
        return ready[0].serial
    if ready:
        names = '、'.join(d.serial for d in ready)
        raise AdbError(f'检测到多台设备({names}), 请先在"规划与设备"页选择要使用的那一台。')
    if devices:
        raise AdbError(_device_state_hint(devices[0].serial, devices[0].state))
    raise AdbError('没有检测到安卓设备。请用数据线连接手机并打开 USB 调试(或启动模拟器), 然后点"刷新"。')


def _device_state_hint(serial: str, state: str) -> str:
    if state == 'unauthorized':
        return f'设备 {serial} 还没有授权这台电脑: 请解锁手机, 在弹出的"允许 USB 调试"对话框里点"允许"。'
    if state == 'offline':
        return f'设备 {serial} 处于离线状态: 重新插拔 USB, 或者执行 adb kill-server 后再试。'
    return f'设备 {serial} 当前状态是 {state}, 还不能使用。'


# ---------------------------------------------------------------- 设备上的 Phigros

def list_packages(adb: Adb) -> list[str]:
    out = adb.shell('pm list packages')
    return [line.split(':', 1)[1].strip() for line in out.splitlines() if line.startswith('package:')]


def find_package(adb: Adb, wanted: str = PHIGROS_PACKAGE) -> str:
    """包名优先精确匹配; 找不到就在名字里含 phigros 的包里挑(只有一个才自动选)"""
    wanted = (wanted or PHIGROS_PACKAGE).strip()
    pkgs = list_packages(adb)
    if wanted in pkgs:
        return wanted
    cands = [p for p in pkgs if 'phigros' in p.lower()]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        raise AdbError(f'设备上没有包名为 {wanted} 的应用, 但有多个名字里带 phigros 的: {", ".join(cands)}。'
                       f'请在"包名"里填写要提取的那个。')
    raise AdbError(f'设备上没有安装 {wanted}。请先安装 Phigros(TapTap 版或 Google Play 版都可以)。')


def package_version(adb: Adb, package: str) -> str | None:
    try:
        out = adb.shell(f'dumpsys package {shlex.quote(package)} | grep -m1 versionName', timeout=20)
    except AdbError:
        return None
    m = re.search(r'versionName=(\S+)', out)
    return m.group(1) if m else None


def apk_paths(adb: Adb, package: str) -> list[str]:
    """pm path 列出的全部 APK(base.apk 在前, 后面是各种 split_*.apk)"""
    out = adb.shell(f'pm path {shlex.quote(package)}')
    paths = [line.split(':', 1)[1].strip() for line in out.splitlines() if line.startswith('package:')]
    if not paths:
        raise AdbError(f'pm path 没有返回 {package} 的安装位置, 应用可能没有装好。')
    paths.sort(key=lambda p: (posixpath.basename(p) != 'base.apk', p))
    return paths


def _obb_sort_key(name: str):
    """main.<版本号>.<包名>.obb 在前、patch.<版本号>... 在后, 版本号小的在前 —— 越靠后越新, 解包时后面的覆盖前面的
    (Android 的扩展文件语义: patch 覆盖 main)。设备上偶尔会残留旧版本的 OBB, 不能让旧的盖掉新的"""
    m = re.match(r'(main|patch)\.(\d+)\.', name, re.IGNORECASE)
    if not m:
        return (2, 0, name)
    return (0 if m.group(1).lower() == 'main' else 1, int(m.group(2)), name)


def obb_paths(adb: Adb, package: str, warn: LogFn | None = None) -> list[str]:
    """/sdcard/Android/obb/<包名>/ 下的 .obb 文件。目录不存在就是没有(TapTap 版没有 OBB)。
    没有权限读(个别系统限制了 Android/obb 的访问)时通过 warn 提醒, 而不是装作「没有 OBB」。"""
    denied = None
    for root in OBB_ROOTS:
        folder = f'{root}/{package}'
        try:
            out = adb.shell(f'ls {shlex.quote(folder)}')
        except AdbError as e:
            if 'permission denied' in str(e).lower():
                denied = folder
            continue
        names = sorted((n.strip() for n in out.splitlines() if n.strip().lower().endswith('.obb')),
                       key=_obb_sort_key)
        if names:
            return [f'{folder}/{n}' for n in names]
    if denied and warn:
        warn(f'没有权限读取 {denied}(系统限制了对 Android/obb 的访问)。'
             f'如果你装的是 Google Play 版, 请在手机上用文件管理器把里面的 .obb 拷出来, '
             f'再用"选择 APK/OBB 解包…"')
    return []


def remote_size(adb: Adb, path: str) -> int | None:
    """设备上文件的大小(字节)。stat 不行就解析 ls -l, 都不行返回 None"""
    q = shlex.quote(path)
    try:
        s = adb.shell(f'stat -c %s {q}', timeout=20).strip()
        if s.isdigit():
            return int(s)
    except AdbError:
        pass
    try:
        out = adb.shell(f'ls -l {q}', timeout=20)
        m = re.search(r'\s(\d+)\s+\d{4}-\d{2}-\d{2}\s', out)
        if m:
            return int(m.group(1))
    except AdbError:
        pass
    return None


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int | None
    kind: str  # 'apk' | 'obb'

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


def plan_pull(adb: Adb, package: str, *, include_obb: bool = True, warn: LogFn | None = None) -> list[RemoteFile]:
    files = [RemoteFile(p, remote_size(adb, p), 'apk') for p in apk_paths(adb, package)]
    if include_obb:
        files += [RemoteFile(p, remote_size(adb, p), 'obb') for p in obb_paths(adb, package, warn)]
    return files


# ---------------------------------------------------------------- 拉取

def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    return name or 'file'


def _remove_quiet(path: str, retries: int = 5) -> None:
    for _ in range(retries):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)  # Windows 上进程刚退出时文件句柄可能还没放


def _size_or_zero(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _drain(stream, sink: deque) -> None:
    try:
        for line in iter(stream.readline, ''):
            sink.append(line.rstrip('\r\n'))
    except Exception:
        pass


def pull_file(adb: Adb, remote: str, local: str, *, size: int | None = None,
              progress: Callable[[int], None] | None = None, cancel=None, poll: float = 0.2) -> int:
    """adb pull 一个文件到 local, 返回字节数。

    进度靠轮询本地临时文件(local + '.part')的大小, 不去解析 adb 的输出(各版本格式不一样,
    非 tty 时还可能什么都不打)。传完校验大小再改名; 失败/取消会删掉半截文件, 不会留下
    一个"看起来完整"的残缺 APK。
    """
    folder = os.path.dirname(os.path.abspath(local))
    os.makedirs(folder, exist_ok=True)
    tmp = local + '.part'
    _remove_quiet(tmp)
    try:
        proc = subprocess.Popen(adb.argv('pull', remote, tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        raise AdbError(ADB_NOT_FOUND) from None
    except OSError as e:
        raise AdbError(f'无法运行 adb: {e}') from None
    tail: deque[str] = deque(maxlen=20)
    reader = threading.Thread(target=_drain, args=(proc.stdout, tail), daemon=True)
    reader.start()
    try:
        while True:
            rc = proc.poll()
            if progress:
                progress(_size_or_zero(tmp))
            if rc is not None:
                break
            if cancel is not None:
                if cancel.wait(poll):
                    raise Cancelled()
            else:
                time.sleep(poll)
    except BaseException:
        _stop_process(proc)
        reader.join(1)
        _remove_quiet(tmp)
        raise
    reader.join(2)
    if rc != 0:
        _remove_quiet(tmp)
        raise AdbError(_explain(['pull', remote], rc, '\n'.join(tail)))
    actual = _size_or_zero(tmp)
    if size is not None and actual != size:
        _remove_quiet(tmp)
        raise AdbError(f'{posixpath.basename(remote)} 传输不完整: 设备上是 {size} 字节, 实际收到 {actual} 字节。'
                       f'可能是传输被中断或者磁盘已满, 请重试。')
    os.replace(tmp, local)
    if progress:
        progress(actual)
    return actual


@dataclass
class PullResult:
    package: str
    serial: str | None
    version: str | None
    files: list[str] = field(default_factory=list)    # 本地路径: APK 在前, OBB 在后
    skipped: list[str] = field(default_factory=list)  # 本地已有同样大小的文件, 没有重新拉
    total_bytes: int = 0
    elapsed: float = 0.0


def pull_package(adb: Adb, package: str = PHIGROS_PACKAGE, dest_dir: str = DEFAULT_APK_DIR, *,
                 include_obb: bool = True, force: bool = False, progress: ProgressFn | None = None,
                 cancel=None, log: LogFn | None = None) -> PullResult:
    """把 Phigros 的 APK(全部分包)和 OBB 拉到 dest_dir/<包名>/ 。

    进度是所有文件合计的字节数。本地已有同样大小的文件会跳过(再解包一次不用重新拉 2GB)。
    """
    log = log or _noop
    t0 = time.monotonic()
    _check_cancel(cancel)
    pkg = find_package(adb, package)
    if pkg != package:
        log(f'设备上没有 {package}, 改用 {pkg}')
    version = package_version(adb, pkg)
    log(f'设备 {adb.serial or "(默认)"} · {pkg}' + (f' · 版本 {version}' if version else ''))
    remote = plan_pull(adb, pkg, include_obb=include_obb, warn=log)
    obb_n = sum(1 for f in remote if f.kind == 'obb')
    if include_obb and not obb_n:
        log('没有找到 OBB 数据包(TapTap 版没有 OBB, 资源都在 APK 里; Google Play 版请确认游戏里的数据已下载完)')
    folder = os.path.join(dest_dir, _safe_name(pkg))
    os.makedirs(folder, exist_ok=True)

    locals_ = [os.path.join(folder, _safe_name(f.name)) for f in remote]
    todo = []
    result = PullResult(pkg, adb.serial, version)
    for f, local in zip(remote, locals_):
        if not force and f.size is not None and _size_or_zero(local) == f.size:
            result.skipped.append(local)
        else:
            todo.append((f, local))
    need = sum(f.size or 0 for f, _ in todo)
    free = shutil.disk_usage(os.path.abspath(folder)).free
    if need and free < need + DISK_MARGIN:
        raise PackageError(f'磁盘空间不足: 要拉取约 {format_size(need)}(另需留一点余量), '
                           f'目标磁盘只剩 {format_size(free)}。请清理空间, 或把 phisap 放到空间更大的磁盘上。')
    log('要提取的文件: ' + '; '.join(f'{f.name}({format_size(f.size) if f.size is not None else "大小未知"})'
                                    for f in remote))
    if result.skipped:
        log('已存在且大小一致, 跳过: ' + ', '.join(os.path.basename(p) for p in result.skipped))

    sizes_known = all(f.size is not None for f, _ in todo)
    total = sum(f.size or 0 for f in remote) if sizes_known else 0
    already = sum(f.size or 0 for f, local in zip(remote, locals_) if local in result.skipped)
    meter = Meter(progress, total, label='' if todo else '本地已有同样大小的文件, 跳过提取')
    meter.update(already, force=True)
    done_before = already
    for index, (f, local) in enumerate(todo, 1):
        _check_cancel(cancel)
        label = f'提取 {f.name}({index}/{len(todo)})'
        meter.label = label

        def on_bytes(n: int, base: int = done_before) -> None:
            meter.update(base + n)

        got = pull_file(adb, f.path, local, size=f.size, progress=on_bytes, cancel=cancel)
        done_before += got
        meter.update(done_before, force=True)
        log(f'已提取 {f.name}: {format_size(got)}')
    result.files = list(locals_)
    result.total_bytes = sum(_size_or_zero(p) for p in locals_)
    result.elapsed = time.monotonic() - t0
    return result


# ---------------------------------------------------------------- 谱面命名

_CHART_NAME_RE = re.compile(r'^chart(?:[_\- ]([A-Za-z][A-Za-z0-9]*))?(?:\s*#\s*\d+)?\.json$', re.IGNORECASE)
_PLAN_CACHE_RE = re.compile(r'\.ans(?:\.v\d+)?\.json$', re.IGNORECASE)
# 和 main.chart_difficulty 认识的难度保持一致(再加 EZ): 大小写统一成大写
_KNOWN_TAGS = {'EZ', 'HD', 'IN', 'AT', 'SPB', 'INB', 'HDB', 'ATB', 'DT'}
_BAD_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_chart_filename(filename: str) -> str | None:
    """谱面文件名规范化; 不是谱面(Chart*.json)就返回 None。

      Chart_IN.json          -> Chart_IN.json
      Chart_AT #4159.json    -> Chart_AT.json     (3.20.0 起名字后面多了 #<编号>)
      Chart_SP.json / Chart.json -> Chart.json    (谱面库里 SP 一直叫 Chart.json, 免得同一难度出现两份)
    """
    m = _CHART_NAME_RE.match(filename.strip())
    if not m:
        return None
    tag = m.group(1)
    if tag is None or tag.upper() == 'SP':
        return 'Chart.json'
    return f'Chart_{tag.upper()}.json' if tag.upper() in _KNOWN_TAGS else f'Chart_{tag}.json'


def clean_song_id(folder: str) -> str | None:
    """Assets/Tracks/<这里>/: 去掉结尾的 .0(版本号 0, 在线下载的谱面库里也是不带的), 并去掉非法字符"""
    if folder in ('', '.', '..') or folder.startswith('#'):
        return None
    sid = folder[:-2] if folder.endswith('.0') else folder
    sid = _BAD_PATH_CHARS.sub('_', sid).strip(' .')
    return sid or None


def chart_target(asset_path: str) -> tuple[str, str] | None:
    """catalog 里的资产路径 -> (歌曲文件夹, 规范化文件名); 不是谱面就返回 None"""
    p = asset_path.replace('\\', '/')
    prefix = 'Assets/Tracks/'
    if not p.startswith(prefix):
        return None
    parts = [x for x in p[len(prefix):].split('/') if x]
    if len(parts) != 2:  # 只认 Assets/Tracks/<歌曲>/<文件>
        return None
    sid = clean_song_id(parts[0])
    name = normalize_chart_filename(parts[1])
    if sid is None or name is None:
        return None
    return sid, name


def chart_difficulty_tag(file_name: str) -> str:
    """规范化后的文件名 -> 难度标签(Chart.json 是 SP)"""
    m = _CHART_NAME_RE.match(file_name)
    tag = (m.group(1) if m else None) or 'SP'
    return tag.upper()


def invalidate_plan_cache(chart_path: str) -> int:
    """谱面内容变了, 它旁边的规划缓存(Chart_AT.ans.v12.json 之类)就作废了。

    程序加载缓存时不会核对谱面内容, 旧缓存配新谱面会按旧的时间轴去点, 全错。返回删掉的个数。
    """
    folder = os.path.dirname(chart_path)
    stem = os.path.splitext(os.path.basename(chart_path))[0]
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        m = _PLAN_CACHE_RE.search(name)
        if m and name[:m.start()] == stem:
            _remove_quiet(os.path.join(folder, name))
            removed += 1
    return removed


def write_chart(tracks_dir: str, song_id: str, file_name: str, payload: bytes, *, overwrite: bool = True) -> str:
    """写一份谱面, 返回 written / overwritten / unchanged / skipped。内容相同就不动(保住修改时间和规划缓存)"""
    folder = os.path.join(tracks_dir, song_id)
    path = os.path.join(folder, file_name)
    status = 'written'
    if os.path.isfile(path):
        if not overwrite:
            return 'skipped'
        if _size_or_zero(path) == len(payload):
            try:
                with open(path, 'rb') as f:
                    if f.read() == payload:
                        return 'unchanged'
            except OSError:
                pass
        status = 'overwritten'
    os.makedirs(folder, exist_ok=True)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(payload)
        os.replace(tmp, path)
    except BaseException:
        _remove_quiet(tmp)  # 磁盘写满之类的失败, 别留下半截临时文件
        raise
    invalidate_plan_cache(path)
    return status


# ---------------------------------------------------------------- 解包

@dataclass
class ChartTarget:
    asset_path: str
    bundle: str
    song_id: str
    file_name: str
    archive: int
    member: zipfile.ZipInfo

    @property
    def stem(self) -> str:
        return os.path.splitext(posixpath.basename(self.asset_path))[0]


@dataclass
class ExtractResult:
    archives: list[str] = field(default_factory=list)
    candidates: int = 0                                   # catalog 里的谱面条目中, 包里有 bundle 的
    written: int = 0
    overwritten: int = 0
    unchanged: int = 0
    skipped: int = 0                                      # 已存在且选了"不覆盖"
    failed: list[tuple[str, str]] = field(default_factory=list)
    songs: set[str] = field(default_factory=set)
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return self.written + self.overwritten + self.unchanged + self.skipped

    def summary(self) -> str:
        parts = [f'{len(self.songs)} 首歌共 {self.total} 份谱面']
        detail = []
        if self.written:
            detail.append(f'新增 {self.written}')
        if self.overwritten:
            detail.append(f'更新 {self.overwritten}')
        if self.unchanged:
            detail.append(f'未变化 {self.unchanged}')
        if self.skipped:
            detail.append(f'跳过已有 {self.skipped}')
        if detail:
            parts.append('(' + ', '.join(detail) + ')')
        if self.failed:
            parts.append(f', {len(self.failed)} 份失败')
        return ''.join(parts)


def read_text_assets(data: bytes, name: str = 'x.bundle') -> list[tuple[str, str]]:
    """解析一个 bundle, 返回里面所有 TextAsset 的 (名字, 文本)"""
    from extract import AssetsManager, FileReader, TextAsset  # 延迟导入: 会拉进 rich

    mgr = AssetsManager()
    mgr.load_file(FileReader(data, name))
    mgr.read_assets()
    return [(o.name, o.text) for f in mgr.asset_files for o in f.objects if isinstance(o, TextAsset)]


def describe_bundle(data: bytes) -> str:
    """bundle 头部的一句话描述(UnityFS v7, 2022.3.10f1, 标志 0x43)。解包失败时附在错误后面,
    用户把日志发过来就能看出是哪个 Unity 版本、什么压缩方式, 不用再要文件"""
    try:
        head = bytes(data[:512])
        pos = 0

        def cstr() -> str:
            nonlocal pos
            end = head.index(b'\0', pos)
            out = head[pos:end].decode('ascii', 'replace')
            pos = end + 1
            return out

        sig = cstr()
        if not sig.startswith('Unity'):
            return '不是 UnityFS 文件(开头 ' + head[:8].hex() + ')'
        version = int.from_bytes(head[pos:pos + 4], 'big')
        pos += 4
        cstr()  # 播放器版本(通常是 5.x.x)
        engine = cstr()
        text = f'{sig} v{version}, {engine}'
        if sig == 'UnityFS':
            flags = int.from_bytes(head[pos + 16:pos + 20], 'big')
            text += f', 标志 0x{flags:x}(压缩 {flags & 0x3F})'
        return text
    except (ValueError, IndexError):
        return '头部无法解析'


def _pick_text(assets: list[tuple[str, str]], stem: str) -> str:
    if not assets:
        raise ValueError('bundle 里没有 TextAsset')
    for name, text in assets:
        if name.lower() == stem.lower():
            return text
    if len(assets) == 1:
        return assets[0][1]
    raise ValueError(f'bundle 里有 {len(assets)} 个 TextAsset, 没有叫 {stem} 的, 无法确定是哪个')


def _check_chart_text(text: str) -> None:
    s = text.strip().lstrip('\ufeff')
    if not (s.startswith('{') and s.endswith('}')):
        raise ValueError('解出来的内容不像谱面 JSON(可能是游戏版本太新或资源已加密)')


def _open_archives(paths: Sequence[str]) -> list[zipfile.ZipFile]:
    zips: list[zipfile.ZipFile] = []
    try:
        for p in paths:
            if not os.path.isfile(p):
                raise PackageError(f'找不到文件: {p}')
            try:
                zips.append(zipfile.ZipFile(p))
            except zipfile.BadZipFile:
                raise PackageError(f'{os.path.basename(p)} 不是有效的 APK/OBB(zip)文件, '
                                   f'可能没拷完整, 请重新提取。') from None
            except OSError as e:
                raise PackageError(f'无法读取 {p}: {e}') from None
    except BaseException:
        for z in zips:
            z.close()
        raise
    return zips


def _find_catalog(zips: Sequence[zipfile.ZipFile]) -> tuple[int, zipfile.ZipInfo] | None:
    """assets/aa/catalog.json; 多个文件里都有时取最后一个(OBB 排在 APK 后面, 扩展文件覆盖 APK)。
    找不到标准位置时退而求其次, 取第一个 catalog*.json"""
    exact = None
    fallback = None
    for idx, zf in enumerate(zips):
        for info in zf.infolist():
            low = info.filename.lower()
            if low == CATALOG_MEMBER:
                exact = (idx, info)
            elif fallback is None and posixpath.basename(low).startswith('catalog') and low.endswith('.json'):
                fallback = (idx, info)
    return exact or fallback


def _has_binary_catalog(zips: Sequence[zipfile.ZipFile]) -> bool:
    return any(i.filename.lower().endswith('catalog.bin') for z in zips for i in z.infolist())


def scan_charts(zips: Sequence[zipfile.ZipFile], *, difficulties: Sequence[str] | None = None,
                log: LogFn | None = None) -> list[ChartTarget]:
    """读 catalog, 找出所有「包里有 bundle 的谱面」。找不到就抛带指引的 PackageError"""
    log = log or _noop
    found = _find_catalog(zips)
    if found is None:
        if _has_binary_catalog(zips):
            raise PackageError('这个版本的游戏使用二进制 catalog(catalog.bin), 目前还不能解析。')
        raise PackageError('没有在所选文件里找到 assets/aa/catalog.json。'
                           '请确认选的是 Phigros 的 APK/OBB; Google Play 版的谱面在 OBB 里, 请把 OBB 一起选上。')
    cat_idx, cat_info = found
    try:
        data = json.loads(zips[cat_idx].read(cat_info).decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as e:
        raise PackageError(f'catalog.json 读不出来: {e}') from None
    from catalog import load_catalog  # 延迟导入

    try:
        fname_map = load_catalog(data).fname_map
    except Exception as e:  # noqa: BLE001 - catalog 格式五花八门, 统一给个能看懂的说明
        raise PackageError(f'catalog.json 的格式不认识({type(e).__name__}: {e})。'
                           f'可能是游戏更新后改了格式, 需要更新 phisap。') from None

    bundles: dict[str, tuple[int, zipfile.ZipInfo]] = {}
    for idx, zf in enumerate(zips):
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = posixpath.basename(info.filename)
            if base.endswith('.bundle'):
                bundles[base] = (idx, info)  # 重名时后面的文件覆盖前面的(同上: 越靠后越新)
    if not bundles:
        raise PackageError('找到了 catalog.json, 但所选文件里没有任何 .bundle 资源。'
                           'Google Play 版的资源在 OBB 数据包里, 请把 OBB 一起选上。')
    want = {d.upper() for d in difficulties} if difficulties else None
    targets = []
    seen: dict[tuple[str, str], str] = {}
    for bundle, asset_path in fname_map.items():
        spec = chart_target(asset_path)
        if spec is None or bundle not in bundles:
            continue
        sid, name = spec
        if want is not None and chart_difficulty_tag(name) not in want:
            continue
        if (sid, name) in seen:  # 规范化后重名(例如 Chart_AT.json 和 Chart_AT #123.json 同时存在)
            log(f'注意: {sid}/{name} 在 catalog 里出现了多次({seen[(sid, name)]} 与 {asset_path}), 后者会覆盖前者')
        seen[(sid, name)] = asset_path
        idx, info = bundles[bundle]
        targets.append(ChartTarget(asset_path, bundle, sid, name, idx, info))
    if not targets:
        raise PackageError('catalog 里没有可识别的谱面(Assets/Tracks/<歌曲>/Chart_*.json)。'
                           '可能是游戏更新后改了资源结构, 或者选的文件不对。')
    log(f'catalog 里有 {len(fname_map)} 条资源, 其中 {len(targets)} 份谱面的 bundle 在所选文件里')
    return targets


def extract_charts(archives: Sequence[str], tracks_dir: str = DEFAULT_TRACKS_DIR, *, overwrite: bool = True,
                   difficulties: Sequence[str] | None = None, progress: ProgressFn | None = None,
                   cancel=None, log: LogFn | None = None) -> ExtractResult:
    """解包 APK/OBB 里的谱面到 tracks_dir/<歌曲>/Chart_<难度>.json

    - archives 可以有多个(APK + OBB); catalog 和 bundle 分在不同文件里也行
    - 单个 bundle 解失败不影响别的, 失败原因记在 result.failed; 但如果全部失败会抛 PackageError
    - 进度按「已处理的 bundle 字节数」算, 取消时已写好的谱面保留
    """
    log = log or _noop
    t0 = time.monotonic()
    _check_cancel(cancel)
    if not archives:
        raise PackageError('没有选择要解包的文件。')
    result = ExtractResult(archives=[os.path.abspath(p) for p in archives])
    zips = _open_archives(archives)
    try:
        meter = Meter(progress, 1, unit='items')
        meter.update(0, '读取 catalog…', force=True)
        targets = scan_charts(zips, difficulties=difficulties, log=log)
        _check_cancel(cancel)
        result.candidates = len(targets)
        total = sum(max(t.member.file_size, 1) for t in targets)
        meter = Meter(progress, total, unit='items')
        done_bytes = 0
        first_error = ''
        for index, t in enumerate(targets, 1):
            if cancel is not None and cancel.is_set():
                log(f'已取消: 解包了 {result.total} 份谱面')
                raise Cancelled()
            detail = f'解包谱面 {index}/{len(targets)} · {t.song_id}/{t.file_name}'
            meter.update(done_bytes, detail)
            payload = b''
            try:
                payload = zips[t.archive].read(t.member)
                text = _pick_text(read_text_assets(payload, t.member.filename), t.stem)
                _check_chart_text(text)
                status = write_chart(tracks_dir, t.song_id, t.file_name, text.encode('utf-8'), overwrite=overwrite)
            except Exception as e:  # noqa: BLE001 - 单个坏 bundle 不能拖垮整批
                msg = f'{type(e).__name__}: {e}'
                if payload:
                    msg += f' [bundle: {describe_bundle(payload)}]'
                result.failed.append((t.asset_path, msg))
                first_error = first_error or msg
            else:
                result.songs.add(t.song_id)
                if status == 'written':
                    result.written += 1
                elif status == 'overwritten':
                    result.overwritten += 1
                elif status == 'unchanged':
                    result.unchanged += 1
                else:
                    result.skipped += 1
            done_bytes += max(t.member.file_size, 1)
        meter.update(total, f'解包完成 {len(targets)}/{len(targets)}', force=True)
        if result.failed and not result.total:
            raise PackageError(f'{len(targets)} 份谱面全部解包失败, 第一个错误: {first_error}\n'
                               f'可能是这个游戏版本的资源格式 phisap 还不支持(例如已加密)。')
    finally:
        for z in zips:
            z.close()
    result.elapsed = time.monotonic() - t0
    for asset, msg in result.failed[:5]:
        log(f'解包失败 {asset}: {msg}')
    if len(result.failed) > 5:
        log(f'……还有 {len(result.failed) - 5} 份失败')
    log('解包完成: ' + result.summary() + f', 用时 {format_duration(result.elapsed)}')
    return result


def find_local_archives(apk_dir: str = DEFAULT_APK_DIR) -> list[str]:
    """apk_dir 下所有 .apk / .obb(APK 在前, base.apk 最前, OBB 在后), 给「解包已提取的文件」用"""
    found = []
    for root, _dirs, files in os.walk(apk_dir):
        for f in files:
            if f.lower().endswith(('.apk', '.obb')):
                found.append(os.path.join(root, f))
    found.sort(key=lambda p: (p.lower().endswith('.obb'), os.path.basename(p) != 'base.apk',
                              _obb_sort_key(os.path.basename(p)) if p.lower().endswith('.obb') else (0, 0, p), p))
    return found


@dataclass
class PipelineResult:
    pull: PullResult
    extract: ExtractResult
    deleted: list[str] = field(default_factory=list)


def pull_and_extract(serial: str | None = None, package: str = PHIGROS_PACKAGE, *,
                     apk_dir: str = DEFAULT_APK_DIR, tracks_dir: str = DEFAULT_TRACKS_DIR,
                     include_obb: bool = True, overwrite: bool = True, delete_archives: bool = False,
                     force_pull: bool = False, progress: ProgressFn | None = None, cancel=None,
                     log: LogFn | None = None, adb: Adb | None = None) -> PipelineResult:
    """一条龙: adb 提取 -> 解包。两个阶段各自从 0% 走到 100%, 文字里带 [1/2] [2/2]"""
    log = log or _noop
    adb = adb or Adb(serial)

    def stage(n: int) -> ProgressFn | None:
        if progress is None:
            return None
        return lambda d, t, s: progress(d, t, f'[{n}/2] {s}')

    pulled = pull_package(adb, package, apk_dir, include_obb=include_obb, force=force_pull,
                          progress=stage(1), cancel=cancel, log=log)
    extracted = extract_charts(pulled.files, tracks_dir, overwrite=overwrite, progress=stage(2),
                               cancel=cancel, log=log)
    deleted: list[str] = []
    if delete_archives and not extracted.failed and extracted.total:
        for p in pulled.files:
            _remove_quiet(p)
            deleted.append(p)
        log('已按设置删除提取出来的安装包: ' + ', '.join(os.path.basename(p) for p in deleted))
    return PipelineResult(pulled, extracted, deleted)


# ---------------------------------------------------------------- 命令行

def _console_progress(done: int, total: int, text: str) -> None:
    width = 24
    frac = (done / total) if total else 0.0
    filled = int(width * frac)
    bar = '#' * filled + '-' * (width - filled)
    end = '\r' if sys.stderr.isatty() else '\n'
    sys.stderr.write(f'[{bar}] {text[:100]:<100}{end}')
    sys.stderr.flush()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='从安卓设备提取 Phigros 安装包并解包谱面(没有界面时的命令行入口)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def add_device_args(p):
        p.add_argument('--serial', '-s', help='设备序列号(只有一台设备时可省略)')
        p.add_argument('--package', default=PHIGROS_PACKAGE, help=f'包名, 默认 {PHIGROS_PACKAGE}')
        p.add_argument('--dest', default=DEFAULT_APK_DIR, help='APK/OBB 保存目录')
        p.add_argument('--no-obb', action='store_true', help='不拉 OBB')
        p.add_argument('--force', action='store_true', help='即使本地已有同样大小的文件也重新拉')

    sub.add_parser('devices', help='列出已连接的设备')
    p_pull = sub.add_parser('pull', help='只提取 APK/OBB')
    add_device_args(p_pull)
    p_ext = sub.add_parser('extract', help='解包本地的 APK/OBB')
    p_ext.add_argument('files', nargs='+', help='APK / OBB 文件(可以多个)')
    p_ext.add_argument('--tracks', default=DEFAULT_TRACKS_DIR, help='谱面库目录')
    p_ext.add_argument('--no-overwrite', action='store_true', help='已有的谱面不覆盖')
    p_all = sub.add_parser('all', help='提取并解包')
    add_device_args(p_all)
    p_all.add_argument('--tracks', default=DEFAULT_TRACKS_DIR, help='谱面库目录')
    p_all.add_argument('--no-overwrite', action='store_true')
    p_all.add_argument('--delete', action='store_true', help='解包成功后删除提取出来的 APK/OBB')
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        print(msg, flush=True)

    try:
        if args.cmd == 'devices':
            devs = list_devices()
            for d in devs:
                say(f'{d.serial}\t{d.state}')
            if not devs:
                say('(没有设备)')
            return 0
        if args.cmd == 'extract':
            r = extract_charts(args.files, args.tracks, overwrite=not args.no_overwrite,
                               progress=_console_progress, log=say)
            say(r.summary())
            return 1 if r.failed else 0
        adb = Adb(pick_serial(list_devices(), args.serial))
        if args.cmd == 'pull':
            r = pull_package(adb, args.package, args.dest, include_obb=not args.no_obb, force=args.force,
                             progress=_console_progress, log=say)
            say(f'完成: {len(r.files)} 个文件, 共 {format_size(r.total_bytes)}')
            return 0
        r = pull_and_extract(adb.serial, args.package, apk_dir=args.dest, tracks_dir=args.tracks,
                             include_obb=not args.no_obb, overwrite=not args.no_overwrite,
                             delete_archives=args.delete, force_pull=args.force,
                             progress=_console_progress, log=say, adb=adb)
        say(r.extract.summary())
        return 1 if r.extract.failed else 0
    except Cancelled:
        say('已取消')
        return 130
    except KeyboardInterrupt:
        say('已取消')
        return 130
    except ApkToolError as e:
        say(f'错误: {e}')
        return 2


if __name__ == '__main__':
    sys.exit(main())
