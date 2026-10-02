import lzma
import os.path
import pathlib
import re
from struct import unpack
from typing import IO, Optional

from enum import Enum
from rich.console import Console

from binary_reader import BinaryReader

COMMON_STRINGS = {
    0: 'AABB',
    5: 'AnimationClip',
    19: 'AnimationCurve',
    34: 'AnimationState',
    49: 'Array',
    55: 'Base',
    60: 'BitField',
    69: 'bitset',
    76: 'bool',
    81: 'char',
    86: 'ColorRGBA',
    96: 'Component',
    106: 'data',
    111: 'deque',
    117: 'double',
    124: 'dynamic_array',
    138: 'FastPropertyName',
    155: 'first',
    161: 'float',
    167: 'Font',
    172: 'GameObject',
    183: 'Generic Mono',
    196: 'GradientNEW',
    208: 'GUID',
    213: 'GUIStyle',
    222: 'int',
    226: 'list',
    231: 'long long',
    241: 'map',
    245: 'Matrix4x4f',
    256: 'MdFour',
    263: 'MonoBehaviour',
    277: 'MonoScript',
    288: 'm_ByteSize',
    299: 'm_Curve',
    307: 'm_EditorClassIdentifier',
    331: 'm_EditorHideFlags',
    349: 'm_Enabled',
    359: 'm_ExtensionPtr',
    374: 'm_GameObject',
    387: 'm_Index',
    395: 'm_IsArray',
    405: 'm_IsStatic',
    416: 'm_MetaFlag',
    427: 'm_Name',
    434: 'm_ObjectHideFlags',
    452: 'm_PrefabInternal',
    469: 'm_PrefabParentObject',
    490: 'm_Script',
    499: 'm_StaticEditorFlags',
    519: 'm_Type',
    526: 'm_Version',
    536: 'Object',
    543: 'pair',
    548: 'PPtr<Component>',
    564: 'PPtr<GameObject>',
    581: 'PPtr<Material>',
    596: 'PPtr<MonoBehaviour>',
    616: 'PPtr<MonoScript>',
    633: 'PPtr<Object>',
    646: 'PPtr<Prefab>',
    659: 'PPtr<Sprite>',
    672: 'PPtr<TextAsset>',
    688: 'PPtr<Texture>',
    702: 'PPtr<Texture2D>',
    718: 'PPtr<Transform>',
    734: 'Prefab',
    741: 'Quaternionf',
    753: 'Rectf',
    759: 'RectInt',
    767: 'RectOffset',
    778: 'second',
    785: 'set',
    789: 'short',
    795: 'size',
    800: 'SInt16',
    807: 'SInt32',
    814: 'SInt64',
    821: 'SInt8',
    827: 'staticvector',
    840: 'string',
    847: 'TextAsset',
    857: 'TextMesh',
    866: 'Texture',
    874: 'Texture2D',
    884: 'Transform',
    894: 'TypelessData',
    907: 'UInt16',
    914: 'UInt32',
    921: 'UInt64',
    928: 'UInt8',
    934: 'unsigned int',
    947: 'unsigned long long',
    966: 'unsigned short',
    981: 'vector',
    988: 'Vector2f',
    997: 'Vector3f',
    1006: 'Vector4f',
    1015: 'm_ScriptingClassIdentifier',
    1042: 'Gradient',
    1051: 'Type*',
    1057: 'int2_storage',
    1070: 'int3_storage',
    1083: 'BoundsInt',
    1093: 'm_CorrespondingSourceObject',
    1121: 'm_PrefabInstance',
    1138: 'm_PrefabAsset',
    1152: 'FileSize',
    1161: 'Hash128',
}


def _check_count(what: str, n: int, reader: BinaryReader) -> int:
    """条目数不可能比整份数据的字节数还多; 数据损坏时数量字段会是个离谱的值,
    不拦住的话下面的 for 循环要空转几十亿次(读到末尾只会返回 0), 线程就卡死了。"""
    if n < 0 or n > len(reader):
        raise ValueError(f'数据损坏: {what}数量异常({n})')
    return n


