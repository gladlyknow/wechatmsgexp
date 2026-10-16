#!/usr/bin/env python3
"""审计私有产物权限、ZIP 路径和离线页面的主动远程资源。"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


class ResourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.remote = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        candidates = []
        if tag in ("script", "img", "audio", "video", "source", "iframe"):
            candidates.extend(values.get(name, "") for name in ("src", "poster"))
        elif tag == "link" and "stylesheet" in (values.get("rel") or "").split():
            candidates.append(values.get("href", ""))
        for value in candidates:
            if urlparse(value).scheme in ("http", "https"):
                self.remote.append(value)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def audit_private_root(root: Path, report: dict):
    root = root.resolve()
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            report["errors"].append("私有目录中存在符号链接: %s" % path)
            continue
        if path.is_dir():
            report["directories"] += 1
            if _mode(path) & 0o077:
                report["errors"].append("目录权限过宽 %03o: %s" % (_mode(path), path))
        elif path.is_file():
            report["files"] += 1
            if _mode(path) & 0o077:
                report["errors"].append("文件权限过宽 %03o: %s" % (_mode(path), path))


def audit_html(name: str, content: bytes, report: dict):
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        report["errors"].append("页面不是 UTF-8: %s" % name)
        return
    parser = ResourceParser()
    parser.feed(text)
    for value in parser.remote:
        report["errors"].append("页面含主动远程资源 %s: %s" % (name, value))
    if re.search(r"(?:fetch|XMLHttpRequest|WebSocket)\s*\([^)]*https?://", text):
        report["errors"].append("页面脚本可能主动联网: %s" % name)
    report["offline_pages"] += 1


def safe_member(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts


def audit_export(path: Path, report: dict):
    sensitive_names = {"keys.json", "wx4_keys.json", "key.json"}
    if path.is_dir():
        entries = []
        for item in path.rglob("*"):
            if item.is_file():
                name = item.relative_to(path).as_posix()
                content = item.read_bytes() if name.lower().endswith((".html", ".htm")) else b""
                entries.append((name, content))
    elif zipfile.is_zipfile(path):
        entries = []
        with zipfile.ZipFile(path) as bundle:
            for info in bundle.infolist():
                if not safe_member(info.filename):
                    report["errors"].append("ZIP 路径越界: %s:%s" % (path, info.filename))
                    continue
                if not info.is_dir():
                    content = (bundle.read(info)
                               if info.filename.lower().endswith((".html", ".htm")) else b"")
                    entries.append((info.filename, content))
    else:
        report["errors"].append("不是目录或 ZIP: %s" % path)
        return
    report["exports"] += 1
    for name, content in entries:
        report["export_entries"] += 1
        if PurePosixPath(name).name.lower() in sensitive_names:
            report["errors"].append("导出包包含密钥文件: %s:%s" % (path, name))
        if name.lower().endswith((".html", ".htm")):
            audit_html("%s:%s" % (path, name), content, report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, action="append", default=[])
    parser.add_argument("--export", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    report = {"private_roots": len(args.private_root), "exports": 0,
              "directories": 0, "files": 0, "export_entries": 0,
              "offline_pages": 0, "errors": []}
    for root in args.private_root:
        if not root.is_dir():
            report["errors"].append("私有目录不存在: %s" % root)
        else:
            audit_private_root(root, report)
    for path in args.export:
        audit_export(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
