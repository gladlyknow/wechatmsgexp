# -*- coding: utf-8 -*-
"""统一档案库：多账号、多快照去重合并、来源追踪与 FTS5 搜索。"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .export import safe_name
from .security import private_dir, private_file

SCHEMA_VERSION = 1


SCHEMA = """
create table if not exists archive_meta(
  key text primary key,
  value text not null
);
create table if not exists accounts(
  id integer primary key,
  wxid text not null unique,
  display_name text not null default '',
  source_account_dir text not null default '',
  first_seen text not null,
  last_seen text not null
);
create table if not exists sync_runs(
  id integer primary key,
  account_id integer not null references accounts(id) on delete cascade,
  source_root text not null,
  source_fingerprint text not null default '',
  started_at text not null,
  finished_at text,
  status text not null,
  report_json text not null default '{}'
);
create table if not exists source_files(
  id integer primary key,
  account_id integer not null references accounts(id) on delete cascade,
  source_root text not null,
  rel_path text not null,
  size integer not null,
  mtime_ns integer not null,
  sha256 text not null,
  last_run_id integer references sync_runs(id),
  unique(account_id, source_root, rel_path)
);
create table if not exists contacts(
  account_id integer not null references accounts(id) on delete cascade,
  username text not null,
  alias text not null default '',
  remark text not null default '',
  nickname text not null default '',
  description text not null default '',
  local_type integer not null default 0,
  last_run_id integer references sync_runs(id),
  primary key(account_id, username)
);
create table if not exists sessions(
  account_id integer not null references accounts(id) on delete cascade,
  username text not null,
  display_name text not null default '',
  summary text not null default '',
  last_time integer not null default 0,
  sort_time integer not null default 0,
  is_group integer not null default 0,
  message_count integer not null default 0,
  last_run_id integer references sync_runs(id),
  primary key(account_id, username)
);
create table if not exists room_members(
  account_id integer not null,
  room_username text not null,
  member_username text not null,
  last_run_id integer references sync_runs(id),
  primary key(account_id, room_username, member_username),
  foreign key(account_id, room_username) references sessions(account_id, username)
    on delete cascade
);
create table if not exists messages(
  id integer primary key,
  account_id integer not null references accounts(id) on delete cascade,
  session_username text not null,
  stable_key text not null,
  server_id text not null default '',
  local_id integer not null default 0,
  raw_type integer not null default 0,
  msg_type integer not null default 0,
  type_name text not null default '',
  kind text not null default '',
  create_time integer not null default 0,
  sender_wxid text not null default '',
  sender_name text not null default '',
  is_sender integer not null default 0,
  text text not null default '',
  content text not null default '',
  extra_json text not null default '{}',
  content_hash text not null,
  first_seen_run_id integer references sync_runs(id),
  last_seen_run_id integer references sync_runs(id),
  unique(account_id, stable_key),
  foreign key(account_id, session_username) references sessions(account_id, username)
    on delete cascade
);
create index if not exists idx_messages_session_time
  on messages(account_id, session_username, create_time, id);
create index if not exists idx_messages_server
  on messages(account_id, server_id);