class TypeTreeNode:
    type_: str
    name: str
    byte_size: int
    index: int
    type_flags: int
    version: int
    meta_flag: int
    level: int
    type_str_offset: int
    name_str_offset: int
    ref_type_hash: int


class TypeTree:
    nodes: list[TypeTreeNode]
    string_buffer: bytes


_WARNED_CLASS_IDS: set[int] = set()


class ClassID(Enum):
    UNKNOWN = -1
    OBJECT = 0
    GAME_OBJECT = 1
    TRANSFORM = 4
    MATERIAL = 21
    TEXTURE_2D = 28
    MESH = 43
    SHADER = 48
    TEXT_ASSET = 49
    ANIMATION_CLIP = 74
    AUDIO_SOURCE = 82
    AUDIO_CLIP = 83
    ANIMATOR_CONTROLLER = 91
    ANIMATOR = 95
    MONO_BEHAVIOUR = 114
    MONO_SCRIPT = 115
    FONT = 128
    ASSET_BUNDLE = 142
    PARTICLE_SYSTEM = 198
    PARTICLE_SYSTEM_RENDERER = 199
    SHADER_VARIANT_COLLECTION = 200
    SPRITE_RENDERER = 212
    SPRITE = 213
    CANVAS_RENDERER = 222
    CANVAS = 223
    RECT_TRANSFORM = 224
    CANVAS_GROUP = 225

    @classmethod
    def from_int(cls, id: int) -> 'ClassID':
        try:
            return cls(id)
        except ValueError:
            # 每个未知 ID 只提示一次: 解包上千个 bundle 时同一个 ID 会反复出现, 不能刷屏
            # (集合放在模块级: Enum 类体里的普通属性会被当成枚举成员)
            if id not in _WARNED_CLASS_IDS:
                _WARNED_CLASS_IDS.add(id)
                print('未知的ClassID:', id)
            return ClassID.UNKNOWN


class SerializedType:
    class_id: ClassID
    is_stripped_type: bool
    script_type_index: int
    type_tree: TypeTree
    script_id: bytes
    old_type_hash: bytes
    type_dependencies: list[int]
    class_name: str
    namespace: str
    asm_name: str


class ObjectInfo:
    byte_start: int
    byte_size: int
    type_id: int
    class_id: ClassID
    path_id: int
    serialized_type: SerializedType | None = None
    is_destroyed: int
    stripped: bool


class LocalSerializedObjectIdentifier:
    local_serializerd_file_index: int
    local_identifier_in_file: int


class FileIdentifier:
    guid: bytes
    type: int
    path_name: str


