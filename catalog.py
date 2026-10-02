import json
import re
import struct
from dataclasses import dataclass, field
from io import BytesIO
from base64 import b64decode
from binary_reader import BinaryReader


def detect_catalog_version(data: dict) -> str:
    """区分catalog.json的格式版本。

    3.20.0及以后的版本移除了m_resourceTypes/m_ExtraDataString等字段，
    改用紧凑的bucket/key/entry编码，此处以字段存在性来区分。
    """
    if 'm_resourceTypes' in data and 'm_ExtraDataString' in data:
        return 'legacy'
    return 'v3'


_PREFIX_ID_RE = re.compile(r'^(\d+)[#:](.*)$', re.DOTALL)
TRACK_PREFIX = 'Assets/Tracks/'


def expand_internal_id(raw, prefixes: list) -> str | None:
    """展开 m_InternalIds 里的条目。

    新版 Addressables 会把路径前缀抽到 m_InternalIdPrefixes 里, 条目写成 "<前缀序号>#<余下部分>"
    (实际抓到的 Phigros 目录里也见过用冒号的写法), 不展开的话取到的"文件名"会带着 "0#" 这样的前缀。
    """
    if not isinstance(raw, str):
        return None
    m = _PREFIX_ID_RE.match(raw)
    if m and int(m.group(1)) < len(prefixes) and isinstance(prefixes[int(m.group(1))], str):
        return prefixes[int(m.group(1))] + m.group(2)
    return raw


def bundle_basename(path: str) -> str:
    return re.split(r'[\\/]', path)[-1]


@dataclass
class TrackEntry:
    """catalog 里 Assets/Tracks 下的一个资源, 以及它可能所在的 bundle 文件名(可信度从高到低)"""
    asset_path: str
    bundles: list = field(default_factory=list)


def _read_key_at(blob: bytes, pos: int):
    """读一个键; 认不得的类型返回 None(不抛异常: 键表里夹着别的类型的键是正常的)"""
    try:
        typ = blob[pos]
        pos += 1
        if typ in (0, 1):
            length = struct.unpack_from('<I', blob, pos)[0]
            raw = blob[pos + 4:pos + 4 + length]
            return raw.decode('utf-8' if typ == 0 else 'utf-16le', 'replace')
        if typ == 4:
            return struct.unpack_from('<I', blob, pos)[0]
    except (IndexError, struct.error):
        pass
    return None


