"""测试夹具: 用标准库手工拼出 Unity 的磁盘格式, 不需要真实游戏资源。

仓库里不能放游戏的版权素材, 所以解包相关的测试都靠这里现拼:

  build_serialized_file()  一个 SerializedFile(.assets), 里面只放 TextAsset(谱面 JSON)
  build_bundle()           UnityFS 容器, 支持 LZ4 / LZMA / 不压缩, 块信息在头部或尾部,
                           以及较新 Unity 版本才有的"块信息后补齐 16 字节"标志位
  lz4_block_compress()     朴素的 LZ4 块压缩器(有真正的匹配, 含重叠匹配), 用来喂解压器
  catalog_v3() / catalog_legacy()  Addressables catalog.json 两种格式
  build_apk()              把上面这些装进一个 zip, 当 APK / OBB 用

字段布局按 extract.py 实际解析的顺序写。这套夹具另外用 UnityPy 交叉验证过
(见 tests/test_unity_bundle.py 里带 skipUnless 的那组), 避免"解析器和夹具一起错"。
"""
from __future__ import annotations

import base64
import hashlib
import json
import lzma
import struct
import zipfile

TEXT_ASSET_CLASS_ID = 49


# ---------------------------------------------------------------- 小工具

class Writer:
    """带绝对位置的二进制写入器(对齐要按文件内的绝对偏移算)"""

    def __init__(self, little: bool = True, base: int = 0):
        self.buf = bytearray()
        self.little = little
        self.base = base

    @property
    def pos(self) -> int:
        return self.base + len(self.buf)

    def _pack(self, fmt: str, v):
        self.buf += struct.pack(('<' if self.little else '>') + fmt, v)

    def u8(self, v):
        self._pack('B', v)

    def u16(self, v):
        self._pack('H', v)

    def u32(self, v):
        self._pack('I', v)

    def i32(self, v):
        self._pack('i', v)

    def i64(self, v):
        self._pack('q', v)

    def u64(self, v):
        self._pack('Q', v)

    def raw(self, b: bytes):
        self.buf += b

    def cstr(self, s: str):
        self.buf += s.encode('utf-8') + b'\0'

    def align(self, n: int):
        self.buf += b'\0' * (-self.pos % n)

    def bytes(self) -> bytes:
        return bytes(self.buf)


def align_up(n: int, a: int) -> int:
    return n + (-n % a)


# ---------------------------------------------------------------- LZ4 / LZMA

def _emit_lz4_sequence(out: bytearray, literals: bytes, offset: int = 0, match_len: int = 0):
    lit = len(literals)
    token = min(lit, 15) << 4
    if match_len:
        token |= min(match_len - 4, 15)
    out.append(token)
    if lit >= 15:
        rest = lit - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += literals
    if match_len:
        out += struct.pack('<H', offset)
        if match_len - 4 >= 15:
            rest = match_len - 4 - 15
            while rest >= 255:
                out.append(255)
                rest -= 255
            out.append(rest)


def lz4_block_compress(src: bytes) -> bytes:
    """朴素贪心 LZ4 块压缩。遵守规范里「最后 5 字节必须是字面量」的限制。"""
    n = len(src)
    out = bytearray()
    anchor = 0
    i = 0
    table: dict[bytes, int] = {}
    limit = n - 12  # 匹配起点必须在末尾 12 字节之前
    while i <= limit:
        key = src[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is not None and 0 < i - cand <= 65535:
            m = 4
            while i + m < n - 5 and src[cand + m] == src[i + m]:
                m += 1
            _emit_lz4_sequence(out, src[anchor:i], i - cand, m)
            i += m
            anchor = i
        else:
            i += 1
    _emit_lz4_sequence(out, src[anchor:])
    return bytes(out)


LZMA_DICT = 1 << 16


def lzma_raw_compress(data: bytes) -> bytes:
    """Unity 的 LZMA 块: 1 字节 props + 4 字节字典大小 + 裸 LZMA1 流"""
    lc, lp, pb = 3, 0, 2
    comp = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=[
        {'id': lzma.FILTER_LZMA1, 'dict_size': LZMA_DICT, 'lc': lc, 'lp': lp, 'pb': pb}])
    body = comp.compress(data) + comp.flush()
    return bytes([(pb * 5 + lp) * 9 + lc]) + struct.pack('<I', LZMA_DICT) + body