class SerializedFile:
    class FileHeader:
        metadata_size: int
        file_size: int
        version: int
        data_offset: int
        big_endian: bool
        reserved_bytes: bytes

    assets_manager: 'AssetsManager'
    original_path: pathlib.Path
    path: pathlib.Path
    file_header: FileHeader
    file_big_endian: bool
    unity_version: str
    version: list[int]
    target_platform: int
    enable_type_tree: bool
    types: list[SerializedType]
    big_id_enabled: bool
    object_infos: list[ObjectInfo]
    script_types: list[LocalSerializedObjectIdentifier]
    externals: list[FileIdentifier]
    ref_types: list[SerializedType]
    user_information: str
    objects: list['Object']
    object_map: dict[int, 'Object']
    parent: Optional['BundleFile']

    def __init__(self, reader: 'FileReader', assets_manager: 'AssetsManager', parent: Optional['BundleFile'] = None):
        self.assets_manager = assets_manager
        self.reader = reader
        self.path = reader.path
        self.parent = parent
        self.file_header = self.FileHeader()
        self.file_header.metadata_size = reader.u32
        self.file_header.file_size = reader.u32
        self.file_header.version = reader.u32
        self.file_header.data_offset = reader.u32

        # header
        if self.file_header.version >= 9:
            self.file_header.big_endian = reader.boolean
            self.file_header.reserved_bytes = reader.read(3)
            self.file_big_endian = self.file_header.big_endian
        else:
            reader.pos = self.file_header.file_size - self.file_header.metadata_size
            self.file_big_endian = reader.boolean

        if self.file_header.version >= 22:
            self.file_header.metadata_size = reader.u32
            self.file_header.file_size = reader.u64
            self.file_header.data_offset = reader.u64
            reader.skip(8)

        # metadata
        if not self.file_big_endian:
            reader.big_endian = False

        if self.file_header.version >= 7:
            self.unity_version = reader.cstr()
            self.set_version(self.unity_version)

        if self.file_header.version >= 8:
            self.target_platform = reader.i32

        self.enable_type_tree = True
        if self.file_header.version >= 13:
            self.enable_type_tree = reader.boolean

        # types
        types_count = _check_count('类型', reader.i32, reader)
        self.types = []
        for _ in range(types_count):
            self.types.append(self._read_serialized_type(reader, False))

        self.big_id_enabled = False
        if 7 <= self.file_header.version < 14:
            self.big_id_enabled = bool(reader.i32)

        # objects
        object_count = _check_count('对象', reader.i32, reader)
        self.object_infos = []
        for _ in range(object_count):
            obj_info = ObjectInfo()

            if self.big_id_enabled:
                obj_info.path_id = reader.i64
            elif self.file_header.version < 14:
                obj_info.path_id = reader.i32
            else:
                reader.align(4)
                obj_info.path_id = reader.i64

            if self.file_header.version >= 22:
                obj_info.byte_start = reader.i64
            else:
                obj_info.byte_start = reader.u32

            obj_info.byte_start += self.file_header.data_offset
            obj_info.byte_size = reader.u32
            obj_info.type_id = reader.i32

            if self.file_header.version < 16:
                obj_info.class_id = ClassID(reader.u16)
                obj_info.serialized_type = next(
                    filter(lambda x: x and x.class_id.value == obj_info.type_id, self.types)
                )
            else:
                t = self.types[obj_info.type_id]
                obj_info.serialized_type = t
                obj_info.class_id = t.class_id

            if self.file_header.version < 11:
                obj_info.is_destroyed = reader.u16

            if 11 <= self.file_header.version < 17:
                script_type_index = reader.i16
                if obj_info.serialized_type is not None:
                    obj_info.serialized_type.script_type_index = script_type_index

            if self.file_header.version == 15 or self.file_header.version == 16:
                obj_info.stripped = reader.boolean

            self.object_infos.append(obj_info)

        # scripts
        if self.file_header.version >= 11:
            script_count = _check_count('脚本类型', reader.i32, reader)
            self.script_types = []

            for _ in range(script_count):
                script_type = LocalSerializedObjectIdentifier()
                script_type.local_serializerd_file_index = reader.i32

                if self.file_header.version < 14:
                    script_type.local_identifier_in_file = reader.i32
                else:
                    reader.align(4)
                    script_type.local_identifier_in_file = reader.i64

                self.script_types.append(script_type)

        # externals
        external_count = _check_count('外部引用', reader.i32, reader)
        self.externals = []
        for _ in range(external_count):
            external = FileIdentifier()
            if self.file_header.version >= 6:
                reader.cstr()

            if self.file_header.version >= 5:
                external.guid = reader.read(16)
                external.type = reader.i32

            external.path_name = reader.cstr()
            self.externals.append(external)

        # ref types
        if self.file_header.version >= 20:
            self.ref_types = [self._read_serialized_type(reader, True)
                              for _ in range(_check_count('引用类型', reader.i32, reader))]

        if self.file_header.version >= 5:
            self.user_information = reader.cstr()

        self.objects = []
        self.object_map = {}

        self.reader = reader

    def _read_serialized_type(self, reader: BinaryReader, is_ref_type: bool) -> SerializedType:
        t = SerializedType()
        t.class_id = ClassID.from_int(reader.i32)

        version = self.file_header.version
        if version >= 16:
            t.is_stripped_type = reader.boolean

        if version >= 17:
            t.script_type_index = reader.u16

        if version >= 13:
            if is_ref_type and t.script_type_index >= 0:
                t.script_id = reader.read(16)
            elif (version < 16 and t.class_id.value < 0) or (
                version >= 16 and t.class_id == t.class_id == ClassID.MONO_BEHAVIOUR
            ):
                t.script_id = reader.read(16)
            t.old_type_hash = reader.read(16)

        if self.enable_type_tree:
            t.type_tree = TypeTree()
            t.type_tree.nodes = []
            if version >= 12 or version == 10:
                nodes_count = _check_count('类型树节点', reader.i32, reader)
                length = _check_count('类型树字符串表长度', reader.i32, reader)

                for _ in range(nodes_count):
                    node = TypeTreeNode()
                    node.version = reader.u16
                    node.level = reader.u8
                    node.type_flags = reader.u8
                    node.type_str_offset = reader.u32
                    node.name_str_offset = reader.u32
                    node.byte_size = reader.i32
                    node.index = reader.i32
                    node.meta_flag = reader.i32

                    if version >= 19:
                        node.ref_type_hash = reader.u64
                    t.type_tree.nodes.append(node)

                t.type_tree.string_buffer = reader.read(length)

                with BinaryReader(t.type_tree.string_buffer) as buf_reader:

                    def read_string(offset: int) -> str:
                        if offset & 0x80000000 == 0:
                            buf_reader.pos = offset
                            return buf_reader.cstr()
                        offset = offset & 0x7FFFFFFF
                        return COMMON_STRINGS.get(offset, str(offset))

                    for node in t.type_tree.nodes:
                        node.type_ = read_string(node.type_str_offset)
                        node.name = read_string(node.name_str_offset)

            else:
                nodes = t.type_tree.nodes

                def read_type_tree(level: int = 0):
                    nd = TypeTreeNode()
                    nodes.append(nd)
                    nd.level = level
                    nd.type_ = reader.cstr()
                    nd.name = reader.cstr()
                    nd.byte_size = reader.i32

                    if version == 2:
                        reader.skip(4)

                    if version == 3:
                        nd.index = reader.i32

                    nd.type_flags = reader.i32
                    nd.version = reader.i32

                    if version != 3:
                        nd.meta_flag = reader.i32

                    for _ in range(reader.i32):
                        read_type_tree(level + 1)

                read_type_tree(0)

            if version >= 21:
                if is_ref_type:
                    t.class_name = reader.cstr()
                    t.namespace = reader.cstr()
                    t.asm_name = reader.cstr()
                else:
                    t.type_dependencies = [reader.i32 for _ in range(_check_count('类型依赖', reader.u32, reader))]

        return t

    def add_object(self, obj: 'Object'):
        self.objects.append(obj)
        self.object_map[obj.path_id] = obj

    def set_version(self, unity_version: str):
        self.version = []
        for sp in unity_version.split('.'):
            try:
                self.version.append(int(sp))
            except ValueError:
                pass


