"""测试用的假 adb: 一个 Python 脚本, 按 adb 的命令行约定应答, 状态来自 JSON 文件。

用法(测试里): Adb(exe=[sys.executable, 'tests/fake_adb.py']), 环境变量 FAKE_ADB_STATE 指向状态文件。

状态文件字段(都可省略):
  devices      [[序列号, 状态], ...]               默认 [["EMU1", "device"]]
  packages     ["com.xxx", ...]                    pm list packages 的结果
  pm_paths     {包名: ["/data/app/.../base.apk", ...]}
  dirs         {目录: ["main.1.com.x.obb", ...]}    ls 的结果, 不在里面就是目录不存在
  denied_dirs  [目录, ...]                         ls 这些目录返回 Permission denied
  sources      {设备路径: 本地真实文件}              pull / stat 的数据来源
  sizes        {设备路径: 大小}                     覆盖 stat 报告的大小(用来造"报告大小和实际不符")
  versions     {包名: "3.20.0"}
  no_stat      true -> stat 不可用, 只能走 ls -l(模拟老系统)
  no_size      true -> stat 和 ls -l 都给不出大小
  chunk        每次写多少字节, 默认 65536
  chunk_sleep  每块之间睡多少秒(用来给取消/进度留出时间)
  truncate     pull 时少传多少字节但仍返回 0(模拟传输被截断)
  pull_fail    true -> 传一半然后以退出码 1 失败
  log_file     把每次调用的 argv 以 JSON 行追加到这个文件
"""
import json
import os
import shlex
import sys
import time


def load_state():
    with open(os.environ['FAKE_ADB_STATE'], encoding='utf-8') as f:
        return json.load(f)


def err(msg):
    print(msg, file=sys.stderr)


def main(argv):
    state = load_state()
    args = argv[1:]
    if state.get('log_file'):
        with open(state['log_file'], 'a', encoding='utf-8') as f:
            f.write(json.dumps(args, ensure_ascii=False) + '\n')
    serial = None
    if args[:1] == ['-s']:
        serial, args = args[1], args[2:]
    cmd = args[0] if args else ''
    devices = [tuple(d) for d in state.get('devices', [['EMU1', 'device']])]
    if cmd == 'devices':
        print('* daemon not running; starting now at tcp:5037')
        print('* daemon started successfully')
        print('List of devices attached')
        for s, st in devices:
            print(f'{s}\t{st}')
        return 0
    states = dict(devices)
    ready = [s for s, st in devices if st == 'device']
    if serial is None:
        if len(ready) > 1:
            err('adb: more than one device/emulator')
            return 1
        if not ready:
            err('adb: no devices/emulators found')
            return 1
        serial = ready[0]
    elif serial not in states:
        err(f"adb: device '{serial}' not found")
        return 1
    elif states[serial] != 'device':
        err(f'adb: device {states[serial]}')
        return 1
    if cmd == 'shell':
        return shell(state, shlex.split(' '.join(args[1:])))
    if cmd == 'pull':
        return pull(state, args[1], args[2])
    err(f'fake adb: 不支持的命令 {cmd}')
    return 1


def size_of(state, path):
    if path in state.get('sizes', {}):
        return state['sizes'][path]
    src = state.get('sources', {}).get(path)
    if src and os.path.isfile(src):
        return os.path.getsize(src)
    return None


def shell(state, words):
    if words[:3] == ['pm', 'list', 'packages']:
        for p in state.get('packages', []):
            print(f'package:{p}')
        return 0
    if words[:2] == ['pm', 'path']:
        paths = state.get('pm_paths', {}).get(words[2], [])
        for p in paths:
            print(f'package:{p}')
        return 0 if paths else 1
    if words[:2] == ['dumpsys', 'package']:
        ver = state.get('versions', {}).get(words[2])
        if ver:
            print(f'    versionName={ver}')
        return 0
    if words[:1] == ['ls'] and '-l' not in words:
        d = words[-1]
        if d in state.get('denied_dirs', []):
            err(f'ls: {d}: Permission denied')
            return 1
        if d in state.get('dirs', {}):
            for n in state['dirs'][d]:
                print(n)
            return 0
        err(f'ls: {d}: No such file or directory')
        return 1
    if words[:3] == ['stat', '-c', '%s']:
        if state.get('no_stat') or state.get('no_size'):
            err("stat: Unknown option 'c'")
            return 1
        size = size_of(state, words[3])
        if size is None:
            err(f"stat: '{words[3]}': No such file or directory")
            return 1
        print(size)
        return 0
    if words[:2] == ['ls', '-l']:
        size = None if state.get('no_size') else size_of(state, words[2])
        if size is None:
            err(f'ls: {words[2]}: No such file or directory')
            return 1
        print(f'-rw-r--r-- 1 system system {size} 2026-09-20 12:00 {os.path.basename(words[2])}')
        return 0
    err(f'fake adb: 不支持的 shell 命令 {words}')
    return 1


def pull(state, remote, local):
    src = state.get('sources', {}).get(remote)
    if not src or not os.path.isfile(src):
        err(f"adb: error: failed to stat remote object '{remote}': No such file or directory")
        return 1
    chunk = int(state.get('chunk', 65536))
    sleep = float(state.get('chunk_sleep', 0))
    total = os.path.getsize(src)
    limit = total - int(state.get('truncate', 0))
    if state.get('pull_fail'):
        limit = total // 2
    written = 0
    with open(src, 'rb') as fin, open(local, 'wb') as fout:
        while written < limit:
            data = fin.read(min(chunk, limit - written))
            if not data:
                break
            fout.write(data)
            fout.flush()
            written += len(data)
            if sleep:
                time.sleep(sleep)
    if state.get('pull_fail'):
        err('adb: error: failed to copy: remote read failed (device went offline)')
        return 1
    print(f'{remote}: 1 file pulled, 0 skipped. 99.0 MB/s ({written} bytes in 0.010s)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