def parse_track_entries(data: dict) -> tuple:
    """直接按 Addressables 的表结构读出 Assets/Tracks 下的资源和它们的 bundle 文件名。

    返回 (entries: list[TrackEntry], stats: dict)。旧版/新版 catalog 的表结构是同一套:
      m_KeyDataString    键表(字符串/整数)
      m_BucketDataString 键下标 -> (键在键表里的偏移, 条目下标列表)
      m_EntryDataString  条目表, 每条 7 个 int32: internalId, provider, dependencyKey, depHash, data,
                         primaryKey, resourceType
      m_InternalIds(+m_InternalIdPrefixes)  每个条目真正的加载路径
    资源条目的 dependencyKey 指向 bundle 的键, bundle 在磁盘上的真实文件名只能从它的 internalId
    (加载路径)里取 basename: 从 Phigros 4.0.0 起键里的 bundle 名(<hash>_<file>.bundle)和真实文件名不再一致,
    直接拿键当文件名就一个 bundle 也对不上。
    """
    key_blob = b64decode(data['m_KeyDataString'])
    bucket_blob = b64decode(data['m_BucketDataString'])
    entry_blob = b64decode(data['m_EntryDataString'])
    prefixes = data.get('m_InternalIdPrefixes') or []
    raw_ids = data.get('m_InternalIds') or []
    internal_ids = [expand_internal_id(x, prefixes) or '' for x in raw_ids]

    # 桶表: 第 i 个桶对应第 i 个键
    buckets: list = []  # [(键偏移, [条目下标])]
    count = struct.unpack_from('<I', bucket_blob, 0)[0]
    pos = 4
    for _ in range(count):
        offset, n = struct.unpack_from('<II', bucket_blob, pos)
        pos += 8
        buckets.append((offset, list(struct.unpack_from(f'<{n}I', bucket_blob, pos))))
        pos += 4 * n

    keys = [_read_key_at(key_blob, off) for off, _ in buckets]
    if sum(k is None for k in keys) > len(keys) // 2:
        # 偏移对不上: 退回按顺序读(键表开头是个键的个数)
        keys = []
        pos = 4
        for _ in range(struct.unpack_from('<I', key_blob, 0)[0]):
            k = _read_key_at(key_blob, pos)
            if k is None:
                break
            keys.append(k)
            length = struct.unpack_from('<I', key_blob, pos + 1)[0] if isinstance(k, str) else 4
            pos += 1 + 4 + (length if isinstance(k, str) else 0)

    # 条目表: 开头 4 字节是个数, 之后每条 28 字节
    n_entries = struct.unpack_from('<I', entry_blob, 0)[0]
    base = 4 if 4 + 28 * n_entries <= len(entry_blob) else max(len(entry_blob) - 28 * n_entries, 0)
    entries = [struct.unpack_from('<7i', entry_blob, base + 28 * i) for i in range(n_entries)
               if base + 28 * (i + 1) <= len(entry_blob)]

    # 主键下标 -> 以它为主键的条目(bundle 自己的条目: 主键就是 bundle 的键)
    by_primary: dict = {}
    for e in entries:
        by_primary.setdefault(e[5], []).append(e)

    def bundle_names(dep_key: int) -> list:
        names: list = []

        def add(name):
            if isinstance(name, str) and name.endswith('.bundle') and name not in names:
                names.append(name)

        # 1. 正解: 资源条目的 dependencyKey 指向 bundle 的键, 以这个键为主键的条目就是 bundle 自己的条目,
        #    它的 internalId 指向 m_InternalIds 里的真实加载路径(取 basename 才是磁盘上的文件名)
        for e in by_primary.get(dep_key, []):
            if 0 <= e[0] < len(internal_ids):
                add(bundle_basename(internal_ids[e[0]]))
        # 2. 同一件事换个路: 依赖键的桶里列出的条目
        if 0 <= dep_key < len(buckets):
            for ei in buckets[dep_key][1]:
                if 0 <= ei < len(entries) and 0 <= entries[ei][0] < len(internal_ids):
                    add(bundle_basename(internal_ids[entries[ei][0]]))
        # 3. 经验做法: bundle 条目排在最前面时, 键下标 == 加载路径下标
        if 0 <= dep_key < len(internal_ids):
            add(bundle_basename(internal_ids[dep_key]))
        # 4. 最后才是键本身(旧版 Phigros 里它就是文件名)
        if 0 <= dep_key < len(keys):
            add(keys[dep_key])
        return names

    out: list = []
    for internal_id, _provider, dep_key, _hash, _data, primary_key, _rtype in entries:
        if not 0 <= primary_key < len(keys):
            continue
        asset = keys[primary_key]
        if not isinstance(asset, str) or not asset.startswith(TRACK_PREFIX):
            continue
        out.append(TrackEntry(asset, bundle_names(dep_key)))

    str_keys = [k for k in keys if isinstance(k, str)]
    dirs: dict = {}
    for k in str_keys:
        if '/' in k:
            head = '/'.join(k.split('/')[:2])
            dirs[head] = dirs.get(head, 0) + 1
    stats = {
        'keys': len(keys), 'entries': len(entries), 'internal_ids': len(raw_ids),
        'bundle_ids': sum(1 for x in internal_ids if x.endswith('.bundle')), 'prefixes': len(prefixes),
        'track_assets': len(out),
        'asset_samples': [e.asset_path for e in out[:3]],
        'bundle_samples': [n for e in out[:3] for n in e.bundles[:2]],
        # 以下几项只给诊断用: 资源路径对不上时, 看一眼 catalog 里到底有些什么键
        'key_samples': str_keys[:6],
        'top_dirs': sorted(dirs.items(), key=lambda kv: -kv[1])[:5],
    }
    return out, stats