# ---------------------------------------------------------------- SerializedFile

# TextAsset 的类型树(Unity 2019+ 的结构): (层级, 类型名, 字段名, 字节数, 下标, 类型标志)
TEXT_ASSET_TREE = [
    (0, 'TextAsset', 'Base', -1, 0, 0),
    (1, 'string', 'm_Name', -1, 1, 0),
    (2, 'Array', 'Array', -1, 2, 1),
    (3, 'int', 'size', 4, 3, 0),
    (3, 'char', 'data', 1, 4, 0),
    (1, 'string', 'm_Script', -1, 5, 0),
    (2, 'Array', 'Array', -1, 6, 1),
    (3, 'int', 'size', 4, 7, 0),
    (3, 'char', 'data', 1, 8, 0),
]


def _write_type_tree(w: 'Writer', version: int):
    """类型树的 blob 格式(格式版本 >= 12): 节点表 + 字符串表, 21 起末尾还有类型依赖数组"""
    strings = bytearray()
    offsets: dict[str, int] = {}
    for _lvl, tname, fname, *_ in TEXT_ASSET_TREE:
        for name in (tname, fname):
            if name not in offsets:
                offsets[name] = len(strings)
                strings += name.encode() + b'\0'
    w.i32(len(TEXT_ASSET_TREE))
    w.i32(len(strings))
    for lvl, tname, fname, size, index, flags in TEXT_ASSET_TREE:
        w.u16(1)                 # 节点版本
        w.u8(lvl)
        w.u8(flags)
        w.u32(offsets[tname])
        w.u32(offsets[fname])
        w.i32(size)
        w.i32(index)
        w.i32(0x4000 if size == -1 else 0)  # meta_flag
        if version >= 19:
            w.u64(0)             # ref_type_hash
    w.raw(bytes(strings))
    if version >= 21:
        w.u32(0)                 # 类型依赖


def build_serialized_file(texts: list[tuple[str, str]], *, version: int = 17,
                          unity_version: str = '2019.4.31f1', platform: int = 13,
                          type_tree: bool = False) -> bytes:
    """一个只含 TextAsset 的 SerializedFile。支持格式版本 17..22(22 起头部变宽)。
    type_tree=True 时写入完整的 TextAsset 类型树(正式构建里很常见, 解析器必须能正确跳过/读取它)"""
    if not 17 <= version <= 22:
        raise ValueError('夹具只实现了 SerializedFile 格式 17..22')

    # 对象数据: m_Name(对齐字符串) + m_Script(对齐字符串)
    blobs: list[bytes] = []
    for name, text in texts:
        d = Writer()
        nb = name.encode('utf-8')
        d.i32(len(nb))
        d.raw(nb)
        d.align(4)
        tb = text if isinstance(text, bytes) else text.encode('utf-8')  # 允许直接给字节(测试非 UTF-8 内容)
        d.i32(len(tb))
        d.raw(tb)
        d.align(4)
        blobs.append(d.bytes())

    header_size = 20 if version < 22 else 48
    meta = Writer(little=True, base=header_size)
    meta.cstr(unity_version)
    meta.i32(platform)
    meta.u8(1 if type_tree else 0)  # enable_type_tree
    meta.i32(1)                     # 只有一个类型: TextAsset
    meta.i32(TEXT_ASSET_CLASS_ID)
    meta.u8(0)                      # is_stripped_type
    meta.u16(0xFFFF)                # script_type_index
    meta.raw(b'\0' * 16)            # old_type_hash
    if type_tree:
        _write_type_tree(meta, version)
    meta.i32(len(blobs))
    rel = 0
    for idx, blob in enumerate(blobs):
        meta.align(4)
        meta.i64(idx + 1)           # path_id
        if version < 22:
            meta.u32(rel)
        else:
            meta.i64(rel)
        meta.u32(len(blob))
        meta.i32(0)                 # 类型下标
        rel += align_up(len(blob), 8)
    meta.i32(0)                     # script types
    meta.i32(0)                     # externals
    if version >= 20:
        meta.i32(0)                 # ref types
    meta.cstr('')                   # user information

    data_offset = align_up(header_size + len(meta.buf), 16)
    data = bytearray()
    for blob in blobs:
        data += blob + b'\0' * (-len(blob) % 8)
    file_size = data_offset + len(data)
    pad = b'\0' * (data_offset - header_size - len(meta.buf))

    h = Writer(little=False)
    if version < 22:
        h.u32(len(meta.buf))
        h.u32(file_size)
        h.u32(version)
        h.u32(data_offset)
        h.u8(0)                     # 小端
        h.raw(b'\0\0\0')
    else:
        h.u32(0)
        h.u32(0)
        h.u32(version)
        h.u32(0)
        h.u8(0)
        h.raw(b'\0\0\0')
        h.u32(len(meta.buf))
        h.u64(file_size)
        h.u64(data_offset)
        h.u64(0)
    assert len(h.buf) == header_size
    return h.bytes() + meta.bytes() + pad + bytes(data)


