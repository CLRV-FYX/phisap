"""最小 DEX 组装器。只覆盖这只手机版用到的指令。"""
from __future__ import annotations

import hashlib
import struct
import zlib


def uleb(n: int) -> bytes:
    if n < 0:
        raise ValueError(n)
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        last = (n == 0 and (b & 0x40) == 0) or (n == -1 and (b & 0x40) != 0)
        if not last:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def mutf8(text: str) -> bytes:
    """MUTF-8。长度是 UTF-16 码元个数，不是 UTF-8 字节数。写错的话系统直接拒载 dex。"""
    out = bytearray()
    utf16_size = 0
    for ch in text:
        cp = ord(ch)
        if cp == 0:
            out.extend((0xC0, 0x80))
            utf16_size += 1
        elif cp < 0x80:
            out.append(cp)
            utf16_size += 1
        elif cp < 0x800:
            out.append(0xC0 | (cp >> 6))
            out.append(0x80 | (cp & 0x3F))
            utf16_size += 1
        elif cp < 0x10000:
            out.append(0xE0 | (cp >> 12))
            out.append(0x80 | ((cp >> 6) & 0x3F))
            out.append(0x80 | (cp & 0x3F))
            utf16_size += 1
        else:
            cp -= 0x10000
            for unit in (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)):
                out.append(0xE0 | (unit >> 12))
                out.append(0x80 | ((unit >> 6) & 0x3F))
                out.append(0x80 | (unit & 0x3F))
            utf16_size += 2
    return uleb(utf16_size) + bytes(out) + b'\x00'


def _u16(n: int) -> int:
    return n & 0xffff


def _uleb_at(data: bytes, off: int) -> tuple[int, int]:
    n = s = 0
    while True:
        b = data[off]
        off += 1
        n |= (b & 0x7F) << s
        if not b & 0x80:
            return n, off
        s += 7


def _sleb_at(data: bytes, off: int) -> tuple[int, int]:
    n = s = 0
    while True:
        b = data[off]
        off += 1
        n |= (b & 0x7F) << s
        s += 7
        if not b & 0x80:
            if s < 32 and b & 0x40:
                n |= (~0) << s
            return n, off


