from __future__ import annotations

import os
import json
import sqlite3
import stat
import tempfile
import time
import unittest
import zipfile
from io import BytesIO

from wx4.archive import open_archive
from wx4.archive_export import export_archive_bundle
from wx4.webapp import create_app


class WebFakeData:
    wxid = "wxid_web"
    contacts = {"wxid_web": {"username": "wxid_web", "nick_name": "Web"}}
    room_members = {}

    def __init__(self, root):
        self.root = root

    def account_dir(self):
        return os.path.dirname(self.root)

    @staticmethod
    def name_of(_username):
        return "Web"

    @staticmethod
    def sessions():
        return [{"username": "friend", "summary": "hello", "last_time": 10,
                 "sort_time": 10, "is_group": False}]

    @staticmethod
    def has_messages(_username):
        return True

    @staticmethod
    def session_display(_username):
        return "Friend"

    @staticmethod
    def message_db_rel(_shard):
        return "message.db"

    @staticmethod
    def messages(_username):
        return [{"session": "friend", "server_id": 1, "local_id": 1, "raw_type": 1,
                 "type": 1, "type_name": "文本", "kind": "text", "create_time": 10,
                 "sender_wxid": "friend", "sender": "Friend", "is_sender": False,
                 "text": "hello web", "content": "hello web", "extra": {}, "shard": 0}]


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        source = os.path.join(self.temp.name, "source")
        os.makedirs(source)
        with open(os.path.join(source, "message.db"), "wb") as stream:
            stream.write(b"db")
        self.db = os.path.join(self.temp.name, "archive.sqlite3")
        with open_archive(self.db) as archive:
            archive.sync(WebFakeData(source))
        media_dir = os.path.join(self.temp.name, "media")
        os.makedirs(media_dir)
        with open(os.path.join(media_dir, "test.jpg"), "wb") as stream:
            stream.write(b"fake-jpeg")
        con = sqlite3.connect(self.db)
        account_id = con.execute("select id from accounts limit 1").fetchone()[0]
        message_id = con.execute("select id from messages limit 1").fetchone()[0]
        con.execute(
            "insert into sessions(account_id,username,display_name,is_group,message_count) "
            "values(?,?,?,?,?)", (account_id, "team@chatroom", "Team", 1, 0))
        con.execute(
            "insert into room_members(account_id,room_username,member_username) values(?,?,?)",
            (account_id, "team@chatroom", "friend"))
        con.execute(
            "insert into attachments(message_id,kind,path,status,mime_type,size) "
            "values(?,?,?,?,?,?)",
            (message_id, "image", "media/test.jpg", "ready", "image/jpeg", 9))
        con.commit()
        con.close()
        self.app = create_app(self.db, token="secret")
        self.client = self.app.test_client()
        self.headers = {"X-Archive-Token": "secret"}

    def tearDown(self):
        self.temp.cleanup()

    def test_token_api_and_security_headers(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        response = self.client.get("/api/overview", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["counts"]["messages"], 1)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        page = self.client.get("/?token=secret")
        self.assertEqual(page.status_code, 200)
        self.assertIn("微信记忆库".encode("utf-8"), page.data)

    def test_sessions_messages_search_and_path_traversal_guard(self):
        account = self.client.get("/api/accounts", headers=self.headers).get_json()[0]
        sessions = self.client.get(
            "/api/sessions?account=%s" % account["id"], headers=self.headers).get_json()
        self.assertEqual(sessions[0]["username"], "friend")
        messages = self.client.get(
            "/api/messages?account=%s&session=friend" % account["id"],
            headers=self.headers).get_json()
        self.assertEqual(messages[0]["text"], "hello web")
        hits = self.client.get(
            "/api/search?q=hello&account=%s" % account["id"], headers=self.headers).get_json()
        self.assertEqual(len(hits), 1)

        con = sqlite3.connect(self.db)
        message_id = con.execute("select id from messages limit 1").fetchone()[0]
        con.execute("insert into attachments(message_id,kind,path) values(?,?,?)",
                    (message_id, "file", "../../etc/passwd"))
        attachment_id = con.execute("select last_insert_rowid()").fetchone()[0]
        con.commit()
        con.close()
        response = self.client.get(
            "/api/attachments/%d/file" % attachment_id, headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_directory_timeline_media_and_date_filter(self):
        account = self.client.get("/api/accounts", headers=self.headers).get_json()[0]
        aid = account["id"]
        contacts = self.client.get(
            "/api/directory?account=%s&category=contacts" % aid,
            headers=self.headers).get_json()
        self.assertGreaterEqual(contacts["total"], 1)
        self.assertEqual(contacts["items"][0]["display_name"], "Web")
        groups = self.client.get(
            "/api/directory?account=%s&category=groups" % aid,
            headers=self.headers).get_json()
        self.assertEqual(groups["items"][0]["username"], "team@chatroom")
        self.assertEqual(groups["items"][0]["member_count"], 1)

        timeline = self.client.get(
            "/api/timeline?account=%s&session=friend" % aid,
            headers=self.headers).get_json()
        self.assertEqual(timeline["time_start"], 10)
        self.assertEqual(timeline["months"][0]["count"], 1)
        analytics = self.client.get(
            "/api/analytics?account=%s" % aid, headers=self.headers).get_json()
        self.assertEqual(analytics["summary"]["messages"], 1)
        self.assertEqual(analytics["summary"]["received"], 1)
        self.assertEqual(analytics["top_sessions"][0]["display_name"], "Friend")
        self.assertEqual(analytics["daily"][0]["count"], 1)
        inside = self.client.get(
            "/api/messages?account=%s&session=friend&start=1&end=20" % aid,
            headers=self.headers).get_json()
        outside = self.client.get(
            "/api/messages?account=%s&session=friend&start=20" % aid,
            headers=self.headers).get_json()
        self.assertEqual(len(inside), 1)
        self.assertEqual(outside, [])

        media = self.client.get(
            "/api/media?account=%s&category=image" % aid,
            headers=self.headers).get_json()
        self.assertEqual(media["total"], 1)
        self.assertEqual(media["items"][0]["mime_type"], "image/jpeg")
        file_response = self.client.get(media["items"][0]["url"], headers=self.headers)
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.data, b"fake-jpeg")
        file_response.close()

    def test_background_html_export_is_downloadable_and_offline(self):
        account = self.client.get("/api/accounts", headers=self.headers).get_json()[0]
        response = self.client.post(
            "/api/exports", headers=self.headers,
            json={"account": account["id"], "session": "friend", "format": "html"})
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["id"]
        job = None
        for _attempt in range(100):
            jobs = self.client.get("/api/exports", headers=self.headers).get_json()
            job = next(item for item in jobs if item["id"] == job_id)
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.01)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["message_count"], 1)
        self.assertEqual(job["attachment_count"], 1)
        download = self.client.get(
            "/api/exports/%s/download" % job_id, headers=self.headers)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "application/zip")
        with zipfile.ZipFile(BytesIO(download.data)) as bundle:
            names = bundle.namelist()
            html_name = next(name for name in names if name.endswith(".html"))
            html_doc = bundle.read(html_name).decode("utf-8")
            self.assertIn("hello web", html_doc)
            self.assertTrue(any(name.startswith("media/") for name in names))
            self.assertNotIn("http://127.0.0.1", html_doc)
        download.close()

    def test_structured_batch_and_sqlite_exports(self):
        con = sqlite3.connect(self.db)
        aid = con.execute("select id from accounts limit 1").fetchone()[0]
        con.execute(
            "insert into accounts(wxid,display_name,source_account_dir,first_seen,last_seen) "
            "values('wxid_other','Other','','2026-01-01','2026-01-01')")
        other = con.execute("select last_insert_rowid()").fetchone()[0]
        con.execute(
            "insert into sessions(account_id,username,display_name,message_count) values(?,?,?,?)",
            (other, "other-friend", "Other Friend", 1))
        con.execute(
            "insert into messages(account_id,session_username,stable_key,text,content_hash) "
            "values(?,?,?,?,?)", (other, "other-friend", "other-key", "private other", "hash"))
        con.commit()
        con.close()
        root = os.path.join(self.temp.name, "formats")
        os.makedirs(root)
        for fmt in ("json", "csv", "txt"):
            result = export_archive_bundle(
                self.db, aid, "friend", fmt, os.path.join(root, fmt))
            self.assertEqual(result["message_count"], 1)
            self.assertTrue(os.path.isfile(result["zip_path"]))
        json_path = os.path.join(root, "json", "Friend.json")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(root, "json")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(json_path).st_mode), 0o600)
        with open(json_path, encoding="utf-8") as stream:
            payload = json.load(stream)
        self.assertEqual(payload["messages"][0]["text"], "hello web")
        self.assertNotIn(self.temp.name, json.dumps(payload))
        with open(os.path.join(root, "csv", "Friend.csv"),
                  encoding="utf-8-sig") as stream:
            self.assertIn("附件", stream.readline())

        contacts = export_archive_bundle(
            self.db, aid, "*", "contacts_csv", os.path.join(root, "contacts"))
        messages = export_archive_bundle(
            self.db, aid, "*", "messages_csv", os.path.join(root, "messages"))
        batch = export_archive_bundle(
            self.db, aid, "*", "html", os.path.join(root, "batch"))
        sqlite_result = export_archive_bundle(
            self.db, aid, "*", "sqlite", os.path.join(root, "sqlite"))
        self.assertGreaterEqual(contacts["contact_count"], 1)
        self.assertEqual(messages["message_count"], 1)
        self.assertEqual(batch["session_count"], 1)
        self.assertTrue(os.path.isfile(os.path.join(root, "batch", "index.html")))
        portable_db = os.path.join(root, "sqlite", "archive.sqlite3")
        portable = sqlite3.connect(portable_db)
        self.assertEqual(portable.execute("select count(*) from accounts").fetchone()[0], 1)
        self.assertEqual(portable.execute("select count(*) from messages").fetchone()[0], 1)
        self.assertEqual(portable.execute(
            "select count(*) from messages where text='private other'").fetchone()[0], 0)
        self.assertEqual(portable.execute("select source_account_dir from accounts").fetchone()[0], "")
        self.assertEqual(portable.execute("select count(*) from source_files").fetchone()[0], 0)
        attachment_path = portable.execute("select path from attachments").fetchone()[0]
        portable.close()
        self.assertTrue(os.path.isfile(os.path.join(root, "sqlite", attachment_path)))
        self.assertEqual(sqlite_result["attachment_count"], 1)
        for name in os.listdir(root):
            if not name.endswith(".zip"):
                continue
            with zipfile.ZipFile(os.path.join(root, name)) as bundle:
                self.assertTrue(all(not item.startswith(("/", "\\")) and ".." not in item.split("/")
                                    for item in bundle.namelist()))

    def test_missing_archive_starts_first_run_wizard(self):
        empty_root = os.path.join(self.temp.name, "xwechat_files")
        os.makedirs(empty_root)
        missing_db = os.path.join(self.temp.name, "fresh", "archive.sqlite3")
        wizard = create_app(missing_db, token="setup-secret", data_root=empty_root,
                            setup_root=os.path.join(self.temp.name, "setup"))
        client = wizard.test_client()
        headers = {"X-Archive-Token": "setup-secret"}
        health = client.get("/api/health", headers=headers).get_json()
        self.assertFalse(health["archive_ready"])
        status = client.get("/api/setup/status", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["accounts"], [])
        self.assertEqual(client.get("/api/overview", headers=headers).status_code, 409)
        self.assertEqual(client.get("/?token=setup-secret").status_code, 200)


if __name__ == "__main__":
    unittest.main()