# ---------------------------------------------------------------- UnityFS

COMPRESSION_FLAG = {'none': 0, 'lzma': 1, 'lz4': 2, 'lz4hc': 3}


def _compress(data: bytes, mode: str) -> bytes:
    if mode == 'none':
        return data
    if mode in ('lz4', 'lz4hc'):
        return lz4_block_compress(data)
    if mode == 'lzma':
        return lzma_raw_compress(data)
    raise ValueError(mode)


def build_bundle(files: dict[str, bytes], *, version: int = 7, engine: str = '2019.4.31f1',
                 player: str = '5.x.x', compression: str = 'lz4', info_at_end: bool = False,
                 block_size: int = 4096, padding_flag: bool = False, extra_flags: int = 0,
                 block_compression: str | None = None) -> bytes:
    """拼一个 UnityFS 容器。

    info_at_end    块信息放在文件末尾(标志位 0x80), 否则紧跟头部(0x40)
    padding_flag   设置 0x200: Unity 2021.3.2+/2022.1+ 里表示「块信息之后补齐到 16 字节再放数据」
                   (更老的版本里同一位表示 UnityCN 加密, 解析器按版本号区分)
    extra_flags    原样 OR 进头部标志, 用来造「加密」之类的位
    """
    block_compression = block_compression or compression
    nodes = []
    data = bytearray()
    for path, content in files.items():
        nodes.append((len(data), len(content), path))
        data += content

    blocks = []
    for off in range(0, max(len(data), 1), block_size):
        chunk = bytes(data[off:off + block_size])
        comp = _compress(chunk, block_compression)
        # 压缩后反而更大时 Unity 会存原始块; 这里也照做以覆盖「块标志为 0」的分支
        if block_compression != 'none' and len(comp) >= len(chunk):
            blocks.append((len(chunk), chunk, 0))
        else:
            blocks.append((len(chunk), comp, COMPRESSION_FLAG[block_compression]))

    info = Writer(little=False)
    info.raw(b'\0' * 16)
    info.i32(len(blocks))
    for usize, payload, flags in blocks:
        info.u32(usize)
        info.u32(len(payload))
        info.u16(flags)
    info.i32(len(nodes))
    for off, size, path in nodes:
        info.i64(off)
        info.i64(size)
        info.u32(4)
        info.cstr(path)
    info_raw = info.bytes()
    info_comp = _compress(info_raw, compression)

    flags = COMPRESSION_FLAG[compression] | (0x80 if info_at_end else 0x40)
    if padding_flag:
        flags |= 0x200
    flags |= extra_flags

    def header(total: int) -> bytes:
        h = Writer(little=False)
        h.cstr('UnityFS')
        h.u32(version)
        h.cstr(player)
        h.cstr(engine)
        h.i64(total)
        h.u32(len(info_comp))
        h.u32(len(info_raw))
        h.u32(flags)
        return h.bytes()

    payload = b''.join(p for _, p, _ in blocks)
    body = Writer(little=False, base=len(header(0)))
    body.align(16)  # version>=7 或 2019.4.15+ 的头部之后补齐到 16
    if info_at_end:
        body.raw(payload)
        body.raw(info_comp)
    else:
        body.raw(info_comp)
        if padding_flag:
            body.align(16)
        body.raw(payload)
    total = len(header(0)) + len(body.buf)
    return header(total) + body.bytes()


