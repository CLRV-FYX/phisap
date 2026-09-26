import json
import struct
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


def load_catalog(data: dict):
    """解析catalog.json，自动识别格式版本。

    返回带有fname_map属性的对象：{bundle文件名: 资产路径(Assets/Tracks/...)}
    """
    if detect_catalog_version(data) == 'legacy':
        return Catalog(data)
    return CatalogV3(data)


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
        internal_ids = data.get('m_InternalIds', [])

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
                value = internal_ids[entry].rsplit('/', 1)[-1]
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


__all__ = ['Catalog', 'CatalogV3', 'load_catalog', 'detect_catalog_version']