class ObjectReader(BinaryReader):
    asset_file: SerializedFile
    path_id: int
    byte_start: int
    byte_size: int
    class_id: ClassID
    serialized_type: SerializedType | None
    version: list[int]
    platform: int
    format_version: int

    def __init__(self, reader: BinaryReader, asset_file: SerializedFile, object_info: ObjectInfo):
        super().__init__(reader._stream)
        self.big_endian = reader.big_endian
        self.asset_file = asset_file
        self.path_id = object_info.path_id
        self.byte_start = object_info.byte_start
        self.byte_size = object_info.byte_size
        self.class_id = object_info.class_id
        self.serialized_type = object_info.serialized_type
        self.platform = asset_file.target_platform
        self.version = asset_file.version
        self.format_version = asset_file.file_header.version


class Object:
    asset_file: SerializedFile
    reader: ObjectReader
    path_id: int
    version: list[int]
    platform: int
    class_id: ClassID
    serialized_type: SerializedType | None
    byte_size: int

    def __init__(self, reader: ObjectReader):
        self.reader = reader
        reader.pos = reader.byte_start
        self.asset_file = reader.asset_file
        self.class_id = reader.class_id
        self.path_id = reader.path_id
        self.version = reader.version
        self.platform = reader.platform
        self.serialized_type = reader.serialized_type
        self.byte_size = reader.byte_size

        if self.platform == -2:
            _object_hide_flags = reader.u32


