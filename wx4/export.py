# -*- coding: utf-8 -*-
"""导出聊天记录：TXT / CSV / JSON / HTML。"""
from __future__ import annotations

import csv
import html
import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from .reader import Wx4Data
from .security import private_dir, private_file

INVALID_FS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def csv_safe(value):
    """阻止聊天文本在 Excel/LibreOffice 中被解释为公式。"""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n")
    return "'" + value if stripped.startswith(("=", "+", "-", "@")) else value


def safe_name(name: str, maxlen: int = 60) -> str:
    name = INVALID_FS.sub("_", (name or "").strip())
    name = name.strip(" .")
    if not name:
        name = "unknown"
    return name[:maxlen]


def _ensure(path: str) -> str:
    return private_dir(path)


def _media_line(m: dict, base_dir: str | None) -> str:
    """给一条消息附上媒体文件清单（相对路径）。"""
    items = []
    for p in (m.get("extra") or {}).get("images", []) or []:
        items.append(os.path.relpath(p, base_dir) if base_dir else os.path.basename(p))
    v = (m.get("extra") or {}).get("voice_file")
    if v:
        items.append(os.path.relpath(v, base_dir) if base_dir else os.path.basename(v))
    for key in ("audio_file", "video_file", "video_poster"):
        value = (m.get("extra") or {}).get(key)
        if value:
            items.append(os.path.relpath(value, base_dir) if base_dir else os.path.basename(value))
    for key in ("files", "emoji_files"):
        for value in (m.get("extra") or {}).get(key, []) or []:
            items.append(os.path.relpath(value, base_dir) if base_dir else os.path.basename(value))
    return "  [附件] " + " ; ".join(items) if items else ""


def _collect_media(data: Wx4Data, session: str, msgs: list, media_root: str,
                   aes_key=None, copy_dat: bool = True, progress=None) -> None:
    from . import media as media_mod
    media_mod.export_session_media(data, session, msgs,
                                   os.path.join(media_root, safe_name(
                                       data.session_display(session))),
                                   aes_key=aes_key, copy_dat=copy_dat,
                                   progress=progress)


# ---------------------------------------------------------------------------
# 单个会话
# ---------------------------------------------------------------------------
def export_txt(data: Wx4Data, session: str, out_path: str,
               media_base: str | None = None, msgs: list | None = None) -> str:
    msgs = data.messages(session) if msgs is None else msgs
    private_dir(os.path.dirname(os.path.abspath(out_path)) or ".")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("会话：%s (%s)\n" % (data.session_display(session), session))
        f.write("导出时间：%s  共 %d 条消息\n"
                % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(msgs)))
        f.write("=" * 70 + "\n\n")
        for m in msgs:
            who = "我" if m["is_sender"] else m["sender"]
            f.write("[%s] %s\n%s\n" % (m["time_str"], who, m["text"]))
            line = _media_line(m, media_base)
            if line:
                f.write(line + "\n")
            f.write("\n")
    return private_file(out_path)


def export_csv(data: Wx4Data, session: str, out_path: str,
               msgs: list | None = None) -> str:
    msgs = data.messages(session) if msgs is None else msgs
    private_dir(os.path.dirname(os.path.abspath(out_path)) or ".")
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "会话", "发送者", "是否我发送", "类型", "内容",
                    "附件", "server_id", "local_id", "分片"])
        for m in msgs:
            w.writerow([csv_safe(m["time_str"]), csv_safe(data.session_display(session)), csv_safe(m["sender"]),
                        "是" if m["is_sender"] else "否", m["type_name"],
                        csv_safe(m["text"]), csv_safe(_media_line(m, os.path.dirname(out_path)).removeprefix("  [附件] ")),
                        m["server_id"], m["local_id"], m.get("shard", "")])
    return private_file(out_path)


def export_json(data: Wx4Data, session: str, out_path: str,
                msgs: list | None = None) -> str:
    msgs = data.messages(session) if msgs is None else msgs
    payload = {
        "session": session,
        "display": data.session_display(session),
        "wxid": data.wxid,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(msgs),
        "messages": [_jsonable(m) for m in msgs],
    }
    private_dir(os.path.dirname(os.path.abspath(out_path)) or ".")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return private_file(out_path)