def make_chart_bundle(name: str, text: str, **kw) -> bytes:
    """一个谱面 bundle: 容器里只有一个 CAB 文件, 里面一个 TextAsset"""
    sf_kw = {k: kw.pop(k) for k in ('sf_version', 'platform', 'type_tree') if k in kw}
    sf = build_serialized_file(
        [(name, text)], version=sf_kw.get('sf_version', 17), platform=sf_kw.get('platform', 13),
        unity_version=kw.get('engine', '2019.4.31f1'), type_tree=sf_kw.get('type_tree', False))
    cab = 'CAB-' + hashlib.md5(name.encode()).hexdigest()
    return build_bundle({cab: sf}, **kw)


# ---------------------------------------------------------------- Addressables catalog

def bundle_name_for(address: str) -> str:
    """32 位十六进制 + .bundle, 和真实 catalog 里的 bundle 文件名同形"""
    return hashlib.md5(address.encode()).hexdigest() + '.bundle'


def catalog_v3(pairs: list[tuple[str, str]]) -> dict:
    """3.20.0+ 的紧凑 catalog。pairs = [(资产路径, bundle 文件名)]。编码规则同 tests/test_catalog.py"""
    keys_blob = bytearray()
    offsets = []
    keys: list[str] = []
    for address, bundle in pairs:
        for k in (address, bundle):
            offsets.append(len(keys_blob))
            enc = k.encode('utf-8')
            keys_blob += b'\x00' + struct.pack('<I', len(enc)) + enc
            keys.append(k)
    entry_targets = []
    for i in range(len(pairs)):
        entry_targets += [2 * i + 1, -1]  # 资产键 -> 对应 bundle 键
    entry_blob = struct.pack('<I', len(entry_targets)) + b'\0' * 8
    for t in entry_targets:
        entry_blob += struct.pack('<i', t) + b'\0' * 24
    bucket_blob = struct.pack('<I', len(keys))
    for i in range(len(keys)):
        bucket_blob += struct.pack('<III', offsets[i], 1, i)
    return {
        'm_KeyDataString': base64.b64encode(bytes(keys_blob)).decode(),
        'm_BucketDataString': base64.b64encode(bucket_blob).decode(),
        'm_EntryDataString': base64.b64encode(entry_blob).decode(),
        'm_InternalIds': ['x'] * len(keys),
    }


def catalog_legacy(pairs: list[tuple[str, str]]) -> dict:
    """3.14 及以前的旧格式 catalog(含 m_resourceTypes / m_ExtraDataString)"""
    keys: list[str] = []
    for address, bundle in pairs:
        keys += [address, bundle]
    key_blob = struct.pack('<I', len(keys))
    for k in keys:
        enc = k.encode('utf-8')
        key_blob += b'\x00' + struct.pack('<I', len(enc)) + enc
    bucket_blob = struct.pack('<I', len(keys))
    for _ in keys:
        bucket_blob += struct.pack('<ii', 0, 0)  # offset, 0 个条目
    entry_blob = struct.pack('<I', len(pairs))
    for i in range(len(pairs)):
        # internalId, provider, dependencyKey, depHash, dataIndex, primaryKey, resourceType
        entry_blob += struct.pack('<7i', 0, 0, 2 * i + 1, 0, -1, 2 * i, 0)
    return {
        'm_BucketDataString': base64.b64encode(bucket_blob).decode(),
        'm_KeyDataString': base64.b64encode(key_blob).decode(),
        'm_EntryDataString': base64.b64encode(entry_blob).decode(),
        'm_ExtraDataString': base64.b64encode(b'\0' * 4).decode(),
        'm_InternalIds': ['internal'],
        'm_ProviderIds': ['provider'],
        'm_resourceTypes': [{'m_AssemblyName': 'a', 'm_ClassName': 'b'}],
    }