create table if not exists message_sources(
  message_id integer not null references messages(id) on delete cascade,
  sync_run_id integer not null references sync_runs(id) on delete cascade,
  source_db text not null default '',
  shard integer not null default 0,
  local_id integer not null default 0,
  primary key(message_id, sync_run_id, source_db, local_id)
);
create table if not exists quotes(
  message_id integer primary key references messages(id) on delete cascade,
  refer_from text not null default '',
  refer_name text not null default '',
  refer_text text not null default '',
  reply_text text not null default ''
);
create table if not exists attachments(
  id integer primary key,
  message_id integer not null references messages(id) on delete cascade,
  kind text not null,
  path text not null,
  status text not null default 'ready',
  mime_type text not null default '',
  size integer not null default 0,
  sha256 text not null default '',
  last_run_id integer references sync_runs(id),
  unique(message_id, kind, path)
);
create virtual table if not exists messages_fts using fts5(
  text, content, session_username unindexed, sender_name unindexed,
  account_id unindexed, tokenize='unicode61'
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _json_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


class Archive:
    def __init__(self, path: str, progress=None):
        self.path = os.path.abspath(path)
        self.progress = progress or (lambda _message: None)
        private_dir(os.path.dirname(self.path))
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("pragma foreign_keys=on")
        self.con.execute("pragma journal_mode=wal")
        self.con.execute("pragma synchronous=normal")
        self.con.executescript(SCHEMA)
        self.con.execute(
            "insert into archive_meta(key,value) values('schema_version',?) "
            "on conflict(key) do update set value=excluded.value", (str(SCHEMA_VERSION),))
        self.con.commit()
        try:
            private_file(self.path)
        except OSError:
            pass
        self._hash_cache = {}

    def close(self):
        try:
            self.con.execute("pragma wal_checkpoint(truncate)")
        except sqlite3.Error:
            pass
        self.con.close()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                try:
                    private_file(path)
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def _account(self, data) -> int:
        wxid = (data.wxid or os.path.basename(data.account_dir()) or "unknown").strip()
        now = _now()
        row = self.con.execute("select id from accounts where wxid=?", (wxid,)).fetchone()
        if row:
            self.con.execute(
                "update accounts set display_name=?,source_account_dir=?,last_seen=? where id=?",
                (data.name_of(wxid), os.path.abspath(data.account_dir()), now, row[0]))
            return row[0]
        cur = self.con.execute(
            "insert into accounts(wxid,display_name,source_account_dir,first_seen,last_seen) "
            "values(?,?,?,?,?)",
            (wxid, data.name_of(wxid), os.path.abspath(data.account_dir()), now, now))
        return cur.lastrowid

    def _source_files(self, account_id: int, root: str):
        root = os.path.abspath(root)
        files, changed = [], []
        known_paths = {
            row[0] for row in self.con.execute(
                "select rel_path from source_files where account_id=? and source_root=?",
                (account_id, root))
        }
        for directory, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".db"):
                    continue
                path = os.path.join(directory, name)
                rel = os.path.relpath(path, root).replace("\\", "/")
                stat = os.stat(path)
                old = self.con.execute(
                    "select size,mtime_ns,sha256 from source_files "
                    "where account_id=? and source_root=? and rel_path=?",
                    (account_id, root, rel)).fetchone()
                if old and old[0] == stat.st_size and old[1] == stat.st_mtime_ns:
                    sha = old[2]
                else:
                    sha = _sha256_file(path)
                record = (rel, stat.st_size, stat.st_mtime_ns, sha)
                files.append(record)
                if not old or old[2] != sha:
                    changed.append(rel)
        files.sort()
        current_paths = {record[0] for record in files}
        changed.extend("<removed>:%s" % rel for rel in sorted(known_paths - current_paths))
        fingerprint = hashlib.sha256(
            "\n".join("%s:%s:%s" % (rel, size, sha)
                      for rel, size, _mtime, sha in files).encode("utf-8")).hexdigest()
        return root, files, changed, fingerprint

    @staticmethod
    def _stable_key(account_wxid: str, message: dict) -> str:
        session = message.get("session") or ""
        server_id = message.get("server_id") or 0
        if server_id:
            return "svr:%s:%s" % (session, server_id)
        raw = json.dumps({
            "account": account_wxid, "session": session,
            "create_time": message.get("create_time") or 0,
            "local_id": message.get("local_id") or 0,
            "raw_type": message.get("raw_type") or 0,
            "sender": message.get("sender_wxid") or "",
            "content": message.get("content") or "",
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "fallback:" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _semantic_extra(extra: dict) -> dict:
        media_keys = {
            "images", "image_thumbs", "voice_source", "voice_file", "audio_file",
            "video_file", "video_poster", "files", "emoji_tokens", "emoji_file",
        }
        return {key: value for key, value in (extra or {}).items() if key not in media_keys}

    def _archive_path(self, path: str) -> str:
        if not path:
            return ""
        return os.path.relpath(os.path.abspath(path), os.path.dirname(self.path)).replace("\\", "/")

    def _attachment_rows(self, message: dict):
        extra = message.get("extra") or {}
        for path in extra.get("images", []) or []:
            yield "image", path, "encrypted_raw" if path.lower().endswith(".dat") else "ready"
        for path in (extra.get("image_thumbs") or {}).values():
            yield "image_thumbnail", path, "ready"
        for key, kind in (("voice_source", "voice_silk"), ("voice_file", "voice_audio"),
                          ("audio_file", "audio"), ("video_file", "video"),
                          ("video_poster", "video_poster"), ("emoji_file", "emoji")):
            path = extra.get(key)
            if path:
                yield kind, path, "ready"
        for path in extra.get("files", []) or []:
            yield "file", path, "ready"
        for path in (extra.get("emoji_tokens") or {}).values():
            yield "emoji", path, "ready"

    def _file_meta(self, path: str):
        absolute = os.path.abspath(path)
        if absolute in self._hash_cache:
            return self._hash_cache[absolute]
        try:
            size = os.path.getsize(absolute)
            sha = _sha256_file(absolute)
        except OSError:
            size, sha = 0, ""
        mime = mimetypes.guess_type(absolute)[0] or "application/octet-stream"
        result = (size, sha, mime)
        self._hash_cache[absolute] = result
        return result

    def sync(self, data, sessions: list[str] | None = None, with_media: bool = False,
             media_root: str | None = None, force: bool = False) -> dict:
        started = _now()
        account_id = self._account(data)
        account = self.con.execute("select wxid from accounts where id=?", (account_id,)).fetchone()[0]
        source_root, source_files, changed_files, fingerprint = self._source_files(
            account_id, data.root)
        cur = self.con.execute(
            "insert into sync_runs(account_id,source_root,source_fingerprint,started_at,status) "
            "values(?,?,?,?,?)", (account_id, source_root, fingerprint, started, "running"))
        run_id = cur.lastrowid
        self.con.commit()
        report = {
            "run_id": run_id, "account": account, "source_root": source_root,
            "source_files": len(source_files), "changed_source_files": changed_files,
            "inserted": 0, "updated": 0, "unchanged": 0,
            "sessions": 0, "contacts": 0, "attachments": 0,
            "media_issues": 0, "skipped": False,
            "scope": "partial" if sessions else "full",
        }
        try:
            known = self.con.execute(
                "select count(*) from source_files where account_id=? and source_root=?",
                (account_id, source_root)).fetchone()[0]
            if not changed_files and known and not force and not with_media and not sessions:
                report["skipped"] = True
                self._finish_run(run_id, "complete", report, source_files, account_id, source_root)
                self.progress("源快照无变化，增量同步跳过消息扫描")
                return report

            self._sync_contacts(data, account_id, run_id, report)
            selected = set(sessions or [])
            session_rows = data.sessions()
            if selected:
                session_rows = [row for row in session_rows if row["username"] in selected]
            for session in session_rows:
                username = session["username"]
                if not data.has_messages(username):
                    continue
                messages = data.messages(username)
                media_stats = None
                if with_media:
                    from .media import export_session_media
                    root = media_root or os.path.join(os.path.dirname(self.path), "media")
                    private_dir(root)
                    target = os.path.join(root, safe_name(account),
                                          safe_name(data.session_display(username)))
                    media_stats = export_session_media(
                        data, username, messages, target, progress=self.progress)
                    report["media_issues"] += len(media_stats.issues)
                self._sync_session(data, session, messages, account_id, run_id, account,
                                   report)
            self._finish_run(run_id, "complete", report, source_files, account_id, source_root,
                             record_sources=not bool(sessions))
            self.progress(
                "档案同步完成：新增 %d，更新 %d，未变 %d，附件 %d" %
                (report["inserted"], report["updated"], report["unchanged"],
                 report["attachments"]))
            return report
        except Exception as exc:
            report["error"] = str(exc)
            self.con.execute(
                "update sync_runs set finished_at=?,status='failed',report_json=? where id=?",
                (_now(), json.dumps(report, ensure_ascii=False), run_id))
            self.con.commit()
            raise

    def _finish_run(self, run_id, status, report, source_files, account_id, source_root,
                    record_sources=True):
        if record_sources:
            current = {record[0] for record in source_files}
            for row in self.con.execute(
                    "select rel_path from source_files where account_id=? and source_root=?",
                    (account_id, source_root)).fetchall():
                if row[0] not in current:
                    self.con.execute(
                        "delete from source_files where account_id=? and source_root=? and rel_path=?",
                        (account_id, source_root, row[0]))
            for rel, size, mtime_ns, sha in source_files:
                self.con.execute(
                    "insert into source_files(account_id,source_root,rel_path,size,mtime_ns,sha256,last_run_id) "
                    "values(?,?,?,?,?,?,?) on conflict(account_id,source_root,rel_path) do update set "
                    "size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,"
                    "last_run_id=excluded.last_run_id",
                    (account_id, source_root, rel, size, mtime_ns, sha, run_id))
        self.con.execute(
            "update sync_runs set finished_at=?,status=?,report_json=? where id=?",
            (_now(), status, json.dumps(report, ensure_ascii=False), run_id))
        self.con.commit()

    def _sync_contacts(self, data, account_id, run_id, report):
        seen = set()
        for contact in data.contacts.values():
            username = _json_text(contact.get("username"))
            if not username or username in seen:
                continue
            seen.add(username)
            self.con.execute(
                "insert into contacts(account_id,username,alias,remark,nickname,description,"
                "local_type,last_run_id) values(?,?,?,?,?,?,?,?) on conflict(account_id,username) "
                "do update set alias=excluded.alias,remark=excluded.remark,nickname=excluded.nickname,"
                "description=excluded.description,local_type=excluded.local_type,last_run_id=excluded.last_run_id",
                (account_id, username, _json_text(contact.get("alias")),
                 _json_text(contact.get("remark")), _json_text(contact.get("nick_name")),
                 _json_text(contact.get("description")), int(contact.get("local_type") or 0),
                 run_id))
        report["contacts"] = len(seen)

    def _sync_session(self, data, session, messages, account_id, run_id,
                      account_wxid, report):
        username = session["username"]
        self.con.execute(
            "insert into sessions(account_id,username,display_name,summary,last_time,sort_time,"
            "is_group,message_count,last_run_id) values(?,?,?,?,?,?,?,?,?) "
            "on conflict(account_id,username) do update set display_name=excluded.display_name,"
            "summary=excluded.summary,last_time=excluded.last_time,sort_time=excluded.sort_time,"
            "is_group=excluded.is_group,message_count=excluded.message_count,last_run_id=excluded.last_run_id",
            (account_id, username, data.session_display(username), session.get("summary") or "",
             int(session.get("last_time") or 0), int(session.get("sort_time") or 0),
             int(bool(session.get("is_group"))), len(messages), run_id))
        report["sessions"] += 1
        for member in data.room_members.get(username, []) or []:
            self.con.execute(
                "insert into room_members(account_id,room_username,member_username,last_run_id) "
                "values(?,?,?,?) on conflict(account_id,room_username,member_username) "
                "do update set last_run_id=excluded.last_run_id",
                (account_id, username, member, run_id))
        for message in messages:
            self._sync_message(data, message, account_id, run_id, account_wxid, report)

    def _sync_message(self, data, message, account_id, run_id, account_wxid, report):
        stable = self._stable_key(account_wxid, message)
        semantic_extra = self._semantic_extra(message.get("extra") or {})
        canonical = {
            "server_id": str(message.get("server_id") or ""),
            "raw_type": int(message.get("raw_type") or 0),
            "type": int(message.get("type") or 0), "kind": message.get("kind") or "",
            "create_time": int(message.get("create_time") or 0),
            "sender": message.get("sender_wxid") or "", "text": message.get("text") or "",
            "content": message.get("content") or "", "extra": semantic_extra,
        }
        canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        existing = self.con.execute(
            "select id,content_hash from messages where account_id=? and stable_key=?",
            (account_id, stable)).fetchone()
        values = (
            message.get("session") or "", str(message.get("server_id") or ""),
            int(message.get("local_id") or 0), int(message.get("raw_type") or 0),
            int(message.get("type") or 0), message.get("type_name") or "",
            message.get("kind") or "", int(message.get("create_time") or 0),
            message.get("sender_wxid") or "", message.get("sender") or "",
            int(bool(message.get("is_sender"))), message.get("text") or "",
            message.get("content") or "", json.dumps(semantic_extra, ensure_ascii=False),
            content_hash,
        )
        if existing is None:
            cur = self.con.execute(
                "insert into messages(account_id,stable_key,session_username,server_id,local_id,"
                "raw_type,msg_type,type_name,kind,create_time,sender_wxid,sender_name,is_sender,"
                "text,content,extra_json,content_hash,first_seen_run_id,last_seen_run_id) "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, stable) + values + (run_id, run_id))
            message_id = cur.lastrowid
            report["inserted"] += 1
            fts_changed = True
        else:
            message_id = existing[0]
            fts_changed = existing[1] != content_hash
            if fts_changed:
                report["updated"] += 1
            else:
                report["unchanged"] += 1
            self.con.execute(
                "update messages set session_username=?,server_id=?,local_id=?,raw_type=?,"
                "msg_type=?,type_name=?,kind=?,create_time=?,sender_wxid=?,sender_name=?,"
                "is_sender=?,text=?,content=?,extra_json=?,content_hash=?,last_seen_run_id=? "
                "where id=?", values + (run_id, message_id))
        if fts_changed:
            self.con.execute("delete from messages_fts where rowid=?", (message_id,))
            self.con.execute(
                "insert into messages_fts(rowid,text,content,session_username,sender_name,account_id) "
                "values(?,?,?,?,?,?)",
                (message_id, message.get("text") or "", message.get("content") or "",
                 message.get("session") or "", message.get("sender") or "", account_id))
        self.con.execute(
            "insert or ignore into message_sources(message_id,sync_run_id,source_db,shard,local_id) "
            "values(?,?,?,?,?)",
            (message_id, run_id, data.message_db_rel(message.get("shard") or 0),
             int(message.get("shard") or 0), int(message.get("local_id") or 0)))
        extra = message.get("extra") or {}
        if message.get("kind") == "quote":
            self.con.execute(
                "insert into quotes(message_id,refer_from,refer_name,refer_text,reply_text) "
                "values(?,?,?,?,?) on conflict(message_id) do update set "
                "refer_from=excluded.refer_from,refer_name=excluded.refer_name,"
                "refer_text=excluded.refer_text,reply_text=excluded.reply_text",
                (message_id, extra.get("refer_from") or "", extra.get("refer_name") or "",
                 extra.get("refer_text") or "", extra.get("reply_text") or ""))
        seen_attachments = set()
        for kind, path, status in self._attachment_rows(message):
            archived_path = self._archive_path(path)
            key = (kind, archived_path)
            if not archived_path or key in seen_attachments:
                continue
            seen_attachments.add(key)
            size, sha, mime = self._file_meta(path)
            self.con.execute(
                "insert into attachments(message_id,kind,path,status,mime_type,size,sha256,last_run_id) "
                "values(?,?,?,?,?,?,?,?) on conflict(message_id,kind,path) do update set "
                "status=excluded.status,mime_type=excluded.mime_type,size=excluded.size,"
                "sha256=excluded.sha256,last_run_id=excluded.last_run_id",
                (message_id, kind, archived_path, status, mime, size, sha, run_id))
            report["attachments"] += 1

    def search(self, query: str, account: str | None = None, limit: int = 50) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        # unicode61 会把连续中文视为一个 token。对普通关键词
        # 默认做前缀匹配，使“语音”能命中“语音2”。
        has_syntax = any(mark in query for mark in ('"', "*", "(", ")", " OR ", " AND ", " NOT "))
        if has_syntax:
            fts_query = query
        else:
            fts_query = " AND ".join(
                '"%s"*' % token.replace('"', '""') for token in query.split())
        params = [fts_query]
        where = "messages_fts match ?"
        if account:
            row = self.con.execute("select id from accounts where wxid=?", (account,)).fetchone()
            if not row:
                return []
            where += " and messages_fts.account_id=?"
            params.append(row[0])
        params.append(max(1, min(int(limit), 500)))
        rows = self.con.execute(
            "select m.id,a.wxid,m.session_username,m.sender_name,m.create_time,m.kind,m.text,"
            "snippet(messages_fts,0,'[',']','…',18) as snippet "
            "from messages_fts join messages m on m.id=messages_fts.rowid "
            "join accounts a on a.id=m.account_id where " + where +
            " order by rank limit ?", tuple(params)).fetchall()
        if not rows:
            like_params = ["%%%s%%" % query, "%%%s%%" % query]
            account_sql = ""
            if account:
                account_sql = " and a.wxid=?"
                like_params.append(account)
            like_params.append(max(1, min(int(limit), 500)))
            rows = self.con.execute(
                "select m.id,a.wxid,m.session_username,m.sender_name,m.create_time,m.kind,m.text,"
                "m.text as snippet from messages m join accounts a on a.id=m.account_id "
                "where (m.text like ? or m.content like ?)" + account_sql +
                " order by m.create_time desc limit ?", tuple(like_params)).fetchall()
        return [dict(row) for row in rows]


def open_archive(path: str, progress=None) -> Archive:
    return Archive(path, progress=progress)