def _jsonable(m: dict) -> dict:
    """把消息里的 bytes 换成字符串，保证 JSON 可序列化。"""
    out = {}
    for k, v in m.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = bytes(v).hex()
        elif isinstance(v, dict):
            out[k] = {kk: (vv.hex() if isinstance(vv, (bytes, bytearray)) else vv)
                      for kk, vv in v.items()}
        else:
            out[k] = v
    return out


HTML_CSS = """
body{font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;background:#ededed;
     margin:0;padding:0 0 40px;}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:12px 20px;
       z-index:10;}
header h1{font-size:17px;margin:0 0 4px;}
header .meta{font-size:12px;color:#888;}
.tools{margin-top:8px;}
.tools input{padding:6px 10px;border:1px solid #ccc;border-radius:6px;width:260px;font-size:13px;}
.tools button{padding:6px 12px;border:1px solid #ccc;background:#f7f7f7;border-radius:6px;
              cursor:pointer;font-size:13px;}
.tools button:hover{background:#eee;}
main{padding:16px 20px;max-width:900px;margin:0 auto;}
.msg{display:flex;margin:10px 0;gap:8px;}
.msg .who{flex:0 0 44px;height:44px;border-radius:6px;background:#c9c9c9;color:#fff;
          font-size:11px;display:flex;align-items:center;justify-content:center;overflow:hidden;}
.msg .who img{width:100%;height:100%;object-fit:cover;}
.bub{background:#fff;border-radius:8px;padding:8px 12px;max-width:640px;font-size:14px;
     line-height:1.55;white-space:pre-wrap;word-break:break-word;}
.msg .name{font-size:11px;color:#999;margin-bottom:3px;}
.msg .time{font-size:11px;color:#b0b0b0;margin-top:4px;}
.msg.me{flex-direction:row-reverse;}
.msg.me .bub{background:#95ec69;}
.msg.me .name,.msg.me .time{text-align:right;}
.sys{text-align:center;color:#999;font-size:12px;margin:12px 0;}
.attimg{max-width:260px;max-height:260px;border-radius:6px;display:block;margin-top:4px;}
.att{font-size:12px;color:#666;margin-top:4px;}
.att a{color:#1a73e8;}
.media-audio{display:block;width:min(360px,72vw);margin:4px 0;}
.media-video{display:block;width:min(520px,76vw);max-height:68vh;border-radius:8px;background:#111;}
.emoji{width:28px;height:28px;object-fit:contain;vertical-align:middle;margin:0 1px;}
.linkcard{display:block;min-width:260px;max-width:520px;padding:10px 12px;border-radius:7px;
          background:rgba(255,255,255,.72);color:#202124;text-decoration:none;border:1px solid #e3e5e7;}
.linkcard:hover{border-color:#9aa0a6;box-shadow:0 2px 8px rgba(0,0,0,.08);}
.linktitle{font-weight:600;margin-bottom:3px;}.linkdesc,.linkhost{font-size:12px;color:#73777d;}
.quote{margin:7px 0 0;padding:7px 9px;border-left:3px solid #aeb4ba;background:rgba(0,0,0,.05);
       color:#61666d;font-size:12px;white-space:pre-wrap;}
.quote b{display:block;color:#45494e;margin-bottom:2px;}
.hidden{display:none;}
"""

HTML_JS = """
function flt(){var q=document.getElementById('q').value.trim().toLowerCase();
 var n=0;document.querySelectorAll('main .msg').forEach(function(e){
  var hit=!q||e.dataset.t.toLowerCase().indexOf(q)>=0;
  e.classList.toggle('hidden',!hit);if(hit)n++;});
 document.getElementById('cnt').textContent='显示 '+n+' 条';}
function toggleTime(){document.querySelectorAll('.time').forEach(function(e){
  e.classList.toggle('hidden');});}
"""


