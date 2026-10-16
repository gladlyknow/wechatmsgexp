from __future__ import annotations

import os
import tempfile
import time
import unittest

from wx4.archive import open_archive
from wx4.webapp import create_app


class LargeArchiveTests(unittest.TestCase):
    MESSAGE_COUNT = 100_000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "archive.sqlite3")
        with open_archive(self.path) as archive:
            con = archive.con
            con.execute(
                "insert into accounts(wxid,display_name,source_account_dir,first_seen,last_seen) "
                "values('wxid_scale','Scale','','2026-01-01','2026-01-01')")
            aid = con.execute("select last_insert_rowid()").fetchone()[0]
            con.execute(
                "insert into sessions(account_id,username,display_name,message_count,sort_time) "
                "values(?,?,?,?,?)", (aid, "benchmark", "十万消息会话",
                                      self.MESSAGE_COUNT, 1_700_000_000 + self.MESSAGE_COUNT))
            con.execute(
                "with recursive seq(x) as (values(1) union all select x+1 from seq where x<?) "
                "insert into messages(account_id,session_username,stable_key,server_id,local_id,"
                "msg_type,type_name,kind,create_time,sender_wxid,sender_name,is_sender,text,"
                "content,extra_json,content_hash) select ?, 'benchmark',printf('k%06d',x),"
                "printf('%d',x),x,1,'文本','text',1700000000+x,'friend','Friend',x%2,"
                "'benchmark message','benchmark message','{}',printf('h%06d',x) from seq",
                (self.MESSAGE_COUNT, aid))
            con.execute(
                "insert into messages_fts(rowid,text,content,session_username,sender_name,account_id) "
                "select id,text,content,session_username,sender_name,account_id from messages")
            con.commit()
        self.client = create_app(self.path, token="scale").test_client()
        self.headers = {"X-Archive-Token": "scale"}
        self.aid = self.client.get("/api/accounts", headers=self.headers).get_json()[0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_hundred_thousand_message_paging_search_and_analytics(self):
        started = time.perf_counter()
        first = self.client.get(
            "/api/messages?account=%s&session=benchmark&limit=80" % self.aid,
            headers=self.headers).get_json()
        page_elapsed = time.perf_counter() - started
        self.assertEqual(len(first), 80)
        started = time.perf_counter()
        second = self.client.get(
            "/api/messages?account=%s&session=benchmark&limit=80&before=%s" %
            (self.aid, first[0]["create_time"]), headers=self.headers).get_json()
        second_elapsed = time.perf_counter() - started
        self.assertEqual(len(second), 80)
        self.assertFalse({item["id"] for item in first} & {item["id"] for item in second})
        self.assertLess(max(page_elapsed, second_elapsed), 2.0)

        started = time.perf_counter()
        hits = self.client.get(
            "/api/search?account=%s&q=benchmark&limit=80" % self.aid,
            headers=self.headers).get_json()
        search_elapsed = time.perf_counter() - started
        self.assertEqual(len(hits), 80)
        self.assertLess(search_elapsed, 5.0)

        started = time.perf_counter()
        report = self.client.get(
            "/api/analytics?account=%s&year=2023" % self.aid,
            headers=self.headers).get_json()
        analytics_elapsed = time.perf_counter() - started
        self.assertEqual(report["summary"]["messages"], self.MESSAGE_COUNT)
        self.assertEqual(report["top_sessions"][0]["count"], self.MESSAGE_COUNT)
        self.assertLess(analytics_elapsed, 10.0)


if __name__ == "__main__":
    unittest.main()
