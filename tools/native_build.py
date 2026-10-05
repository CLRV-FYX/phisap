"""编方式二用的三个 ARM64 程序：游戏内钩子、root 触摸守护、注入器。"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = Path(__file__).resolve().parent / 'native'
ZIG_CANDIDATES = (
    os.environ.get('ZIG', ''),
    '/tmp/phisap-venv/lib/python3.11/site-packages/ziglang/zig',
    '/tmp/zig-venv/lib/python3.11/site-packages/ziglang/zig',
    str(Path.home() / '.local/share/zig/zig'),
)


def find_zig() -> str:
    for path in ZIG_CANDIDATES:
        if path and Path(path).is_file():
            return path
    found = shutil.which('zig')
    if found:
        return found
    raise RuntimeError('找不到 zig，没法编 aarch64 钩子')


def _elf_machine(blob: bytes) -> int:
    if blob[:4] != b'\x7fELF':
        raise RuntimeError('不是 ELF')
    return struct.unpack_from('<H', blob, 18)[0]


def _section_names(blob: bytes) -> list[str]:
    if blob[4] != 2:
        return []
    e_shoff, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<QHHH', blob, 40)
    # e_shoff is at 40, but e_shentsize is at 58. Unpack separately.
    e_shoff = struct.unpack_from('<Q', blob, 40)[0]
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', blob, 58)
    if not e_shoff or not e_shnum:
        return []

    def shdr(i: int):
        off = e_shoff + i * e_shentsize
        return struct.unpack_from('<IIQQQQIIQQ', blob, off)

    str_off = shdr(e_shstrndx)[4]
    names = []
    for i in range(e_shnum):
        name_off = shdr(i)[0]
        names.append(blob[str_off + name_off:].split(b'\0', 1)[0].decode('latin1'))
    return names


def _prebuilt_native() -> dict[str, bytes] | None:
    """没装 zig 时沿用已验证过的 aarch64 钩子。这次只改 Java 悬浮窗，不重编 so。"""
    import zipfile
    apk = ROOT / 'android' / 'phisap-pocket.apk'
    if not apk.is_file():
        return None
    with zipfile.ZipFile(apk) as blob:
        names = set(blob.namelist())

        def pick(*cands: str) -> bytes | None:
            for name in cands:
                if name in names:
                    return blob.read(name)
            return None

        so = pick('lib/arm64-v8a/libphisap.so', 'assets/libphisap.so')
        tapd = pick('assets/phisap-tapd', 'lib/arm64-v8a/libphisap-tapd.so')
        inject = pick('assets/phisap-inject', 'lib/arm64-v8a/libphisap-inject.so')
    if not so or not tapd or not inject:
        return None
    return {'so': so, 'tapd': tapd, 'inject': inject}


def build_native() -> dict[str, bytes]:
    script = (NATIVE / 'inside.sh').read_bytes().replace(b'\r\n', b'\n')
    try:
        zig = find_zig()
    except RuntimeError:
        zig = None
    if zig:
        out = Path('/tmp/phisap-native')
        out.mkdir(parents=True, exist_ok=True)
        so = out / 'libphisap.so'
        tapd = out / 'phisap-tapd'
        inject = out / 'phisap-inject'
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-android', '-shared', '-fPIC',
            '-nostdlib', '-fno-stack-protector', '-O2', '-fno-exceptions',
            str(NATIVE / 'hook.c'), str(NATIVE / 'hook.S'), '-o', str(so),
        ])
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-musl', '-static', '-O2',
            '-fno-stack-protector', str(NATIVE / 'tapd.c'), '-o', str(tapd),
        ])
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-musl', '-static', '-O2',
            '-fno-stack-protector', str(NATIVE / 'inject.c'), str(NATIVE / 'cave.S'),
            '-o', str(inject),
        ])
        ioctl = out / 'libphisap-ioctl.so'
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-android', '-shared', '-fPIC',
            '-nostdlib', '-fno-stack-protector', '-fno-builtin', '-O2',
            '-Wl,-z,max-page-size=16384',
            str(NATIVE / 'ioctl.c'), '-o', str(ioctl),
        ])
        blobs = {
            'so': so.read_bytes(),
            'tapd': tapd.read_bytes(),
            'inject': inject.read_bytes(),
            'ioctl': ioctl.read_bytes(),
        }
    else:
        raise RuntimeError('找不到 zig，不能用旧包里的 inject 交差')
    blobs['script'] = script
    for name in ('so', 'tapd', 'inject'):
        if _elf_machine(blobs[name]) != 0xB7:
            raise RuntimeError(f'{name} 不是 aarch64')
    if blobs['so'][16] != 3:
        raise RuntimeError('libphisap.so 不是动态库')
    names = _section_names(blobs['so'])
    if '.init_array' not in names:
        raise RuntimeError('钩子没有 constructor')
    for needle in (b'JudgeLineControl', b'UpdateInfo', b'libil2cpp.so', b'phisap-hook'):
        if needle not in blobs['so']:
            raise RuntimeError(f'钩子里没有 {needle.decode()}')
    if b'LD_PRELOAD=' not in blobs['script']:
        raise RuntimeError('wrap 脚本没有设 LD_PRELOAD')
    if b'/dev/uinput' not in blobs['tapd']:
        raise RuntimeError('触摸守护没有打开 uinput')
    if b'phisap-inject-14' not in blobs['inject']:
        raise RuntimeError('inject 不是这一版，不能用旧的')
    if b'Java_app_phisap_pocket_Injector_nioctl' not in blobs['ioctl']:
        raise RuntimeError('触摸库没有 nioctl')
    if blobs['ioctl'][16] != 3:
        raise RuntimeError('libphisap-ioctl.so 不是动态库')
    if _elf_machine(blobs['ioctl']) != 0xB7:
        raise RuntimeError('触摸库不是 aarch64')
    return blobs


if __name__ == '__main__':
    built = build_native()
    for key, blob in built.items():
        print(key, len(blob))