class EditorExtension(Object):
    def __init__(self, reader: ObjectReader):
        super().__init__(reader)
        if self.platform == -2:
            _prefab_parent_object = PPtr(reader)
            _prefab_internal = PPtr(reader)


class NamedObject(EditorExtension):
    name: str

    def __init__(self, reader: ObjectReader):
        super().__init__(reader)
        self.name = reader.aligned_string()


class PPtr:
    file_id: int
    path_id: int

    asset_file: SerializedFile
    index: int = -2

    def __init__(self, reader: ObjectReader):
        self.file_id = reader.i32
        self.path_id = reader.i32 if reader.format_version < 14 else reader.i64
        self.asset_file = reader.asset_file


class TextAsset(NamedObject):
    text: str

    def __init__(self, reader: ObjectReader):
        super().__init__(reader)
        self.text = reader.string(reader.i32)


class StreamFile:
    path: str
    stream: bytes


class FileReader(BinaryReader):
    GZIP_MAGIC = b'\x1f\x8b'
    BROTLI_MAGIC = b'brotli'
    ZIP_MAGIC = b'PK\x03\x04'
    ZIP_SPANNED_MAGIC = b'PK\x07\x08'

    class FileType(Enum):
        BundleFile = 0
        WebFile = 1
        GZipFile = 2
        BrotliFile = 3
        AssetsFile = 4
        ZipFile = 5
        ResourceFile = 6

    path: pathlib.Path
    file_type: FileType

    def __init__(self, stream: IO[bytes] | bytes | bytearray, path: str | pathlib.Path):
        super().__init__(stream)
        self.path = pathlib.Path(path)
        self.file_type = self.check_file_type()

    def check_file_type(self) -> FileType:
        signature = self.bcstrl(20)
        self.pos = 0
        match signature:
            case b'UnityWeb' | b'UnityRaw' | b'UnityArchive' | b'UnityFS':
                return self.FileType.BundleFile
            case b'UnityWebData1.0':
                return self.FileType.WebFile
            case _:
                magic = self.read(2)
                if magic == self.GZIP_MAGIC:
                    return self.FileType.GZipFile
                self.pos = 0x20
                magic = self.read(6)
                if magic == self.BROTLI_MAGIC:
                    return self.FileType.BrotliFile
                self.pos = 0
                if self.is_serialized_file():
                    return self.FileType.AssetsFile
                self.pos = 0
                magic = self.read(4)
                if magic == self.ZIP_MAGIC or magic == self.ZIP_SPANNED_MAGIC:
                    return self.FileType.ZipFile
                return self.FileType.ResourceFile

    def is_serialized_file(self) -> bool:
        length = len(self)

        if length < 20:
            return False

        self.skip(4)
        file_size = self.u32
        version = self.u32
        data_offset = self.u32

        self.skip(4)

        if version >= 22:
            # 格式 22(Unity 2022.1+)起头部变宽, 前面那个 u32 的 file_size/data_offset 恒为 0,
            # 真值在后面的 u64 里。以前这里拿 u32 的 file_size < 48 判断, 对 22 以上的文件
            # 永远返回 False, 整份资产文件被当成"资源文件"跳过, 表现为解出来 0 个谱面。
            if length < 48:
                return False
            self.skip(4)
            file_size = self.u64
            data_offset = self.u64

        if file_size != length or data_offset > file_size:
            return False

        return True

