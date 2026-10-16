# -*- coding: utf-8 -*-
"""只读的本地档案 Web API 与现代化聊天界面。"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request, send_file


def create_app(archive_db: str, token: str | None = None,
               data_root: str | None = None, setup_root: str | None = None) -> Flask:
    archive_db = os.path.abspath(archive_db)
    archive_parent = Path(archive_db).parent
    default_setup = (archive_parent.parent if archive_parent.name == "archive"
                     else archive_parent / "setup")
    setup_root = os.path.abspath(setup_root or str(default_setup))
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    app.config.update(
        ARCHIVE_DB=archive_db,
        ARCHIVE_ROOT=os.path.dirname(archive_db),
        ARCHIVE_READY=os.path.isfile(archive_db),
        DATA_ROOT=data_root,
        SETUP_ROOT=setup_root,
        ACCESS_TOKEN=token or secrets.token_urlsafe(24),
        JSON_AS_ASCII=False,
    )
    export_root = Path(app.config["ARCHIVE_ROOT"]) / "exports"
    export_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(export_root, 0o700)
    except OSError:
        pass
    export_jobs = {}
    export_lock = threading.Lock()
    setup_job = {"status": "idle", "message": "尚未开始"}
    setup_lock = threading.Lock()

    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def public_job(job):
        return {key: value for key, value in job.items() if key != "zip_path"}

    def db():
        if "archive_db" not in g:
            uri = "file:%s?mode=ro" % app.config["ARCHIVE_DB"].replace("\\", "/")
            g.archive_db = sqlite3.connect(uri, uri=True)
            g.archive_db.row_factory = sqlite3.Row
            g.archive_db.execute("pragma query_only=on")
        return g.archive_db

    @app.teardown_appcontext
    def close_db(_error=None):
        con = g.pop("archive_db", None)
        if con is not None:
            con.close()

    @app.before_request
    def require_token():
        if request.path.startswith("/static/"):
            return None
        supplied = request.headers.get("X-Archive-Token") or request.args.get("token") or ""
        if not secrets.compare_digest(supplied, app.config["ACCESS_TOKEN"]):
            abort(401)
        if (request.path.startswith("/api/") and
                request.path != "/api/health" and
                not request.path.startswith("/api/setup/") and
                not app.config["ARCHIVE_READY"]):
            return jsonify({"error": "档案库尚未初始化"}), 409
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def account_id(value: str | None):
        if not value:
            row = db().execute("select id from accounts order by last_seen desc limit 1").fetchone()
        elif value.isdigit():
            row = db().execute("select id from accounts where id=?", (int(value),)).fetchone()
        else:
            row = db().execute("select id from accounts where wxid=?", (value,)).fetchone()
        return row[0] if row else None

    def bounded(name, default, maximum):
        try:
            return max(1, min(int(request.args.get(name, default)), maximum))
        except (TypeError, ValueError):
            return default

    def nonnegative(name, default=0):
        try:
            return max(0, int(request.args.get(name, default)))
        except (TypeError, ValueError):
            return default

    def attachment_map(message_ids):
        if not message_ids:
            return {}
        marks = ",".join("?" for _ in message_ids)
        rows = db().execute(
            "select id,message_id,kind,status,mime_type,size,path from attachments "
            "where message_id in (%s) order by id" % marks, tuple(message_ids)).fetchall()
        out = {}
        for row in rows:
            item = dict(row)
            item["url"] = "/api/attachments/%d/file" % row["id"]
            item.pop("path", None)
            out.setdefault(row["message_id"], []).append(item)
        return out

    def message_payload(rows):
        attachments = attachment_map([row["id"] for row in rows])
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["extra"] = json.loads(item.pop("extra_json") or "{}")
            except json.JSONDecodeError:
                item["extra"] = {}
            item["attachments"] = attachments.get(item["id"], [])
            result.append(item)
        return result

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "archive": os.path.basename(app.config["ARCHIVE_DB"]),
                        "archive_ready": app.config["ARCHIVE_READY"]})

    def setup_account(wxid=None):
        from . import paths as wx_paths
        accounts = wx_paths.list_account_dirs(app.config["DATA_ROOT"])
        selected = next((item for item in accounts
                         if item["wxid"] == wxid or item["dir_name"] == wxid), None)
        return accounts, selected or (accounts[0] if accounts else None)

    def setup_paths(account):
        root = Path(app.config["SETUP_ROOT"]).resolve()
        workspace = (root / account["wxid"]).resolve()
        try:
            workspace.relative_to(root)
        except ValueError:
            raise ValueError("账号工作目录超出设置范围")
        return workspace, workspace / "keys.json", workspace / "decrypted"

    def key_command(account, key_path):
        helper = Path(__file__).resolve().parent.parent / "tools" / "linux_key_helper.py"
        return shlex.join(["pkexec", sys.executable, str(helper), "--db-storage",
                           account["db_storage"], "--key-file", str(key_path)])

    def decrypted_count(path):
        if not path.is_dir():
            return 0
        return sum(1 for item in path.rglob("*.db") if item.is_file())

    @app.get("/api/setup/status")
    def setup_status():
        from . import decrypt as dec
        from . import key as keymod
        from . import paths as wx_paths
        accounts, selected = setup_account(request.args.get("account"))
        processes = keymod.find_weixin_processes()
        memory_readable = False
        for process in processes:
            try:
                fd = os.open("/proc/%d/mem" % process["pid"], os.O_RDONLY)
                os.close(fd)
                memory_readable = True
                break
            except OSError:
                continue
        items = [{"wxid": item["wxid"], "dir_name": item["dir_name"],
                  "database_count": len(dec.list_encrypted(item["db_storage"]))}
                 for item in accounts]
        details = None
        if selected:
            workspace, key_path, decrypted = setup_paths(selected)
            key_count = 0
            if key_path.is_file():
                try:
                    key_count = sum(1 for name in keymod.load_keys(str(key_path))
                                    if not name.startswith("_"))
                except (OSError, ValueError, json.JSONDecodeError):
                    key_count = 0
            decrypted_total = decrypted_count(decrypted)
            details = {
                "wxid": selected["wxid"], "database_count": len(dec.list_encrypted(selected["db_storage"])),
                "key_count": key_count, "decrypted_count": decrypted_total,
                "key_command": key_command(selected, key_path),
                "workspace": str(workspace),
            }
        archived = False
        if app.config["ARCHIVE_READY"] and selected:
            try:
                con = sqlite3.connect(
                    "file:%s?mode=ro" % app.config["ARCHIVE_DB"].replace("\\", "/"), uri=True)
                archived = bool(con.execute(
                    "select 1 from accounts where wxid=? limit 1", (selected["wxid"],)).fetchone())
                con.close()
            except sqlite3.Error:
                archived = False
        with setup_lock:
            job = dict(setup_job)
        return jsonify({
            "data_root": wx_paths.get_xwechat_root(app.config["DATA_ROOT"]),
            "accounts": items, "selected": details,
            "process_count": len(processes), "memory_readable": memory_readable,
            "archive_ready": app.config["ARCHIVE_READY"], "account_archived": archived,
            "job": job,
        })

    def run_setup_job(account):
        from . import decrypt as dec
        from . import key as keymod
        from .archive import open_archive
        from .reader import open_data

        def report(message):
            with setup_lock:
                setup_job["message"] = message

        try:
            workspace, key_path, decrypted = setup_paths(account)
            with setup_lock:
                setup_job.update(status="running", message="正在准备安全快照与解密",
                                 started_at=now())
            if decrypted_count(decrypted) == 0:
                payload = keymod.load_keys(str(key_path))
                keys = {name: value for name, value in payload.items()
                        if not name.startswith("_")}
                if not keys:
                    raise RuntimeError("密钥文件中没有可用密钥")
                result = dec.decrypt_all(keys, account["db_storage"], str(decrypted),
                                         progress=report)
                if not result["ok"]:
                    raise RuntimeError("没有数据库成功解密")
                if result["failed"]:
                    raise RuntimeError("有 %d 个数据库解密失败" % len(result["failed"]))
            report("正在建立统一索引并还原媒体")
            data = open_data(str(decrypted), account["wxid"], progress=report,
                             account_dir=account["path"])
            if not data.msg_dbs:
                raise RuntimeError("解密目录中没有消息数据库")
            media_root = str(Path(app.config["ARCHIVE_ROOT"]) / "media")
            with open_archive(app.config["ARCHIVE_DB"], progress=report) as archive:
                result = archive.sync(data, with_media=True, media_root=media_root)
            app.config["ARCHIVE_READY"] = True
            with setup_lock:
                setup_job.update(status="done", message="档案已可使用", finished_at=now(),
                                 report=result)
        except Exception as exc:  # noqa: BLE001 - 后台任务边界
            with setup_lock:
                setup_job.update(status="failed", message="初始化失败：%s" % exc,
                                 finished_at=now())

    @app.post("/api/setup/run")
    def setup_run():
        payload = request.get_json(silent=True) or {}
        _accounts, account = setup_account(str(payload.get("account") or ""))
        if not account:
            return jsonify({"error": "没有找到可用微信账号"}), 404
        workspace, key_path, _decrypted = setup_paths(account)
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(workspace, 0o700)
        except OSError:
            pass
        if not key_path.is_file() and decrypted_count(_decrypted) == 0:
            return jsonify({"error": "需要先完成一次受限权限取钥",
                            "key_command": key_command(account, key_path)}), 409
        with setup_lock:
            if setup_job.get("status") in ("queued", "running"):
                return jsonify({"error": "初始化任务正在运行"}), 409
            setup_job.clear()
            setup_job.update(status="queued", message="等待开始", account=account["wxid"],
                             created_at=now())
        worker = threading.Thread(target=run_setup_job, args=(account,), daemon=True)
        worker.start()
        return jsonify(dict(setup_job)), 202

    @app.get("/api/overview")
    def overview():
        con = db()
        counts = {}
        for table in ("accounts", "contacts", "sessions", "messages", "attachments"):
            counts[table] = con.execute("select count(*) from " + table).fetchone()[0]
        span = con.execute("select min(create_time),max(create_time) from messages").fetchone()
        types = [dict(row) for row in con.execute(
            "select kind,count(*) as count from messages group by kind order by count desc")]
        last_run = con.execute(
            "select started_at,finished_at,status,report_json from sync_runs "
            "order by id desc limit 1").fetchone()
        run = dict(last_run) if last_run else None
        if run:
            try:
                run["report"] = json.loads(run.pop("report_json") or "{}")
            except json.JSONDecodeError:
                run["report"] = {}
        return jsonify({"counts": counts, "time_start": span[0] or 0,
                        "time_end": span[1] or 0, "types": types, "last_run": run})

    @app.get("/api/accounts")
    def accounts():
        rows = db().execute(
            "select a.id,a.wxid,a.display_name,a.last_seen,"
            "(select count(*) from sessions s where s.account_id=a.id) as session_count,"
            "(select count(*) from messages m where m.account_id=a.id) as message_count "
            "from accounts a order by a.last_seen desc").fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/sessions")
    def sessions():
        aid = account_id(request.args.get("account"))
        if aid is None:
            return jsonify([])
        query = (request.args.get("q") or "").strip()
        limit = bounded("limit", 100, 300)
        offset = nonnegative("offset")
        sql = ("select username,display_name,summary,last_time,sort_time,is_group,message_count "
               "from sessions where account_id=?")
        params = [aid]
        if query:
            sql += " and (display_name like ? or username like ? or summary like ?)"
            like = "%%%s%%" % query
            params.extend([like, like, like])
        sql += " order by sort_time desc limit ? offset ?"
        params.extend([limit, offset])
        return jsonify([dict(row) for row in db().execute(sql, tuple(params))])

    @app.get("/api/directory")
    def directory():
        aid = account_id(request.args.get("account"))
        if aid is None:
            return jsonify({"items": [], "total": 0})
        category = request.args.get("category") or "contacts"
        query = (request.args.get("q") or "").strip()
        limit = bounded("limit", 100, 300)
        offset = nonnegative("offset")
        if category == "groups":
            where = "s.account_id=? and s.is_group=1"
            params = [aid]
            if query:
                where += " and (s.display_name like ? or s.username like ?)"
                like = "%%%s%%" % query
                params.extend([like, like])
            total = db().execute(
                "select count(*) from sessions s where " + where, tuple(params)).fetchone()[0]
            rows = db().execute(
                "select s.username,s.display_name,s.summary,s.last_time,s.message_count,1 as is_group,"
                "(select count(*) from room_members rm where rm.account_id=s.account_id "
                "and rm.room_username=s.username) as member_count "
                "from sessions s where " + where +
                " order by s.sort_time desc limit ? offset ?", tuple(params + [limit, offset]))
        else:
            where = "c.account_id=? and c.username not like '%@chatroom'"
            params = [aid]
            if query:
                where += (" and (c.remark like ? or c.nickname like ? or c.alias like ? "
                          "or c.username like ?)")
                like = "%%%s%%" % query
                params.extend([like, like, like, like])
            total = db().execute(
                "select count(*) from contacts c where " + where, tuple(params)).fetchone()[0]
            rows = db().execute(
                "select c.username,coalesce(nullif(c.remark,''),nullif(c.nickname,''),c.username) "
                "as display_name,c.alias,c.remark,c.nickname,c.description,c.local_type,0 as is_group,"
                "coalesce(s.message_count,0) as message_count,0 as member_count,s.last_time "
                "from contacts c left join sessions s on s.account_id=c.account_id "
                "and s.username=c.username where " + where +
                " order by (s.last_time is null),s.last_time desc,display_name limit ? offset ?",
                tuple(params + [limit, offset]))
        return jsonify({"items": [dict(row) for row in rows], "total": total,
                        "offset": offset, "limit": limit})

    @app.get("/api/timeline")
    def timeline():
        aid = account_id(request.args.get("account"))
        session = request.args.get("session") or ""
        if aid is None:
            return jsonify({"months": [], "time_start": 0, "time_end": 0})
        where = "account_id=?"
        params = [aid]
        if session:
            where += " and session_username=?"
            params.append(session)
        span = db().execute(
            "select min(create_time),max(create_time) from messages where " + where,
            tuple(params)).fetchone()
        rows = db().execute(
            "select strftime('%Y-%m',create_time,'unixepoch','localtime') as month,"
            "count(*) as count,min(create_time) as time_start,max(create_time) as time_end "
            "from messages where " + where + " group by month order by month desc",
            tuple(params)).fetchall()
        return jsonify({"months": [dict(row) for row in rows],
                        "time_start": span[0] or 0, "time_end": span[1] or 0})

    def word_frequencies(rows, limit=60):
        stop = {"这个", "那个", "就是", "还是", "可以", "不是", "什么", "怎么",
                "一个", "我们", "你们", "他们", "没有", "已经", "因为", "所以",
                "如果", "然后", "一下", "哈哈", "哈哈哈", "好的", "嗯嗯", "链接",
                "消息", "图片", "视频", "语音", "文件", "微信", "http", "https"}
        counts = Counter()
        for row in rows:
            text_value = (row[0] or "")[:1000]
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]{1,31}", text_value):
                token = token.lower().strip(".-_")
                if len(token) >= 2 and token not in stop:
                    counts[token] += 1
            for run in re.findall(r"[\u3400-\u9fff]{2,24}", text_value):
                if 2 <= len(run) <= 6 and run not in stop:
                    counts[run] += 2
                for size in (2, 3, 4):
                    for index in range(0, len(run) - size + 1):
                        token = run[index:index + size]
                        if token not in stop:
                            counts[token] += 1
        return [{"word": word, "count": count}
                for word, count in counts.most_common(limit) if count >= 2]

    @app.get("/api/analytics")
    def analytics():
        aid = account_id(request.args.get("account"))
        if aid is None:
            return jsonify({})
        years = [row[0] for row in db().execute(
            "select distinct strftime('%Y',create_time,'unixepoch','localtime') as year "
            "from messages where account_id=? and create_time>0 order by year desc", (aid,))]
        requested = request.args.get("year") or (years[0] if years else "")
        year = requested if re.fullmatch(r"(?:19|20|21)\d{2}", requested) else ""
        where = "m.account_id=?"
        params = [aid]
        if year:
            where += " and strftime('%Y',m.create_time,'unixepoch','localtime')=?"
            params.append(year)
        summary = db().execute(
            "select count(*) as messages,count(distinct m.session_username) as sessions,"
            "count(distinct date(m.create_time,'unixepoch','localtime')) as active_days,"
            "min(m.create_time) as first_time,max(m.create_time) as last_time,"
            "sum(case when m.is_sender=1 then 1 else 0 end) as sent,"
            "sum(case when m.is_sender=0 then 1 else 0 end) as received,"
            "sum(length(m.text)) as characters "
            "from messages m where " + where, tuple(params)).fetchone()
        first = db().execute(
            "select m.create_time,m.session_username,m.sender_name,m.text,"
            "coalesce(s.display_name,m.session_username) as session_name "
            "from messages m left join sessions s on s.account_id=m.account_id "
            "and s.username=m.session_username where " + where +
            " order by m.create_time,m.id limit 1", tuple(params)).fetchone()
        daily = [dict(row) for row in db().execute(
            "select date(m.create_time,'unixepoch','localtime') as day,count(*) as count "
            "from messages m where " + where + " group by day order by day", tuple(params))]
        monthly = [dict(row) for row in db().execute(
            "select strftime('%Y-%m',m.create_time,'unixepoch','localtime') as month,"
            "count(*) as count from messages m where " + where +
            " group by month order by month", tuple(params))]
        hourly = [dict(row) for row in db().execute(
            "select cast(strftime('%H',m.create_time,'unixepoch','localtime') as integer) "
            "as hour,count(*) as count from messages m where " + where +
            " group by hour order by hour", tuple(params))]
        types = [dict(row) for row in db().execute(
            "select m.kind,count(*) as count from messages m where " + where +
            " group by m.kind order by count desc", tuple(params))]
        top_sessions = [dict(row) for row in db().execute(
            "select m.session_username,coalesce(s.display_name,m.session_username) as display_name,"
            "s.is_group,count(*) as count,max(m.create_time) as last_time "
            "from messages m left join sessions s on s.account_id=m.account_id "
            "and s.username=m.session_username where " + where +
            " group by m.session_username order by count desc limit 12", tuple(params))]
        group_senders = [dict(row) for row in db().execute(
            "select m.sender_wxid,coalesce(nullif(m.sender_name,''),m.sender_wxid) as sender_name,"
            "count(*) as count from messages m where " + where +
            " and m.session_username like '%@chatroom' and m.is_sender=0 "
            "group by m.sender_wxid order by count desc limit 12", tuple(params))]
        text_rows = db().execute(
            "select m.text from messages m where " + where +
            " and m.kind in ('text','quote','link') and m.text<>'' limit 100000",
            tuple(params)).fetchall()
        return jsonify({
            "years": years, "year": year, "summary": dict(summary),
            "first": dict(first) if first else None, "daily": daily,
            "monthly": monthly, "hourly": hourly, "types": types,
            "top_sessions": top_sessions, "group_senders": group_senders,
            "words": word_frequencies(text_rows),
        })

    @app.get("/api/media")
    def media():
        aid = account_id(request.args.get("account"))
        if aid is None:
            return jsonify({"items": [], "total": 0})
        category = request.args.get("category") or "all"
        session = request.args.get("session") or ""
        start = nonnegative("start")
        end = nonnegative("end")
        limit = bounded("limit", 80, 200)
        offset = nonnegative("offset")
        kind_map = {
            "image": ("image",),
            "voice": ("voice_audio", "audio"),
            "video": ("video",),
            "emoji": ("emoji",),
            "file": ("file",),
        }
        where = "m.account_id=? and a.status='ready'"
        params = [aid]
        kinds = kind_map.get(category)
        if kinds:
            where += " and a.kind in (%s)" % ",".join("?" for _ in kinds)
            params.extend(kinds)
        else:
            where += " and a.kind not in ('image_thumbnail','video_poster')"
        if session:
            where += " and m.session_username=?"
            params.append(session)
        if start:
            where += " and m.create_time>=?"
            params.append(start)
        if end:
            where += " and m.create_time<?"
            params.append(end)
        base = " from attachments a join messages m on m.id=a.message_id "
        total = db().execute("select count(*)" + base + "where " + where,
                             tuple(params)).fetchone()[0]
        rows = db().execute(
            "select a.id,a.kind,a.mime_type,a.size,m.id as message_id,m.session_username,"
            "m.create_time,m.sender_name,m.text,s.display_name as session_name,"
            "(select t.id from attachments t where t.message_id=m.id and t.kind='image_thumbnail' "
            "order by t.id limit 1) as thumbnail_id,"
            "(select p.id from attachments p where p.message_id=m.id and p.kind='video_poster' "
            "order by p.id limit 1) as poster_id " + base +
            "left join sessions s on s.account_id=m.account_id and s.username=m.session_username "
            "where " + where + " order by m.create_time desc,a.id desc limit ? offset ?",
            tuple(params + [limit, offset])).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["url"] = "/api/attachments/%d/file" % row["id"]
            if item["thumbnail_id"]:
                item["thumbnail_url"] = "/api/attachments/%d/file" % item["thumbnail_id"]
            if item["poster_id"]:
                item["poster_url"] = "/api/attachments/%d/file" % item["poster_id"]
            items.append(item)
        return jsonify({"items": items, "total": total,
                        "offset": offset, "limit": limit})

    @app.get("/api/messages")
    def messages():
        aid = account_id(request.args.get("account"))
        session = request.args.get("session") or ""
        if aid is None or not session:
            return jsonify([])
        limit = bounded("limit", 80, 200)
        before = nonnegative("before")
        start = nonnegative("start")
        end = nonnegative("end")
        kinds = [kind for kind in (request.args.get("kinds") or "").split(",") if kind]
        sql = ("select id,server_id,local_id,msg_type,type_name,kind,create_time,sender_wxid,"
               "sender_name,is_sender,text,content,extra_json from messages "
               "where account_id=? and session_username=?")
        params = [aid, session]
        if before:
            sql += " and create_time<?"
            params.append(before)
        if start:
            sql += " and create_time>=?"
            params.append(start)
        if end:
            sql += " and create_time<?"
            params.append(end)
        if kinds:
            sql += " and kind in (%s)" % ",".join("?" for _ in kinds)
            params.extend(kinds)
        sql += " order by create_time desc,id desc limit ?"
        params.append(limit)
        rows = list(db().execute(sql, tuple(params)))
        rows.reverse()
        return jsonify(message_payload(rows))

    @app.get("/api/session-detail")
    def session_detail():
        aid = account_id(request.args.get("account"))
        username = request.args.get("session") or ""
        if aid is None or not username:
            abort(404)
        row = db().execute(
            "select username,display_name,summary,last_time,sort_time,is_group,message_count "
            "from sessions where account_id=? and username=?", (aid, username)).fetchone()
        if not row:
            abort(404)
        payload = dict(row)
        payload["members"] = [dict(item) for item in db().execute(
            "select rm.member_username,coalesce(nullif(c.remark,''),nullif(c.nickname,''),"
            "rm.member_username) as display_name from room_members rm left join contacts c "
            "on c.account_id=rm.account_id and c.username=rm.member_username "
            "where rm.account_id=? and rm.room_username=? order by display_name limit 100",
            (aid, username))]
        payload["type_counts"] = [dict(item) for item in db().execute(
            "select kind,count(*) as count from messages where account_id=? and session_username=? "
            "group by kind order by count desc", (aid, username))]
        return jsonify(payload)

    def search_rows(query, aid, limit):
        has_syntax = any(mark in query for mark in ('"', "*", "(", ")", " OR ", " AND ", " NOT "))
        fts_query = query if has_syntax else " AND ".join(
            '"%s"*' % token.replace('"', '""') for token in query.split())
        sql = ("select m.id,m.session_username,m.sender_name,m.create_time,m.kind,m.text,"
               "snippet(messages_fts,0,'[',']','…',18) as snippet from messages_fts "
               "join messages m on m.id=messages_fts.rowid where messages_fts match ?")
        params = [fts_query]
        if aid is not None:
            sql += " and messages_fts.account_id=?"
            params.append(aid)
        sql += " order by rank limit ?"
        params.append(limit)
        try:
            rows = list(db().execute(sql, tuple(params)))
        except sqlite3.Error:
            rows = []
        if rows:
            return rows
        sql = ("select id,session_username,sender_name,create_time,kind,text,text as snippet "
               "from messages where (text like ? or content like ?)")
        params = ["%%%s%%" % query, "%%%s%%" % query]
        if aid is not None:
            sql += " and account_id=?"
            params.append(aid)
        sql += " order by create_time desc limit ?"
        params.append(limit)
        return list(db().execute(sql, tuple(params)))

    @app.get("/api/search")
    def search():
        query = (request.args.get("q") or "").strip()
        if not query:
            return jsonify([])
        aid = account_id(request.args.get("account")) if request.args.get("account") else None
        rows = search_rows(query, aid, bounded("limit", 80, 200))
        return jsonify([dict(row) for row in rows])

    def run_export_job(job_id, aid, session, fmt):
        from .archive_export import export_archive_bundle
        with export_lock:
            export_jobs[job_id].update(status="running", message="正在读取档案",
                                       started_at=now())

        def report(message):
            with export_lock:
                export_jobs[job_id]["message"] = message

        try:
            result = export_archive_bundle(
                app.config["ARCHIVE_DB"], aid, session, fmt,
                str(export_root / job_id), progress=report)
            with export_lock:
                export_jobs[job_id].update(
                    status="done", message="导出完成", finished_at=now(),
                    message_count=result["message_count"],
                    attachment_count=result["attachment_count"],
                    session_count=result.get("session_count", 0),
                    contact_count=result.get("contact_count", 0),
                    size=result["zip_size"], zip_path=result["zip_path"])
        except Exception as exc:  # noqa: BLE001 - 任务边界需要转为可见状态
            with export_lock:
                export_jobs[job_id].update(
                    status="failed", message="导出失败：%s" % exc, finished_at=now())

    @app.get("/api/exports")
    def exports():
        with export_lock:
            rows = [public_job(dict(job)) for job in export_jobs.values()]
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return jsonify(rows)

    @app.post("/api/exports")
    def create_export():
        payload = request.get_json(silent=True) or {}
        aid = account_id(str(payload.get("account") or ""))
        session = str(payload.get("session") or "").strip()
        fmt = str(payload.get("format") or "html").lower()
        formats = ("html", "md", "json", "csv", "txt", "sqlite",
                   "contacts_csv", "messages_csv")
        if fmt in ("sqlite", "contacts_csv", "messages_csv"):
            session = "*"
        if aid is None or not session or fmt not in formats:
            return jsonify({"error": "账号、会话或格式无效"}), 400
        if session == "*":
            row = db().execute(
                "select display_name,(select count(*) from messages where account_id=?) "
                "as message_count from accounts where id=?", (aid, aid)).fetchone()
        else:
            row = db().execute(
                "select display_name,message_count from sessions where account_id=? and username=?",
                (aid, session)).fetchone()
        if not row:
            return jsonify({"error": "会话不存在"}), 404
        job_id = secrets.token_hex(8)
        job = {
            "id": job_id, "account_id": aid, "session": session,
            "display_name": row["display_name"] or session, "format": fmt,
            "status": "queued", "message": "等待开始", "created_at": now(),
            "started_at": None, "finished_at": None, "message_count": 0,
            "attachment_count": 0, "session_count": 0, "contact_count": 0, "size": 0,
        }
        with export_lock:
            export_jobs[job_id] = job
        worker = threading.Thread(target=run_export_job,
                                  args=(job_id, aid, session, fmt), daemon=True)
        worker.start()
        return jsonify(public_job(job)), 202

    @app.get("/api/exports/<job_id>/download")
    def download_export(job_id):
        with export_lock:
            job = dict(export_jobs.get(job_id) or {})
        if not job:
            abort(404)
        if job.get("status") != "done" or not job.get("zip_path"):
            return jsonify({"error": "导出尚未完成"}), 409
        path = Path(job["zip_path"]).resolve()
        try:
            path.relative_to(export_root.resolve())
        except ValueError:
            abort(403)
        if not path.is_file():
            abort(404)
        filename = "%s_%s.zip" % (job["display_name"], job["format"])
        return send_file(path, as_attachment=True, download_name=filename,
                         mimetype="application/zip", conditional=True)

    @app.get("/api/attachments/<int:attachment_id>/file")
    def attachment_file(attachment_id):
        row = db().execute(
            "select path,mime_type from attachments where id=?", (attachment_id,)).fetchone()
        if not row:
            abort(404)
        root = Path(app.config["ARCHIVE_ROOT"]).resolve()
        path = (root / row["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            abort(403)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype=row["mime_type"] or None,
                         conditional=True, max_age=3600)

    return app


def run_server(archive_db: str, host="127.0.0.1", port=8765,
               token: str | None = None, debug=False,
               data_root: str | None = None, setup_root: str | None = None):
    app = create_app(archive_db, token=token, data_root=data_root,
                     setup_root=setup_root)
    access_token = app.config["ACCESS_TOKEN"]
    url = "http://%s:%d/?token=%s" % (host, port, access_token)
    print("\n本地知识库：%s\n" % url, flush=True)
    # Werkzeug 默认会把完整查询串写入请求日志；首次地址中的访问令牌不能落日志。
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host=host, port=port, debug=debug, use_reloader=False)