def _avatar_html(data: Wx4Data, username: str, label: str) -> str:
    url = data.avatar_of(username)
    # 离线导出不应在打开时向微信 CDN 泄露访问行为。只接受
    # 相对路径或已内联的图片，远程头像回退到文字占位。
    lower = (url or "").strip().lower()
    local_url = bool(url) and (lower.startswith("data:image/") or
                               not (lower.startswith(("http://", "https://", "//")) or
                                    ":" in lower.split("/", 1)[0]))
    if local_url:
        return ('<div class="who"><img loading="lazy" src="%s" alt=""></div>'
                % html.escape(url, quote=True))
    return '<div class="who">%s</div>' % html.escape(label[:2])


def _media_html(m: dict, base_dir: str | None) -> str:
    """把已导出的图片/语音/音频/视频嵌进气泡。"""
    if not base_dir:
        return ""
    out = []
    thumbs = (m.get("extra") or {}).get("image_thumbs", {}) or {}
    for p in (m.get("extra") or {}).get("images", []) or []:
        rel = os.path.relpath(p, base_dir).replace("\\", "/")
        if rel.lower().endswith(".dat"):
            out.append("<div class='att'>[图片未解密] <a href='%s'>%s</a></div>"
                       % (html.escape(rel, quote=True), html.escape(os.path.basename(rel))))
        else:
            thumb = thumbs.get(p) or p
            thumb_rel = os.path.relpath(thumb, base_dir).replace("\\", "/")
            out.append("<a href='%s'><img class='attimg' loading='lazy' src='%s'></a>"
                       % (html.escape(rel, quote=True), html.escape(thumb_rel, quote=True)))
    v = (m.get("extra") or {}).get("voice_file")
    if v:
        rel = os.path.relpath(v, base_dir).replace("\\", "/")
        out.append("<audio class='media-audio' controls preload='metadata' src='%s'>"
                   "<a href='%s'>下载语音</a></audio>"
                   % (html.escape(rel, quote=True), html.escape(rel, quote=True)))
    a = (m.get("extra") or {}).get("audio_file")
    if a:
        rel = os.path.relpath(a, base_dir).replace("\\", "/")
        out.append("<audio class='media-audio' controls preload='metadata' src='%s'>"
                   "<a href='%s'>下载音频</a></audio>"
                   % (html.escape(rel, quote=True), html.escape(rel, quote=True)))
    video = (m.get("extra") or {}).get("video_file")
    if video:
        rel = os.path.relpath(video, base_dir).replace("\\", "/")
        poster = (m.get("extra") or {}).get("video_poster")
        poster_attr = ""
        if poster:
            poster_rel = os.path.relpath(poster, base_dir).replace("\\", "/")
            poster_attr = " poster='%s'" % html.escape(poster_rel, quote=True)
        out.append("<video class='media-video' controls preload='metadata'%s>"
                   "<source src='%s' type='video/mp4'><a href='%s'>下载视频</a></video>"
                   % (poster_attr, html.escape(rel, quote=True),
                      html.escape(rel, quote=True)))
    for p in (m.get("extra") or {}).get("files", []) or []:
        rel = os.path.relpath(p, base_dir).replace("\\", "/")
        out.append("<div class='att'><a href='%s' download>%s</a></div>" %
                   (html.escape(rel, quote=True), html.escape(os.path.basename(p))))
    for p in (m.get("extra") or {}).get("emoji_files", []) or []:
        rel = os.path.relpath(p, base_dir).replace("\\", "/")
        out.append("<img class='emoji' style='width:72px;height:72px' loading='lazy' "
                   "src='%s' alt='表情'>" % html.escape(rel, quote=True))
    return "".join(out)