try:  # 装了 lz4 库就用它(C 实现, 快几十倍); 没装就用下面的纯 Python 版本
    from lz4.block import decompress as _lz4_c_decompress
except ImportError:  # pragma: no cover - 取决于环境里装没装 lz4
    _lz4_c_decompress = None


def lz4_decompress_py(data) -> bytearray:
    """纯 Python 的 LZ4 块解压(不依赖 lz4 库)。

    旧实现逐字节 append 复制匹配, 大谱面(几 MB)要好几秒; 这里改成切片复制,
    重叠匹配(偏移小于匹配长度)用「重复模式」一次性展开, 速度快一个数量级。
    数据损坏/被截断时抛 ValueError, 而不是悄悄返回一份残缺数据。
    """
    if not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    n = len(data)
    out = bytearray()
    i = 0
    try:
        while i < n:
            token = data[i]
            i += 1
            literal = token >> 4
            if literal == 15:
                while True:
                    b = data[i]
                    i += 1
                    literal += b
                    if b != 255:
                        break
            if literal:
                if i + literal > n:
                    raise ValueError('LZ4 数据被截断(字面量越界)')
                out += data[i:i + literal]
                i += literal
            if i >= n:
                break  # 最后一个序列只有字面量, 没有匹配
            offset = data[i] | (data[i + 1] << 8)
            i += 2
            if offset == 0 or offset > len(out):
                raise ValueError(f'LZ4 数据损坏(非法偏移 {offset})')
            match = token & 15
            if match == 15:
                while True:
                    b = data[i]
                    i += 1
                    match += b
                    if b != 255:
                        break
            match += 4
            begin = len(out) - offset
            if offset >= match:
                out += out[begin:begin + match]
            else:  # 重叠复制: 把最近 offset 个字节当模式重复
                pattern = out[begin:]
                q, r = divmod(match, offset)
                out += pattern * q + pattern[:r]
    except IndexError:
        raise ValueError('LZ4 数据被截断') from None
    return out


def lz4_decompress(data, uncompressed_size: int | None = None) -> bytearray:
    """LZ4 块解压。给出解压后大小且装了 lz4 库时走 C 实现, 否则走纯 Python 实现。"""
    if _lz4_c_decompress is not None and uncompressed_size:
        try:
            return bytearray(_lz4_c_decompress(bytes(data), uncompressed_size=uncompressed_size))
        except Exception:
            pass  # 交给纯 Python 版再试一次, 它能给出更明确的错误
    return lz4_decompress_py(data)