class _NoCatalog:
    fname_map: dict = {}


def load_catalog(data: dict):
    """解析catalog.json，自动识别格式版本。

    返回带有这些属性的对象:
      fname_map      {bundle文件名: 资产路径(Assets/Tracks/...)}  (老接口, 一个 bundle 只对应一个资产)
      track_entries  [TrackEntry]  每个资产 -> 候选 bundle 文件名(按可信度排序), 一个 bundle 里有多个资产也不会丢
      stats          诊断用的统计(键/条目/加载路径的个数, 样例)
    两套解析谁出错都不致命, 只要有一套读出东西就行; 都读不出来才抛异常。
    """
    cat = None
    first_error = None
    try:
        cat = Catalog(data) if detect_catalog_version(data) == 'legacy' else CatalogV3(data)
    except Exception as e:  # noqa: BLE001
        first_error = e
    track: list = []
    stats: dict = {}
    try:
        track, stats = parse_track_entries(data)
    except Exception as e:  # noqa: BLE001
        first_error = first_error or e
    if cat is None and not track:
        raise first_error if first_error is not None else ValueError('catalog 里没有可用的表')
    if cat is None:
        cat = _NoCatalog()
        cat.fname_map = {}
    # 老接口读出来的 (bundle -> 资产) 也并进来: 新解析器没认出来的资产靠它兜底, 认出来的把它的 bundle 名当作备选
    by_asset = {e.asset_path: e for e in track}
    for bundle, asset in cat.fname_map.items():
        if asset in by_asset:
            if bundle not in by_asset[asset].bundles:
                by_asset[asset].bundles.append(bundle)
        else:
            by_asset[asset] = TrackEntry(asset, [bundle])
    cat.track_entries = list(by_asset.values())
    cat.stats = stats
    return cat


class Catalog:
    """旧版catalog格式（Phigros 3.14及以前）"""
    buckets: list[dict]
    keys: list
    entries: list[dict]
    fname_map: dict[str, str]

    def __init__(self, data: dict) -> None:
        self.buckets = []
        bds = data['m_BucketDataString']
        reader = BinaryReader(BytesIO(b64decode(bds)), False)
        bucket_count = reader.u32
        for _ in range(bucket_count):
            self.buckets.append({
                'offset': reader.i32,
                'entries': [reader.i32 for _ in range(reader.i32)]
            })

        self.keys = []
        kds = data['m_KeyDataString']
        reader = BinaryReader(BytesIO(b64decode(kds)), False)
        key_count = reader.u32
        for _ in range(key_count):
            self.keys.append(self.read_object(reader))

        eds = BinaryReader(BytesIO(b64decode(data['m_EntryDataString'])), False)
        xds = BinaryReader(BytesIO(b64decode(data['m_ExtraDataString'])), False)
        entry_count = eds.u32
        self.entries = []
        iids = data['m_InternalIds']
        pids = data['m_ProviderIds']
        rtps = data['m_resourceTypes']
        for _ in range(entry_count):
            internal_id = eds.i32
            provider_index = eds.i32
            dependency_key_index = eds.i32
            dep_hash = eds.i32
            data_index = eds.i32
            primary_key = eds.i32
            resource_type = eds.i32
            obj = None
            if data_index >= 0:
                xds.pos = data_index
                obj = self.read_object(xds)
            self.entries.append({
                'internalId': iids[internal_id],
                'provider': pids[provider_index],
                'dependencyKey': None if dependency_key_index < 0 else self.keys[dependency_key_index],
                'depHash': dep_hash,
                'primaryKey': self.keys[primary_key],
                'resourceType': rtps[resource_type],
                'data': obj,
                'keys': []
            })

        for b, k in zip(self.buckets, self.keys):
            for e in b['entries']:
                self.entries[e]['keys'].append(k)

        self.fname_map = {}
        for e in self.entries:
            if isinstance(e['primaryKey'], str) and isinstance(e['dependencyKey'], str):
                self.fname_map[e['dependencyKey']] = e['primaryKey']

    @classmethod
    def read_object(cls, reader: BinaryReader):
        obj_type = reader.u8
        if obj_type == 0:  # ascii string
            return reader.string(reader.u32)
        elif obj_type == 1:  # unicode(16) string
            return reader.string(reader.u32, 'utf-16')
        elif obj_type == 2:  # u16
            return reader.u16
        elif obj_type == 3:  # u32
            return reader.u32
        elif obj_type == 4:  # i32
            return reader.i32
        elif obj_type == 7:  # json object
            return {
                'assembly_name': reader.string(reader.u8),
                'class_name': reader.string(reader.u8),
                'json': json.loads(reader.string(reader.i32, 'utf-16'))
            }
        else:
            raise RuntimeError(f'type {obj_type} not supported now.')