def _decode_mutf8(raw: bytes) -> str:
    """按 MUTF-8 解出字符串，并数 UTF-16 码元。返回 (text, utf16_size)。"""
    chars = []
    utf16 = 0
    i = 0
    while i < len(raw):
        b0 = raw[i]
        if b0 == 0:
            raise ValueError('interior NUL in MUTF-8')
        if b0 < 0x80:
            chars.append(chr(b0))
            utf16 += 1
            i += 1
        elif b0 < 0xE0:
            b1 = raw[i + 1]
            cp = ((b0 & 0x1F) << 6) | (b1 & 0x3F)
            chars.append(chr(cp))
            utf16 += 1
            i += 2
        else:
            b1 = raw[i + 1]
            b2 = raw[i + 2]
            cp = ((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F)
            chars.append(chr(cp))
            utf16 += 1
            i += 3
    return ''.join(chars), utf16


# opcode -> code units. 只覆盖组装器用到的，以及校验时必须能走完的指令。
_OPCODE_UNITS = {
    0x00: 1, 0x01: 1, 0x07: 1, 0x0A: 1, 0x0B: 1, 0x0C: 1, 0x0D: 1, 0x0E: 1, 0x0F: 1, 0x11: 1,
    0x12: 1, 0x13: 2, 0x14: 3, 0x16: 2, 0x1A: 2, 0x1F: 2, 0x21: 1, 0x22: 2, 0x23: 2, 0x24: 3,
    0x28: 1, 0x29: 2, 0x32: 2, 0x33: 2, 0x34: 2, 0x35: 2, 0x36: 2, 0x37: 2,
    0x38: 2, 0x39: 2, 0x3A: 2, 0x3B: 2, 0x3C: 2, 0x3D: 2,
    0x44: 2, 0x46: 2, 0x48: 2, 0x4B: 2, 0x4D: 2, 0x4F: 2,
    0x52: 2, 0x53: 2, 0x54: 2, 0x55: 2, 0x56: 2, 0x59: 2, 0x5A: 2, 0x5B: 2, 0x5C: 2, 0x5D: 2,
    0x60: 2, 0x61: 2, 0x62: 2, 0x67: 2, 0x68: 2, 0x69: 2,
    0x6E: 3, 0x6F: 3, 0x70: 3, 0x71: 3, 0x72: 3, 0x74: 3, 0x75: 3, 0x76: 3, 0x77: 3, 0x78: 3,
    0x81: 1, 0x8D: 1, 0x90: 2, 0x91: 2, 0x92: 2, 0x93: 2, 0x9B: 2, 0x9C: 2,
    0xD0: 2, 0xD3: 2, 0xD8: 2, 0xDA: 2, 0xDD: 2, 0xDE: 2, 0xE1: 2, 0xE2: 2,
    0x31: 2,
}


def audit_dex(blob: bytes) -> None:
    """按 dex 规范检查系统会在启动前拒绝的结构错误。"""
    if blob[:8] != b'dex\n035\x00':
        raise RuntimeError('bad dex magic')
    if zlib.adler32(blob[12:]) & 0xFFFFFFFF != struct.unpack_from('<I', blob, 8)[0]:
        raise RuntimeError('dex checksum mismatch')
    if hashlib.sha1(blob[32:]).digest() != blob[12:32]:
        raise RuntimeError('dex signature mismatch')

    def u32(off: int) -> int:
        return struct.unpack_from('<I', blob, off)[0]

    def u16(off: int) -> int:
        return struct.unpack_from('<H', blob, off)[0]

    string_ids_size, string_ids_off = u32(56), u32(60)
    type_ids_size, type_ids_off = u32(64), u32(68)
    proto_ids_size, proto_ids_off = u32(72), u32(76)
    field_ids_size, field_ids_off = u32(80), u32(84)
    method_ids_size, method_ids_off = u32(88), u32(92)
    class_defs_size, class_defs_off = u32(96), u32(100)

    strings = []
    for i in range(string_ids_size):
        off = u32(string_ids_off + i * 4)
        declared, ptr = _uleb_at(blob, off)
        end = blob.index(b'\x00', ptr)
        text, actual = _decode_mutf8(blob[ptr:end])
        if declared != actual:
            raise RuntimeError(f'string {i} utf16_size {declared} != {actual}: {text[:40]!r}')
        strings.append(text)
    for i in range(1, len(strings)):
        if strings[i] <= strings[i - 1]:
            raise RuntimeError(f'strings not strictly sorted at {i}')

    type_desc = [strings[u32(type_ids_off + i * 4)] for i in range(type_ids_size)]
    type_order = [strings.index(d) for d in type_desc]
    if type_order != sorted(type_order):
        raise RuntimeError('type_ids not sorted')

    class_idxs = [u32(class_defs_off + i * 32) for i in range(class_defs_size)]
    if class_idxs != sorted(class_idxs) or len(set(class_idxs)) != len(class_idxs):
        raise RuntimeError(f'class_defs not strictly sorted: {class_idxs}')

    proto_keys = []
    for i in range(proto_ids_size):
        base = proto_ids_off + i * 12
        ret = u32(base + 4)
        params_off = u32(base + 8)
        args = ()
        if params_off:
            n = u32(params_off)
            args = tuple(u16(params_off + 4 + j * 2) for j in range(n))
        proto_keys.append((ret, args))
    if proto_keys != sorted(proto_keys) or len(set(proto_keys)) != len(proto_keys):
        raise RuntimeError('proto_ids not strictly sorted')

    field_keys = []
    for i in range(field_ids_size):
        base = field_ids_off + i * 8
        field_keys.append((u16(base), u32(base + 4), u16(base + 2)))
    if field_keys != sorted(field_keys) or len(set(field_keys)) != len(field_keys):
        raise RuntimeError('field_ids not strictly sorted')

    method_keys = []
    for i in range(method_ids_size):
        base = method_ids_off + i * 8
        method_keys.append((u16(base), u32(base + 4), u16(base + 2)))
    if method_keys != sorted(method_keys) or len(set(method_keys)) != len(method_keys):
        raise RuntimeError('method_ids not strictly sorted')

    def code_bounds(code_off: int) -> set[int]:
        if code_off % 4:
            raise RuntimeError(f'code item {code_off} not 4-aligned')
        insns_size = u32(code_off + 12)
        tries_size = u16(code_off + 6)
        units = list(struct.unpack_from('<' + 'H' * insns_size, blob, code_off + 16))
        bounds = set()
        pc = 0
        while pc < insns_size:
            bounds.add(pc)
            op = units[pc] & 0xFF
            if op not in _OPCODE_UNITS:
                raise RuntimeError(f'unknown opcode {op:#x} at {code_off}+{pc}')
            pc += _OPCODE_UNITS[op]
        if pc != insns_size:
            raise RuntimeError(f'insns overshoot at {code_off}')
        if tries_size:
            tries_at = code_off + 16 + insns_size * 2
            if insns_size % 2:
                tries_at += 2
            handlers_at = tries_at + tries_size * 8
            list_size, ptr = _uleb_at(blob, handlers_at)
            handler_starts = set()
            for _ in range(list_size):
                handler_starts.add(ptr - handlers_at)
                size, ptr = _sleb_at(blob, ptr)
                typed = abs(size)
                for _ in range(typed):
                    _, ptr = _uleb_at(blob, ptr)
                    addr, ptr = _uleb_at(blob, ptr)
                    if addr not in bounds:
                        raise RuntimeError(f'catch addr {addr} not on instruction boundary')
                if size <= 0:
                    addr, ptr = _uleb_at(blob, ptr)
                    if addr not in bounds:
                        raise RuntimeError(f'catch-all {addr} not on instruction boundary')
            for t in range(tries_size):
                start, count, hoff = struct.unpack_from('<IHH', blob, tries_at + t * 8)
                end = start + count
                if start not in bounds or (end not in bounds and end != insns_size):
                    raise RuntimeError(f'try {start}+{count} not on instruction boundary')
                if hoff not in handler_starts:
                    raise RuntimeError(f'handler_off {hoff} is not a handler start {sorted(handler_starts)}')
        return bounds

    for i in range(class_defs_size):
        base = class_defs_off + i * 32
        data_off = u32(base + 24)
        if not data_off:
            continue
        # class_data: 4 ulebs then fields/methods. Walk to code_offs.
        p = data_off
        counts = []
        for _ in range(4):
            n, p = _uleb_at(blob, p)
            counts.append(n)
        for _ in range(counts[0] + counts[1]):
            _, p = _uleb_at(blob, p)
            _, p = _uleb_at(blob, p)
        for _ in range(counts[2] + counts[3]):
            _, p = _uleb_at(blob, p)
            _, p = _uleb_at(blob, p)
            code_off, p = _uleb_at(blob, p)
            if code_off:
                code_bounds(code_off)

    # map 必须按偏移升序，并且最后一项就是 map 自己。按类型号排序会把 0x1000
    # 插到数据段前面，ART 在进界面之前直接拒载整个 dex。
    map_off = u32(52)
    if map_off % 4:
        raise RuntimeError('map_off not 4-aligned')
    map_count = u32(map_off)
    if map_count == 0:
        raise RuntimeError('empty map')
    prev = -1
    for i in range(map_count):
        typ, _unused, size, off = struct.unpack_from('<HHII', blob, map_off + 4 + i * 12)
        if off < prev:
            raise RuntimeError(f'map item {typ:#x} at {off} is before previous offset {prev}')
        if size == 0:
            raise RuntimeError(f'map item {typ:#x} has size 0')
        prev = off
        if i == map_count - 1:
            if typ != 0x1000 or off != map_off or size != 1:
                raise RuntimeError(f'map list item is not last: type={typ:#x} off={off}')


def _s16(n: int) -> int:
    if not -32768 <= n <= 32767:
        raise ValueError(f'branch {n} does not fit in 16 bits')
    return n & 0xffff


class Asm:
    """寄存器 v0..v15。参数在高位寄存器。"""

    def __init__(self, nregs: int, ins: int):
        if not 1 <= nregs <= 16:
            raise ValueError('this assembler keeps registers in 0..15')
        self.nregs = nregs
        self.ins = ins
        self.ops: list[tuple] = []
        self.catch_all = None  # (start_label, end_label, handler_label)

    def raw(self, *units: int):
        self.ops.append(('raw', units))

    def label(self, name: str):
        self.ops.append(('label', name))

    def const(self, reg: int, value: int):
        if -8 <= value <= 7:
            self.raw(((value & 0xf) << 12) | ((reg & 0xf) << 8) | 0x12)
        elif -32768 <= value <= 32767:
            self.raw((reg << 8) | 0x13, _u16(value))
        else:
            self.raw((reg << 8) | 0x14, value & 0xffff, (value >> 16) & 0xffff)

    def const_wide(self, reg: int, value: int):
        if not -32768 <= value <= 32767:
            raise ValueError('const-wide only supports 16-bit here')
        self.raw((reg << 8) | 0x16, _u16(value))

    def const_string(self, reg: int, text: str):
        self.ops.append(('string', reg, text))

    def move(self, dest: int, src: int):
        self.raw(((src & 0xf) << 12) | ((dest & 0xf) << 8) | 0x01)

    def move_object(self, dest: int, src: int):
        self.raw(((src & 0xf) << 12) | ((dest & 0xf) << 8) | 0x07)

    def move_result(self, dest: int):
        self.raw((dest << 8) | 0x0a)

    def move_result_wide(self, dest: int):
        self.raw((dest << 8) | 0x0b)

    def move_result_object(self, dest: int):
        self.raw((dest << 8) | 0x0c)

    def move_exception(self, dest: int):
        self.raw((dest << 8) | 0x0d)

    def ret(self, reg: int | None = None, kind: str = 'void'):
        if kind == 'void':
            self.raw(0x0e)
        elif kind == 'object':
            self.raw((reg << 8) | 0x11)
        else:
            self.raw((reg << 8) | 0x0f)

    def goto(self, name: str):
        self.ops.append(('goto', name))

    def if_op(self, op: int, a: int, name: str, b: int | None = None):
        self.ops.append(('if', op, a, b, name))

    def if_eqz(self, reg: int, name: str):
        self.if_op(0x38, reg, name)

    def if_nez(self, reg: int, name: str):
        self.if_op(0x39, reg, name)

    def if_ltz(self, reg: int, name: str):
        self.if_op(0x3a, reg, name)

    def if_gez(self, reg: int, name: str):
        self.if_op(0x3b, reg, name)

    def if_gtz(self, reg: int, name: str):
        self.if_op(0x3c, reg, name)

    def if_lez(self, reg: int, name: str):
        self.if_op(0x3d, reg, name)

    def if_lt(self, a: int, b: int, name: str):
        self.if_op(0x34, a, name, b)

    def if_ge(self, a: int, b: int, name: str):
        self.if_op(0x35, a, name, b)

    def if_eq(self, a: int, b: int, name: str):
        self.if_op(0x32, a, name, b)

    def if_ne(self, a: int, b: int, name: str):
        self.if_op(0x33, a, name, b)

    def new(self, reg: int, typ: str):
        self.ops.append(('type21', 0x22, reg, typ))

    def check_cast(self, reg: int, typ: str):
        self.ops.append(('type21', 0x1f, reg, typ))

    def new_array(self, dest: int, size: int, typ: str):
        self.ops.append(('type22', 0x23, dest, size, typ))

    def array_length(self, dest: int, arr: int):
        self.raw(((arr & 0xf) << 12) | ((dest & 0xf) << 8) | 0x21)

    def iget(self, dest: int, obj: int, field: tuple, kind: str = 'int'):
        op = {'int': 0x52, 'wide': 0x53, 'object': 0x54, 'boolean': 0x55, 'byte': 0x56}[kind]
        self.ops.append(('field22', op, dest, obj, field))

    def iput(self, val: int, obj: int, field: tuple, kind: str = 'int'):
        op = {'int': 0x59, 'wide': 0x5a, 'object': 0x5b, 'boolean': 0x5c, 'byte': 0x5d}[kind]
        self.ops.append(('field22', op, val, obj, field))

    def sget(self, dest: int, field: tuple, kind: str = 'object'):
        op = {'int': 0x60, 'wide': 0x61, 'object': 0x62}[kind]
        self.ops.append(('field21', op, dest, field))

    def sput(self, val: int, field: tuple, kind: str = 'object'):
        op = {'int': 0x67, 'wide': 0x68, 'object': 0x69}[kind]
        self.ops.append(('field21', op, val, field))

    def invoke(self, kind: str, method: tuple, regs: list[int], result: int | None = None, result_kind: str = 'object'):
        op = {
            'virtual': 0x6e, 'super': 0x6f, 'direct': 0x70, 'static': 0x71, 'interface': 0x72,
        }[kind]
        self.ops.append(('invoke', op, method, tuple(regs)))
        if result is not None:
            if result_kind == 'wide':
                self.move_result_wide(result)
            elif result_kind == 'int':
                self.move_result(result)
            else:
                self.move_result_object(result)

    def binop(self, op: int, dest: int, a: int, b: int):
        self.raw((dest << 8) | op, ((b & 0xff) << 8) | (a & 0xff))

    def add(self, dest: int, a: int, b: int):
        self.binop(0x90, dest, a, b)

    def sub(self, dest: int, a: int, b: int):
        self.binop(0x91, dest, a, b)

    def mul(self, dest: int, a: int, b: int):
        self.binop(0x92, dest, a, b)

    def div(self, dest: int, a: int, b: int):
        self.binop(0x93, dest, a, b)

    def add_lit(self, dest: int, src: int, lit: int):
        if -128 <= lit <= 127 and dest < 256:
            self.raw((dest << 8) | 0xd8, ((_u16(lit) & 0xff) << 8) | (src & 0xff))
        else:
            self.raw(((src & 0xf) << 12) | ((dest & 0xf) << 8) | 0xd0, _u16(lit))

    def mul_lit8(self, dest: int, src: int, lit: int):
        self.raw((dest << 8) | 0xda, ((_u16(lit) & 0xff) << 8) | (src & 0xff))

    def div_lit(self, dest: int, src: int, lit: int):
        self.raw(((src & 0xf) << 12) | ((dest & 0xf) << 8) | 0xd3, _u16(lit))

    def and_lit8(self, dest: int, src: int, lit: int):
        self.raw((dest << 8) | 0xdd, ((lit & 0xff) << 8) | (src & 0xff))

    def or_lit8(self, dest: int, src: int, lit: int):
        self.raw((dest << 8) | 0xde, ((lit & 0xff) << 8) | (src & 0xff))

    def shr_lit8(self, dest: int, src: int, lit: int):
        self.raw((dest << 8) | 0xe1, ((lit & 0xff) << 8) | (src & 0xff))

    def ushr_lit8(self, dest: int, src: int, lit: int):
        self.raw((dest << 8) | 0xe2, ((lit & 0xff) << 8) | (src & 0xff))

    def int_to_byte(self, dest: int, src: int):
        self.raw(((src & 0xf) << 12) | ((dest & 0xf) << 8) | 0x8d)

    def cmp_long(self, dest: int, a: int, b: int):
        self.raw((dest << 8) | 0x31, ((b & 0xff) << 8) | (a & 0xff))

    def int_to_long(self, dest: int, src: int):
        self.raw(((src & 0xf) << 12) | ((dest & 0xf) << 8) | 0x81)

    def add_long(self, dest: int, a: int, b: int):
        self.raw((dest << 8) | 0x9b, ((b & 0xff) << 8) | (a & 0xff))

    def sub_long(self, dest: int, a: int, b: int):
        self.raw((dest << 8) | 0x9c, ((b & 0xff) << 8) | (a & 0xff))

    def aget(self, dest: int, arr: int, idx: int, kind: str = 'int'):
        op = {'int': 0x44, 'object': 0x46, 'byte': 0x48}[kind]
        self.raw((dest << 8) | op, ((idx & 0xff) << 8) | (arr & 0xff))

    def aput(self, val: int, arr: int, idx: int, kind: str = 'int'):
        op = {'int': 0x4b, 'object': 0x4d, 'byte': 0x4f}[kind]
        self.raw((val << 8) | op, ((idx & 0xff) << 8) | (arr & 0xff))

    def fill_new_array(self, typ: str, regs: list[int], dest: int):
        # filled-new-array then move-result-object. 4-bit regs, max 5.
        self.ops.append(('filled', typ, tuple(regs)))
        self.move_result_object(dest)

    def try_all(self, start: str, end: str, handler: str):
        self.catch_all = (start, end, handler)

    def encode(self, dex: 'DexBuilder') -> bytes:
        units: list[int] = []
        labels: dict[str, int] = {}
        fixups: list[tuple] = []
        for op in self.ops:
            kind = op[0]
            if kind == 'label':
                labels[op[1]] = len(units)
                continue
            if kind == 'raw':
                units.extend(op[1])
                continue
            if kind == 'string':
                _, reg, text = op
                units.extend(((reg << 8) | 0x1a, dex.string_index(text)))
                continue
            if kind == 'type21':
                _, opcode, reg, typ = op
                units.extend(((reg << 8) | opcode, dex.type_index(typ)))
                continue
            if kind == 'type22':
                _, opcode, a, b, typ = op
                units.extend((((b & 0xf) << 12) | ((a & 0xf) << 8) | opcode, dex.type_index(typ)))
                continue
            if kind == 'field22':
                _, opcode, a, b, field = op
                units.extend((((b & 0xf) << 12) | ((a & 0xf) << 8) | opcode, dex.field_index(field)))
                continue
            if kind == 'field21':
                _, opcode, reg, field = op
                units.extend(((reg << 8) | opcode, dex.field_index(field)))
                continue
            if kind == 'invoke':
                _, opcode, method, regs = op
                if any(r > 15 for r in regs):
                    raise ValueError(f'invoke regs {regs}')
                if len(regs) > 5:
                    if regs != tuple(range(regs[0], regs[0] + len(regs))):
                        raise ValueError(f'range invoke needs consecutive regs {regs}')
                    range_op = {0x6e: 0x74, 0x6f: 0x75, 0x70: 0x76, 0x71: 0x77, 0x72: 0x78}[opcode]
                    # 3rc: AA|op, method index, first register.
                    units.append(((len(regs) & 0xff) << 8) | range_op)
                    units.append(dex.method_index(method))
                    units.append(regs[0] & 0xffff)
                    continue
                padded = list(regs) + [0] * (5 - len(regs))
                units.append(((len(regs) & 0xf) << 12) | ((padded[4] & 0xf) << 8) | opcode)
                units.append(dex.method_index(method))
                units.append(
                    ((padded[3] & 0xf) << 12) | ((padded[2] & 0xf) << 8)
                    | ((padded[1] & 0xf) << 4) | (padded[0] & 0xf)
                )
                continue
            if kind == 'filled':
                _, typ, regs = op
                padded = list(regs) + [0] * (5 - len(regs))
                units.append(((len(regs) & 0xf) << 12) | ((padded[4] & 0xf) << 8) | 0x24)
                units.append(dex.type_index(typ))
                units.append(
                    ((padded[3] & 0xf) << 12) | ((padded[2] & 0xf) << 8)
                    | ((padded[1] & 0xf) << 4) | (padded[0] & 0xf)
                )
                continue
            if kind == 'goto':
                fixups.append(('goto', len(units), op[1]))
                units.extend((0x29, 0))
                continue
            if kind == 'if':
                _, opcode, a, b, name = op
                fixups.append(('if', len(units), name, opcode, a, b))
                units.extend((0, 0))
                continue
            raise ValueError(kind)
        for kind, at, *rest in fixups:
            if kind == 'goto':
                name = rest[0]
                off = labels[name] - at
                units[at] = 0x29
                units[at + 1] = _s16(off)
            else:
                name, opcode, a, b = rest
                off = labels[name] - at
                if b is None:
                    units[at] = (a << 8) | opcode
                    units[at + 1] = _s16(off)
                else:
                    units[at] = ((b & 0xf) << 12) | ((a & 0xf) << 8) | opcode
                    units[at + 1] = _s16(off)
        tries = b''
        handlers = b''
        if self.catch_all:
            start, end, handler = self.catch_all
            start_at = labels[start]
            end_at = labels[end]
            handler_at = labels[handler]
            # handler_off 从列表起点算，必须落在某个 encoded_catch_handler 上，不能指到前面的 size。
            # size 0 表示没有带类型的 catch，后面跟一个 catch-all 地址。
            list_size = uleb(1)
            handlers = list_size + sleb(0) + uleb(handler_at)
            tries = struct.pack('<IHH', start_at, end_at - start_at, len(list_size))
        if len(units) % 2 and tries:
            units.append(0)
        tries_size = 1 if self.catch_all else 0
        header = struct.pack('<HHHHII', self.nregs, self.ins, self.outs(), tries_size, 0, len(units))
        body = struct.pack('<' + 'H' * len(units), *units) if units else b''
        blob = header + body + tries + handlers
        if len(blob) % 4:
            blob += b'\x00' * (4 - len(blob) % 4)
        return blob

    def outs(self) -> int:
        out = 0
        for op in self.ops:
            if op[0] == 'invoke':
                out = max(out, len(op[3]))
            elif op[0] == 'filled':
                out = max(out, len(op[2]))
        return out


class Method:
    def __init__(self, cls: str, name: str, ret: str, args: tuple[str, ...], flags: int, asm: Asm, annotated: bool = False):
        self.cls = cls
        self.name = name
        self.ret = ret
        self.args = args
        self.flags = flags
        self.asm = asm
        self.annotated = annotated

    @property
    def ops(self):
        if self.asm is None:
            return ()
        return self.asm.ops

    @property
    def proto(self) -> tuple:
        return (self.ret, self.args)

    @property
    def key(self) -> tuple:
        return (self.cls, self.name, self.proto)


class DexBuilder:
    def __init__(self):
        self.classes: list[dict] = []
        self._methods: list[Method] = []
        self._fields: list[tuple] = []

    def add_class(self, name: str, super_name: str, flags: int = 1, interfaces: tuple[str, ...] = ()):
        rec = {
            'name': name, 'super': super_name, 'flags': flags,
            'interfaces': interfaces, 'methods': [], 'fields': [],
        }
        self.classes.append(rec)
        return rec

    def add_field(self, cls: str, name: str, typ: str, flags: int):
        self._fields.append((cls, name, typ, flags))
        for c in self.classes:
            if c['name'] == cls:
                c['fields'].append((name, typ, flags))

    def add_method(self, cls: str, name: str, ret: str, args: tuple[str, ...], flags: int, asm: Asm, annotated: bool = False):
        method = Method(cls, name, ret, args, flags, asm, annotated)
        self._methods.append(method)
        for c in self.classes:
            if c['name'] == cls:
                c['methods'].append(method)
        return method

    def _collect(self):
        strings = set()
        types = set()

        def add_type(desc: str):
            types.add(desc)
            strings.add(desc)

        def add_str(text: str):
            strings.add(text)

        for c in self.classes:
            add_type(c['name'])
            add_type(c['super'])
            for iface in c['interfaces']:
                add_type(iface)
        for cls, name, typ, _flags in self._fields:
            add_type(cls)
            add_type(typ)
            add_str(name)
        for m in self._methods:
            add_type(m.cls)
            add_str(m.name)
            add_type(m.ret)
            for arg in m.args:
                add_type(arg)
        # referenced by code
        for m in self._methods:
            for op in m.ops:
                if op[0] == 'string':
                    add_str(op[2])
                elif op[0] in ('type21', 'type22', 'filled'):
                    add_type(op[-1] if op[0] != 'filled' else op[1])
                elif op[0] in ('field22', 'field21'):
                    field = op[-1]
                    add_type(field[0])
                    add_str(field[1])
                    add_type(field[2])
                elif op[0] == 'invoke':
                    method = op[2]
                    add_type(method[0])
                    add_str(method[1])
                    add_type(method[2][0])
                    for arg in method[2][1]:
                        add_type(arg)
        add_type('Landroid/webkit/JavascriptInterface;')
        strings.add('JavascriptInterface')
        return strings, types

    def build(self) -> bytes:
        strings, types = self._collect()
        self.strings = sorted(strings)
        self.string_ids = {s: i for i, s in enumerate(self.strings)}
        self.types = sorted(types, key=lambda d: self.string_ids[d])
        self.type_ids = {t: i for i, t in enumerate(self.types)}

        protos = set()
        for m in self._methods:
            protos.add(m.proto)
        for m in self._methods:
            for op in m.ops:
                if op[0] == 'invoke':
                    protos.add(op[2][2])

        def proto_key(proto):
            ret, args = proto
            return (self.type_ids[ret], tuple(self.type_ids[a] for a in args))

        self.protos = sorted(protos, key=proto_key)
        self.proto_ids = {p: i for i, p in enumerate(self.protos)}

        fields = set()
        for cls, name, typ, _flags in self._fields:
            fields.add((cls, name, typ))
        for m in self._methods:
            for op in m.ops:
                if op[0] in ('field22', 'field21'):
                    fields.add(op[-1])
        self.fields = sorted(fields, key=lambda f: (self.type_ids[f[0]], self.string_ids[f[1]], self.type_ids[f[2]]))
        self.field_ids = {f: i for i, f in enumerate(self.fields)}

        methods = set()
        for m in self._methods:
            methods.add(m.key)
        for m in self._methods:
            for op in m.ops:
                if op[0] == 'invoke':
                    methods.add(op[2])
        self.methods = sorted(methods, key=lambda m: (
            self.type_ids[m[0]], self.string_ids[m[1]], self.proto_ids[m[2]],
        ))
        self.method_ids = {m: i for i, m in enumerate(self.methods)}

        # encode variable data first so offsets are known
        string_blobs = [mutf8(s) for s in self.strings]
        # type lists for protos with args, and class interfaces
        type_lists = []
        type_list_index = {}

        def intern_type_list(items: tuple[str, ...]) -> int:
            if items in type_list_index:
                return type_list_index[items]
            type_list_index[items] = len(type_lists)
            type_lists.append(items)
            return type_list_index[items]

        proto_list_pos = {}
        for proto in self.protos:
            args = proto[1]
            if args:
                proto_list_pos[proto] = intern_type_list(args)
        iface_list_pos = {}
        for c in self.classes:
            if c['interfaces']:
                iface_list_pos[c['name']] = intern_type_list(c['interfaces'])

        code_blobs = []
        code_of = {}
        for m in self._methods:
            if m.asm is None:
                continue
            blob = m.asm.encode(self)
            code_of[m.key] = len(code_blobs)
            code_blobs.append(blob)

        # annotations for JavascriptInterface
        annotated = [m for m in self._methods if m.annotated]
        ann_type = self.type_ids['Landroid/webkit/JavascriptInterface;']
        ann_item = bytes([0x01]) + uleb(ann_type) + uleb(0)

        # layout
        # We'll place data after the id section. Compute sizes.
        def align(n, a=4):
            return (n + a - 1) & ~(a - 1)

        header_size = 0x70
        string_ids_off = header_size
        string_ids_size = len(self.strings) * 4
        type_ids_off = string_ids_off + string_ids_size
        type_ids_size = len(self.types) * 4
        proto_ids_off = type_ids_off + type_ids_size
        proto_ids_size = len(self.protos) * 12
        field_ids_off = proto_ids_off + proto_ids_size
        field_ids_size = len(self.fields) * 8
        method_ids_off = field_ids_off + field_ids_size
        method_ids_size = len(self.methods) * 8
        class_defs_off = method_ids_off + method_ids_size
        class_defs_size = len(self.classes) * 32
        data_off = align(class_defs_off + class_defs_size)

        data = bytearray()
        data_base = data_off

        def add(blob: bytes, alignment=4) -> int:
            while (data_base + len(data)) % alignment:
                data.append(0)
            off = data_base + len(data)
            data.extend(blob)
            return off

        string_offs = [add(b, 1) for b in string_blobs]
        # type lists are 4-aligned
        type_list_offs = []
        for items in type_lists:
            blob = struct.pack('<I', len(items)) + struct.pack('<' + 'H' * len(items), *[self.type_ids[t] for t in items])
            if len(items) % 2 == 1:
                blob += b'\x00\x00'
            type_list_offs.append(add(blob))

        code_offs = [add(b) for b in code_blobs]
        ann_item_off = add(ann_item, 1) if annotated else 0
        ann_set_off = add(struct.pack('<II', 1, ann_item_off)) if annotated else 0

        # class_def 必须按 type_idx 升序。系统用二分查找，乱序会在进界面之前拒载整个 dex。
        self.classes.sort(key=lambda c: self.type_ids[c['name']])
        # class data first, annotation directories after, so each map group stays contiguous.
        class_data_offs = []
        ann_dir_blobs = []
        for c in self.classes:
            static_fields = []
            instance_fields = []
            for name, typ, flags in c['fields']:
                idx = self.field_ids[(c['name'], name, typ)]
                (static_fields if flags & 0x8 else instance_fields).append((idx, flags))
            direct = []
            virtual = []
            for m in c['methods']:
                idx = self.method_ids[m.key]
                code_off = code_offs[code_of[m.key]] if m.key in code_of else 0
                target = direct if (m.flags & 0x8) or m.name == '<init>' or (m.flags & 0x2) else virtual
                target.append((idx, m.flags, code_off))
            static_fields.sort()
            instance_fields.sort()
            direct.sort()
            virtual.sort()

            def enc_fields(items):
                out = b''
                prev = 0
                for idx, flags in items:
                    out += uleb(idx - prev) + uleb(flags)
                    prev = idx
                return out

            def enc_methods(items):
                out = b''
                prev = 0
                for idx, flags, code_off in items:
                    out += uleb(idx - prev) + uleb(flags) + uleb(code_off)
                    prev = idx
                return out

            blob = (
                uleb(len(static_fields)) + uleb(len(instance_fields))
                + uleb(len(direct)) + uleb(len(virtual))
                + enc_fields(static_fields) + enc_fields(instance_fields)
                + enc_methods(direct) + enc_methods(virtual)
            )
            class_data_offs.append(add(blob, 1))
            ann_methods = [m for m in c['methods'] if m.annotated]
            ann_methods.sort(key=lambda m: self.method_ids[m.key])
            if ann_methods:
                dir_blob = struct.pack('<IIII', 0, 0, len(ann_methods), 0)
                for m in ann_methods:
                    dir_blob += struct.pack('<II', self.method_ids[m.key], ann_set_off)
                ann_dir_blobs.append(dir_blob)
            else:
                ann_dir_blobs.append(None)
        ann_dir_offs = []
        for blob in ann_dir_blobs:
            ann_dir_offs.append(add(blob) if blob is not None else 0)

        map_items = []

        def map_item(typ, size, off):
            if size:
                map_items.append((typ, size, off))

        map_off_placeholder = data_base + len(data)
        # map is written after we know its offset; reserve after aligning
        while (data_base + len(data)) % 4:
            data.append(0)
        map_off = data_base + len(data)

        # ids are before data
        map_item(0x0000, 1, 0)
        map_item(0x0001, len(self.strings), string_ids_off)
        map_item(0x0002, len(self.types), type_ids_off)
        map_item(0x0003, len(self.protos), proto_ids_off)
        map_item(0x0004, len(self.fields), field_ids_off)
        map_item(0x0005, len(self.methods), method_ids_off)
        map_item(0x0006, len(self.classes), class_defs_off)
        if type_lists:
            map_item(0x1001, len(type_lists), type_list_offs[0])
        if code_blobs:
            map_item(0x2001, len(code_blobs), code_offs[0])
        if annotated:
            map_item(0x2004, 1, ann_item_off)
            map_item(0x1003, 1, ann_set_off)
        if any(ann_dir_offs):
            first = next(o for o in ann_dir_offs if o)
            map_item(0x2006, sum(1 for o in ann_dir_offs if o), first)
        map_item(0x2000, len(self.classes), class_data_offs[0])
        map_item(0x2002, len(self.strings), string_offs[0])
        map_items.append((0x1000, 1, map_off))
        # 按偏移排，不能按类型号排。0x1000 的偏移在文件末尾，按类型号会插到数据段前面。
        map_items.sort(key=lambda item: item[2])
        map_blob = struct.pack('<I', len(map_items))
        for typ, size, off in map_items:
            map_blob += struct.pack('<HHII', typ, 0, size, off)
        data.extend(map_blob)

        file_size = data_base + len(data)
        out = bytearray(file_size)
        out[data_base:data_base + len(data)] = data

        # string ids
        for i, off in enumerate(string_offs):
            struct.pack_into('<I', out, string_ids_off + i * 4, off)
        for i, desc in enumerate(self.types):
            struct.pack_into('<I', out, type_ids_off + i * 4, self.string_ids[desc])
        for i, proto in enumerate(self.protos):
            ret, args = proto
            shorty = self._shorty(proto)
            params_off = type_list_offs[proto_list_pos[proto]] if args else 0
            struct.pack_into('<III', out, proto_ids_off + i * 12, self.string_ids[shorty], self.type_ids[ret], params_off)
        for i, field in enumerate(self.fields):
            cls, name, typ = field
            struct.pack_into('<HHI', out, field_ids_off + i * 8, self.type_ids[cls], self.type_ids[typ], self.string_ids[name])
        for i, method in enumerate(self.methods):
            cls, name, proto = method
            struct.pack_into('<HHI', out, method_ids_off + i * 8, self.type_ids[cls], self.proto_ids[proto], self.string_ids[name])
        for i, c in enumerate(self.classes):
            iface_off = type_list_offs[iface_list_pos[c['name']]] if c['interfaces'] else 0
            struct.pack_into(
                '<IIIIIIII', out, class_defs_off + i * 32,
                self.type_ids[c['name']], c['flags'], self.type_ids[c['super']],
                iface_off, 0xffffffff, ann_dir_offs[i], class_data_offs[i], 0,
            )

        # header
        out[0:8] = b'dex\n035\x00'
        struct.pack_into('<I', out, 32, file_size)
        struct.pack_into('<I', out, 36, 0x70)
        struct.pack_into('<I', out, 40, 0x12345678)
        struct.pack_into('<II', out, 52, map_off, 0)  # map_off at 52? 
        # header layout:
        # 0 magic 8
        # 8 checksum 4
        # 12 signature 20
        # 32 file_size 4
        # 36 header_size 4
        # 40 endian 4
        # 44 link_size 4
        # 48 link_off 4
        # 52 map_off 4
        # 56 string_ids_size 4
        # 60 string_ids_off 4
        # 64 type_ids_size 4
        # 68 type_ids_off 4
        # 72 proto... wait header is only 0x70 = 112 bytes.
        # 52 map_off
        # 56 string_ids_size
        # 60 string_ids_off
        # 64 type_ids_size
        # 68 type_ids_off
        # 72 proto_ids_size
        # 76 proto_ids_off
        # 80 field_ids_size
        # 84 field_ids_off
        # 88 method_ids_size
        # 92 method_ids_off
        # 96 class_defs_size
        # 100 class_defs_off
        # 104 data_size
        # 108 data_off
        # That's 112. Good. I wrote map_off wrong above with a dummy. Fix:
        struct.pack_into('<I', out, 44, 0)
        struct.pack_into('<I', out, 48, 0)
        struct.pack_into('<I', out, 52, map_off)
        struct.pack_into('<II', out, 56, len(self.strings), string_ids_off)
        struct.pack_into('<II', out, 64, len(self.types), type_ids_off)
        struct.pack_into('<II', out, 72, len(self.protos), proto_ids_off)
        struct.pack_into('<II', out, 80, len(self.fields), field_ids_off)
        struct.pack_into('<II', out, 88, len(self.methods), method_ids_off)
        struct.pack_into('<II', out, 96, len(self.classes), class_defs_off)
        struct.pack_into('<II', out, 104, len(data), data_off)
        digest = hashlib.sha1(out[32:]).digest()
        out[12:32] = digest
        out[8:12] = struct.pack('<I', zlib.adler32(out[12:]) & 0xffffffff)
        blob = bytes(out)
        audit_dex(blob)
        return blob

    def _shorty(self, proto) -> str:
        ret, args = proto
        chars = [self._shorty_char(ret)]
        for arg in args:
            chars.append(self._shorty_char(arg))
        text = ''.join(chars)
        # shorty must be in the string pool. Add if missing — too late if build already sorted.
        if text not in self.string_ids:
            raise ValueError(f'shorty {text} missing from string pool')
        return text

    @staticmethod
    def _shorty_char(desc: str) -> str:
        if desc.startswith('['):
            # array shorty is the element? No, shorty of an array is '['? 
            # Actually shorty of a reference (including arrays) is 'L'.
            return 'L'
        if desc.startswith('L'):
            return 'L'
        return desc

    def string_index(self, text: str) -> int:
        return self.string_ids[text]

    def type_index(self, desc: str) -> int:
        return self.type_ids[desc]

    def field_index(self, field: tuple) -> int:
        return self.field_ids[field]

    def method_index(self, method: tuple) -> int:
        return self.method_ids[method]


# shorty strings must be interned. DexBuilder._collect doesn't add shorty strings.
# Patch build() by wrapping _collect. Done below via a subclass hook in build — fix _collect.
def _patch():
    orig = DexBuilder._collect

    def _collect(self):
        strings, types = orig(self)
        protos = set()
        for m in self._methods:
            protos.add(m.proto)
            for op in m.ops:
                if op[0] == 'invoke':
                    protos.add(op[2][2])
        for proto in protos:
            shorty = ''.join(DexBuilder._shorty_char(t) for t in (proto[0], *proto[1]))
            strings.add(shorty)
        return strings, types

    DexBuilder._collect = _collect


_patch()