def catalog_v4(pairs: list[tuple[str, str]], *, key_style: str = 'hash_file', prefix_sep: str | None = '#',
               bundles_first: bool = True, noise_keys: bool = True, guid_keys: bool = True,
               bad_bucket_offsets: bool = False) -> dict:
    """按 Addressables 真实的表结构拼的 catalog(Phigros 4.0.0 的样子)。pairs = [(资产路径, 磁盘上真实的 bundle 文件名)]。

    和 catalog_v3(沿用 tests/test_catalog.py 的简化编码)不同, 这里是真实布局:
      键表: 开头 4 字节键个数, 之后是顺序排列的键(0=ascii, 1=utf16, 4=整数); 桶表按偏移指回键表
      条目表: 开头 4 字节个数, 每条 7 个 int32: internalId, provider, dependencyKey, depHash, data, primaryKey, resourceType
      资源条目的 dependencyKey 指向 bundle 的键; bundle 的真实文件名只在它自己条目的加载路径(m_InternalIds)里
    key_style   bundle 键的写法: 'hash_file' = <哈希>_<真实文件名>.bundle(4.0.0 起, 和磁盘文件名不一致),
                'plain' = 键就是真实文件名(旧版)
    prefix_sep  m_InternalIds 用前缀压缩时的分隔符('#' 是 Addressables 的写法, ':' 是见过的另一种), None = 不压缩
    bundles_first  True = bundle 条目排在前面(键下标 == 加载路径下标, "经验做法"碰巧成立);
                   False = 资源条目在前, 这时只有按"依赖键的桶 -> bundle 条目 -> 加载路径"取才是对的
    guid_keys   每个资源条目除了地址键还登记一个 32 位十六进制的 Unity GUID 键(真实 catalog 里都有),
                它们会让后面的键下标和条目下标错位
    bad_bucket_offsets  桶表里的键偏移全是垃圾: 只能靠键表自己的顺序读键, 桶->条目的关系也不可信
    """
    runtime = '{UnityEngine.AddressableAssets.Addressables.RuntimePath}/Android/'
    prefixes = [runtime, 'Assets/Tracks/'] if prefix_sep else []

    def internal_id(path: str) -> str:
        if prefix_sep:
            for i, pre in enumerate(prefixes):
                if path.startswith(pre):
                    return f'{i}{prefix_sep}{path[len(pre):]}'
        return path

    bundle_keys: dict[str, str] = {}
    for _asset, real in pairs:
        if real not in bundle_keys:
            digest = hashlib.md5(real.encode()).hexdigest()
            bundle_keys[real] = real if key_style == 'plain' else f'{digest}_{real}'
    uniq_bundles = list(bundle_keys)

    # 条目: (主键字符串, 加载路径, 依赖键字符串或 None)
    bundle_entries = [(bundle_keys[r], runtime + r, None) for r in uniq_bundles]
    asset_entries = [(a, a, bundle_keys[r]) for a, r in pairs]
    entries_spec = bundle_entries + asset_entries if bundles_first else asset_entries + bundle_entries

    keys: list = []
    key_index: dict = {}

    def kidx(k):
        if k not in key_index:
            key_index[k] = len(keys)
            keys.append(k)
        return key_index[k]

    guid_of: dict[str, str] = {}
    for pk, _iid, dep in entries_spec:  # 键按条目顺序登记: 主键先于依赖键
        kidx(pk)
        if guid_keys and dep is not None:  # 只有资源条目(有依赖)才有 GUID
            guid_of[pk] = hashlib.md5(('guid:' + pk).encode()).hexdigest()
            kidx(guid_of[pk])
        if dep is not None:
            kidx(dep)
    if noise_keys:  # 夹几个不是字符串的键(整数键、utf16 键), 解析器必须跳得过去
        for n in (7, 4096, 'Ünïcode-键'):
            kidx(n)

    internal_ids: list[str] = []
    entry_blob = bytearray()
    entry_blob += struct.pack('<I', len(entries_spec))
    entry_keys: list[list[int]] = [[] for _ in keys]
    for ei, (pk, iid, dep) in enumerate(entries_spec):
        internal_ids.append(internal_id(iid))
        entry_blob += struct.pack('<7i', len(internal_ids) - 1, 0, -1 if dep is None else key_index[dep], 0, -1,
                                  key_index[pk], 0)
        entry_keys[key_index[pk]].append(ei)
        if pk in guid_of:
            entry_keys[key_index[guid_of[pk]]].append(ei)

    key_blob = bytearray(struct.pack('<I', len(keys)))
    offsets = []
    for k in keys:
        offsets.append(len(key_blob))
        if isinstance(k, int):
            key_blob += b'\x04' + struct.pack('<I', k)
        elif k.isascii():
            key_blob += b'\x00' + struct.pack('<I', len(k)) + k.encode()
        else:
            enc = k.encode('utf-16le')
            key_blob += b'\x01' + struct.pack('<I', len(enc)) + enc
    bucket_blob = bytearray(struct.pack('<I', len(keys)))
    for i in range(len(keys)):
        bucket_blob += struct.pack('<II', 0x7FFFFF00 if bad_bucket_offsets else offsets[i], len(entry_keys[i])) + b''.join(
            struct.pack('<I', e) for e in entry_keys[i])

    out = {
        'm_LocatorId': 'AddressablesMainContentCatalog',
        'm_KeyDataString': base64.b64encode(bytes(key_blob)).decode(),
        'm_BucketDataString': base64.b64encode(bytes(bucket_blob)).decode(),
        'm_EntryDataString': base64.b64encode(bytes(entry_blob)).decode(),
        'm_InternalIds': internal_ids,
    }
    if prefix_sep:
        out['m_InternalIdPrefixes'] = prefixes
    return out