def lzma_decompress_unity(data, uncompressed_size: int | None = None) -> bytes:
    """Unity 的 LZMA 块: 5 字节头(props + 小端字典大小) + 没有长度字段的裸 LZMA1 数据。"""
    if len(data) < 5:
        raise ValueError('LZMA 数据太短')
    props = data[0]
    if props >= 9 * 5 * 5:
        raise ValueError(f'LZMA 参数非法(props={props})')
    dict_size = max(unpack('<I', bytes(data[1:5]))[0], 4096)
    lc = props % 9
    props //= 9
    lp = props % 5
    pb = props // 5
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
        {'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}])
    return decoder.decompress(bytes(data[5:]), max_length=uncompressed_size or -1)


class BundleFile:
    class Header:
        signature: str
        version: int
        unity_version: str
        unity_revision: str
        size: int
        compressed_blocks_info_size: int
        uncompressed_blocks_info_size: int
        flags: int

    class StorageBlock:
        compressed_size: int
        uncompressed_size: int
        flags: int

    class Node:
        offset: int
        size: int
        flags: int
        path: str

    header: Header
    blocks_info: list[StorageBlock]
    directory_info: list[Node]
    files: list[StreamFile]
    reader: FileReader

    def __init__(self, reader: FileReader):
        self.reader = reader
        self.header = self.Header()
        self.header.signature = reader.cstr()
        self.header.version = reader.u32
        self.header.unity_version = reader.cstr()
        self.header.unity_revision = reader.cstr()

        signature = self.header.signature

        if signature == 'UnityArchive':
            raise RuntimeError('not supported')
        elif signature in ['UnityWeb', 'UnityRaw'] and self.header.version != 6:
            raise RuntimeError('not supported')
        elif signature == 'UnityFS' or (signature in ['UnityWeb', 'UnityRaw'] and self.header.version == 6):
            if len(reader) - reader.pos < 20:  # i64 + 3 个 u32
                raise ValueError('bundle 头部被截断')
            self.header.size = reader.i64
            self.header.compressed_blocks_info_size = reader.u32
            self.header.uncompressed_blocks_info_size = reader.u32
            self.header.flags = reader.u32
            if signature != 'UnityFS':
                reader.skip(1)

            flags = self.header.flags
            unity = self._parse_unity_version(self.header.unity_revision)
            new_flags = self._uses_new_flags(unity)
            # 同一个标志位在新旧 Unity 里含义不同: 旧版 0x200 = 加密, 新版 0x400 = 加密、
            # 0x200 = 「块信息之后要补齐到 16 字节」。必须按引擎版本区分, 否则新版 bundle 会被
            # 误判成加密, 或者漏掉补齐导致后面整段数据错位。
            if flags & (0x400 if new_flags else 0x200):
                raise RuntimeError('这个 AssetBundle 被加密了(没有解密密钥), 无法解包')

            # 头部之后补齐到 16 字节: 格式版本 >= 7, 或者 Unity 2019.4.15+
            # (有的游戏在这些引擎版本里写的还是格式版本 6, 但数据其实已经对齐了)
            if self.header.version >= 7 or (unity[0] == 2019 and unity >= (2019, 4, 15)):
                reader.align(16)

            info_size = self.header.compressed_blocks_info_size
            if flags & 0x80:  # 块信息放在文件末尾
                position = reader.pos
                if len(reader) - info_size < position:
                    raise ValueError('bundle 文件被截断(放不下块信息)')
                reader.pos = len(reader) - info_size
                block_info_bytes = reader.read(info_size)
                reader.pos = position
            else:
                block_info_bytes = reader.read(info_size)
            if len(block_info_bytes) != info_size:
                raise ValueError('bundle 文件被截断(块信息不完整)')

            uncompressed_data = self._decompress(
                block_info_bytes, self.header.uncompressed_blocks_info_size, flags)

            with BinaryReader(uncompressed_data) as uc_reader:
                _uncompressed_data_hash = uc_reader.read(16)
                self.blocks_info = []
                for _ in range(_check_count('数据块', uc_reader.i32, uc_reader)):
                    block = self.StorageBlock()
                    block.uncompressed_size = uc_reader.u32
                    block.compressed_size = uc_reader.u32
                    block.flags = uc_reader.u16
                    self.blocks_info.append(block)

                self.directory_info = []
                for _ in range(_check_count('目录项', uc_reader.i32, uc_reader)):
                    node = self.Node()
                    node.offset = uc_reader.i64
                    node.size = uc_reader.i64
                    node.flags = uc_reader.u32
                    node.path = uc_reader.cstr()
                    self.directory_info.append(node)

            if new_flags and flags & 0x200:  # BlockInfoNeedPaddingAtStart
                reader.align(16)

            block_stream = bytearray()

            for block in self.blocks_info:
                chunk = reader.read(block.compressed_size)
                if len(chunk) != block.compressed_size:
                    raise ValueError('bundle 文件被截断(数据块不完整)')
                block_stream.extend(self._decompress(chunk, block.uncompressed_size, block.flags))

            with BinaryReader(block_stream) as s_reader:
                self.files = []
                for node in self.directory_info:
                    file = StreamFile()
                    file.path = node.path
                    s_reader.pos = node.offset
                    file.stream = s_reader.read(node.size)
                    self.files.append(file)


    @staticmethod
    def _parse_unity_version(revision: str) -> tuple[int, int, int]:
        m = re.match(r'(\d+)\.(\d+)\.(\d+)', revision or '')
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)

    @staticmethod
    def _uses_new_flags(v: tuple[int, int, int]) -> bool:
        """Unity 2020.3.34 / 2021.3.2 / 2022.1.1 起, 头部标志位的含义换了一套"""
        old = (v < (2020,)
               or (v[0] == 2020 and v < (2020, 3, 34))
               or (v[0] == 2021 and v < (2021, 3, 2))
               or (v[0] == 2022 and v < (2022, 1, 1)))
        return not old

    @staticmethod
    def _decompress(data: bytes, uncompressed_size: int, flags: int) -> bytes:
        match flags & 0x3F:
            case 1:  # LZMA
                out = lzma_decompress_unity(data, uncompressed_size)
            case 2 | 3:  # LZ4 | LZ4HC
                out = lz4_decompress(data, uncompressed_size)
            case _:  # 不压缩
                return bytes(data)
        if len(out) != uncompressed_size:
            raise RuntimeError(f'解压后的大小不对: 期望 {uncompressed_size}, 实际 {len(out)}(压缩方式 {flags & 0x3F})')
        return out


