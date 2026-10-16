# -*- coding: utf-8 -*-
"""从统一档案库生成可离线携带的会话与结构化数据导出包。"""
from __future__ import annotations

import csv
import html
import json
import os
import shutil
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .export import (export_csv, export_html, export_json, export_markdown,
                     export_txt, safe_name, csv_safe)


class _ArchiveDataAdapter:
    def __init__(self, wxid: str, session: str, display: str):
        self.wxid = wxid
        self._session = session
        self._display = display

    def session_display(self, session: str) -> str:
        return self._display if session == self._session else session

    @staticmethod
    def avatar_of(_username: str) -> str:
        return ""


def _read_only(path: str) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
    return con


def _safe_source(root: Path, value: str) -> Path | None:
    source = (root / value).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return None
    return source if source.is_file() else None


def _message_extra(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _portable(value, base: Path):
    if isinstance(value, dict):
        return {key: _portable(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item, base) for item in value]
    if isinstance(value, str) and os.path.isabs(value):
        try:
            return os.path.relpath(value, base).replace("\\", "/")
        except ValueError:
            return value
    return value


def _write_manifest(destination: Path, manifest: dict):
    path = destination / "export-manifest.json"
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


def _bundle(destination: Path) -> Path:
    zip_path = destination.parent / (destination.name + ".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as bundle:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(destination))
    os.chmod(zip_path, 0o600)
    return zip_path


def export_archive_session(archive_db: str, account_id: int, session: str,
                           fmt: str, output_dir: str, progress=None,
                           bundle: bool = True) -> dict:
    """导出一个会话并返回可供任务中心展示的结果。"""
    if fmt not in ("html", "md", "json", "csv", "txt"):
        raise ValueError("不支持的会话导出格式")
    progress = progress or (lambda _message: None)
    archive_root = Path(archive_db).resolve().parent
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    media_dir = destination / "media"
    media_dir.mkdir(mode=0o700)

    with closing(_read_only(archive_db)) as con:
        account = con.execute(
            "select wxid from accounts where id=?", (account_id,)).fetchone()
        session_row = con.execute(
            "select display_name from sessions where account_id=? and username=?",
            (account_id, session)).fetchone()
        if not account or not session_row:
            raise LookupError("账号或会话不存在")
        rows = con.execute(
            "select id,server_id,local_id,msg_type,type_name,kind,create_time,sender_wxid,"
            "sender_name,is_sender,text,content,extra_json from messages "
            "where account_id=? and session_username=? order by create_time,id",
            (account_id, session)).fetchall()
        message_ids = [row["id"] for row in rows]
        attachments = {}
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            for item in con.execute(
                    "select id,message_id,kind,path,status from attachments "
                    "where message_id in (%s) order by id" % marks, tuple(message_ids)):
                attachments.setdefault(item["message_id"], []).append(item)

    progress("正在整理 %d 条消息" % len(rows))
    copied = 0
    messages = []
    for index, row in enumerate(rows, 1):
        extra = _message_extra(row["extra_json"])
        copied_items = {}
        for item in attachments.get(row["id"], []):
            if item["status"] != "ready":
                continue
            source = _safe_source(archive_root, item["path"])
            if source is None:
                continue
            filename = "%d_%s" % (item["id"], safe_name(source.name, 100))
            target = media_dir / filename
            if not target.exists():
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
                copied += 1
            copied_items.setdefault(item["kind"], []).append(str(target))
        images = copied_items.get("image", [])
        thumbs = copied_items.get("image_thumbnail", [])
        if images:
            extra["images"] = images
            if thumbs:
                extra["image_thumbs"] = {
                    image: thumbs[min(position, len(thumbs) - 1)]
                    for position, image in enumerate(images)
                }
        if copied_items.get("voice_audio"):
            extra["voice_file"] = copied_items["voice_audio"][0]
        if copied_items.get("audio"):
            extra["audio_file"] = copied_items["audio"][0]
        if copied_items.get("video"):
            extra["video_file"] = copied_items["video"][0]
        if copied_items.get("video_poster"):
            extra["video_poster"] = copied_items["video_poster"][0]
        if copied_items.get("file"):
            extra["files"] = copied_items["file"]
        if copied_items.get("emoji"):
            extra["emoji_files"] = copied_items["emoji"]
        message = dict(row)
        message.update({
            "sender": row["sender_name"],
            "time_str": (datetime.fromtimestamp(row["create_time"]).strftime("%Y-%m-%d %H:%M:%S")
                         if row["create_time"] else ""),
            "extra": extra,
        })
        messages.append(message)
        if index % 500 == 0:
            progress("已整理 %d/%d 条消息" % (index, len(rows)))

    adapter = _ArchiveDataAdapter(account["wxid"], session, session_row["display_name"])
    display = safe_name(session_row["display_name"] or session)
    document = destination / ("%s.%s" % (display, fmt))
    if fmt == "html":
        export_html(adapter, session, str(document), media_base=str(destination), msgs=messages)
    elif fmt == "md":
        export_markdown(adapter, session, str(document), media_base=str(destination), msgs=messages)
    elif fmt == "json":
        portable = []
        for message in messages:
            item = dict(message)
            item["extra"] = _portable(item.get("extra") or {}, destination)
            portable.append(item)
        export_json(adapter, session, str(document), msgs=portable)
    elif fmt == "csv":
        export_csv(adapter, session, str(document), msgs=messages)
    else:
        export_txt(adapter, session, str(document), media_base=str(destination), msgs=messages)
    manifest = {
        "account": account["wxid"], "session": session,
        "display_name": session_row["display_name"], "format": fmt,
        "message_count": len(messages), "attachment_count": copied,
        "document": document.name,
    }
    _write_manifest(destination, manifest)
    if not bundle:
        return manifest
    progress("正在打包离线副本")
    zip_path = _bundle(destination)
    return {**manifest, "zip_path": str(zip_path), "zip_size": zip_path.stat().st_size}


def _account_row(con, account_id):
    row = con.execute("select id,wxid,display_name from accounts where id=?",
                      (account_id,)).fetchone()
    if not row:
        raise LookupError("账号不存在")
    return row


def export_archive_table(archive_db: str, account_id: int, table_kind: str,
                         output_dir: str, progress=None) -> dict:
    """导出联系人 CSV 或全量消息 CSV。"""
    progress = progress or (lambda _message: None)
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    with closing(_read_only(archive_db)) as con:
        account = _account_row(con, account_id)
        if table_kind == "contacts_csv":
            rows = con.execute(
                "select username,alias,remark,nickname,description,local_type "
                "from contacts where account_id=? order by remark,nickname,username",
                (account_id,)).fetchall()
            filename = "contacts.csv"
            headers = ["微信号", "别名", "备注", "昵称", "描述", "本地类型"]
            values = [[csv_safe(row[key]) for key in row.keys()] for row in rows]
        elif table_kind == "messages_csv":
            rows = con.execute(
                "select datetime(m.create_time,'unixepoch','localtime') as time,"
                "m.session_username,coalesce(s.display_name,m.session_username) as session_name,"
                "m.sender_wxid,m.sender_name,m.is_sender,m.type_name,m.kind,m.text,"
                "m.server_id,m.local_id,"
                "(select group_concat(a.kind||':'||a.path,';') from attachments a "
                "where a.message_id=m.id) as attachments "
                "from messages m left join sessions s on s.account_id=m.account_id "
                "and s.username=m.session_username where m.account_id=? "
                "order by m.create_time,m.id", (account_id,)).fetchall()
            filename = "all_messages.csv"
            headers = ["时间", "会话微信号", "会话名称", "发送者微信号", "发送者",
                       "是否我发送", "类型", "归一类型", "内容", "server_id",
                       "local_id", "附件索引"]
            values = [[csv_safe(row[key]) for key in row.keys()] for row in rows]
        else:
            raise ValueError("不支持的数据表导出类型")
    progress("正在写入 %d 行" % len(values))
    document = destination / filename
    with open(document, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(values)
    os.chmod(document, 0o600)
    manifest = {
        "account": account["wxid"], "session": "*", "display_name": account["display_name"],
        "format": table_kind, "message_count": len(values) if table_kind == "messages_csv" else 0,
        "contact_count": len(values) if table_kind == "contacts_csv" else 0,
        "attachment_count": 0, "document": filename,
    }
    _write_manifest(destination, manifest)
    zip_path = _bundle(destination)
    return {**manifest, "zip_path": str(zip_path), "zip_size": zip_path.stat().st_size}


def export_archive_sqlite(archive_db: str, account_id: int, output_dir: str,
                          progress=None) -> dict:
    """创建仅包含所选账号、同时重写媒体相对路径的 SQLite 副本。"""
    progress = progress or (lambda _message: None)
    source_root = Path(archive_db).resolve().parent
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    target_path = destination / "archive.sqlite3"
    progress("正在创建账号隔离的 SQLite 副本")
    with closing(_read_only(archive_db)) as source, closing(sqlite3.connect(target_path)) as target:
        account = _account_row(source, account_id)
        source.backup(target)
        target.execute("pragma foreign_keys=on")
        target.execute("delete from accounts where id<>?", (account_id,))
        target.execute("delete from messages_fts where cast(account_id as integer)<>?", (account_id,))
        target.execute("update accounts set source_account_dir='' where id=?", (account_id,))
        target.execute("update sync_runs set source_root='',report_json='{}' where account_id=?",
                       (account_id,))
        target.execute("delete from source_files where account_id=?", (account_id,))
        target.commit()
        media_dir = destination / "media"
        copied = 0
        rows = target.execute("select id,path,status from attachments order by id").fetchall()
        for attachment_id, value, status in rows:
            source_path = _safe_source(source_root, value) if status == "ready" else None
            if source_path is None:
                target.execute("update attachments set status='missing' where id=?", (attachment_id,))
                continue
            media_dir.mkdir(mode=0o700, exist_ok=True)
            name = "%d_%s" % (attachment_id, safe_name(source_path.name, 100))
            target_media = media_dir / name
            shutil.copy2(source_path, target_media)
            os.chmod(target_media, 0o600)
            target.execute("update attachments set path=? where id=?",
                           ((Path("media") / name).as_posix(), attachment_id))
            copied += 1
        target.commit()
        message_count = target.execute("select count(*) from messages").fetchone()[0]
    os.chmod(target_path, 0o600)
    manifest = {
        "account": account["wxid"], "session": "*", "display_name": account["display_name"],
        "format": "sqlite", "message_count": message_count,
        "attachment_count": copied, "document": target_path.name,
    }
    _write_manifest(destination, manifest)
    zip_path = _bundle(destination)
    return {**manifest, "zip_path": str(zip_path), "zip_size": zip_path.stat().st_size}


def export_archive_batch(archive_db: str, account_id: int, fmt: str,
                         output_dir: str, progress=None) -> dict:
    """把账号内全部有消息的会话逐个导出到一个 ZIP。"""
    progress = progress or (lambda _message: None)
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    with closing(_read_only(archive_db)) as con:
        account = _account_row(con, account_id)
        sessions = con.execute(
            "select username,display_name,message_count from sessions "
            "where account_id=? and message_count>0 order by sort_time desc", (account_id,)).fetchall()
    exported, messages, attachments = [], 0, 0
    for index, row in enumerate(sessions, 1):
        folder = "%03d_%s" % (index, safe_name(row["display_name"] or row["username"], 70))
        progress("[%d/%d] 正在导出会话" % (index, len(sessions)))
        result = export_archive_session(
            archive_db, account_id, row["username"], fmt,
            str(destination / folder), progress=progress, bundle=False)
        exported.append({"folder": folder, "document": result["document"],
                         "display_name": result["display_name"]})
        messages += result["message_count"]
        attachments += result["attachment_count"]
    if fmt == "html":
        links = "".join("<li><a href='%s/%s'>%s</a></li>" %
                        (quote(item["folder"]), quote(item["document"]),
                         html.escape(item["display_name"]))
                        for item in exported)
        index_doc = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                     "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                     "<title>微信档案导出</title><style>body{font-family:system-ui;max-width:760px;"
                     "margin:40px auto;padding:20px}li{padding:7px}a{color:#07875b}</style>"
                     "<h1>微信档案导出</h1><ul>%s</ul></html>" % links)
        (destination / "index.html").write_text(index_doc, encoding="utf-8")
        os.chmod(destination / "index.html", 0o600)
    manifest = {
        "account": account["wxid"], "session": "*", "display_name": account["display_name"],
        "format": fmt, "session_count": len(exported), "message_count": messages,
        "attachment_count": attachments, "document": "index.html" if fmt == "html" else "",
    }
    _write_manifest(destination, manifest)
    zip_path = _bundle(destination)
    return {**manifest, "zip_path": str(zip_path), "zip_size": zip_path.stat().st_size}


def export_archive_bundle(archive_db: str, account_id: int, session: str,
                          fmt: str, output_dir: str, progress=None) -> dict:
    """任务中心统一入口。"""
    if fmt in ("contacts_csv", "messages_csv"):
        return export_archive_table(archive_db, account_id, fmt, output_dir, progress)
    if fmt == "sqlite":
        return export_archive_sqlite(archive_db, account_id, output_dir, progress)
    if session == "*":
        return export_archive_batch(archive_db, account_id, fmt, output_dir, progress)
    return export_archive_session(archive_db, account_id, session, fmt, output_dir, progress)