def _safe_http_url(value: str) -> str:
    value = (value or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    return value if parsed.scheme in ("http", "https") and parsed.netloc else ""


def _text_with_emojis(m: dict, base_dir: str | None) -> str:
    text = m.get("content") or m.get("text") or ""
    tokens = (m.get("extra") or {}).get("emoji_tokens", {}) or {}
    if not base_dir or not tokens:
        return html.escape(text)
    pattern = "(" + "|".join(re.escape(k) for k in sorted(tokens, key=len, reverse=True)) + ")"
    out = []
    for part in re.split(pattern, text):
        path = tokens.get(part)
        if not path:
            out.append(html.escape(part))
            continue
        rel = os.path.relpath(path, base_dir).replace("\\", "/")
        out.append("<img class='emoji' loading='lazy' src='%s' alt='%s' title='%s'>" %
                   (html.escape(rel, quote=True), html.escape(part, quote=True),
                    html.escape(part, quote=True)))
    return "".join(out)


def _message_body_html(m: dict, base_dir: str | None) -> str:
    extra = m.get("extra") or {}
    media = _media_html(m, base_dir)
    if m.get("kind") == "link":
        url = _safe_http_url(extra.get("url", ""))
        if url:
            title = extra.get("title") or url
            desc = extra.get("description") or ""
            host = urlparse(url).netloc
            return ("<a class='linkcard' href='%s' target='_blank' rel='noopener noreferrer'>"
                    "<div class='linktitle'>%s</div><div class='linkdesc'>%s</div>"
                    "<div class='linkhost'>%s</div></a>" %
                    (html.escape(url, quote=True), html.escape(title), html.escape(desc),
                     html.escape(host)))
    if m.get("kind") == "quote":
        reply = extra.get("reply_text") or m.get("text") or "[引用消息]"
        name = extra.get("refer_name") or extra.get("refer_from") or "原消息"
        quoted = extra.get("refer_text") or ""
        return ("%s<blockquote class='quote'><b>%s</b>%s</blockquote>%s" %
                (html.escape(reply), html.escape(name), html.escape(quoted), media))
    if m.get("kind") == "text":
        return _text_with_emojis(m, base_dir) + media
    return media or html.escape(m.get("text") or "")


def _md_escape(value: str) -> str:
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", value or "")


def _message_body_markdown(m: dict, base_dir: str | None) -> str:
    extra = m.get("extra") or {}
    lines = []
    for path in extra.get("images", []) or []:
        rel = os.path.relpath(path, base_dir).replace("\\", "/") if base_dir else os.path.basename(path)
        thumb = (extra.get("image_thumbs") or {}).get(path) or path
        thumb_rel = (os.path.relpath(thumb, base_dir).replace("\\", "/")
                     if base_dir else os.path.basename(thumb))
        if path.lower().endswith(".dat"):
            lines.append("[图片未解密](%s)" % rel)
        else:
            lines.append("[![图片](%s)](%s)" % (thumb_rel, rel))
    for key, label in (("voice_file", "语音"), ("audio_file", "音频")):
        path = extra.get(key)
        if path:
            rel = os.path.relpath(path, base_dir).replace("\\", "/") if base_dir else os.path.basename(path)
            lines.append('<audio controls preload="metadata" src="%s">%s</audio>' %
                         (html.escape(rel, quote=True), label))
    video = extra.get("video_file")
    if video:
        rel = os.path.relpath(video, base_dir).replace("\\", "/") if base_dir else os.path.basename(video)
        poster = extra.get("video_poster")
        poster_attr = ""
        if poster:
            poster_rel = (os.path.relpath(poster, base_dir).replace("\\", "/")
                          if base_dir else os.path.basename(poster))
            poster_attr = ' poster="%s"' % html.escape(poster_rel, quote=True)
        lines.append('<video controls preload="metadata"%s src="%s">视频</video>' %
                     (poster_attr, html.escape(rel, quote=True)))
    for path in extra.get("files", []) or []:
        rel = os.path.relpath(path, base_dir).replace("\\", "/") if base_dir else os.path.basename(path)
        lines.append("[📎 %s](%s)" % (_md_escape(os.path.basename(path)), rel))
    for path in extra.get("emoji_files", []) or []:
        rel = os.path.relpath(path, base_dir).replace("\\", "/") if base_dir else os.path.basename(path)
        lines.append('<img src="%s" alt="表情" width="72">' % html.escape(rel, quote=True))

    kind = m.get("kind")
    if kind == "link":
        url = _safe_http_url(extra.get("url", ""))
        if url:
            lines.insert(0, "[%s](%s)" % (_md_escape(extra.get("title") or url), url))
    elif kind == "quote":
        reply = _md_escape(extra.get("reply_text") or m.get("text") or "[引用消息]")
        name = _md_escape(extra.get("refer_name") or extra.get("refer_from") or "原消息")
        quoted = _md_escape(extra.get("refer_text") or "")
        lines.insert(0, "%s\n\n> **%s**\n> %s" % (reply, name, quoted))
    elif kind == "text":
        content = m.get("content") or m.get("text") or ""
        tokens = extra.get("emoji_tokens", {}) or {}
        if tokens and base_dir:
            pattern = "(" + "|".join(re.escape(k) for k in sorted(tokens, key=len, reverse=True)) + ")"
            rendered = []
            for part in re.split(pattern, content):
                path = tokens.get(part)
                if path:
                    rel = os.path.relpath(path, base_dir).replace("\\", "/")
                    rendered.append('<img src="%s" alt="%s" width="28">' %
                                    (html.escape(rel, quote=True), html.escape(part, quote=True)))
                else:
                    rendered.append(_md_escape(part))
            lines.insert(0, "".join(rendered))
        else:
            lines.insert(0, _md_escape(content))
    elif not lines:
        lines.append(_md_escape(m.get("text") or ""))
    return "\n\n".join(line for line in lines if line)


def export_markdown(data: Wx4Data, session: str, out_path: str,
                    media_base: str | None = None, msgs: list | None = None) -> str:
    msgs = data.messages(session) if msgs is None else msgs
    display = data.session_display(session)
    out = ["# %s" % _md_escape(display), "",
           "> 会话：`%s`  " % session,
           "> 导出时间：%s  " % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "> 消息数：%d" % len(msgs), ""]
    for m in msgs:
        if m.get("kind") == "system":
            out.extend(["> 系统：%s" % _md_escape(m.get("text") or ""), ""])
            continue
        who = "我" if m.get("is_sender") else (m.get("sender") or "")
        out.extend(["## %s · %s" % (_md_escape(who), m.get("time_str") or ""), "",
                    _message_body_markdown(m, media_base), ""])
    private_dir(os.path.dirname(os.path.abspath(out_path)) or ".")
    with open(out_path, "w", encoding="utf-8") as stream:
        stream.write("\n".join(out).rstrip() + "\n")
    return private_file(out_path)


def export_html(data: Wx4Data, session: str, out_path: str,
                include_time: bool = True, media_base: str | None = None,
                msgs: list | None = None) -> str:
    msgs = data.messages(session) if msgs is None else msgs
    display = data.session_display(session)
    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>%s - 聊天记录</title>" % html.escape(display),
        "<style>%s</style></head><body>" % HTML_CSS,
        "<header><h1>%s</h1>" % html.escape(display),
        "<div class='meta'>%s · 共 %d 条 · 导出 %s</div>"
        % (html.escape(session), len(msgs),
           datetime.now().strftime("%Y-%m-%d %H:%M")),
        "<div class='tools'><input id='q' placeholder='搜索消息内容…' oninput='flt()'> "
        "<button onclick='flt()'>搜索</button> "
        "<button onclick='toggleTime()'>显示/隐藏时间</button> "
        "<span class='meta' id='cnt'></span></div></header><main>",
    ]
    for m in msgs:
        if m["kind"] == "system":
            parts.append("<div class='msg' data-t='%s'><div class='sys'>%s</div></div>"
                         % (html.escape(m["text"].lower()),
                            html.escape(m["text"])))
            continue
        cls = "msg me" if m["is_sender"] else "msg"
        who = "我" if m["is_sender"] else m["sender"]
        av = _avatar_html(data, m["sender_wxid"] or session, who)
        tm = ("<div class='time'%s>%s</div>"
              % ("" if include_time else " style='display:none'", m["time_str"]))
        body = _message_body_html(m, media_base)
        parts.append(
            "<div class='%s' data-t='%s'>%s<div><div class='name'>%s</div>"
            "<div class='bub'>%s</div>%s</div></div>"
            % (cls, html.escape(m["text"].lower()), av, html.escape(who),
               body, tm))
    parts.append("</main><script>%s</script></body></html>" % HTML_JS)
    private_dir(os.path.dirname(os.path.abspath(out_path)) or ".")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return private_file(out_path)


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------
EXPORTERS = {
    "txt": export_txt,
    "csv": export_csv,
    "json": export_json,
    "html": export_html,
    "md": export_markdown,
}


