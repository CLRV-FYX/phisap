"""scrcpy-server 16触点补丁的测试(需要仓库中的官方 scrcpy-server-v4.1)"""
import io
import os
import shutil
import tempfile
import unittest
import zipfile

import server_patch

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scrcpy-server-v4.1')


@unittest.skipUnless(os.path.isfile(SERVER), '缺少 scrcpy-server-v4.1')
class TestServerPatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.server = os.path.join(self.tmp, 'scrcpy-server-v4.1')
        shutil.copyfile(SERVER, self.server)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_patch(self):
        logs = []
        path, n = server_patch.prepare_server(self.server, logs.append)
        self.assertEqual(n, 16)
        self.assertEqual(logs, [])
        self.assertTrue(path.endswith('-16pt'))
        orig = zipfile.ZipFile(self.server)
        patched = zipfile.ZipFile(path)
        self.assertIsNone(patched.testzip())
        self.assertEqual([i.filename for i in orig.infolist()], [i.filename for i in patched.infolist()])
        for a, b in zip(orig.infolist(), patched.infolist()):
            self.assertEqual(a.compress_type, b.compress_type)
            if a.filename != 'classes.dex':
                self.assertEqual(orig.read(a), patched.read(b))
        d0, d1 = orig.read('classes.dex'), patched.read('classes.dex')
        self.assertTrue(server_patch._dex_checksums_ok(d1))
        diffs = [i for i in range(32, len(d0)) if d0[i] != d1[i]]
        self.assertEqual(diffs, [off + 2 for off, _, _ in sorted(server_patch._PATCH_SITES)])
        for off, _, _ in server_patch._PATCH_SITES:
            self.assertEqual(d1[off + 2:off + 4], b'\x10\x00')
        # 再次调用: 内容不变时不重写
        mtime = os.path.getmtime(path)
        self.assertEqual(server_patch.prepare_server(self.server, logs.append), (path, 16))
        self.assertEqual(os.path.getmtime(path), mtime)

    def test_fallback_on_unknown_server(self):
        with open(self.server, 'r+b') as f:
            f.seek(100)
            f.write(b'\x00\x01\x02')
        logs = []
        path, n = server_patch.prepare_server(self.server, logs.append)
        self.assertEqual((path, n), (self.server, 10))
        self.assertTrue(logs and '10' in logs[0])

    def test_fallback_on_modified_dex(self):
        z = zipfile.ZipFile(SERVER)
        dex = bytearray(z.read('classes.dex'))
        off = server_patch._PATCH_SITES[0][0]
        dex[off + 2] = 0x0b
        server_patch._fix_dex_checksums(dex)
        with self.assertRaises(server_patch.PatchError):
            server_patch.patch_dex(bytes(dex))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as out:
            out.writestr('classes.dex', bytes(dex))
        with self.assertRaises(server_patch.PatchError):
            server_patch.patch_jar(buf.getvalue())  # 大小不符


if __name__ == '__main__':
    unittest.main()