class AssetsManager:
    asset_files: list[SerializedFile]
    asset_file_hashes: list[str]
    resource_file_readers: dict[str, BinaryReader]
    asset_file_index_cache: dict[str, int]

    def __init__(self):
        self.asset_files = []
        self.asset_file_hashes = []
        self.resource_file_readers = {}
        self.asset_file_index_cache = {}

    def load_file(self, file: IO[bytes] | FileReader):
        if not isinstance(file, FileReader):
            file = FileReader(file, file.name)
        if file.file_type == FileReader.FileType.BundleFile:
            self.load_bundle(file)

    def load_bundle(self, reader: FileReader, original_path: pathlib.Path | None = None):
        bundle_file = BundleFile(reader)
        for file in bundle_file.files:
            subreader = FileReader(file.stream, reader.path.parent / pathlib.Path(file.path).name)
            subreader.pos = 0
            if subreader.file_type == FileReader.FileType.AssetsFile:
                self.load_assets(
                    subreader, original_path or reader.path, bundle_file.header.unity_revision, bundle_file
                )
            else:
                self.resource_file_readers[os.path.basename(file.path)] = subreader

    def load_assets(self, reader: FileReader, original_path: pathlib.Path, unity_version: str, bundle_file: BundleFile):
        if reader.path.name not in self.asset_file_hashes:
            asset_file = SerializedFile(reader, self, bundle_file)
            asset_file.original_path = original_path
            if not unity_version and asset_file.file_header.version < 7:
                asset_file.set_version(unity_version)
            self.asset_files.append(asset_file)
            self.asset_file_hashes.append(asset_file.path.name)

    def read_assets(self, console: Console | None = None):
        files = self.asset_files
        if console:
            from rich.progress import track

            files = track(files, description='正在解析文件...', console=console)

        for asset_file in files:
            for object_info in asset_file.object_infos:
                with ObjectReader(asset_file.reader, asset_file, object_info) as obj_reader:
                    if obj_reader.class_id == ClassID.TEXT_ASSET:
                        asset_file.add_object(TextAsset(obj_reader))


if __name__ == '__main__':
    pass