def export_all(data: Wx4Data, out_dir: str, fmt: str = "html",
               sessions: list[str] | None = None, progress=None,
               only_with_messages: bool = True, media: bool = False,
               copy_dat: bool = True, aes_key=None) -> dict:
    """把（选定的）会话逐个导出。

    media=True 时额外把图片/语音导出到 <out_dir>/media/<会话>/ 下，
    HTML/TXT 里会带上附件链接。
    """
    progress = progress or (lambda m: None)
    _ensure(out_dir)
    if sessions is None:
        sessions = [s["username"] for s in data.sessions()]
    if only_with_messages:
        sessions = [s for s in sessions if data.has_messages(s)]
    media_root = os.path.join(out_dir, "media") if media else None
    if media:
        _ensure(media_root)
    done, empty = [], []
    for i, s in enumerate(sessions, 1):
        msgs = data.messages(s)
        if not msgs:
            empty.append(s)
            continue
        display = safe_name(data.session_display(s))
        fname = "%s_%s.%s" % (display, s[:24].replace("@", "_at_"), fmt)
        path = os.path.join(out_dir, fname)
        try:
            if media:
                _collect_media(data, s, msgs, media_root, aes_key=aes_key,
                               copy_dat=copy_dat, progress=progress)
            if fmt == "html":
                export_html(data, s, path,
                            media_base=(out_dir if media else None), msgs=msgs)
            elif fmt == "md":
                export_markdown(data, s, path,
                                media_base=(out_dir if media else None), msgs=msgs)
            elif fmt == "txt":
                export_txt(data, s, path,
                           media_base=(out_dir if media else None), msgs=msgs)
            else:
                EXPORTERS[fmt](data, s, path, msgs=msgs)
            done.append(path)
            progress("[%d/%d] 会话导出完成" % (i, len(sessions)))
        except Exception as e:                            # noqa: BLE001
            progress("[%d/%d] 会话导出失败：%s" % (i, len(sessions), e))
    progress("完成：%d 个会话已导出，%d 个无消息" % (len(done), len(empty)))
    return {"exported": done, "empty": empty, "out_dir": out_dir}


