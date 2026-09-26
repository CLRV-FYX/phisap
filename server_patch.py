"""scrcpy-server 触点上限补丁(10 → 16)。

官方 scrcpy-server 的 PointersState.MAX_POINTERS = 10: 同时按下第11根手指时,
服务端会打印 "Too many pointers" 并**静默丢弃**这次触控。
Android 本身(MotionEvent / InputDispatcher)的硬上限是 16 根手指(MAX_POINTERS = 16),
这是系统能注入的最大值, 无法再高。

scrcpy 是 Java 程序, 无法在本机不装 Android SDK 的情况下重新编译, 因此这里直接修改
官方 jar 中 classes.dex 里使用该常量的 4 条指令(`const/16 vX, 10` → `const/16 vX, 16`):

    Controller.<init>                 两个 pointerProperties/pointerCoords 数组的长度
    Controller.initPointers           初始化这两个数组的循环上界
    PointersState.nextUnusedLocalId   本地指针id的分配上界
    PointersState.getPointerIndex     "指针已满" 的判断

每一处都会先逐字节校验(只接受与官方 v4.1 完全一致的文件), 然后重新计算 dex 头中的
SHA-1 签名与 adler32 校验和(ART 加载 dex 时会校验 adler32)。
任何一步不符合预期都会放弃补丁, 继续使用官方原版 jar 与 10 触点上限, 不会影响正常使用。
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import zipfile
import zlib

ANDROID_MAX_POINTERS = 16   # Android 系统硬上限
SCRCPY_MAX_POINTERS = 10    # 官方 scrcpy-server 的上限

# 官方 scrcpy-server-v4.1 (733706 字节)
_EXPECTED_JAR_SIZE = 733706
# (dex 文件偏移, 原始4字节, 位置说明)。指令格式 const/16 vAA, #+BBBB: 13 AA BB BB
_PATCH_SITES = (
    (0x4B75C, bytes.fromhex('13010a00'), 'Controller.<init>'),
    (0x4B92E, bytes.fromhex('13010a00'), 'Controller.initPointers'),
    (0x4D312, bytes.fromhex('13010a00'), 'PointersState.nextUnusedLocalId'),
    (0x4D264, bytes.fromhex('13020a00'), 'PointersState.getPointerIndex'),
)

PATCHED_SUFFIX = f'-{ANDROID_MAX_POINTERS}pt'


class PatchError(Exception):
    pass


def _fix_dex_checksums(dex: bytearray) -> None:
    """重新计算 dex 头: [12:32] = SHA-1(dex[32:]), [8:12] = adler32(dex[12:])"""
    dex[12:32] = hashlib.sha1(bytes(dex[32:])).digest()
    struct.pack_into('<I', dex, 8, zlib.adler32(bytes(dex[12:])) & 0xFFFFFFFF)


def _dex_checksums_ok(dex: bytes) -> bool:
    sha_ok = dex[12:32] == hashlib.sha1(dex[32:]).digest()
    adler_ok = struct.unpack_from('<I', dex, 8)[0] == (zlib.adler32(dex[12:]) & 0xFFFFFFFF)
    return sha_ok and adler_ok


def patch_dex(dex: bytes, max_pointers: int = ANDROID_MAX_POINTERS) -> bytes:
    if not dex.startswith(b'dex\n'):
        raise PatchError('classes.dex 文件头不正确')
    if not _dex_checksums_ok(dex):
        raise PatchError('classes.dex 校验和不正确(文件可能已损坏)')
    if not 1 <= max_pointers <= 0x7FFF:
        raise PatchError(f'max_pointers 超出范围: {max_pointers}')
    out = bytearray(dex)
    for offset, original, where in _PATCH_SITES:
        if bytes(out[offset:offset + 4]) != original:
            raise PatchError(f'{where} 处的指令与官方 v4.1 不一致(偏移 0x{offset:x})')
        out[offset + 2:offset + 4] = struct.pack('<H', max_pointers)
    _fix_dex_checksums(out)
    return bytes(out)


def patch_jar(jar: bytes, max_pointers: int = ANDROID_MAX_POINTERS) -> bytes:
    if len(jar) != _EXPECTED_JAR_SIZE:
        raise PatchError(f'scrcpy-server 文件大小为 {len(jar)} 字节, 不是官方 v4.1 ({_EXPECTED_JAR_SIZE} 字节)')
    src = zipfile.ZipFile(io.BytesIO(jar))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as dst:
        found = False
        for info in src.infolist():
            data = src.read(info)
            if info.filename == 'classes.dex':
                data = patch_dex(data, max_pointers)
                found = True
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            dst.writestr(zi, data)
        if not found:
            raise PatchError('scrcpy-server 中没有 classes.dex')
    return buf.getvalue()


def prepare_server(server_file: str, log=print) -> tuple[str, int]:
    """返回 (实际要推送到设备的 server 文件路径, 可同时使用的最大触点数)。

    优先使用(并按需生成)16触点补丁版; 补丁失败时退回官方原版与10触点。
    补丁版与原版放在同一目录, 文件名追加 -16pt。
    """
    patched_file = server_file + PATCHED_SUFFIX
    try:
        with open(server_file, 'rb') as f:
            jar = f.read()
        data = patch_jar(jar)
        # 仅在内容变化时重写, 避免每次运行都写盘
        old = None
        if os.path.isfile(patched_file):
            with open(patched_file, 'rb') as f:
                old = f.read()
        if old != data:
            tmp = patched_file + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, patched_file)
        return patched_file, ANDROID_MAX_POINTERS
    except Exception as e:  # 任何问题(文件损坏/版本不符/磁盘只读等)都退回官方原版, 不影响使用
        log(f'[warn] 无法生成 {ANDROID_MAX_POINTERS} 触点版 scrcpy-server({e}), '
            f'使用官方原版, 最多同时 {SCRCPY_MAX_POINTERS} 个触点')
        return server_file, SCRCPY_MAX_POINTERS


def max_touch_points(server_file: str, log=print) -> int:
    return prepare_server(server_file, log)[1]


__all__ = ['prepare_server', 'max_touch_points', 'patch_jar', 'patch_dex', 'PatchError',
           'ANDROID_MAX_POINTERS', 'SCRCPY_MAX_POINTERS', 'PATCHED_SUFFIX']
