"""catalog.py 格式识别与新版(3.20.0+)catalog解析的单元测试。

新版catalog结构按照真实安装包内的编码规则构造:
- m_KeyDataString: 类型字节(0=ascii,1=utf16,4=整数) + u32长度 + 内容
- m_BucketDataString: u32计数 + (键指针, 长度, 条目索引)*
- m_EntryDataString: 12字节头 + 每条目28字节(条目内首个i32=映射到的output索引)
"""
import base64
import struct

import unittest

from catalog import CatalogV3, detect_catalog_version, load_catalog


def _build_v3_catalog() -> dict:
    # 键表
    keys = {
        'chart': 'Assets/Tracks/4159/Chart_SP.json',
        'guid': 'a' * 32,
        'bundle': 'b' * 32 + '.bundle',
        'music': 'Assets/Tracks/4159/music.wav',
        'bundle2': 'c' * 32 + '.bundle',
        'illus': 'Assets/Tracks/4159/illustration.jpg',
        'bundle3': 'd' * 33 + '.bundle',  # 长度40, 应走m_InternalIds回退
    }
    key_order = ['chart', 'guid', 'bundle', 'music', 'bundle2', 'illus', 'bundle3']
    keys_blob = b''
    key_offsets = {}
    for name in key_order:
        value = keys[name]
        key_offsets[name] = len(keys_blob)
        if name == 'music':  # utf16键
            encoded = value.encode('utf-16')
            keys_blob += b'\x01' + struct.pack('<I', len(encoded)) + encoded
        else:
            encoded = value.encode('utf-8')
            keys_blob += b'\x00' + struct.pack('<I', len(encoded)) + encoded

    # 条目表: 每个条目记录首个i32 = 键值映射到的output索引
    # chart->bundle, guid->bundle(应被GUID过滤), bundle->-1,
    # music->bundle2, bundle2->-1, illus->bundle3(长度!=39, 走internal_ids), bundle3->-1
    entry_indices = [2, 2, -1, 4, -1, 6, -1]
    # 条目表头共12字节(4字节计数+8字节前缀)，之后每条目定长28字节
    entry_blob = struct.pack('<I', len(entry_indices)) + b'\x00' * 8
    for idx in entry_indices:
        entry_blob += struct.pack('<i', idx) + b'\x00' * 24

    # 桶表
    bucket_blob = struct.pack('<I', len(key_order))
    for i, name in enumerate(key_order):
        bucket_blob += struct.pack('<III', key_offsets[name], 1, i)

    return {
        'm_KeyDataString': base64.b64encode(keys_blob).decode(),
        'm_BucketDataString': base64.b64encode(bucket_blob).decode(),
        'm_EntryDataString': base64.b64encode(entry_blob).decode(),
        'm_InternalIds': ['x', 'y', 'z', 'w', 'v', 'u', 'Assets/illustr/realname.bundle'],
    }


class TestCatalogDispatch(unittest.TestCase):
    def test_detect_legacy(self):
        data = {'m_resourceTypes': [], 'm_ExtraDataString': '', 'm_InternalIds': []}
        self.assertEqual(detect_catalog_version(data), 'legacy')

    def test_detect_v3(self):
        self.assertEqual(detect_catalog_version({'m_KeyDataString': ''}), 'v3')

    def test_load_catalog_dispatch(self):
        catalog = load_catalog(_build_v3_catalog())
        self.assertIsInstance(catalog, CatalogV3)


class TestCatalogV3(unittest.TestCase):
    def test_fname_map(self):
        catalog = load_catalog(_build_v3_catalog())
        self.assertEqual(catalog.fname_map, {
            'b' * 32 + '.bundle': 'Assets/Tracks/4159/Chart_SP.json',
            'c' * 32 + '.bundle': 'Assets/Tracks/4159/music.wav',
            'realname.bundle': 'Assets/Tracks/4159/illustration.jpg',  # 长度!=39时取internal_id
        })


if __name__ == '__main__':
    unittest.main()
