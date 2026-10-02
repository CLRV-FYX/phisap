"""catalog.py 格式识别与新版(3.20.0+)catalog解析的单元测试。

新版catalog结构按照真实安装包内的编码规则构造:
- m_KeyDataString: 类型字节(0=ascii,1=utf16,4=整数) + u32长度 + 内容
- m_BucketDataString: u32计数 + (键指针, 长度, 条目索引)*
- m_EntryDataString: 12字节头 + 每条目28字节(条目内首个i32=映射到的output索引)
"""
import base64
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from catalog import CatalogV3, bundle_basename, detect_catalog_version, expand_internal_id, load_catalog
from unity_fixtures import catalog_v4


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


class TestExpandInternalId(unittest.TestCase):
    def test_prefix_expansion(self):
        prefixes = ['{UnityEngine.AddressableAssets.Addressables.RuntimePath}/Android/', 'Assets/Tracks/']
        table = {
            '0#abc_def.bundle': prefixes[0] + 'abc_def.bundle',      # Addressables 的写法
            '1:Foo.Bar.0/Chart_IN.json': 'Assets/Tracks/Foo.Bar.0/Chart_IN.json',  # 冒号写法
            '9#x.bundle': '9#x.bundle',                              # 序号越界: 原样返回, 不抛异常
            'Assets/Tracks/a.json': 'Assets/Tracks/a.json',          # 没压缩
            '{Runtime}/Android/a.bundle': '{Runtime}/Android/a.bundle',
            '0#': prefixes[0],
        }
        for raw, expected in table.items():
            with self.subTest(raw=raw):
                self.assertEqual(expand_internal_id(raw, prefixes), expected)
        self.assertIsNone(expand_internal_id(None, prefixes))
        self.assertEqual(expand_internal_id('0#a.bundle', []), '0#a.bundle')  # 没有前缀表就不展开

    def test_bundle_basename(self):
        self.assertEqual(bundle_basename('{UnityEngine.AddressableAssets.Addressables.RuntimePath}/Android/x_y.bundle'), 'x_y.bundle')
        self.assertEqual(bundle_basename('C:\\a\\b\\z.bundle'), 'z.bundle')
        self.assertEqual(bundle_basename('plain.bundle'), 'plain.bundle')


REAL = {  # 磁盘上的真实 bundle 文件名
    'Assets/Tracks/Foo.Bar.0/Chart_IN.json': 'a' * 32 + '.bundle',
    'Assets/Tracks/Foo.Bar.0/Chart_AT.json': 'b' * 32 + '.bundle',
    'Assets/Tracks/Foo.Bar.0/music.wav': 'c' * 32 + '.bundle',
    'Assets/Tracks/Baz.Qux.0/Chart_EZ.json': 'd' * 32 + '.bundle',
}


class TestTrackEntriesV4(unittest.TestCase):
    """Phigros 4.0.0 起, 键里的 bundle 名(<哈希>_<文件名>.bundle)和磁盘上的真实文件名不再一致,
    m_InternalIds 还可能用前缀压缩。以前直接拿键当文件名, 一个 bundle 也对不上 ——
    界面报"catalog里没有可识别的谱面"。"""

    def layouts(self):
        for style in ('hash_file', 'plain'):
            for sep in ('#', ':', None):
                for first in (True, False):
                    for guid in (True, False):
                        yield dict(key_style=style, prefix_sep=sep, bundles_first=first, guid_keys=guid)

    def test_real_file_name_is_the_top_candidate_in_every_layout(self):
        pairs = list(REAL.items())
        for opts in self.layouts():
            with self.subTest(**opts):
                cat = load_catalog(catalog_v4(pairs, **opts))
                by_asset = {e.asset_path: e for e in cat.track_entries}
                for asset, real in pairs:
                    self.assertEqual(by_asset[asset].bundles[0], real, asset)

    def test_old_key_as_filename_logic_would_fail(self):
        """回归: 4.0.0 风格下, 键本身(<哈希>_<文件名>.bundle)不是磁盘文件名"""
        pairs = list(REAL.items())
        cat = load_catalog(catalog_v4(pairs, key_style='hash_file'))
        by_asset = {e.asset_path: e for e in cat.track_entries}
        for asset, real in pairs:
            keys_as_names = [b for b in by_asset[asset].bundles if b != real]
            self.assertTrue(all(b.endswith('_' + real) for b in keys_as_names), keys_as_names)

    def test_resolves_even_when_bucket_table_is_useless(self):
        """桶表的偏移全是垃圾时, 键靠键表自己的顺序读, bundle 靠"以依赖键为主键的条目"定位, 照样对得上"""
        pairs = list(REAL.items())
        for opts in self.layouts():
            with self.subTest(**opts):
                cat = load_catalog(catalog_v4(pairs, bad_bucket_offsets=True, **opts))
                by_asset = {e.asset_path: e for e in cat.track_entries}
                for asset, real in pairs:
                    self.assertEqual(by_asset[asset].bundles[0], real, asset)

    def test_other_assets_and_noise_keys_are_ignored(self):
        cat = load_catalog(catalog_v4(list(REAL.items()) + [('Assets/Other/x.png', 'e' * 32 + '.bundle')]))
        paths = {e.asset_path for e in cat.track_entries}
        self.assertEqual(paths, set(REAL))                      # Assets/Other 不算, 整数键/utf16 键不会弄崩
        self.assertTrue(cat.stats['keys'] > 10)
        self.assertEqual(cat.stats['track_assets'], 4)
        self.assertEqual(cat.stats['prefixes'], 2)
        self.assertEqual(cat.stats['bundle_ids'], 5)
        self.assertTrue(any('Assets' in k for k in cat.stats['key_samples']))
        self.assertTrue(cat.stats['top_dirs'])

    def test_one_bundle_holding_several_assets(self):
        shared = 'f' * 32 + '.bundle'
        pairs = [('Assets/Tracks/S.A.0/Chart_EZ.json', shared), ('Assets/Tracks/S.A.0/Chart_HD.json', shared)]
        cat = load_catalog(catalog_v4(pairs))
        self.assertEqual([e.bundles[0] for e in cat.track_entries], [shared, shared])
        self.assertEqual(len(cat.fname_map), 1)  # 老接口一个 bundle 只能记一个资产, 新接口不会丢

    def test_old_formats_still_resolve_through_fname_map(self):
        for build in (_build_v3_catalog,):
            cat = load_catalog(build())
            by_asset = {e.asset_path: e.bundles for e in cat.track_entries}
            self.assertEqual(by_asset['Assets/Tracks/4159/Chart_SP.json'], ['b' * 32 + '.bundle'])
            self.assertEqual(by_asset['Assets/Tracks/4159/illustration.jpg'], ['realname.bundle'])

    def test_unparseable_new_tables_fall_back_to_old_interface(self):
        cat = load_catalog(_build_v3_catalog())  # 这个夹具是简化编码, 新解析器读不出条目, 但老接口能读
        self.assertEqual(len(cat.track_entries), 3)

    def test_garbage_raises(self):
        with self.assertRaises(Exception):
            load_catalog({'m_KeyDataString': '!!!'})
        data = catalog_v4(list(REAL.items()))
        data['m_BucketDataString'] = base64.b64encode(b'\x05\x00').decode()  # 桶表被破坏
        with self.assertRaises(Exception):
            load_catalog(data)


if __name__ == '__main__':
    unittest.main()