# ---------------------------------------------------------------- APK / OBB

def build_apk(path: str, members: dict[str, bytes], *, stored_ext: tuple[str, ...] = ('.bundle',)) -> str:
    """装一个 zip 当 APK/OBB。.bundle 用 STORED(真实安装包里就是不压缩的), 其余 DEFLATE"""
    with zipfile.ZipFile(path, 'w') as z:
        for name, content in members.items():
            mode = zipfile.ZIP_STORED if name.endswith(stored_ext) else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name), content, compress_type=mode)
    return path


def chart_json(tag: str = 'x', n_lines: int = 3) -> str:
    """一份看起来像官谱的小 JSON(只给测试用, 不是真谱面)"""
    return json.dumps({'formatVersion': 3, 'offset': 0.0, 'tag': tag,
                       'judgeLineList': [{'bpm': 120.0, 'notesAbove': [], 'notesBelow': []}
                                         for _ in range(n_lines)]}, ensure_ascii=False)


def _catalog_for(kind: str, pairs: list[tuple[str, str]]) -> dict:
    """kind: 'v3' / 'legacy' / 'v4' / 'v4:plain' / 'v4:colon' / 'v4:nocompress' / 'v4:assets_first'"""
    if kind == 'v3':
        return catalog_v3(pairs)
    if kind == 'legacy':
        return catalog_legacy(pairs)
    if kind.startswith('v4'):
        opts = kind.split(':')[1:]
        return catalog_v4(pairs, key_style='plain' if 'plain' in opts else 'hash_file',
                          prefix_sep=None if 'nocompress' in opts else (':' if 'colon' in opts else '#'),
                          bundles_first='assets_first' not in opts)
    raise ValueError(kind)


def make_package(path: str, charts: dict[str, str], *, catalog: str = 'v3',
                 extra_bundles: dict[str, bytes] | None = None, bundle_kw: dict | None = None,
                 catalog_member: str = 'assets/aa/catalog.json') -> dict[str, str]:
    """造一个带 catalog + 若干谱面 bundle 的包。charts = {资产路径: 谱面文本}。
    返回 {资产路径: bundle 文件名}"""
    bundle_kw = bundle_kw or {}
    members: dict[str, bytes] = {}
    pairs = []
    names = {}
    for address, text in charts.items():
        bname = bundle_name_for(address)
        stem = address.rsplit('/', 1)[-1].rsplit('.', 1)[0].split(' #')[0]
        members['assets/aa/Android/' + bname] = make_chart_bundle(stem, text, **bundle_kw)
        pairs.append((address, bname))
        names[address] = bname
    if extra_bundles:
        members.update(extra_bundles)
    if catalog_member:
        members[catalog_member] = json.dumps(_catalog_for(catalog, pairs)).encode('utf-8')
    build_apk(path, members)
    return names
