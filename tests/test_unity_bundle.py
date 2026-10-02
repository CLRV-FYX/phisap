"""extract.py 的 Unity 解析回归测试(全部用 tests/unity_fixtures.py 现拼的数据, 不含游戏素材)。

这次补的几处, 都是拿夹具对照 UnityPy 发现旧解析器会出错的地方:
  - 块信息放在文件末尾(0x80): 旧代码定位写反了(seek 到文件末尾之后), 读到空数据
  - LZMA 压缩的 bundle: 旧代码直接抛 'LZMA unsupported'
  - Unity 2021.3.2+/2022.1+ 的「块信息后补齐 16 字节」标志(0x200): 旧代码不认识, 数据整体错位
  - SerializedFile 格式 22(Unity 2022.1+): 识别函数读错字段, 整个文件被当成资源文件跳过
  - Unity 2019.4.15+ 但格式版本写成 6 的 bundle: 头部其实已经对齐, 旧代码没对齐
  - 纯 Python LZ4 解压太慢, 且数据损坏时悄悄返回残缺数据
"""
import io
import itertools
import json
import os
import random
import struct
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import extract  # noqa: E402
from binary_reader import BinaryReader  # noqa: E402
from extract import (AssetsManager, FileReader, TextAsset, lz4_decompress, lz4_decompress_py,  # noqa: E402
                     lzma_decompress_unity)
from unity_fixtures import (build_bundle, build_serialized_file, lz4_block_compress,  # noqa: E402
                            lzma_raw_compress, make_chart_bundle)

try:
    import lz4.block as lz4_lib  # noqa: E402
except ImportError:
    lz4_lib = None

try:
    import UnityPy  # noqa: E402
except ImportError:
    UnityPy = None


def lz4_reference(data: bytes) -> bytearray:
    """旧实现的原样拷贝, 作为解压结果的对照(逐字节复制, 慢但直观)"""
    result = bytearray()
    reader = BinaryReader(data, big_endian=False)
    data_size = len(data)
    while True:
        token = reader.u8
        literal_length = token >> 4
        if literal_length == 15:
            while (add := reader.u8) == 255:
                literal_length += 255
            else:
                literal_length += add
        result.extend(reader.read(literal_length))
        if reader.pos == data_size:
            break
        offset = reader.u16
        if offset == 0:
            continue
        match_length = token & 0b1111
        if match_length == 15:
            while (add := reader.u8) == 255:
                match_length += 255
            else:
                match_length += add
        match_length += 4
        begin = len(result) - offset
        for i in range(match_length):
            result.append(result[begin + i])
    return result


def parse_text_assets(data: bytes, name: str = 'x.bundle') -> list[tuple[str, str]]:
    mgr = AssetsManager()
    mgr.load_file(FileReader(data, name))
    mgr.read_assets()
    return [(o.name, o.text) for f in mgr.asset_files for o in f.objects if isinstance(o, TextAsset)]


def call_with_timeout(fn, *args, timeout: float = 10.0):
    """在线程里跑 fn; 超时算失败。解析坏数据时的死循环要能被测试抓到, 而不是把整个测试套件卡死。"""
    box: dict = {}

    def target():
        try:
            box['value'] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - 原样带回主线程
            box['error'] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise AssertionError(f'{getattr(fn, "__name__", fn)} 超过 {timeout}s 没返回(疑似死循环)')
    if 'error' in box:
        raise box['error']
    return box['value']


def sample(kind: str, n: int, rng: random.Random) -> bytes:
    if kind == 'rand':
        return bytes(rng.randrange(256) for _ in range(n))
    if kind == 'text':
        words = [b'judgeLine', b'notesAbove', b'{"time":', b'"holdTime":0,', b'floorPosition', b'1.0', b'[', b']', b',']
        return b''.join(rng.choice(words) for _ in range(n // 6 + 1))[:n]
    if kind == 'run':
        return bytes([rng.randrange(3)]) * n
    out = bytearray()  # mix
    while len(out) < n:
        out += rng.choice([bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40))),
                           b'ab' * rng.randrange(1, 300), bytes(rng.randrange(1, 700))])
    return bytes(out[:n])