def _u32(buf: bytes, pos: int):
    return struct.unpack_from('<I', buf, pos)[0], pos + 4


def _i32(buf: bytes, pos: int):
    return struct.unpack_from('<i', buf, pos)[0], pos + 4


class CatalogV3:
    """新版catalog格式（Phigros 3.20.0起）。

    结构与旧版不同：m_KeyDataString为带类型字节的键表，
    m_BucketDataString为(键指针, 长度, 条目索引)序列，
    m_EntryDataString为固定28字节/条目的数据表。
    解析结果与旧版保持一致：fname_map = {bundle文件名: Assets/Tracks/...}
    """
    fname_map: dict[str, str]

    def __init__(self, data: dict) -> None:
        data_key = b64decode(data['m_KeyDataString'])
        data_bucket = b64decode(data['m_BucketDataString'])
        data_entry = b64decode(data['m_EntryDataString'])
        prefixes = data.get('m_InternalIdPrefixes') or []
        internal_ids = [expand_internal_id(x, prefixes) or '' for x in data.get('m_InternalIds', [])]

        def read_key(pos: int):
            typ = data_key[pos]
            pos += 1
            if typ == 0:  # ascii string
                length, pos = _u32(data_key, pos)
                return data_key[pos:pos + length].decode('utf-8'), pos + length
            elif typ == 1:  # unicode(16) string
                length, pos = _u32(data_key, pos)
                return data_key[pos:pos + length].decode('utf-16'), pos + length
            elif typ == 4:  # integer
                return _u32(data_key, pos)
            else:
                raise ValueError(f'unsupported key type {typ}')

        output: list[tuple] = []
        count, pos = _u32(data_bucket, 0)
        for _ in range(count):
            p_key, pos = _u32(data_bucket, pos)
            key, _ = read_key(p_key)
            length, pos = _u32(data_bucket, pos)
            entry = -1
            for _ in range(length):
                idx, pos = _u32(data_bucket, pos)
                # 条目数据定长28字节，前有4字节计数头+8字节偏移
                entry, _ = _i32(data_entry, 12 + 28 * idx)
            output.append((key, entry))

        # 解析键值映射：键的值指向output中另一项的键（通常是bundle文件名）
        for i, (key, entry) in enumerate(output):
            if entry == -1:
                continue
            value = output[entry][0]
            if isinstance(value, str) and value.endswith('.bundle') and len(value) != 32 + 7:
                value = bundle_basename(internal_ids[entry])
            output[i] = (key, value)

        fname_map: dict[str, str] = {}
        for key, value in output:
            if not isinstance(key, str) or not key.startswith('Assets/Tracks/'):
                continue
            if key.startswith('Assets/Tracks/#'):
                continue
            # 过滤掉与真实键一一配对的unity GUID键
            if len(key) == 32 and all(c in '0123456789abcdef' for c in key):
                continue
            if isinstance(value, str) and value.endswith('.bundle'):
                fname_map[value] = key

        self.fname_map = fname_map


__all__ = ['Catalog', 'CatalogV3', 'TrackEntry', 'load_catalog', 'detect_catalog_version', 'parse_track_entries',
           'expand_internal_id', 'bundle_basename']