def export_index(data: Wx4Data, out_dir: str, fmt: str = "html",
                 progress=None) -> str:
    """生成会话索引页，方便逐条点开。"""
    sessions = [s for s in data.sessions() if data.has_messages(s["username"])]
    rows = []
    for s in sessions:
        display = safe_name(data.session_display(s["username"]))
        fname = "%s_%s.%s" % (display, s["username"][:24].replace("@", "_at_"), fmt)
        t = (datetime.fromtimestamp(s["sort_time"]).strftime("%Y-%m-%d %H:%M")
             if s["sort_time"] else "")
        rows.append("<tr><td><a href='%s'>%s</a></td><td>%s</td><td>%s</td>"
                    "<td>%s</td></tr>"
                    % (html.escape(fname), html.escape(s["display"]),
                       html.escape(s["username"]), html.escape(t),
                       html.escape(s["summary"][:60])))
    doc = ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
           "<title>聊天记录索引</title><style>body{font-family:'Microsoft YaHei',"
           "sans-serif;padding:20px;}table{border-collapse:collapse;width:100%;}"
           "td,th{border-bottom:1px solid #eee;padding:6px 10px;font-size:13px;"
           "text-align:left;}a{color:#1a73e8;text-decoration:none;}</style></head>"
           "<body><h2>聊天记录索引（" + str(len(rows)) + " 个会话）</h2>"
           "<table><tr><th>会话</th><th>wxid</th><th>最后消息时间</th>"
           "<th>摘要</th></tr>" + "".join(rows) + "</table></body></html>")
    path = os.path.join(out_dir, "index.html")
    private_dir(out_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    if progress:
        progress("会话索引页已生成")
    return private_file(path)
