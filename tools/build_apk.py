"""把口袋版打成单个可安装的 APK。不依赖 aapt 或 JDK。

界面是 assets/ui.html，逻辑在手写的 classes.dex 里。
签名同时带 v1 和 v2，密钥固定在 tools/pocket-signing.pem，重复安装不用先卸载。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_dex import build_dex  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'android' / 'phisap-pocket.apk'
UI = ROOT / 'android' / 'app' / 'src' / 'main' / 'assets'
POCKET = ROOT / 'android' / 'pocket'
JAR_URL = (
    'https://api.github.com/repos/Sable/android-platforms/contents/'
    'android-28/android.jar?ref=master'
)
KEY_PATH = Path(__file__).resolve().parent / 'pocket-signing.pem'
CERT_PATH = Path(__file__).resolve().parent / 'pocket-signing.crt'

PKG = 'app.phisap.pocket'
V2_ID = 0x7109871a
V2_ALG = 0x0103  # RSA PKCS1 v1.5 + SHA-256
CHUNK = 1024 * 1024
ATTR = {
    'label': 0x01010001,
    'name': 0x01010003,
    'exported': 0x01010010,
    'minSdkVersion': 0x0101020c,
    'versionCode': 0x0101021b,
    'versionName': 0x0101021c,
    'targetSdkVersion': 0x01010270,
    'allowBackup': 0x01010280,
    'hardwareAccelerated': 0x010102d3,
    'compileSdkVersion': 0x01010572,
    'compileSdkVersionCodename': 0x01010573,
}


def _u16(n: int) -> bytes:
    return struct.pack('<H', n)


def _u32(n: int) -> bytes:
    return struct.pack('<I', n & 0xffffffff)


def _utf16_pool(strings: list[str]) -> bytes:
    encoded = []
    for text in strings:
        raw = text.encode('utf-16-le')
        encoded.append(_u16(len(text)) + raw + b'\x00\x00')
    offsets = []
    blob = bytearray()
    for item in encoded:
        offsets.append(len(blob))
        blob.extend(item)
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    body = b''.join(_u32(n) for n in offsets) + bytes(blob)
    size = header_size + len(body)
    pad = (4 - size % 4) % 4
    header = struct.pack('<HHIIIIII', 0x0001, header_size, size + pad, len(strings), 0, 0, strings_start, 0)
    return header + body + b'\x00' * pad


def _res_value(data_type: int, data: int, raw: int) -> bytes:
    return struct.pack('<IIIHBBI', 0xffffffff, 0, raw & 0xffffffff, 8, 0, data_type, data & 0xffffffff)


def _attr(ns: int, name: int, raw: int, data_type: int, data: int) -> bytes:
    return struct.pack('<IIIHBBI', ns & 0xffffffff, name, raw & 0xffffffff, 8, 0, data_type, data & 0xffffffff)


def _start(name: int, attrs: list[bytes], line: int = 1) -> bytes:
    ext = struct.pack('<IIHHHHHH', 0xffffffff, name, 36, 20, len(attrs), 0, 0, 0)
    body = ext + b''.join(attrs)
    size = 16 + len(body)
    return struct.pack('<HHII', 0x0102, 16, size, line) + struct.pack('<i', -1) + body


def _end(name: int) -> bytes:
    return struct.pack('<HHII', 0x0103, 16, 24, 1) + struct.pack('<iI', -1, 0xffffffff) + _u32(name)


def _ns(kind: int, prefix: int, uri: int) -> bytes:
    return struct.pack('<HHII', kind, 16, 24, 1) + struct.pack('<i', -1) + _u32(prefix) + _u32(uri)


def _empty_pool() -> bytes:
    """没有字符串的资源串池。size 和 headerSize 都必须 4 字节对齐，否则系统直接拒掉。"""
    # stringsStart 必须落在块内。size 等于 stringsStart 时，有的系统会当成坏表。
    return struct.pack('<HHIIIIII', 0x0001, 28, 32, 0, 0, 0, 28, 0) + b'\x00\x00\x00\x00'


def build_arsc() -> bytes:
    """最小资源表。Android 8 起打不开 resources.arsc 就装不上，哪怕清单不引用资源。"""
    name = PKG.encode('utf-16-le')
    if len(name) > 254:
        raise RuntimeError('package name too long for resources.arsc')
    name = name + b'\x00' * (256 - len(name))
    type_pool = _empty_pool()
    key_pool = _empty_pool()
    type_strings = 288
    key_strings = type_strings + len(type_pool)
    pkg_size = key_strings + len(key_pool)
    package = struct.pack('<HHI', 0x0200, 288, pkg_size) + struct.pack('<I', 0x7f) + name
    package += struct.pack('<IIIII', type_strings, 0, key_strings, 0, 0)
    if len(package) != 288:
        raise RuntimeError(f'package header {len(package)}')
    package += type_pool + key_pool
    body = _empty_pool() + package
    total = 12 + len(body)
    if total % 4 or pkg_size % 4:
        raise RuntimeError('resources.arsc is not 4-byte aligned')
    return struct.pack('<HHI', 0x0002, 12, total) + struct.pack('<I', 1) + body


def build_manifest() -> bytes:
    attrs = list(ATTR)
    strings = attrs + [
        'package', 'android', 'http://schemas.android.com/apk/res/android',
        'manifest', 'uses-sdk', 'application', 'activity', 'intent-filter', 'action', 'category',
        PKG, 'app.phisap.pocket.MainActivity', 'phisap',
        'android.intent.action.MAIN', 'android.intent.category.LAUNCHER',
        '2', '1.0', '34', '14', '26', '28', 'true', 'false',
    ]
    idx = {s: i for i, s in enumerate(strings)}
    uri = idx['http://schemas.android.com/apk/res/android']
    pool = _utf16_pool(strings)
    resmap = struct.pack('<HHI', 0x0180, 8, 8 + 4 * len(attrs)) + b''.join(_u32(ATTR[name]) for name in attrs)

    def A(name: str, raw: str, kind: str, value: int = 0) -> bytes:
        types = {'string': 0x03, 'int': 0x10, 'bool': 0x12}
        data = idx[raw] if kind == 'string' else value
        return _attr(uri, idx[name], idx[raw], types[kind], data)

    def plain(name: str, raw: str, kind: str, value: int = 0) -> bytes:
        types = {'string': 0x03, 'int': 0x10}
        data = idx[raw] if kind == 'string' else value
        return _attr(0xffffffff, idx[name], idx[raw], types[kind], data)

    chunks = [
        _ns(0x0100, idx['android'], idx['http://schemas.android.com/apk/res/android']),
        _start(idx['manifest'], [
            A('versionCode', '2', 'int', 2),
            A('versionName', '1.0', 'string'),
            A('compileSdkVersion', '34', 'int', 34),
            A('compileSdkVersionCodename', '14', 'string'),
            plain('package', PKG, 'string'),
        ]),
        _start(idx['uses-sdk'], [
            A('minSdkVersion', '26', 'int', 26),
            A('targetSdkVersion', '28', 'int', 28),
        ]),
        _end(idx['uses-sdk']),
        _start(idx['application'], [
            A('label', 'phisap', 'string'),
            A('allowBackup', 'false', 'bool', 0),
            A('hardwareAccelerated', 'true', 'bool', 0xffffffff),
        ]),
        _start(idx['activity'], [
            A('name', 'app.phisap.pocket.MainActivity', 'string'),
            A('exported', 'true', 'bool', 0xffffffff),
            A('label', 'phisap', 'string'),
        ]),
        _start(idx['intent-filter'], []),
        _start(idx['action'], [A('name', 'android.intent.action.MAIN', 'string')]),
        _end(idx['action']),
        _start(idx['category'], [A('name', 'android.intent.category.LAUNCHER', 'string')]),
        _end(idx['category']),
        _end(idx['intent-filter']),
        _end(idx['activity']),
        _end(idx['application']),
        _end(idx['manifest']),
        _ns(0x0101, idx['android'], idx['http://schemas.android.com/apk/res/android']),
    ]
    body = pool + resmap + b''.join(chunks)
    return struct.pack('<HHI', 0x0003, 8, 8 + len(body)) + body


def _dos() -> tuple[int, int]:
    date = ((2026 - 1980) << 9) | (10 << 5) | 3
    return 0, date


def _align_extra(base: int, name_len: int) -> bytes:
    start = base + 30 + name_len
    pad = (4 - start % 4) % 4
    if pad == 0:
        return b''
    extra_len = pad + 4
    return struct.pack('<HH', 0, extra_len - 4) + b'\x00' * (extra_len - 4)


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    time, date = _dos()
    local = bytearray()
    central = bytearray()
    for name, data in entries:
        raw_name = name.encode('utf-8')
        extra = _align_extra(len(local), len(raw_name))
        offset = len(local)
        crc = zlib.crc32(data) & 0xffffffff
        local.extend(struct.pack(
            '<IHHHHHIIIHH', 0x04034b50, 20, 0, 0, time, date, crc, len(data), len(data), len(raw_name), len(extra),
        ))
        local.extend(raw_name)
        local.extend(extra)
        if len(local) % 4:
            raise RuntimeError(f'{name} data not aligned')
        local.extend(data)
        central.extend(struct.pack(
            '<IHHHHHHIIIHHHHHII',
            0x02014b50, 20, 20, 0, 0, time, date, crc, len(data), len(data),
            len(raw_name), 0, 0, 0, 0, 0, offset,
        ))
        central.extend(raw_name)
    cd_off = len(local)
    eocd = struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(entries), len(entries), len(central), cd_off, 0)
    return bytes(local) + bytes(central) + eocd


def _chunk_digest(sections: list[bytes]) -> bytes:
    parts = []
    for section in sections:
        for off in range(0, len(section), CHUNK):
            chunk = section[off:off + CHUNK]
            parts.append(hashlib.sha256(b'\xa5' + _u32(len(chunk)) + chunk).digest())
    return hashlib.sha256(b'\x5a' + _u32(len(parts)) + b''.join(parts)).digest()


def _lp(blob: bytes) -> bytes:
    return _u32(len(blob)) + blob


def _cert_start(cert) -> datetime:
    start = getattr(cert, 'not_valid_before_utc', None)
    if start is None:
        start = cert.not_valid_before
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    return start


def _load_key():
    if KEY_PATH.exists():
        key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        KEY_PATH.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    if CERT_PATH.exists():
        cert = x509.load_pem_x509_certificate(CERT_PATH.read_bytes())
        # 手机时间早于证书生效日时，系统会把整包判成无效。生效日放到 2016。
        if _cert_start(cert) <= datetime(2020, 1, 1, tzinfo=timezone.utc):
            return key, cert
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'phisap')])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2016, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2046, 1, 1, tzinfo=timezone.utc))
        .sign(key, hashes.SHA256())
    )
    KEY_PATH.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def _v1_files(files: list[tuple[str, bytes]], key, cert) -> list[tuple[str, bytes]]:
    sections = []
    for name, data in files:
        digest = hashlib.sha256(data).digest()
        import base64
        sections.append(f'Name: {name}\r\nSHA-256-Digest: {base64.b64encode(digest).decode()}\r\n\r\n')
    manifest = 'Manifest-Version: 1.0\r\nCreated-By: phisap\r\n\r\n' + ''.join(sections)
    mf = manifest.encode('utf-8')
    sf_main = (
        'Signature-Version: 1.0\r\nCreated-By: phisap\r\n'
        f'SHA-256-Digest-Manifest: {base64.b64encode(hashlib.sha256(mf).digest()).decode()}\r\nX-Android-APK-Signed: 2\r\n\r\n'
    )
    sf_sections = []
    for section in sections:
        raw = section.encode('utf-8')
        sf_sections.append(
            section.split('\r\n', 1)[0] + '\r\n'
            f'SHA-256-Digest: {base64.b64encode(hashlib.sha256(raw).digest()).decode()}\r\n\r\n'
        )
    sf = (sf_main + ''.join(sf_sections)).encode('utf-8')
    rsa = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(sf)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature])
    )
    return files + [
        ('META-INF/MANIFEST.MF', mf),
        ('META-INF/CERT.SF', sf),
        ('META-INF/CERT.RSA', rsa),
    ]


def _eocd_for_digest(eocd: bytes, signing_block_off: int) -> bytes:
    """摘要里的中央目录偏移必须写成签名块起点，不能写成签名块之后的真实偏移。"""
    out = bytearray(eocd)
    struct.pack_into('<I', out, 16, signing_block_off)
    return bytes(out)


def _v2(apk: bytes, key, cert) -> bytes:
    eocd_off = apk.rfind(b'PK\x05\x06')
    cd_off = struct.unpack_from('<I', apk, eocd_off + 16)[0]
    before = apk[:cd_off]
    central = apk[cd_off:eocd_off]
    eocd = bytearray(apk[eocd_off:])
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    pubkey = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    # Signature length is fixed, so the block size is known before hashing.
    sig_len = key.key_size // 8
    digest_record = _u32(4 + 4 + 32) + _u32(V2_ALG) + _u32(32) + b'\x00' * 32
    digests = _lp(digest_record)
    certs = _lp(_lp(cert_der))
    signed_len = len(digests) + len(certs) + 4
    sig_record = _u32(4 + 4 + sig_len) + _u32(V2_ALG) + _u32(sig_len) + b'\x00' * sig_len
    signatures = _lp(sig_record)
    signer_body_len = 4 + signed_len + len(signatures) + 4 + len(pubkey)
    signers_len = 4 + signer_body_len
    value_len = 4 + signers_len
    block_size = 8 + 8 + 4 + value_len + 8 + 16
    struct.pack_into('<I', eocd, 16, cd_off + block_size)
    digest = _chunk_digest([before, central, _eocd_for_digest(eocd, cd_off)])
    digest_record = _u32(4 + 4 + 32) + _u32(V2_ALG) + _lp(digest)
    digests = _lp(digest_record)
    signed = digests + certs + _u32(0)
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    if len(signature) != sig_len:
        raise RuntimeError('unexpected signature length')
    sig_record = _u32(4 + 4 + sig_len) + _u32(V2_ALG) + _lp(signature)
    signatures = _lp(sig_record)
    signer = _lp(signed) + signatures + _lp(pubkey)
    signers = _lp(_lp(signer))
    pair_len = 4 + len(signers)
    size_field = 8 + pair_len + 8 + 16
    block = (
        struct.pack('<Q', size_field)
        + struct.pack('<Q', pair_len)
        + _u32(V2_ID)
        + signers
        + struct.pack('<Q', size_field)
        + b'APK Sig Block 42'
    )
    if len(block) != block_size:
        raise RuntimeError(f'signing block {len(block)} != planned {block_size}')
    return before + block + central + bytes(eocd)


def _verify_v2(apk: bytes, cert) -> None:
    eocd_off = apk.rfind(b'PK\x05\x06')
    cd_off = struct.unpack_from('<I', apk, eocd_off + 16)[0]
    if apk[cd_off - 16:cd_off] != b'APK Sig Block 42':
        raise RuntimeError('missing v2 magic')
    size2 = struct.unpack_from('<Q', apk, cd_off - 24)[0]
    block_off = cd_off - (size2 + 8)
    before = apk[:block_off]
    central = apk[cd_off:eocd_off]
    eocd = apk[eocd_off:]
    if struct.unpack_from('<I', eocd, 16)[0] != cd_off:
        raise RuntimeError('eocd does not point at the central directory')
    digest = _chunk_digest([before, central, _eocd_for_digest(eocd, block_off)])
    # Walk the v2 value and check the stored digest plus the RSA signature.
    pair_len = struct.unpack_from('<Q', apk, block_off + 8)[0]
    ident = struct.unpack_from('<I', apk, block_off + 16)[0]
    if ident != V2_ID:
        raise RuntimeError(f'v2 id {ident:#x}')
    value = apk[block_off + 20:block_off + 8 + 8 + pair_len]
    signers_len = struct.unpack_from('<I', value, 0)[0]
    signer = value[4:4 + signers_len]
    signer_len = struct.unpack_from('<I', signer, 0)[0]
    body = signer[4:4 + signer_len]
    signed_len = struct.unpack_from('<I', body, 0)[0]
    signed = body[4:4 + signed_len]
    rest = body[4 + signed_len:]
    sigs_len = struct.unpack_from('<I', rest, 0)[0]
    sigs = rest[4:4 + sigs_len]
    rec_len = struct.unpack_from('<I', sigs, 0)[0]
    alg, sig_len = struct.unpack_from('<II', sigs, 4)
    signature = sigs[12:12 + sig_len]
    if alg != V2_ALG or rec_len != 4 + 4 + sig_len:
        raise RuntimeError('unexpected v2 signature record')
    stored = signed[4 + 4 + 4:4 + 4 + 4 + 32]
    # digests = u32 len + record. record = u32 reclen + u32 alg + u32 dlen + digest
    dlen = struct.unpack_from('<I', signed, 0)[0]
    record = signed[4:4 + dlen]
    got = record[12:12 + struct.unpack_from('<I', record, 8)[0]]
    if got != digest:
        raise RuntimeError('v2 content digest mismatch')
    cert.public_key().verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())


def _find_aapt2() -> Path:
    env = os.environ.get('AAPT2')
    candidates = [Path(env)] if env else []
    candidates += [
        Path(__file__).resolve().parent / 'prebuilt' / 'aapt2',
        Path('/tmp/sdk/aapt2'),
    ]
    for path in candidates:
        if path.is_file():
            path.chmod(0o755)
            return path
    raise RuntimeError('找不到 aapt2')


def _find_android_jar() -> Path:
    env = os.environ.get('ANDROID_JAR')
    candidates = [Path(env)] if env else []
    candidates += [Path('/tmp/sdk/android.jar'), Path(__file__).resolve().parent / 'android.jar']
    for path in candidates:
        if path.is_file() and path.stat().st_size > 1_000_000:
            return path
    dest = Path('/tmp/sdk/android.jar')
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        JAR_URL, headers={'Accept': 'application/vnd.github.raw', 'User-Agent': 'phisap'},
    )
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open('wb') as out:
        shutil.copyfileobj(resp, out)
    return dest


def _aapt2_files() -> list[tuple[str, bytes]]:
    """用 aapt2 生成清单和资源表。手写的二进制清单系统会当成无效安装包。"""
    aapt2 = _find_aapt2()
    jar = _find_android_jar()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        compiled = tmp_path / 'resources.zip'
        base = tmp_path / 'base.apk'
        subprocess.check_call([str(aapt2), 'compile', '--dir', str(POCKET / 'res'), '-o', str(compiled)])
        subprocess.check_call([
            str(aapt2), 'link', '-o', str(base), '-I', str(jar),
            '--manifest', str(POCKET / 'AndroidManifest.xml'),
            str(compiled), '-A', str(UI),
            '--min-sdk-version', '26', '--target-sdk-version', '28',
            '--rename-manifest-package', PKG,
        ])
        with zipfile.ZipFile(base) as blob:
            return [(info.filename, blob.read(info.filename)) for info in blob.infolist()]


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def _zip_compat(entries: list[tuple[str, bytes]]) -> bytes:
    """按 aapt2 的方式打包。resources.arsc 不压缩、4 字节对齐，对齐用裸填充，不用 id 为 0 的 extra 头。"""
    local = bytearray()
    central = bytearray()
    for name, data in entries:
        raw_name = name.encode('utf-8')
        store = name == 'resources.arsc' or name.startswith('META-INF/') or name.endswith('.png')
        payload = data if store else _raw_deflate(data)
        method = 0 if store else 8
        extra = b''
        if name == 'resources.arsc':
            start = len(local) + 30 + len(raw_name)
            extra = b'\x00' * ((4 - start % 4) % 4)
        offset = len(local)
        crc = zlib.crc32(data) & 0xffffffff
        local.extend(struct.pack(
            '<IHHHHHIIIHH', 0x04034b50, 0, 0, method, 0, 0, crc, len(payload), len(data),
            len(raw_name), len(extra),
        ))
        local.extend(raw_name)
        local.extend(extra)
        if name == 'resources.arsc' and len(local) % 4:
            raise RuntimeError('resources.arsc is not 4-byte aligned')
        local.extend(payload)
        central.extend(struct.pack(
            '<IHHHHHHIIIHHHHHII',
            0x02014b50, 0, 0, 0, method, 0, 0, crc, len(payload), len(data),
            len(raw_name), 0, 0, 0, 0, 0, offset,
        ))
        central.extend(raw_name)
    cd_off = len(local)
    eocd = struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(entries), len(entries), len(central), cd_off, 0)
    return bytes(local) + bytes(central) + eocd


def build() -> Path:
    key, cert = _load_key()
    files = _aapt2_files()
    files.append(('classes.dex', build_dex()))
    signed = _v1_files(files, key, cert)
    apk = _v2(_zip_compat(signed), key, cert)
    _verify_v2(apk, cert)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(apk)
    badging = subprocess.check_output([str(_find_aapt2()), 'dump', 'badging', str(OUT)], text=True)
    if "package: name='app.phisap.pocket'" not in badging or 'app.phisap.pocket.MainActivity' not in badging:
        raise RuntimeError('aapt2 did not recognize the package')
    return OUT


if __name__ == '__main__':
    path = build()
    print(path, path.stat().st_size)
