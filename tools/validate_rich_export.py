#!/usr/bin/env python3
"""验证微信 HTML 导出的富媒体是真实、本地、可解码且可交互的。"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image


class ExportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.emojis = []
        self.audios = []
        self.videos = []
        self.video_sources = []
        self.links = []
        self.image_links = []
        self._anchor = None

    @staticmethod
    def _attrs(attrs):
        return {key: (value or "") for key, value in attrs}

    def handle_starttag(self, tag, attrs):
        attrs = self._attrs(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "a":
            self._anchor = attrs
            if "linkcard" in classes:
                self.links.append(attrs)
        elif tag == "img":
            if "attimg" in classes:
                self.images.append(attrs)
                if self._anchor:
                    self.image_links.append(self._anchor)
            if "emoji" in classes or attrs.get("alt") == "表情":
                self.emojis.append(attrs)
        elif tag == "audio":
            self.audios.append(attrs)
        elif tag == "video":
            self.videos.append(attrs)
        elif tag == "source":
            self.video_sources.append(attrs)

    def handle_endtag(self, tag):
        if tag == "a":
            self._anchor = None


def _local(html_path: Path, value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https", "data"):
        return None
    return (html_path.parent / unquote(parsed.path)).resolve()


def _probe(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels",
        "-of", "json", str(path),
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return json.loads(result.stdout)


def validate(paths: list[Path]) -> dict:
    result = {
        "documents": len(paths),
        "html_files": sum(path.suffix.lower() == ".html" for path in paths),
        "markdown_files": sum(path.suffix.lower() in (".md", ".markdown") for path in paths),
        "images": 0, "image_links": 0,
        "audio_players": 0, "video_players": 0, "url_links": 0,
        "emojis": 0, "animated_emoji_files": 0, "errors": [],
    }
    checked_images = set()
    checked_emoji = set()
    for html_path in paths:
        source = html_path.read_text(encoding="utf-8")
        parser = ExportParser()
        parser.feed(source)
        if html_path.suffix.lower() in (".md", ".markdown"):
            for match in re.finditer(r"\[!\[[^\]]*\]\(([^)]+)\)\]\(([^)]+)\)", source):
                parser.images.append({"src": match.group(1)})
                parser.image_links.append({"href": match.group(2)})
        result["images"] += len(parser.images)
        result["image_links"] += len(parser.image_links)
        result["audio_players"] += len(parser.audios)
        result["video_players"] += len(parser.videos)
        result["url_links"] += len(parser.links)
        result["emojis"] += len(parser.emojis)

        for node in parser.images + parser.emojis:
            path = _local(html_path, node.get("src", ""))
            if path is None or path in checked_images:
                continue
            checked_images.add(path)
            if not path.is_file():
                result["errors"].append("图片不存在: %s" % path)
                continue
            try:
                with Image.open(path) as image:
                    frames = getattr(image, "n_frames", 1)
                    image.seek(min(frames - 1, 1))
                    image.load()
                    if node in parser.emojis and path not in checked_emoji:
                        checked_emoji.add(path)
                        if frames > 1:
                            result["animated_emoji_files"] += 1
            except OSError as exc:
                result["errors"].append("图片无法解码: %s (%s)" % (path, exc))

        for anchor in parser.image_links:
            path = _local(html_path, anchor.get("href", ""))
            if path is None or not path.is_file():
                result["errors"].append("图片原图链接无效: %s" % anchor.get("href", ""))

        for node in parser.audios:
            if "controls" not in node:
                result["errors"].append("音频播放器缺少 controls")
            path = _local(html_path, node.get("src", ""))
            try:
                probe = _probe(path) if path and path.is_file() else {}
                streams = probe.get("streams", [])
                duration = float(probe.get("format", {}).get("duration") or 0)
                if duration <= 0 or not any(s.get("codec_type") == "audio" for s in streams):
                    raise ValueError("无有效音频流")
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                result["errors"].append("音频无法解码: %s (%s)" % (path, exc))

        direct_video_sources = [{"src": node.get("src", "")}
                                for node in parser.videos if node.get("src")]
        if len(parser.video_sources) + len(direct_video_sources) < len(parser.videos):
            result["errors"].append("视频播放器缺少 source")
        for node in parser.videos:
            if "controls" not in node:
                result["errors"].append("视频播放器缺少 controls")
            poster = _local(html_path, node.get("poster", ""))
            if not poster or not poster.is_file():
                result["errors"].append("视频封面不存在: %s" % node.get("poster", ""))
        for node in parser.video_sources + direct_video_sources:
            path = _local(html_path, node.get("src", ""))
            try:
                probe = _probe(path) if path and path.is_file() else {}
                streams = probe.get("streams", [])
                duration = float(probe.get("format", {}).get("duration") or 0)
                if duration <= 0 or not any(s.get("codec_type") == "video" for s in streams):
                    raise ValueError("无有效视频流")
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                result["errors"].append("视频无法解码: %s (%s)" % (path, exc))

        for node in parser.links:
            parsed = urlparse(node.get("href", ""))
            rel = set(node.get("rel", "").split())
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                result["errors"].append("URL 非安全 HTTP(S): %s" % node.get("href", ""))
            if node.get("target") != "_blank" or "noopener" not in rel:
                result["errors"].append("URL 链接缺少安全点击属性")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", nargs="+", type=Path,
                        help="一个或多个 HTML/Markdown 导出文件")
    parser.add_argument("--require", action="append", default=[], metavar="NAME=N",
                        help="要求某项数量至少为 N，可重复")
    args = parser.parse_args(argv)
    paths = [path.resolve() for path in args.documents]
    report = validate(paths)
    for item in args.require:
        name, sep, raw = item.partition("=")
        if not sep or name not in report:
            parser.error("--require 格式必须是已知 NAME=N")
        if int(report[name]) < int(raw):
            report["errors"].append("%s=%s，低于要求 %s" %
                                    (name, report[name], raw))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