class TestLz4(unittest.TestCase):
    def test_roundtrip_matches_reference(self):
        rng = random.Random(1234)
        for kind in ('rand', 'text', 'run', 'mix'):
            for size in (0, 1, 5, 12, 13, 17, 100, 1000, 20000):
                data = sample(kind, size, rng)
                comp = lz4_block_compress(data)
                with self.subTest(kind=kind, size=size):
                    self.assertEqual(bytes(lz4_decompress_py(comp)), data)
                    self.assertEqual(bytes(lz4_reference(comp)), data)
                    self.assertEqual(bytes(lz4_decompress(comp, size or None)), data)

    def test_overlapping_match_and_long_extensions(self):
        # 单字节重复 = 偏移 1 的重叠匹配; 4000 字节随机数据 = 字面量长度扩展字节(>=255);
        # 重复 5000 次的 16 字节串 = 匹配长度扩展字节
        rng = random.Random(5)
        blob = bytes(rng.randrange(256) for _ in range(4000)) + b'z' * 3000 + b'0123456789abcdef' * 5000
        comp = lz4_block_compress(blob)
        self.assertEqual(bytes(lz4_decompress_py(comp)), blob)

    def test_truncated_data_raises(self):
        data = sample('text', 5000, random.Random(2))
        comp = lz4_block_compress(data)
        for cut in (1, 3, len(comp) // 2, len(comp) - 1):
            with self.subTest(cut=cut):
                try:
                    out = lz4_decompress_py(comp[:cut])
                except ValueError:
                    continue
                # 恰好在序列边界被截断时不会抛, 但绝不能「看起来完整」
                self.assertNotEqual(bytes(out), data)

    def test_bad_offset_raises(self):
        # token: 1 个字面量 + 匹配; 偏移 9 超出已输出的 1 字节
        with self.assertRaises(ValueError):
            lz4_decompress_py(bytes([0x10, 0x41, 0x09, 0x00, 0x00]))
        with self.assertRaises(ValueError):  # 偏移 0
            lz4_decompress_py(bytes([0x10, 0x41, 0x00, 0x00]))

    @unittest.skipIf(lz4_lib is None, '没装 lz4 库, 跳过与官方实现的互相验证')
    def test_cross_check_with_lz4_library(self):
        rng = random.Random(99)
        for kind in ('rand', 'text', 'run', 'mix'):
            for size in (1, 13, 700, 70000):
                data = sample(kind, size, rng)
                for comp in (lz4_lib.compress(data, store_size=False),
                             lz4_lib.compress(data, mode='high_compression', compression=9, store_size=False),
                             lz4_block_compress(data)):
                    with self.subTest(kind=kind, size=size):
                        self.assertEqual(bytes(lz4_decompress_py(comp)), data)
                        self.assertEqual(lz4_lib.decompress(comp, uncompressed_size=size), data)  # 夹具压缩器合规
                        self.assertEqual(bytes(lz4_decompress(comp, size)), data)

    def test_fast_path_falls_back_on_c_error(self):
        data = b'hello hello hello hello hello'
        comp = lz4_block_compress(data)

        def boom(*_a, **_k):
            raise RuntimeError('C 扩展炸了')

        old = extract._lz4_c_decompress
        extract._lz4_c_decompress = boom
        try:
            self.assertEqual(bytes(lz4_decompress(comp, len(data))), data)
        finally:
            extract._lz4_c_decompress = old


class TestLzma(unittest.TestCase):
    def test_roundtrip(self):
        data = sample('text', 30000, random.Random(3))
        self.assertEqual(lzma_decompress_unity(lzma_raw_compress(data), len(data)), data)

    def test_bad_props_and_short_input(self):
        with self.assertRaises(ValueError):
            lzma_decompress_unity(b'\x01\x02')
        with self.assertRaises(ValueError):
            lzma_decompress_unity(bytes([225, 0, 0, 1, 0]) + b'xx')  # props >= 225


class TestBundleMatrix(unittest.TestCase):
    TEXT = json.dumps({'x': list(range(0, 9000, 3)), 's': '谱面·中文'}, ensure_ascii=False)

    def test_all_layouts(self):
        count = 0
        for comp, at_end, pad, engine, sf_ver, ver, tree in itertools.product(
                ('none', 'lz4', 'lzma'), (False, True), (False, True),
                ('2019.4.31f1', '2021.3.20f1', '2022.3.10f1'), (17, 21, 22), (6, 7), (False, True)):
            if pad and engine.startswith('2019'):
                continue  # 2019 里 0x200 是加密位, 不会这么造
            if ver == 6 and not engine.startswith('2019'):
                continue
            with self.subTest(comp=comp, at_end=at_end, pad=pad, engine=engine, sf=sf_ver, ver=ver, tree=tree):
                data = make_chart_bundle('Chart_IN', self.TEXT, compression=comp, info_at_end=at_end,
                                         padding_flag=pad, engine=engine, version=ver, sf_version=sf_ver,
                                         type_tree=tree)
                self.assertEqual(parse_text_assets(data), [('Chart_IN', self.TEXT)])
                count += 1
        self.assertGreater(count, 180)

    def test_mixed_block_compression(self):
        # 块信息用 LZ4、数据块用 LZMA(以及反过来), 再加上多个小块
        for info_comp, block_comp in (('lz4', 'lzma'), ('lzma', 'lz4'), ('none', 'lz4')):
            with self.subTest(info=info_comp, blocks=block_comp):
                data = make_chart_bundle('Chart_AT', self.TEXT, compression=info_comp,
                                         block_compression=block_comp, block_size=700)
                self.assertEqual(parse_text_assets(data), [('Chart_AT', self.TEXT)])

    def test_incompressible_block_stored_raw(self):
        rng = random.Random(8)
        noise = ''.join(chr(rng.randrange(0x4E00, 0x9FA5)) for _ in range(3000))  # 压不动的汉字
        data = make_chart_bundle('Chart_EZ', noise, compression='lz4', block_size=512)
        self.assertEqual(parse_text_assets(data), [('Chart_EZ', noise)])

    def test_multiple_text_assets_in_one_file(self):
        sf = build_serialized_file([('A', 'aaa'), ('B', 'bbb' * 100), ('C', '')])
        data = build_bundle({'CAB-1': sf}, compression='lz4')
        self.assertEqual(parse_text_assets(data), [('A', 'aaa'), ('B', 'bbb' * 100), ('C', '')])

    def test_multiple_files_in_bundle(self):
        sf1 = build_serialized_file([('one', '1')])
        sf2 = build_serialized_file([('two', '2')])
        data = build_bundle({'CAB-1': sf1, 'CAB-2': sf2}, compression='lzma')
        self.assertEqual(sorted(parse_text_assets(data)), [('one', '1'), ('two', '2')])


class TestEncryptionFlags(unittest.TestCase):
    def test_new_engine_encryption_bit_rejected(self):
        data = make_chart_bundle('Chart_IN', '{}', engine='2021.3.20f1', extra_flags=0x400)
        with self.assertRaisesRegex(RuntimeError, '加密'):
            parse_text_assets(data)

    def test_old_engine_encryption_bit_rejected(self):
        data = make_chart_bundle('Chart_IN', '{}', engine='2019.4.31f1', extra_flags=0x200)
        with self.assertRaisesRegex(RuntimeError, '加密'):
            parse_text_assets(data)

    def test_same_bit_means_padding_on_new_engine(self):
        data = make_chart_bundle('Chart_IN', '{"a":1}', engine='2022.3.10f1', padding_flag=True)
        self.assertEqual(parse_text_assets(data), [('Chart_IN', '{"a":1}')])

    def test_version_rule_boundaries(self):
        uses_new = extract.BundleFile._uses_new_flags
        for ver, expected in (((2019, 4, 31), False), ((2020, 3, 33), False), ((2020, 3, 34), True),
                              ((2021, 3, 1), False), ((2021, 3, 2), True), ((2022, 1, 0), False),
                              ((2022, 1, 1), True), ((2023, 1, 0), True), ((0, 0, 0), False)):
            with self.subTest(ver=ver):
                self.assertEqual(uses_new(ver), expected)

    def test_unparseable_revision_defaults_to_old_semantics(self):
        self.assertEqual(extract.BundleFile._parse_unity_version(''), (0, 0, 0))
        self.assertEqual(extract.BundleFile._parse_unity_version('2019.4.31f1c1'), (2019, 4, 31))


class TestClassId(unittest.TestCase):
    def test_unknown_id_is_reported_once_and_does_not_become_an_enum_member(self):
        import contextlib
        extract._WARNED_CLASS_IDS.discard(123456)
        members = len(list(extract.ClassID))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            first = extract.ClassID.from_int(123456)
            second = extract.ClassID.from_int(123456)
        self.assertIs(first, extract.ClassID.UNKNOWN)
        self.assertIs(second, extract.ClassID.UNKNOWN)
        self.assertEqual(buf.getvalue().count('123456'), 1)  # 解包上千个 bundle 时不能刷屏
        self.assertEqual(len(list(extract.ClassID)), members)  # 提示用的集合不能混进枚举成员
        self.assertIs(extract.ClassID.from_int(49), extract.ClassID.TEXT_ASSET)


class TestFileTypeDetection(unittest.TestCase):
    def test_serialized_versions(self):
        for ver in (17, 19, 21, 22):
            with self.subTest(ver=ver):
                reader = FileReader(build_serialized_file([('a', 'b')], version=ver), 'x.assets')
                self.assertEqual(reader.file_type, FileReader.FileType.AssetsFile)

    def test_bundle_and_other(self):
        self.assertEqual(FileReader(make_chart_bundle('a', 'b'), 'x').file_type, FileReader.FileType.BundleFile)
        self.assertEqual(FileReader(b'PK\x03\x04' + b'\0' * 100, 'x').file_type, FileReader.FileType.ZipFile)
        self.assertEqual(FileReader(os.urandom(200), 'x').file_type, FileReader.FileType.ResourceFile)

    def test_corrupted_bundles_never_hang(self):
        """随机破坏文件里的字节: 允许抛异常, 也允许(破坏落在无校验的数据区时)解出被改过的文本,
        但绝不能卡死。完整性靠上层的 JSON 校验兜底, 这里只管解析器本身不死循环。"""
        text = '{"a":1}' * 300
        rng = random.Random(77)
        raised = 0
        for comp in ('none', 'lz4', 'lzma'):
            clean = make_chart_bundle('Chart_IN', text, compression=comp)
            for trial in range(60):
                data = bytearray(clean)
                for _ in range(rng.randrange(1, 4)):
                    data[rng.randrange(len(data))] ^= rng.randrange(1, 256)
                try:
                    out = call_with_timeout(parse_text_assets, bytes(data), timeout=20)
                except AssertionError:
                    raise  # 超时 = 死循环
                except Exception:
                    raised += 1
                    continue
                self.assertIsInstance(out, list)
        self.assertGreater(raised, 20)  # 大部分破坏应当被检测到(否则这个测试就没在测东西)

    def test_truncated_bundle_raises(self):
        clean = make_chart_bundle('Chart_IN', '{"a":1}' * 300, compression='lz4')
        for cut in (10, 30, len(clean) // 2, len(clean) - 5):
            with self.subTest(cut=cut):
                with self.assertRaises(Exception):
                    call_with_timeout(parse_text_assets, clean[:cut])

    def test_unterminated_string_raises_instead_of_looping(self):
        with self.assertRaises(EOFError):
            call_with_timeout(BinaryReader(b'abc').cstr)
        with self.assertRaises(EOFError):
            call_with_timeout(BinaryReader(b'').bcstr)
        self.assertEqual(BinaryReader(b'abc\0def').cstr(), 'abc')

    def test_absurd_counts_rejected_quickly(self):
        sf = bytearray(build_serialized_file([('a', 'b')]))
        # 元数据里: Unity 版本串(12 字节含 \0) + platform(4) + type_tree 标志(1), 然后是类型数量
        meta_start = 20
        count_pos = meta_start + len('2019.4.31f1') + 1 + 4 + 1
        self.assertEqual(struct.unpack_from('<i', sf, count_pos)[0], 1)  # 先确认定位对了
        struct.pack_into('<i', sf, count_pos, 0x7FFFFFF0)
        with self.assertRaisesRegex(ValueError, '数量异常'):
            call_with_timeout(parse_text_assets, build_bundle({'CAB-1': bytes(sf)}, compression='lz4'))


@unittest.skipIf(UnityPy is None, '没装 UnityPy, 跳过与它的交叉验证(仅开发时用)')
class TestFixturesAgainstUnityPy(unittest.TestCase):
    """夹具是我们自己写的, 万一夹具和解析器「一起错」测试就失去意义了。
    UnityPy 是独立实现且久经考验, 它能读出同样的内容, 说明夹具确实是 Unity 的真实格式。"""

    def test_unitypy_reads_fixtures(self):
        text = json.dumps({'k': list(range(2000)), 's': '谱面'}, ensure_ascii=False)
        for comp, at_end, engine, sf_ver, tree in itertools.product(
                ('none', 'lz4', 'lzma'), (False, True), ('2019.4.31f1', '2022.3.10f1'), (17, 21, 22), (False, True)):
            with self.subTest(comp=comp, at_end=at_end, engine=engine, sf=sf_ver, tree=tree):
                data = make_chart_bundle('Chart_IN', text, compression=comp, info_at_end=at_end,
                                         engine=engine, sf_version=sf_ver, type_tree=tree,
                                         padding_flag=engine.startswith('2022'))
                env = UnityPy.load(data)
                assets = [o.read() for o in env.objects if o.type.name == 'TextAsset']
                self.assertEqual([(a.m_Name, a.m_Script) for a in assets], [('Chart_IN', text)])


if __name__ == '__main__':
    unittest.main()
