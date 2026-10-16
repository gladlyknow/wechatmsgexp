from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from wx4.archive import open_archive


class FakeData:
    def __init__(self, root, wxid="wxid_a", text="hello archive"):
        self.root = root
        self.wxid = wxid
        self._text = text
        self.contacts = {
            wxid: {"username": wxid, "alias": "", "remark": "",
                   "nick_name": "Me", "description": "", "local_type": 1},
            "wxid_friend": {"username": "wxid_friend", "alias": "", "remark": "Friend",
                            "nick_name": "Friend", "description": "", "local_type": 1},
        }
        self.room_members = {}

    def account_dir(self):
        return os.path.dirname(self.root)

    def name_of(self, username):
        return "Me" if username == self.wxid else username

    def sessions(self):
        return [{"username": "wxid_friend", "summary": self._text,
                 "last_time": 100, "sort_time": 100, "is_group": False}]

    @staticmethod
    def has_messages(_username):
        return True

    @staticmethod
    def session_display(_username):
        return "Friend"

    @staticmethod
    def message_db_rel(_shard):
        return "message/message_0.db"

    def messages(self, _username):
        return [{
            "session": "wxid_friend", "server_id": 9001, "local_id": 1,
            "raw_type": 1, "type": 1, "type_name": "文本", "kind": "text",
            "create_time": 100, "sender_wxid": "wxid_friend", "sender": "Friend",
            "is_sender": False, "text": self._text, "content": self._text,
            "extra": {}, "shard": 0,
        }]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.temp.name, "snapshot", "message")
        os.makedirs(self.source)
        self.db_source = os.path.join(self.source, "message_0.db")
        with open(self.db_source, "wb") as stream:
            stream.write(b"snapshot-v1")
        self.archive_path = os.path.join(self.temp.name, "archive", "archive.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_incremental_dedupe_update_search_and_multi_account(self):
        data = FakeData(os.path.dirname(self.source))
        with open_archive(self.archive_path) as archive:
            first = archive.sync(data)
            self.assertEqual(first["inserted"], 1)
            self.assertFalse(first["skipped"])

            second = archive.sync(data)
            self.assertTrue(second["skipped"])
            self.assertEqual(archive.con.execute("select count(*) from messages").fetchone()[0], 1)

            data._text = "hello updated archive"
            with open(self.db_source, "ab") as stream:
                stream.write(b"-v2")
            third = archive.sync(data)
            self.assertEqual(third["updated"], 1)
            self.assertEqual(archive.con.execute("select count(*) from messages").fetchone()[0], 1)
            hits = archive.search("updated", account="wxid_a")
            self.assertEqual(len(hits), 1)
            self.assertIn("updated", hits[0]["text"])

            other_root = os.path.join(self.temp.name, "snapshot-b")
            os.makedirs(other_root)
            with open(os.path.join(other_root, "message_0.db"), "wb") as stream:
                stream.write(b"account-b")
            other = FakeData(other_root, wxid="wxid_b", text="other account")
            fourth = archive.sync(other)
            self.assertEqual(fourth["inserted"], 1)
            self.assertEqual(archive.con.execute("select count(*) from accounts").fetchone()[0], 2)
            self.assertEqual(archive.con.execute("select count(*) from messages").fetchone()[0], 2)

        mode = os.stat(self.archive_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
