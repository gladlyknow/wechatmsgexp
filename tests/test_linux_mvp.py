from __future__ import annotations

import os
import stat
import struct
import tempfile
import unittest
from unittest import mock

from wx4 import key, paths
from wx4.cli import build_parser
from wx4.common import PAGE_SZ, RESERVE_SZ
from wx4.decrypt import merge_wal
from wx4.export import _avatar_html
from Cryptodome.Cipher import AES


class LinuxPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, "xwechat_files")
        self.account = os.path.join(self.root, "wxid_example_abcd")
        os.makedirs(os.path.join(self.account, "db_storage", "message"))
        os.makedirs(os.path.join(self.root, "all_users", "login", "wxid_example"))
        with open(os.path.join(self.root, "all_users", "login", "wxid_example",
                               "key_info.db"), "wb") as f:
            f.write(b"test")

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_root_accepts_parent_and_xwechat_directory(self):
        self.assertEqual(paths.get_xwechat_root(self.temp.name), self.root)
        self.assertEqual(paths.get_xwechat_root(self.root), self.root)

    def test_environment_override_and_account_discovery(self):
        with mock.patch.dict(os.environ, {"WECHAT_DATA_ROOT": self.root}):
            self.assertEqual(paths.get_xwechat_root(), self.root)
            accounts = paths.list_account_dirs()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["wxid"], "wxid_example")
            self.assertEqual(paths.list_logged_in_wxids(), ["wxid_example"])

    def test_find_account_by_full_directory_name(self):
        account = paths.find_account("wxid_example_abcd", self.root)
        self.assertIsNotNone(account)
        self.assertEqual(account["path"], self.account)


class LinuxKeyTests(unittest.TestCase):
    def test_wcdb_literal_candidate_extraction(self):
        key_hex = bytes(range(32)).hex()
        salt_hex = bytes(range(32, 48)).hex()
        data = b"prefix x'" + (key_hex + salt_hex).encode("ascii") + b"' suffix"
        self.assertIn((key_hex, salt_hex), key._search_linux_literals(data))

    def test_invalid_literal_is_ignored(self):
        self.assertEqual(key._search_linux_literals(b"x'not-a-key'"), [])

    def test_key_file_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "keys.json")
            extractor = key.KeyExtractor(temp)
            extractor.save(path)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)


class CliTests(unittest.TestCase):
    def test_decrypt_accepts_output_directory(self):
        args = build_parser().parse_args(["decrypt", "--out-dir", "/tmp/wx4-out"])
        self.assertEqual(args.out_dir, "/tmp/wx4-out")

    def test_offline_avatar_rejects_remote_url(self):
        class FakeData:
            @staticmethod
            def avatar_of(_username):
                return "https://wx.qlogo.cn/private-avatar"

        rendered = _avatar_html(FakeData(), "wxid_test", "测试")
        self.assertNotIn("qlogo", rendered)
        self.assertNotIn("<img", rendered)


class WalTests(unittest.TestCase):
    @staticmethod
    def _encrypted_page(key_bytes: bytes, fill: bytes) -> bytes:
        iv = b"I" * 16
        body = fill * (PAGE_SZ - RESERVE_SZ)
        encrypted = AES.new(key_bytes, AES.MODE_CBC, iv).encrypt(body)
        return encrypted + iv + b"\0" * 64

    def test_merge_uses_current_generation_and_last_commit_only(self):
        key_bytes = bytes(range(32))
        current_salt = (0x11223344, 0x55667788)
        stale_salt = (0x11223343, 0x01020304)

        def frame(page_no, commit, salt, fill):
            header = struct.pack(">IIIIII", page_no, commit, salt[0], salt[1], 0, 0)
            return header + self._encrypted_page(key_bytes, fill)

        wal_header = struct.pack(">IIIIIIII", 0x377F0682, 3007000, PAGE_SZ, 1,
                                 current_salt[0], current_salt[1], 0, 0)
        wal = (wal_header + frame(2, 2, stale_salt, b"S") +
               frame(2, 2, current_salt, b"C") +
               frame(3, 0, current_salt, b"U"))

        with tempfile.TemporaryDirectory() as temp:
            db_path = os.path.join(temp, "test.db")
            wal_path = db_path + "-wal"
            with open(db_path, "wb") as f:
                f.write(b"Z" * (PAGE_SZ * 3))
            with open(wal_path, "wb") as f:
                f.write(wal)

            self.assertEqual(merge_wal(db_path, wal_path, key_bytes), 1)
            self.assertEqual(os.path.getsize(db_path), PAGE_SZ * 2)
            with open(db_path, "rb") as f:
                f.seek(PAGE_SZ)
                self.assertEqual(f.read(PAGE_SZ - RESERVE_SZ),
                                 b"C" * (PAGE_SZ - RESERVE_SZ))


if __name__ == "__main__":
    unittest.main()
