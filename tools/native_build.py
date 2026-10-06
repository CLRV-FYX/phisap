"""构建游戏内 ARM64 钩子、root 触摸守护和 native ioctl 桥。"""
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
    """没装 Zig 时沿用当前 APK 中已验证的 ARM64 钩子、触摸守护和 ioctl 桥。"""
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
        ioctl = pick('assets/libphisap-ioctl.so', 'lib/arm64-v8a/libphisap-ioctl.so')
    if not so or not tapd or not ioctl:
        return None
    return {'so': so, 'tapd': tapd, 'ioctl': ioctl}


def _needed_libc(blob: bytes) -> bool:
    if blob[:4] != b'\x7fELF' or blob[4] != 2:
        return False
    e_phoff = struct.unpack_from('<Q', blob, 32)[0]
    e_phentsize, e_phnum = struct.unpack_from('<HH', blob, 54)
    dyn_off = dyn_sz = 0
    loads = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from('<IIQQQQQQ', blob, off)
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
        if p_type == 2:
            dyn_off, dyn_sz = p_offset, p_filesz
    if not dyn_off:
        return False

    def v2off(v: int):
        for pv, po, psz in loads:
            if pv <= v < pv + psz:
                return po + (v - pv)
        return None

    str_v = 0
    needed = []
    for n in range(0, dyn_sz, 16):
        tag, val = struct.unpack_from('<qQ', blob, dyn_off + n)
        if tag == 0:
            break
        if tag == 5:
            str_v = val
        if tag == 1:
            needed.append(val)
    stro = v2off(str_v)
    if stro is None:
        return False
    names = [blob[stro + v:stro + v + 32].split(b'\0', 1)[0] for v in needed]
    return b'libc.so' in names and b'libdl.so' in names


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
        stub_dir = out / 'stub'
        stub_dir.mkdir(parents=True, exist_ok=True)
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-android', '-shared', '-fPIC',
            '-nostdlib', '-fno-stack-protector', '-O2',
            '-Wl,-z,max-page-size=16384', '-Wl,-soname,libc.so',
            str(NATIVE / 'stub.c'), '-o', str(stub_dir / 'libc.so'),
        ])
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-android', '-shared', '-fPIC',
            '-nostdlib', '-fno-stack-protector', '-O2',
            '-Wl,-z,max-page-size=16384', '-Wl,-soname,libdl.so',
            str(NATIVE / 'stubdl.c'), '-o', str(stub_dir / 'libdl.so'),
        ])
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-android', '-shared', '-fPIC',
            '-nostdlib', '-fno-stack-protector', '-O2', '-fno-exceptions', '-fno-builtin',
            '-Wl,-z,max-page-size=16384', '-Wl,--export-dynamic', '-Wl,--no-as-needed',
            str(NATIVE / 'hook.c'), str(NATIVE / 'hook.S'),
            str(stub_dir / 'libdl.so'), str(stub_dir / 'libc.so'), '-o', str(so),
        ])
        subprocess.check_call([
            zig, 'cc', '-target', 'aarch64-linux-musl', '-static', '-O2',
            '-fno-stack-protector', str(NATIVE / 'tapd.c'), '-o', str(tapd),
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
            'ioctl': ioctl.read_bytes(),
        }
    else:
        blobs = _prebuilt_native()
        if blobs is None:
            raise RuntimeError('找不到 zig，且当前 APK 中没有可复用的已验证原生库')
    blobs['script'] = script
    for name in ('so', 'tapd', 'ioctl'):
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
    script_text = blobs['script']
    if b'force-stop' in script_text or b'ptrace' in script_text or b'setenforce' in script_text:
        raise RuntimeError('新启动脚本仍尝试强停、ptrace 或关闭 SELinux')
    if b'phisap-inject' in script_text or b'PTRACE_' in script_text:
        raise RuntimeError('启动脚本仍走旧注入器')
    if b'/proc/$pid/maps' not in script_text or b'libphisap\\.so' not in script_text:
        raise RuntimeError('启动脚本没有用进程 maps 验证库是否实际加载')
    if b'Application.attach' not in script_text or b'System.load' not in script_text:
        raise RuntimeError('启动脚本没有说明进程内 System.load 路径')
    if b'/dev/uinput' not in blobs['tapd']:
        raise RuntimeError('触摸守护没有打开 uinput')
    if b'zygisk_module_entry' not in blobs['so']:
        raise RuntimeError('原生库缺少旧环境兼容入口')
    if b'usap64' not in blobs['so'] or b'phisap-hook-12' not in blobs['so'] or b'phisap_start' not in blobs['so']:
        raise RuntimeError('钩子不会在进程启动后再开工')
    if not _needed_libc(blobs['so']):
        raise RuntimeError('钩子没有按正常动态库依赖 libc')
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
