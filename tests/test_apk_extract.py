"""apk_tools 的解包部分: catalog -> bundle -> TextAsset -> ./Assets/Tracks/<歌曲>/Chart_<难度>.json

全部用 tests/unity_fixtures.py 现拼的 APK/OBB(含 catalog 两种格式、LZ4/LZMA/不压缩、Unity 2019/2022 的
不同头部标志), 不含任何游戏素材。
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apk_tools as A  # noqa: E402
from unity_fixtures import (build_apk, build_bundle, build_serialized_file, bundle_name_for,  # noqa: E402
                            catalog_v3, catalog_v4, chart_json, make_chart_bundle, make_package)

HAVE_LZ4 = importlib.util.find_spec('lz4') is not None

IN = 'Assets/Tracks/Foo.Bar.0/Chart_IN.json'
AT = 'Assets/Tracks/Foo.Bar.0/Chart_AT #4159.json'
HD = 'Assets/Tracks/Foo.Bar.0/Chart_HD.json'
EZ = 'Assets/Tracks/Foo.Bar.0/Chart_EZ.json'
SP = 'Assets/Tracks/Baz.Qux.1/Chart_SP.json'


def rewrite_zip(src: str, dst: str, replace: dict[str, bytes | None]) -> str:
    """复制 zip, 按 {成员名片段: 新内容} 替换(None=删掉)"""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, 'w') as zout:
        for name in zin.namelist():
            data = zin.read(name)
            hit = next((k for k in replace if k in name), None)
            if hit is not None:
                if replace[hit] is None:
                    continue
                data = replace[hit]
            zout.writestr(zipfile.ZipInfo(name), data)
    return dst


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.tracks = os.path.join(self.tmp, 'Tracks')
        self.logs = []

    def package(self, charts, name='p.apk', **kw):
        path = os.path.join(self.tmp, name)
        self.names = make_package(path, charts, **kw)
        return path

    def extract(self, archives, **kw):
        if isinstance(archives, str):
            archives = [archives]
        kw.setdefault('log', self.logs.append)
        return A.extract_charts(archives, self.tracks, **kw)

    def read(self, *parts) -> str:
        with open(os.path.join(self.tracks, *parts), encoding='utf-8') as f:
            return f.read()

    def listing(self) -> dict:
        out = {}
        if os.path.isdir(self.tracks):
            for song in sorted(os.listdir(self.tracks)):
                out[song] = sorted(os.listdir(os.path.join(self.tracks, song)))
        return out


# ---------------------------------------------------------------- 命名规则

class TestNaming(unittest.TestCase):
    def test_normalize_chart_filename(self):
        table = {
            'Chart_IN.json': 'Chart_IN.json',
            'Chart_in.json': 'Chart_IN.json',
            'Chart_AT #4159.json': 'Chart_AT.json',      # 3.20.0 起带 #编号
            'Chart_AT#4159.json': 'Chart_AT.json',
            'Chart_EZ  # 12.json': 'Chart_EZ.json',
            'Chart_SP.json': 'Chart.json',               # SP 统一叫 Chart.json, 免得同一难度两份
            'Chart.json': 'Chart.json',
            'chart.JSON': 'Chart.json',
            'Chart #9.json': 'Chart.json',
            'Chart_Legacy.json': 'Chart_Legacy.json',    # 不认识的难度标签保留原样
            'Chart-HD.json': 'Chart_HD.json',
            'music.json': None,
            'ChartInfo.json': None,                      # 没有分隔符的不是谱面
            'Chart_IN.json.meta': None,
            'Chart_IN.ans.v12.json': None,               # 规划缓存绝不是谱面
            'illustration.jpg': None,
            '': None,
        }
        for name, expected in table.items():
            with self.subTest(name=name):
                self.assertEqual(A.normalize_chart_filename(name), expected)

    def test_normalized_names_are_understood_by_the_app(self):
        """规范化后的文件名必须能被 main.chart_difficulty 认出来(否则解出来也选不到)"""
        import re
        known = ('EZ', 'SPB', 'INB', 'HDB', 'ATB', 'SP', 'IN', 'HD', 'AT', 'DT')
        token_re = re.compile(r'chart[_\s\-#]?([a-z]+)', re.IGNORECASE)

        def app_difficulty(f):  # 和 main.chart_difficulty 同一套规则(这里不能 import main)
            if f.lower() == 'chart.json':
                return 'SP'
            m = token_re.search(f)
            return m.group(1).upper() if m and m.group(1).upper() in known else None

        for raw, diff in (('Chart_EZ.json', 'EZ'), ('Chart_HD.json', 'HD'), ('Chart_IN.json', 'IN'),
                          ('Chart_AT #4159.json', 'AT'), ('Chart_SP.json', 'SP'), ('Chart.json', 'SP')):
            with self.subTest(raw=raw):
                self.assertEqual(app_difficulty(A.normalize_chart_filename(raw)), diff)
                self.assertEqual(A.chart_difficulty_tag(A.normalize_chart_filename(raw)), diff)

    def test_clean_song_id(self):
        table = {
            'Glaciaxion.SunsetRay.0': 'Glaciaxion.SunsetRay',
            'Foo.Bar.1': 'Foo.Bar.1',          # 非 0 的版本号保留, 不会和 .0 撞车
            'Foo.Bar.10': 'Foo.Bar.10',
            'Foo.Bar': 'Foo.Bar',
            'a:b?c.0': 'a_b_c',
            '#Hidden': None,
            '..': None,
            '.': None,
            '': None,
            '  ': None,
            '.0': None,
        }
        for folder, expected in table.items():
            with self.subTest(folder=folder):
                self.assertEqual(A.clean_song_id(folder), expected)

    def test_chart_target(self):
        table = {
            'Assets/Tracks/A.B.0/Chart_IN.json': ('A.B', 'Chart_IN.json'),
            'Assets/Tracks/A.B.0/Chart_AT #4159.json': ('A.B', 'Chart_AT.json'),
            'Assets\\Tracks\\A.B.0\\Chart_IN.json': ('A.B', 'Chart_IN.json'),
            'Assets/Tracks/A.B.0/music.wav': None,
            'Assets/Tracks/A.B.0/illustration.jpg': None,
            'Assets/Tracks/#X/Chart_IN.json': None,
            'Assets/Tracks/A/B/Chart_IN.json': None,
            'Assets/Other/A.0/Chart_IN.json': None,
            'Assets/Tracks/../Chart_IN.json': None,
            'Assets/Tracks/A.0/../../evil/Chart_IN.json': None,
            'Assets/Tracks//Chart_IN.json': None,
            'Chart_IN.json': None,
        }
        for addr, expected in table.items():
            with self.subTest(addr=addr):
                self.assertEqual(A.chart_target(addr), expected)


# ---------------------------------------------------------------- 写盘与规划缓存

class TestWriteChart(Base):
    def test_status_codes_and_mtime_preserved_when_unchanged(self):
        self.assertEqual(A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"a":1}'), 'written')
        path = os.path.join(self.tracks, 'S', 'Chart_IN.json')
        os.utime(path, (1_000_000, 1_000_000))
        self.assertEqual(A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"a":1}'), 'unchanged')
        self.assertEqual(int(os.path.getmtime(path)), 1_000_000)  # 没变就不重写
        self.assertEqual(A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"a":2}'), 'overwritten')
        self.assertEqual(open(path, 'rb').read(), b'{"a":2}')
        self.assertEqual(A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"a":3}', overwrite=False), 'skipped')
        self.assertEqual(open(path, 'rb').read(), b'{"a":2}')

    def test_same_size_but_different_content_is_overwritten(self):
        A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"a":1}')
        self.assertEqual(A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"a":9}'), 'overwritten')

    def test_no_temp_files_left_behind(self):
        A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{}')
        self.assertEqual(os.listdir(os.path.join(self.tracks, 'S')), ['Chart_IN.json'])

    def test_bytes_written_exactly(self):
        payload = '{"s":"谱面\n中文\r\n"}'.encode('utf-8')  # 含换行和中文: Windows 上不能被换行转换/GBK 弄坏
        A.write_chart(self.tracks, 'S', 'Chart_IN.json', payload)
        self.assertEqual(open(os.path.join(self.tracks, 'S', 'Chart_IN.json'), 'rb').read(), payload)

    def test_plan_cache_invalidated_only_for_changed_chart(self):
        folder = os.path.join(self.tracks, 'S')
        A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"v":1}')
        A.write_chart(self.tracks, 'S', 'Chart_HD.json', b'{"v":1}')
        keep = ['Chart_HD.ans.v12.json', 'Chart_IN.json.bak', 'Chart_INX.ans.v12.json', 'notes.txt']
        drop = ['Chart_IN.ans.v12.json', 'Chart_IN.ans.v9.json', 'Chart_IN.ans.json',
                'Chart_IN.algored.p16.ans.v13.json']
        for n in keep + drop:
            with open(os.path.join(folder, n), 'w') as f:
                f.write('x')
        A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"v":1}')  # 内容没变 -> 缓存还有效
        self.assertTrue(all(os.path.exists(os.path.join(folder, n)) for n in drop))
        A.write_chart(self.tracks, 'S', 'Chart_IN.json', b'{"v":2}')  # 内容变了 -> 旧缓存作废
        for n in drop:
            self.assertFalse(os.path.exists(os.path.join(folder, n)), n)
        for n in keep:
            self.assertTrue(os.path.exists(os.path.join(folder, n)), n)

    def test_sp_chart_cache_name(self):
        folder = os.path.join(self.tracks, 'S')
        A.write_chart(self.tracks, 'S', 'Chart.json', b'{"v":1}')
        with open(os.path.join(folder, 'Chart.ans.v12.json'), 'w') as f:
            f.write('x')
        A.write_chart(self.tracks, 'S', 'Chart.json', b'{"v":2}')
        self.assertFalse(os.path.exists(os.path.join(folder, 'Chart.ans.v12.json')))


# ---------------------------------------------------------------- 解包主流程

class TestExtractBasics(Base):
    def test_all_catalog_formats(self):
        """v3 / legacy 是仓库里原有的两种; v4* 是按 Addressables 真实表结构拼的 Phigros 4.0.0 的样子:
        键里的 bundle 名(<哈希>_<文件名>.bundle)和磁盘文件名不一致, 加载路径用前缀压缩(# 或 :),
        bundle 条目在前/在后, 带不带 GUID 键。以前 4.0.0 一个 bundle 也对不上,
        界面报 "catalog里没有可识别的谱面"。"""
        for fmt in ('v3', 'legacy', 'v4', 'v4:plain', 'v4:colon', 'v4:nocompress', 'v4:assets_first',
                    'v4:colon:assets_first', 'v4:plain:nocompress'):
            with self.subTest(catalog=fmt):
                self.tracks = os.path.join(self.tmp, f'Tracks_{fmt}')
                pkg = self.package({IN: chart_json('in'), AT: chart_json('at'), SP: chart_json('sp')},
                                   name=f'{fmt}.apk', catalog=fmt)
                r = self.extract(pkg)
                self.assertEqual((r.written, r.total, len(r.failed)), (3, 3, 0))
                self.assertEqual(self.listing(), {'Baz.Qux.1': ['Chart.json'], 'Foo.Bar': ['Chart_AT.json', 'Chart_IN.json']})
                self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_AT.json'))['tag'], 'at')

    def test_v4_catalog_with_many_songs_and_all_difficulties(self):
        charts = {f'Assets/Tracks/Song{i:02d}.Artist.0/Chart_{d}.json': chart_json(f'{i}{d}')
                  for i in range(12) for d in ('EZ', 'HD', 'IN', 'AT')}
        charts['Assets/Tracks/Song00.Artist.0/Chart_Legacy.json'] = chart_json('legacy')
        charts['Assets/Tracks/Song00.Artist.0/music.wav'] = 'RIFF'
        pkg = self.package(charts, catalog='v4')
        r = self.extract(pkg)
        self.assertEqual((r.written, len(r.failed), len(r.songs)), (49, 0, 12))
        self.assertEqual(json.loads(self.read('Song07.Artist', 'Chart_HD.json'))['tag'], '7HD')
        self.assertEqual(sorted(os.listdir(os.path.join(self.tracks, 'Song00.Artist'))),
                         ['Chart_AT.json', 'Chart_EZ.json', 'Chart_HD.json', 'Chart_IN.json', 'Chart_Legacy.json'])

    def test_one_bundle_holding_two_charts_is_parsed_once(self):
        shared = 'f' * 32 + '.bundle'
        sf = build_serialized_file([('Chart_EZ', chart_json('ez')), ('Chart_HD', chart_json('hd'))])
        bundle = build_bundle({'CAB-shared': sf}, compression='lz4')
        pairs = [('Assets/Tracks/S.A.0/Chart_EZ.json', shared), ('Assets/Tracks/S.A.0/Chart_HD.json', shared)]
        pkg = os.path.join(self.tmp, 'shared.apk')
        build_apk(pkg, {'assets/aa/catalog.json': json.dumps(catalog_v4(pairs)).encode(),
                        'assets/aa/Android/' + shared: bundle})
        calls = []
        original = A.read_text_assets

        def counting(*a, **k):
            calls.append(1)
            return original(*a, **k)

        A.read_text_assets = counting
        try:
            r = self.extract(pkg)
        finally:
            A.read_text_assets = original
        self.assertEqual((r.written, len(r.failed)), (2, 0))
        self.assertEqual(len(calls), 1)  # 同一个 bundle 只解析一次
        self.assertEqual(json.loads(self.read('S.A', 'Chart_EZ.json'))['tag'], 'ez')
        self.assertEqual(json.loads(self.read('S.A', 'Chart_HD.json'))['tag'], 'hd')

    def test_non_chart_assets_and_hidden_folders_ignored(self):
        pkg = self.package({IN: chart_json('in'), 'Assets/Tracks/#Hidden/Chart_IN.json': chart_json('h'),
                            'Assets/Tracks/Foo.Bar.0/music.wav': 'RIFF', 'Assets/Other/x/Chart_IN.json': chart_json('o')})
        r = self.extract(pkg)
        self.assertEqual(r.total, 1)
        self.assertEqual(self.listing(), {'Foo.Bar': ['Chart_IN.json']})

    def test_content_is_byte_exact_including_chinese_and_crlf(self):
        text = '{"name":"谱面·测试","list":[1,2,3],\r\n"x":"\u00e9\u4e2d"}'
        pkg = self.package({IN: text})
        self.extract(pkg)
        with open(os.path.join(self.tracks, 'Foo.Bar', 'Chart_IN.json'), 'rb') as f:
            self.assertEqual(f.read(), text.encode('utf-8'))

    def test_non_utf8_and_bom_bytes_are_preserved_exactly(self):
        """TextAsset 原样当字节处理: 带 BOM、带非 UTF-8 的字节也不能出错, 写出去必须逐字节一致"""
        cases = {
            IN: b'\xef\xbb\xbf{"bom":true}\r\n',
            HD: b'{"s":"\xff\xfe not utf8"}',
            AT: b'  \n{"padded":1}\x00\x00 \n',
        }
        pkg = self.package(cases, catalog='v4')
        r = self.extract(pkg)
        self.assertEqual((r.written, len(r.failed)), (3, 0), r.failed)
        for addr, raw in cases.items():
            name = A.chart_target(addr)[1]
            with open(os.path.join(self.tracks, 'Foo.Bar', name), 'rb') as f:
                self.assertEqual(f.read(), raw, addr)

    def test_difficulty_filter(self):
        pkg = self.package({IN: chart_json('in'), AT: chart_json('at'), HD: chart_json('hd'), EZ: chart_json('ez'),
                            SP: chart_json('sp')})
        r = self.extract(pkg, difficulties=['at', 'SP'])
        self.assertEqual(r.total, 2)
        self.assertEqual(self.listing(), {'Baz.Qux.1': ['Chart.json'], 'Foo.Bar': ['Chart_AT.json']})

    def test_overwrite_policies(self):
        pkg = self.package({IN: chart_json('v1')})
        self.extract(pkg)
        pkg2 = self.package({IN: chart_json('v2')}, name='new.apk')
        r = self.extract(pkg2, overwrite=False)
        self.assertEqual((r.skipped, r.written), (1, 0))
        self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_IN.json'))['tag'], 'v1')
        r = self.extract(pkg2)
        self.assertEqual((r.overwritten, r.written), (1, 0))
        self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_IN.json'))['tag'], 'v2')
        r = self.extract(pkg2)
        self.assertEqual((r.unchanged, r.overwritten), (1, 0))

    def test_updated_chart_invalidates_its_plan_cache(self):
        pkg = self.package({IN: chart_json('v1'), HD: chart_json('hd')})
        self.extract(pkg)
        folder = os.path.join(self.tracks, 'Foo.Bar')
        for n in ('Chart_IN.ans.v12.json', 'Chart_HD.ans.v12.json'):
            with open(os.path.join(folder, n), 'w') as f:
                f.write('{}')
        pkg2 = self.package({IN: chart_json('v2'), HD: chart_json('hd')}, name='new.apk')
        r = self.extract(pkg2)
        self.assertEqual((r.overwritten, r.unchanged), (1, 1))
        self.assertFalse(os.path.exists(os.path.join(folder, 'Chart_IN.ans.v12.json')))
        self.assertTrue(os.path.exists(os.path.join(folder, 'Chart_HD.ans.v12.json')))

    def test_summary_text(self):
        pkg = self.package({IN: chart_json('in'), SP: chart_json('sp')})
        r = self.extract(pkg)
        self.assertEqual(r.summary(), '2 首歌共 2 份谱面(新增 2)')
        self.assertTrue(any('解包完成' in m for m in self.logs))


class TestExtractAcrossArchives(Base):
    def split(self, charts, catalog_in_first=True):
        full = os.path.join(self.tmp, 'full.zip')
        make_package(full, charts)
        cat, rest = {}, {}
        with zipfile.ZipFile(full) as z:
            for n in z.namelist():
                (cat if n.endswith('catalog.json') else rest)[n] = z.read(n)
        a = build_apk(os.path.join(self.tmp, 'a.zip'), cat)
        b = build_apk(os.path.join(self.tmp, 'b.zip'), rest)
        return [a, b] if catalog_in_first else [b, a]

    def test_catalog_and_bundles_in_different_files(self):
        for first in (True, False):
            with self.subTest(catalog_first=first):
                self.tracks = os.path.join(self.tmp, f'T{first}')
                r = self.extract(self.split({IN: chart_json('in'), SP: chart_json('sp')}, first))
                self.assertEqual(r.total, 2)

    def test_bundles_without_catalog_explains_obb(self):
        _cat, bundles = self.split({IN: chart_json('in')})
        with self.assertRaisesRegex(A.PackageError, 'catalog.json.*OBB'):
            self.extract(bundles)

    def test_catalog_without_bundles_explains_obb(self):
        cat, _bundles = self.split({IN: chart_json('in')})
        with self.assertRaisesRegex(A.PackageError, '没有任何 .bundle.*OBB'):
            self.extract(cat)

    def test_later_archive_wins_for_same_bundle_and_catalog(self):
        """设备上偶尔残留旧版本的 OBB; 重名的 bundle / catalog 以排在后面的文件为准
        (提取时 APK 在前、OBB 按版本号从小到大, 越靠后越新)"""
        old = self.package({IN: chart_json('old')}, name='old.obb')
        new = self.package({IN: chart_json('new')}, name='new.obb')
        self.extract([old, new])
        self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_IN.json'))['tag'], 'new')
        self.tracks = os.path.join(self.tmp, 'T2')
        self.extract([new, old])
        self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_IN.json'))['tag'], 'old')

    def test_duplicate_selection_is_harmless(self):
        pkg = self.package({IN: chart_json('in')})
        r = self.extract([pkg, pkg])
        self.assertEqual((r.total, len(r.failed)), (1, 0))


class TestUnityVariants(Base):
    def test_all_compressions_and_engine_versions(self):
        cases = [
            dict(compression='lz4'),
            dict(compression='lzma'),
            dict(compression='none'),
            dict(compression='lz4', info_at_end=True),
            dict(compression='lz4', engine='2022.3.10f1', padding_flag=True, sf_version=22),
            dict(compression='lzma', engine='2021.3.20f1', padding_flag=True, sf_version=21),
            dict(compression='lz4', engine='2019.4.31f1', version=6),
            dict(compression='lz4', type_tree=True),
            dict(compression='lz4', engine='2022.3.10f1', padding_flag=True, sf_version=22, type_tree=True),
        ]
        for kw in cases:
            with self.subTest(**kw):
                self.tracks = os.path.join(self.tmp, 'T' + str(abs(hash(str(kw)))))
                pkg = self.package({IN: chart_json('in' + str(kw)), AT: chart_json('at')},
                                   name=f'{abs(hash(str(kw)))}.apk', bundle_kw=kw)
                r = self.extract(pkg)
                self.assertEqual((r.total, len(r.failed)), (2, 0))

    def test_large_chart_roundtrip(self):
        big = json.dumps({'tag': 'big', 'notes': [[i, i * 2, i % 7] for i in range(150_000)]})
        self.assertGreater(len(big), 2_000_000)
        for kw in (dict(compression='none'), dict(compression='lzma')):
            with self.subTest(**kw):
                self.tracks = os.path.join(self.tmp, 'T' + kw['compression'])
                pkg = self.package({AT: big}, name=kw['compression'] + '.apk', bundle_kw=kw)
                t0 = time.monotonic()
                r = self.extract(pkg)
                self.assertEqual(r.total, 1)
                self.assertLess(time.monotonic() - t0, 10)
                self.assertEqual(self.read('Foo.Bar', 'Chart_AT.json'), big)

    @unittest.skipUnless(HAVE_LZ4, '没装 lz4 库, 跳过(夹具的纯 Python 压缩器太慢, 压不了大文件)')
    def test_large_lz4_chart_with_pure_python_decoder(self):
        import lz4.block
        from extract import lz4_decompress_py
        raw = json.dumps({'notes': [[i, i * 2, i % 7, 'x' * (i % 5)] for i in range(300_000)]}).encode()
        comp = lz4.block.compress(raw, store_size=False)
        t0 = time.monotonic()
        self.assertEqual(bytes(lz4_decompress_py(comp)), raw)
        self.assertLess(time.monotonic() - t0, 5)  # 十来 MB 的谱面, 纯 Python 也要在几秒内


class TestFailures(Base):
    def test_one_bad_bundle_does_not_stop_the_rest(self):
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd'), AT: chart_json('at')})
        bad = rewrite_zip(pkg, os.path.join(self.tmp, 'bad.apk'),
                          {self.names[HD]: b'UnityFS\0' + b'\xff' * 400})
        r = self.extract(bad)
        self.assertEqual((r.written, len(r.failed)), (2, 1))
        self.assertEqual(r.failed[0][0], HD)
        self.assertEqual(self.listing(), {'Foo.Bar': ['Chart_AT.json', 'Chart_IN.json']})
        self.assertTrue(any('解包失败' in m and 'Chart_HD' in m for m in self.logs))
        self.assertIn('1 份失败', r.summary())

    def test_all_failing_raises_with_first_error(self):
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd')})
        bad = rewrite_zip(pkg, os.path.join(self.tmp, 'bad.apk'),
                          {self.names[IN]: b'garbage', self.names[HD]: b'garbage'})
        with self.assertRaisesRegex(A.PackageError, '全部解包失败'):
            self.extract(bad)

    def test_encrypted_bundle_is_skipped_not_failed(self):
        """4.0.0 第九章隐藏曲的 bundle 是整包 AES 加密的(文件头 47A9C97DBEEFC3E4), 没有密码解不了;
        要明确说"加密了, 已跳过", 不能算成解析失败(那样用户会以为程序坏了)"""
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd')}, catalog='v4')
        secret = rewrite_zip(pkg, os.path.join(self.tmp, 'secret.apk'),
                             {self.names[HD]: A.ENCRYPTED_MAGIC + os.urandom(500)})
        r = self.extract(secret)
        self.assertEqual((r.written, len(r.failed), r.encrypted), (1, 0, [HD]))
        self.assertIn('1 份是加密的已跳过', r.summary())
        self.assertTrue(any('整包加密' in m for m in self.logs))
        self.assertEqual(self.listing(), {'Foo.Bar': ['Chart_IN.json']})

    def test_everything_encrypted_is_not_an_error(self):
        pkg = self.package({HD: chart_json('hd')}, catalog='v4')
        secret = rewrite_zip(pkg, os.path.join(self.tmp, 'secret.apk'), {self.names[HD]: A.ENCRYPTED_MAGIC + b'x' * 64})
        r = self.extract(secret)
        self.assertEqual((r.total, len(r.failed), len(r.encrypted)), (0, 0, 1))

    def test_zip_read_is_retried_on_crc_errors(self):
        pkg = self.package({IN: chart_json('in')}, catalog='v4')
        original = zipfile.ZipFile.read
        calls = {'n': 0}

        def flaky(self_, name, *a, **k):
            if str(getattr(name, 'filename', name)).endswith('.bundle'):
                calls['n'] += 1
                if calls['n'] <= 2:
                    raise zipfile.BadZipFile('Bad CRC-32 for file')
            return original(self_, name, *a, **k)

        zipfile.ZipFile.read = flaky
        try:
            r = self.extract(pkg)
        finally:
            zipfile.ZipFile.read = original
        self.assertEqual((r.written, len(r.failed)), (1, 0))
        self.assertEqual(calls['n'], 3)  # 失败两次, 第三次成功

        calls['n'] = -100  # 一直失败: 重试 3 次后记成这一份失败, 不是整个任务崩掉
        self.tracks = os.path.join(self.tmp, 'T2')
        zipfile.ZipFile.read = flaky
        try:
            with self.assertRaisesRegex(A.PackageError, '全部解包失败'):
                self.extract(pkg)
        finally:
            zipfile.ZipFile.read = original

    def test_non_json_text_is_rejected(self):
        pkg = self.package({IN: 'this is not json', HD: chart_json('hd')})
        r = self.extract(pkg)
        self.assertEqual((r.written, len(r.failed)), (1, 1))
        self.assertIn('不像谱面', r.failed[0][1])
        self.assertEqual(self.listing(), {'Foo.Bar': ['Chart_HD.json']})

    def test_failure_message_names_the_unity_version_and_compression(self):
        """用户把日志发来就能看出是哪个 Unity 版本/压缩方式出的问题"""
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd')})
        enc = make_chart_bundle('Chart_HD', chart_json('hd'), engine='2022.3.10f1', extra_flags=0x400)
        bad = rewrite_zip(pkg, os.path.join(self.tmp, 'enc.apk'), {self.names[HD]: enc})
        r = self.extract(bad)
        self.assertIn('UnityFS v7, 2022.3.10f1', r.failed[0][1])
        self.assertIn('压缩 2', r.failed[0][1])
        self.assertIn('0x', r.failed[0][1])

    def test_describe_bundle(self):
        data = make_chart_bundle('x', '{}', engine='2019.4.31f1', compression='lzma')
        self.assertEqual(A.describe_bundle(data), 'UnityFS v7, 2019.4.31f1, 标志 0x41(压缩 1)')
        self.assertIn('不是 UnityFS', A.describe_bundle(b'PK\x03\x04' + b'\0' * 20))
        self.assertEqual(A.describe_bundle(b'UnityFS'), '头部无法解析')
        self.assertEqual(A.describe_bundle(b''), '头部无法解析')

    def test_encrypted_bundle_is_reported(self):
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd')})
        enc = make_chart_bundle('Chart_HD', chart_json('hd'), engine='2021.3.20f1', extra_flags=0x400)
        bad = rewrite_zip(pkg, os.path.join(self.tmp, 'enc.apk'), {self.names[HD]: enc})
        r = self.extract(bad)
        self.assertEqual(len(r.failed), 1)
        self.assertIn('加密', r.failed[0][1])

    def test_bundle_with_multiple_text_assets_picks_by_name(self):
        sf = build_serialized_file([('other', '{"tag":"other"}'), ('Chart_IN', '{"tag":"wanted"}')])
        extra = build_bundle({'CAB-x': sf}, compression='lz4')
        pkg = self.package({HD: chart_json('hd')})
        multi = rewrite_zip(pkg, os.path.join(self.tmp, 'multi.apk'), {})
        # 再造一个 catalog: IN 指向这个多资产 bundle
        cat = catalog_v3([(IN, bundle_name_for('multi'))])
        build_apk(os.path.join(self.tmp, 'multi2.apk'), {'assets/aa/catalog.json': json.dumps(cat).encode(),
                                                          'assets/aa/Android/' + bundle_name_for('multi'): extra})
        r = self.extract(os.path.join(self.tmp, 'multi2.apk'))
        self.assertEqual(r.total, 1)
        self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_IN.json'))['tag'], 'wanted')
        del multi

    def test_ambiguous_multiple_text_assets_fails_cleanly(self):
        sf = build_serialized_file([('a', '{"t":1}'), ('b', '{"t":2}')])
        extra = build_bundle({'CAB-x': sf}, compression='lz4')
        cat = catalog_v3([(IN, bundle_name_for('amb')), (HD, bundle_name_for('ok'))])
        ok = make_chart_bundle('Chart_HD', chart_json('hd'))
        build_apk(os.path.join(self.tmp, 'amb.apk'), {
            'assets/aa/catalog.json': json.dumps(cat).encode(),
            'assets/aa/Android/' + bundle_name_for('amb'): extra,
            'assets/aa/Android/' + bundle_name_for('ok'): ok})
        r = self.extract(os.path.join(self.tmp, 'amb.apk'))
        self.assertEqual((r.written, len(r.failed)), (1, 1))
        self.assertIn('无法确定', r.failed[0][1])

    def test_not_a_zip(self):
        junk = os.path.join(self.tmp, 'junk.apk')
        with open(junk, 'wb') as f:
            f.write(b'not a zip file' * 100)
        with self.assertRaisesRegex(A.PackageError, '不是有效的 APK/OBB'):
            self.extract(junk)

    def test_missing_file_and_empty_selection(self):
        with self.assertRaisesRegex(A.PackageError, '找不到文件'):
            self.extract(os.path.join(self.tmp, 'nope.apk'))
        with self.assertRaisesRegex(A.PackageError, '没有选择'):
            self.extract([])

    def test_first_archive_closed_when_second_is_bad(self):
        good = self.package({IN: chart_json('in')})
        with self.assertRaises(A.PackageError):
            self.extract([good, os.path.join(self.tmp, 'nope.apk')])
        os.remove(good)  # Windows 上句柄没关就删不掉

    def test_no_catalog_at_all(self):
        build_apk(os.path.join(self.tmp, 'empty.apk'), {'classes.dex': b'dex', 'AndroidManifest.xml': b'<x/>'})
        with self.assertRaisesRegex(A.PackageError, '没有在所选文件里找到 assets/aa/catalog.json'):
            self.extract(os.path.join(self.tmp, 'empty.apk'))

    def test_binary_catalog_reported(self):
        build_apk(os.path.join(self.tmp, 'bin.apk'), {'assets/aa/catalog.bin': b'\0\1\2'})
        with self.assertRaisesRegex(A.PackageError, 'catalog.bin'):
            self.extract(os.path.join(self.tmp, 'bin.apk'))

    def test_unreadable_catalog(self):
        build_apk(os.path.join(self.tmp, 'c.apk'), {'assets/aa/catalog.json': b'{not json'})
        with self.assertRaisesRegex(A.PackageError, 'catalog.json 读不出来'):
            self.extract(os.path.join(self.tmp, 'c.apk'))
        build_apk(os.path.join(self.tmp, 'd.apk'), {'assets/aa/catalog.json': b'{"m_KeyDataString": "!!!"}'})
        with self.assertRaisesRegex(A.PackageError, '格式不认识'):
            self.extract(os.path.join(self.tmp, 'd.apk'))

    def test_catalog_without_any_chart(self):
        pkg = self.package({'Assets/Tracks/Foo.Bar.0/music.wav': 'RIFF'})
        with self.assertRaisesRegex(A.PackageError, '没有可识别的谱面'):
            self.extract(pkg)

    def test_bundle_names_not_matching_gives_actionable_diagnostics(self):
        """catalog 里的谱面一个 bundle 也对不上时: 说清楚是文件名对不上, 并把两边的样例都列出来,
        用户把日志发过来就能看出命名规则怎么变的"""
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd')}, catalog='v4')
        renamed = os.path.join(self.tmp, 'renamed.apk')
        with zipfile.ZipFile(pkg) as zin, zipfile.ZipFile(renamed, 'w') as zout:
            for n in zin.namelist():
                data = zin.read(n)
                if n.endswith('.bundle'):  # 磁盘文件名变了, 和 catalog 里记的任何一个名字都对不上
                    head, base = n.rsplit('/', 1)
                    n = f'{head}/zz_{base}'
                zout.writestr(zipfile.ZipInfo(n), data)
        with self.assertRaises(A.PackageError) as cm:
            self.extract(renamed)
        msg = str(cm.exception)
        self.assertIn('catalog 里有 2 份谱面', msg)
        self.assertIn('bundle 文件名对不上', msg)
        self.assertIn('诊断信息', msg)
        self.assertIn(IN, msg)                                   # 谱面路径样例
        self.assertIn('zz_', msg)                                # 所选文件里的 bundle 名样例
        self.assertIn(self.names[IN], msg)                       # catalog 给出的 bundle 名样例
        self.assertIn('Assets/Tracks', msg)
        self.assertTrue(any('诊断信息' in m for m in self.logs))  # 同样的内容也写进了日志

    def test_assets_under_another_prefix_gives_key_samples(self):
        """谱面不在 Assets/Tracks 下(游戏改了资源结构)时, 诊断里要列出 catalog 键的样例和路径前缀分布"""
        pkg = self.package({'Assets/Charts/Foo.Bar.0/Chart_IN.json': chart_json('in')}, catalog='v4')
        with self.assertRaises(A.PackageError) as cm:
            self.extract(pkg)
        msg = str(cm.exception)
        self.assertIn('没有可识别的谱面', msg)
        self.assertIn('Assets/Charts', msg)  # 键样例/前缀分布里能看到真实的目录

    def test_partly_unresolved_charts_are_skipped_with_a_note(self):
        pkg = self.package({IN: chart_json('in'), HD: chart_json('hd')}, catalog='v4')
        partial = rewrite_zip(pkg, os.path.join(self.tmp, 'partial.apk'), {self.names[HD]: None})  # 少一个 bundle
        r = self.extract(partial)
        self.assertEqual((r.written, len(r.failed)), (1, 0))
        self.assertTrue(any('找不到它们的 bundle' in m for m in self.logs))

    def test_nothing_written_outside_tracks_dir(self):
        evil = {'Assets/Tracks/../../evil/Chart_IN.json': chart_json('e'),
                'Assets/Tracks/..\\..\\evil2\\Chart_IN.json': chart_json('e2'),
                IN: chart_json('ok')}
        pkg = self.package(evil)
        r = self.extract(pkg)
        self.assertEqual(r.total, 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil2')))
        self.assertEqual(self.listing(), {'Foo.Bar': ['Chart_IN.json']})


HAVE_UNITYPY = importlib.util.find_spec('UnityPy') is not None


class TestUnityPyFallback(Base):
    """自带解析器遇到没见过的 Unity 版本出错时, 装了 UnityPy 就自动改用它(不装也不影响正常使用)"""

    def broken_parser(self):
        def boom(*_a, **_k):
            raise RuntimeError('自带解析器不认识这个版本')
        return mock.patch.object(A, 'read_text_assets', boom)

    @unittest.skipUnless(HAVE_UNITYPY, '没装 UnityPy, 跳过(只有开发时会装)')
    def test_falls_back_to_unitypy_and_keeps_exact_bytes(self):
        cases = {IN: chart_json('in').encode(), HD: b'{"s":"\xff\xfe raw"}', AT: b'\xef\xbb\xbf{"bom":1}'}
        pkg = self.package(cases, catalog='v4')
        with self.broken_parser():
            r = self.extract(pkg)
        self.assertEqual((r.written, len(r.failed), r.fallback_used), (3, 0, 3))
        for addr, raw in cases.items():
            with open(os.path.join(self.tracks, 'Foo.Bar', A.chart_target(addr)[1]), 'rb') as f:
                self.assertEqual(f.read(), raw, addr)
        self.assertTrue(any('改用 UnityPy' in m for m in self.logs))

    def test_without_unitypy_the_original_error_surfaces_with_install_hint(self):
        pkg = self.package({IN: chart_json('in')}, catalog='v4')
        with self.broken_parser(), mock.patch.object(A, '_read_with_unitypy', return_value=None), \
                mock.patch.object(A, '_unitypy_installed', return_value=False):
            with self.assertRaises(A.PackageError) as cm:
                self.extract(pkg)
        self.assertIn('自带解析器不认识这个版本', str(cm.exception))   # 报的是自带解析器的错误
        self.assertIn('pip install UnityPy', str(cm.exception))      # 并且告诉用户有这条退路

    def test_hint_is_not_shown_when_unitypy_is_already_installed(self):
        pkg = self.package({IN: chart_json('in')}, catalog='v4')
        with self.broken_parser(), mock.patch.object(A, '_read_with_unitypy', return_value=None), \
                mock.patch.object(A, '_unitypy_installed', return_value=True):
            with self.assertRaises(A.PackageError) as cm:
                self.extract(pkg)
        self.assertNotIn('pip install UnityPy', str(cm.exception))

    def test_missing_unitypy_module_means_no_fallback(self):
        with mock.patch.dict(sys.modules, {'UnityPy': None}):  # import UnityPy 会抛 ImportError
            self.assertIsNone(A._read_with_unitypy(b'whatever'))

    def test_empty_result_from_own_parser_also_triggers_fallback(self):
        pkg = self.package({IN: chart_json('in')}, catalog='v4')
        calls = []

        def empty(*_a, **_k):
            return []

        def fake_unitypy(data):
            calls.append(len(data))
            return [('Chart_IN', chart_json('from-unitypy').encode())]

        with mock.patch.object(A, 'read_text_assets', empty), mock.patch.object(A, '_read_with_unitypy', fake_unitypy):
            r = self.extract(pkg)
        self.assertEqual((r.written, r.fallback_used, len(calls)), (1, 1, 1))
        self.assertEqual(json.loads(self.read('Foo.Bar', 'Chart_IN.json'))['tag'], 'from-unitypy')


class TestProgressAndCancel(Base):
    def make_many(self, n):
        charts = {f'Assets/Tracks/Song{i:03d}.Artist.0/Chart_IN.json': chart_json(f's{i}') for i in range(n)}
        return self.package(charts)

    def test_progress_monotonic_and_reaches_total(self):
        pkg = self.make_many(30)
        events = []
        self.extract(pkg, progress=lambda d, t, s: events.append((d, t, s)))
        self.assertGreaterEqual(len(events), 2)
        dones = [d for d, _, _ in events]
        self.assertEqual(dones, sorted(dones))
        self.assertEqual(events[-1][0], events[-1][1])
        self.assertTrue(events[-1][1] > 1)
        self.assertTrue(any('解包谱面' in s for _, _, s in events))
        self.assertIn('100%', events[-1][2])

    def test_progress_text_names_the_current_chart(self):
        pkg = self.make_many(5)
        events = []
        A.extract_charts([pkg], self.tracks, progress=lambda d, t, s: events.append(s))
        # Meter 对高频更新做了节流, 但第一次更新一定发出
        self.assertTrue(any('Song000.Artist/Chart_IN.json' in s for s in events))

    def test_cancel_stops_early_and_keeps_written_charts(self):
        pkg = self.make_many(40)
        cancel = threading.Event()
        count = {'n': 0}

        original = A.write_chart

        def counting(*a, **k):
            count['n'] += 1
            if count['n'] == 5:
                cancel.set()
            return original(*a, **k)

        A.write_chart = counting
        try:
            with self.assertRaises(A.Cancelled):
                self.extract(pkg, cancel=cancel)
        finally:
            A.write_chart = original
        self.assertEqual(count['n'], 5)
        self.assertEqual(len(self.listing()), 5)  # 已写好的保留, 没写的没有
        self.assertTrue(any('已取消' in m for m in self.logs))

    def test_cancel_before_start(self):
        pkg = self.make_many(3)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(A.Cancelled):
            self.extract(pkg, cancel=cancel)
        self.assertFalse(os.path.exists(self.tracks))


class TestRealSampleChart(Base):
    """用仓库里真实的官谱(Chart_AT.json, 7.7MB)走一遍 bundle -> 解包, 确认大文件和真实内容没问题"""

    def test_repo_chart_roundtrips(self):
        sample = os.path.join(ROOT, 'Chart_AT.json')
        if not os.path.isfile(sample):
            self.skipTest('仓库里没有 Chart_AT.json')
        with open(sample, 'rb') as f:
            raw = f.read()
        pkg = self.package({AT: raw.decode('utf-8')}, bundle_kw=dict(compression='none'))
        r = self.extract(pkg)
        self.assertEqual(r.total, 1)
        with open(os.path.join(self.tracks, 'Foo.Bar', 'Chart_AT.json'), 'rb') as f:
            self.assertEqual(f.read(), raw)  # 逐字节一致


class TestFindLocalArchives(Base):
    def test_order_and_filter(self):
        d = os.path.join(self.tmp, 'apk', 'com.x')
        os.makedirs(d)
        for n in ('main.82.com.x.obb', 'split_b.apk', 'base.apk', 'readme.txt', 'base.apk.part', 'split_a.apk'):
            open(os.path.join(d, n), 'wb').close()
        names = [os.path.basename(p) for p in A.find_local_archives(os.path.join(self.tmp, 'apk'))]
        self.assertEqual(names, ['base.apk', 'split_a.apk', 'split_b.apk', 'main.82.com.x.obb'])
        for n in ('main.9.com.x.obb', 'patch.83.com.x.obb', 'main.100.com.x.obb'):
            open(os.path.join(d, n), 'wb').close()
        names = [os.path.basename(p) for p in A.find_local_archives(os.path.join(self.tmp, 'apk'))]
        self.assertEqual(names[3:], ['main.9.com.x.obb', 'main.82.com.x.obb', 'main.100.com.x.obb', 'patch.83.com.x.obb'])
        self.assertEqual(A.find_local_archives(os.path.join(self.tmp, 'missing')), [])


if __name__ == '__main__':
    unittest.main()
