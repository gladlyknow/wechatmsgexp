# -*- coding: utf-8 -*-
"""媒体附件导出：图片(.dat) / 语音(silk) / 视频 / 文件。

图片（`msg/attach/<md5(会话)>/<年月>/Img/*.dat`）在微信 4.x 里是加密的：

    V1  头 `07 08 56 31 08 07`，AES-128-ECB，密钥固定 = md5("0")[:16]
    V2  头 `07 08 56 32 08 07`，AES-128-ECB，密钥每次随机，只在内存里短暂存在
    旧  单字节 XOR

结构：`[6B 头][4B aes_size LE][4B xor_size LE][1B 异或键][AES 密文][明文段][XOR 段]`

**关于 V2**：密钥不是常驻内存的，只在微信渲染该图片时临时生成。因此本工具
默认**不**尝试自动破解 V2，而是把图片按消息索引原样导出（`--copy-dat`），
并写出 `images.txt` 对照表。V1 会自动解密。想尝试自动找 V2 密钥可以用
`find_dat_aes_key()`（需要先在微信里点开过该图片，且较慢）。

语音（SILK）、视频（mp4）、文件（docx/xlsx/pdf…）都不加密，直接导出。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from Cryptodome.Cipher import AES

from .security import private_dir, private_file

SIG_V1 = bytes.fromhex("070856310807")
SIG_V2 = bytes.fromhex("070856320807")
V1_KEY = hashlib.md5(b"0").hexdigest()[:16].encode()

IMG_EXT = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"),
           (b"RIFF", ".webp"), (b"BM", ".bmp"), (b"wxgf", ".wxgf"))

if sys.platform == "win32":
    import ctypes.wintypes as wt
    kernel32 = ctypes.windll.kernel32
else:
    wt = None
    kernel32 = None
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
                ("AllocationProtect", ctypes.c_uint32), ("_p1", ctypes.c_uint32),
                ("RegionSize", ctypes.c_uint64), ("State", ctypes.c_uint32),
                ("Protect", ctypes.c_uint32), ("Type", ctypes.c_uint32),
                ("_p2", ctypes.c_uint32)]


# ---------------------------------------------------------------------------
# dat 解码
# ---------------------------------------------------------------------------
def sniff_ext(data: bytes) -> str:
    for magic, ext in IMG_EXT:
        if data.startswith(magic):
            return ext
    return ".bin"


def _pkcs7_strip(data: bytes) -> bytes:
    if data and 1 <= data[-1] <= 16 and data[-data[-1]:] == bytes([data[-1]]) * data[-1]:
        return data[:-data[-1]]
    return data


def dat_layout(data: bytes):
    """返回 (aes_ct, mid, xor_ct, xor_key_hint)；不符合结构返回 None。"""
    if len(data) < 15:
        return None
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    if aes_size <= 0 or aes_size > len(data):
        return None
    # PKCS#7 即使明文长度恰好是 16 的倍数，也会追加一个
    # 完整 padding block。
    aligned = aes_size + (16 - (aes_size % 16))
    start = 15
    end = start + aligned
    if end > len(data):
        return None
    xor_start = len(data) - xor_size if xor_size else len(data)
    if xor_start < end:
        return None
    return data[start:end], data[end:xor_start], data[xor_start:], data[14]


def decode_dat(data: bytes, aes_key: bytes | None = None,
               xor_key: int | None = None) -> bytes | None:
    """解出一个 .dat；失败返回 None（V2 没密钥时会返回 None）。"""
    sig = data[:6]
    if sig == SIG_V1:
        key = V1_KEY
    elif sig == SIG_V2:
        key = aes_key
        if not key:
            return None
    else:
        for k in range(256):
            head = bytes(b ^ k for b in data[:4])
            if sniff_ext(head) != ".bin":
                return bytes(b ^ k for b in data)
        return None
    lay = dat_layout(data)
    if not lay:
        return None
    aes_ct, mid, xor_ct, hint = lay
    try:
        pt = _pkcs7_strip(AES.new(key, AES.MODE_ECB).decrypt(aes_ct))
    except ValueError:
        return None
    xk = xor_key if xor_key is not None else hint
    out = pt + mid + bytes(b ^ (xk & 0xFF) for b in xor_ct)
    i = out.find(b"\xff\xd9")
    if out.startswith(b"\xff\xd8") and i > 0:
        out = out[:i + 2]
    i = out.find(b"IEND")
    if out.startswith(b"\x89PNG") and i > 0:
        out = out[:i + 4]
    if out.startswith(b"RIFF") and out[8:12] == b"WEBP" and len(out) >= 12:
        declared = struct.unpack_from("<L", out, 4)[0] + 8
        if 12 <= declared <= len(out):
            out = out[:declared]
    return out


# ---------------------------------------------------------------------------
# 可选：从内存里找 V2 密钥（慢，且需要该图片最近被渲染过）
# ---------------------------------------------------------------------------
def _regions(h):
    out, addr, mbi = [], 0, _MBI()
    while addr < 0x7FFF_FFFF_FFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            out.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return out


def _read(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def find_dat_aes_key(sample_path: str, pids=None, progress=None):
    """用样本 .dat 在微信内存里搜索 V2 的 AES 密钥。

    判据：候选密钥必须是 16 字节可打印 ASCII，且用它解出的第一段密文，
    前缀要与其后紧跟的**明文段**一致。
    """
    progress = progress or (lambda m: None)
    if kernel32 is None:
        progress("非 Windows 环境不支持内存扫描，请使用 Linux 图片密钥派生")
        return None
    with open(sample_path, "rb") as f:
        data = f.read()
    if data[:6] != SIG_V2:
        progress("样本不是 V2 格式")
        return None
    lay = dat_layout(data)
    if not lay:
        return None
    aes_ct, mid, _xor, _hint = lay
    if len(mid) < 16:
        progress("样本没有内嵌明文段，无法校验候选密钥（试试 _t.dat）")
        return None
    expect = mid[:16]

    if pids is None:
        from .key import find_weixin_processes
        pids = [p["pid"] for p in find_weixin_processes()]
    for pid in pids:
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            continue
        try:
            for base, size in _regions(h):
                buf = _read(h, base, size)
                if not buf or len(buf) < 64:
                    continue
                # 只按 16 字节对齐滑动，并先做可打印判断，避免大量无效 AES
                for i in range(0, len(buf) - 16 + 1, 16):
                    cand = buf[i:i + 16]
                    if not all(0x20 <= b < 0x7F for b in cand):
                        continue
                    try:
                        out = AES.new(cand, AES.MODE_ECB).decrypt(aes_ct[:16])
                    except ValueError:
                        continue
                    if out[:len(expect)] == expect:
                        progress("找到图片密钥 %r (PID=%d @0x%X)" % (cand, pid, base + i))
                        return cand
        finally:
            kernel32.CloseHandle(h)
    progress("未在内存中找到图片 AES 密钥")
    return None


def _normalized_wxids(data) -> list[str]:
    """生成 Linux/macOS 微信 V2 图片密钥可能使用的 wxid。"""
    values = [getattr(data, "wxid", ""), os.path.basename(data.account_dir())]
    out = []
    for raw in values:
        raw = (raw or "").strip()
        if not raw:
            continue
        candidates = [raw]
        if raw.startswith("wxid_"):
            candidates.append("wxid_" + raw[5:].split("_", 1)[0])
        else:
            base, sep, suffix = raw.rpartition("_")
            if sep and len(suffix) == 4 and all(c in "0123456789abcdefABCDEF" for c in suffix):
                candidates.append(base)
        for value in candidates:
            if value and value not in out:
                out.append(value)
    return out


def _kvcomm_codes(data) -> list[int]:
    candidates = [
        os.path.expanduser("~/.xwechat/net/kvcomm"),
        os.path.expanduser("~/Documents/xwechat/net/kvcomm"),
        os.path.join(os.path.dirname(os.path.dirname(data.account_dir())),
                     "xwechat", "net", "kvcomm"),
    ]
    codes = set()
    for directory in candidates:
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            match = re.match(r"key_(\d+)_", name)
            if match:
                codes.add(int(match.group(1)))
    return sorted(codes)


def _v2_key_matches(raw: bytes, key: bytes) -> bool:
    layout = dat_layout(raw)
    if not layout:
        return False
    aes_ct = layout[0]
    try:
        first = AES.new(key, AES.MODE_ECB).decrypt(aes_ct[:16])
    except ValueError:
        return False
    return sniff_ext(first) != ".bin"


def discover_v2_image_key(data, sample_paths: list[str], progress=None):
    """从 Linux 微信 kvcomm 文件名派生并反验 V2 AES/XOR 密钥。"""
    progress = progress or (lambda m: None)
    samples = []
    for path in sample_paths:
        try:
            raw = open(path, "rb").read()
        except OSError:
            continue
        if raw.startswith(SIG_V2):
            samples.append(raw)
        if len(samples) >= 3:
            break
    if not samples:
        return None, None
    for wxid in _normalized_wxids(data):
        for code in _kvcomm_codes(data):
            digest = hashlib.md5((str(code) + wxid).encode("utf-8")).hexdigest()
            key = digest[:16].encode("ascii")
            if all(_v2_key_matches(raw, key) for raw in samples):
                progress("已自动验证 Linux V2 图片密钥")
                return key, code & 0xFF
    progress("未能从 Linux kvcomm 自动派生 V2 图片密钥")
    return None, None


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
@dataclass
class MediaStats:
    images: int = 0
    images_raw: int = 0
    voices: int = 0
    audios: int = 0
    videos: int = 0
    files: int = 0
    emojis: int = 0
    issues: list = field(default_factory=list)
    extras: list = field(default_factory=list)


def export_voice(data, msgs, out_dir: str, progress=None) -> int:
    """导出 SILK 源文件，并转为浏览器可直接播放的 MP3。"""
    progress = progress or (lambda m: None)
    n = 0
    private_dir(out_dir)
    try:
        import pilk
    except ImportError:
        progress("缺少 pilk，语音只能保留 SILK 源文件")
        pilk = None
    ffmpeg = shutil.which("ffmpeg")
    for m in msgs:
        if m["type"] != 34 or not m["server_id"]:
            continue
        blob = data.voice_blob(m["session"], m["server_id"])
        if not blob:
            continue
        stamp = m["time_str"].replace(":", "").replace("-", "").replace(" ", "_")
        silk_path = os.path.join(out_dir, "voice_%s_%s.silk" % (stamp, m["server_id"]))
        with open(silk_path, "wb") as f:
            f.write(blob)
        private_file(silk_path)
        m["extra"]["voice_source"] = silk_path
        if pilk is None or not ffmpeg:
            continue
        mp3_path = os.path.splitext(silk_path)[0] + ".mp3"
        pcm_fd, pcm_path = tempfile.mkstemp(prefix="wx4_voice_", suffix=".pcm")
        os.close(pcm_fd)
        try:
            pilk.decode(silk_path, pcm_path, 24000)
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000",
                "-ac", "1", "-i", pcm_path, "-codec:a", "libmp3lame", "-q:a", "4",
                mp3_path,
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if os.path.getsize(mp3_path) > 0:
                private_file(mp3_path)
                m["extra"]["voice_file"] = mp3_path
                n += 1
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            progress("语音转换失败 %s：%s" % (m["server_id"], exc))
        finally:
            try:
                os.unlink(pcm_path)
            except OSError:
                pass
    if n:
        progress("语音 %d 条已转为 MP3" % n)
    return n


def _session_attach_index(attach_dir: str) -> dict:
    idx = {}
    if not attach_dir or not os.path.isdir(attach_dir):
        return idx
    for root, _dirs, files in os.walk(attach_dir):
        for f in files:
            if f.endswith(".dat"):
                stem = os.path.splitext(f)[0]
                base = re.sub(r"_(?:t|h)$", "", stem)
                variant = "thumb" if stem.endswith("_t") else \
                    ("large" if stem.endswith("_h") else "main")
                idx.setdefault(base, {})[variant] = os.path.join(root, f)
    return idx


def _decode_image_file(src: str, dst_prefix: str, aes_key=None,
                       xor_key=None) -> str | None:
    try:
        with open(src, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    out = decode_dat(raw, aes_key, xor_key)
    if not out:
        return None
    ext = sniff_ext(out)
    if ext == ".bin":
        return None
    if ext == ".wxgf":
        # wxgf 是微信包装的 HEVC 单帧；去掉容器头后交给
        # ffmpeg 解成通用 JPEG，否则浏览器无法直接展示。
        start = out.find(b"\x00\x00\x00\x01", 4)
        ffmpeg = shutil.which("ffmpeg")
        if start >= 0 and ffmpeg:
            dst = dst_prefix + ".jpg"
            try:
                subprocess.run([
                    ffmpeg, "-y", "-loglevel", "error", "-f", "hevc", "-i", "pipe:0",
                    "-frames:v", "1", "-q:v", "2", dst,
                ], input=out[start:], check=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
                if os.path.getsize(dst) > 0:
                    private_file(dst)
                    return dst
            except (OSError, subprocess.CalledProcessError):
                pass
        return None
    dst = dst_prefix + ext
    with open(dst, "wb") as f:
        f.write(out)
    private_file(dst)
    return dst


def _make_thumbnail(src: str, dst: str, max_px: int = 520) -> str | None:
    try:
        from PIL import Image
        with Image.open(src) as image:
            image.thumbnail((max_px, max_px))
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(dst, format="JPEG", quality=84, optimize=True)
        private_file(dst)
        return dst
    except (ImportError, OSError, ValueError):
        return None


def _copy_export_file(src: str, dst: str) -> None:
    """用原子替换复制，避免源文件只读权限导致二次导出失败。"""
    private_dir(os.path.dirname(dst))
    fd, tmp = tempfile.mkstemp(prefix=".wx4_copy_", dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def export_images(data, session: str, msgs, out_dir: str,
                  aes_key: bytes | None = None, xor_key: int | None = None,
                  copy_dat: bool = True,
                  progress=None) -> int:
    """用 message_resource.db 的真实资源主名关联、解密图片。"""
    progress = progress or (lambda m: None)
    attach_dir = data.attach_dir(session)
    if not attach_dir:
        return 0
    index = _session_attach_index(attach_dir)
    if not index:
        return 0
    private_dir(out_dir)
    n = 0
    for m in msgs:
        if m["type"] != 3:
            continue
        md5s = data.message_resource_stems(
            session, m.get("server_id") or 0, m.get("local_id") or 0)
        md5s += re.findall(r"\b[0-9a-f]{32}\b", m.get("content") or "")
        md5s += re.findall(r"\b[0-9a-f]{32}\b", m.get("packed_hex") or "")
        for md5 in dict.fromkeys(md5s):
            variants = index.get(md5)
            if not variants:
                continue
            src = variants.get("main") or variants.get("large") or variants.get("thumb")
            stamp = m["time_str"].replace(":", "").replace("-", "").replace(" ", "_")
            prefix = os.path.join(out_dir, "img_%s_%s" % (stamp, md5[:8]))
            dst = _decode_image_file(src, prefix, aes_key, xor_key)
            if dst is None and copy_dat:
                dst = os.path.join(out_dir, "%s.dat" % md5)
                if not os.path.exists(dst):
                    _copy_export_file(src, dst)
            elif dst is None:
                continue
            m["extra"].setdefault("images", []).append(dst)
            if not dst.endswith(".dat"):
                thumb_src = variants.get("thumb")
                thumb = None
                if thumb_src and thumb_src != src:
                    thumb = _decode_image_file(
                        thumb_src, prefix + "_thumb", aes_key, xor_key)
                if thumb is None:
                    thumb = _make_thumbnail(dst, prefix + "_thumb.jpg")
                if thumb:
                    m["extra"].setdefault("image_thumbs", {})[dst] = thumb
            n += 1
            break
    if n:
        progress("图片 %d 张已还原" % n)
    return n


def copy_plain_media(data, out_dir: str, progress=None) -> tuple:
    """视频/文件在 msg/video、msg/file 下是明文，整体复制。"""
    progress = progress or (lambda m: None)
    nv = nf = 0
    for sub in ("video", "file"):
        base = os.path.join(data.msg_dir(), sub)
        if not os.path.isdir(base):
            continue
        dst_dir = os.path.join(out_dir, sub)
        for root, _dirs, files in os.walk(base):
            for f in files:
                src = os.path.join(root, f)
                dst = os.path.join(dst_dir, os.path.relpath(src, base))
                private_dir(os.path.dirname(dst))
                try:
                    _copy_export_file(src, dst)
                except OSError:
                    continue
                if sub == "video":
                    nv += 1
                else:
                    nf += 1
    if nv or nf:
        progress("视频 %d 个、文件 %d 个已复制" % (nv, nf))
    return nv, nf


def _file_index(root: str) -> dict[str, list[str]]:
    out = {}
    if not os.path.isdir(root):
        return out
    for directory, _dirs, files in os.walk(root):
        for name in files:
            stem = os.path.splitext(name)[0]
            stem = re.sub(r"_thumb$", "", stem)
            out.setdefault(stem, []).append(os.path.join(directory, name))
    return out


def export_message_files(data, session: str, msgs, out_dir: str,
                         progress=None) -> tuple[int, int, int]:
    """按消息资源主名关联视频、音频和普通文件。"""
    progress = progress or (lambda m: None)
    roots = [os.path.join(data.msg_dir(), "video"), os.path.join(data.msg_dir(), "file")]
    index = {}
    for root in roots:
        for stem, paths in _file_index(root).items():
            index.setdefault(stem, []).extend(paths)
    videos = audios = files = 0
    audio_exts = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
    video_exts = {".mp4", ".webm", ".mov", ".m4v"}
    for m in msgs:
        if m["type"] not in (43, 62, 49, 11002):
            continue
        stems = data.message_resource_stems(
            session, m.get("server_id") or 0, m.get("local_id") or 0)
        for stem in stems:
            sources = index.get(stem) or []
            if not sources:
                continue
            target_dir = os.path.join(out_dir, "video" if m["type"] in (43, 62) else "file")
            private_dir(target_dir)
            for src in sources:
                name = os.path.basename(src)
                dst = os.path.join(target_dir, name)
                _copy_export_file(src, dst)
                ext = os.path.splitext(name)[1].lower()
                if ext in video_exts:
                    m["extra"]["video_file"] = dst
                    videos += 1
                elif ext in audio_exts:
                    m["extra"]["audio_file"] = dst
                    audios += 1
                elif name.endswith("_thumb.jpg") or ext in {".jpg", ".jpeg", ".png", ".webp"}:
                    preferred = name.endswith("_thumb.jpg") or "video_poster" not in m["extra"]
                    if preferred:
                        m["extra"]["video_poster"] = dst
                else:
                    m["extra"].setdefault("files", []).append(dst)
                    files += 1
            break
    if videos or audios or files:
        progress("已关联视频 %d、音频 %d、文件 %d" % (videos, audios, files))
    return videos, audios, files


def _emoji_resources(data) -> dict[str, str]:
    root = os.path.join(data.account_dir(), "resource", "emoticon_webp")
    out = {}
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        match = re.match(r"^\d+_(.+)_\.webp$", name)
        if match:
            out["[" + match.group(1) + "]"] = os.path.join(root, name)
    return out


def export_emojis(data, msgs, out_dir: str, aes_key=None, xor_key=None,
                  progress=None) -> int:
    """还原文本中的微信内置表情，并尝试关联自定义表情。"""
    progress = progress or (lambda m: None)
    resources = _emoji_resources(data)
    if not resources:
        return 0
    private_dir(out_dir)
    cache = {}
    count = 0
    for m in msgs:
        tokens = [token for token in resources if token in (m.get("content") or "")]
        for token in tokens:
            if token not in cache:
                src = resources[token]
                safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "", token)
                dst = _decode_image_file(
                    src, os.path.join(out_dir, "emoji_" + safe), aes_key, xor_key)
                cache[token] = dst
            dst = cache[token]
            if dst:
                m["extra"].setdefault("emoji_tokens", {})[token] = dst
                count += (m.get("content") or "").count(token)
    if count:
        progress("内置表情 %d 个已还原" % count)
    return count


def export_session_media(data, session: str, msgs, out_root: str,
                         aes_key=None, copy_dat=True, progress=None) -> MediaStats:
    """导出某个会话的全部媒体，返回统计。"""
    progress = progress or (lambda m: None)
    private_dir(out_root)
    st = MediaStats()
    xor_key = None
    if aes_key is None:
        cached = getattr(data, "_v2_image_material", None)
        if cached:
            aes_key, xor_key = cached
        else:
            samples = []
            attach = data.attach_dir(session)
            if attach:
                for root, _dirs, files in os.walk(attach):
                    samples.extend(os.path.join(root, f) for f in files if f.endswith(".dat"))
            samples.extend(_emoji_resources(data).values())
            aes_key, xor_key = discover_v2_image_key(data, samples, progress=progress)
            if aes_key is not None:
                data._v2_image_material = (aes_key, xor_key)
    st.images = export_images(data, session, msgs, os.path.join(out_root, "image"),
                              aes_key=aes_key, xor_key=xor_key,
                              copy_dat=copy_dat, progress=progress)
    st.voices = export_voice(data, msgs, os.path.join(out_root, "voice"), progress=progress)
    st.videos, st.audios, st.files = export_message_files(
        data, session, msgs, out_root, progress=progress)
    st.emojis = export_emojis(data, msgs, os.path.join(out_root, "emoji"),
                              aes_key=aes_key, xor_key=xor_key, progress=progress)
    if st.audios:
        st.extras.append("音频 %d" % st.audios)
    if st.emojis:
        st.extras.append("表情 %d" % st.emojis)

    def issue(message, category, detail):
        st.issues.append({
            "category": category,
            "detail": detail,
            "type": message.get("type"),
            "local_id": message.get("local_id"),
            "server_id": message.get("server_id"),
        })

    for message in msgs:
        extra = message.get("extra") or {}
        if message.get("type") == 3:
            images = extra.get("images") or []
            if not images:
                stems = data.message_resource_stems(
                    session, message.get("server_id") or 0, message.get("local_id") or 0)
                issue(message, "media_not_downloaded" if stems else "metadata_missing",
                      "未找到本地图片资源" if stems else "消息资源缺少本地主名")
            elif all(path.lower().endswith(".dat") for path in images):
                st.images_raw += len(images)
                issue(message, "decrypt_failed", "图片只保留了加密 .dat")
        elif message.get("type") == 34:
            if not extra.get("voice_source"):
                issue(message, "media_not_downloaded", "media_*.db 中没有语音数据")
            elif not extra.get("voice_file"):
                issue(message, "transcode_failed", "SILK 未能转为浏览器可播音频")
        elif message.get("type") in (43, 62) and not extra.get("video_file"):
            stems = data.message_resource_stems(
                session, message.get("server_id") or 0, message.get("local_id") or 0)
            issue(message, "media_not_downloaded" if stems else "metadata_missing",
                  "未找到本地视频文件" if stems else "消息资源缺少本地主名")
        elif message.get("kind") == "emoji" and not extra.get("emoji_file"):
            issue(message, "media_not_downloaded", "未找到自定义表情本体")

    manifest = {
        "session": session,
        "stats": {
            "images": st.images, "images_raw": st.images_raw,
            "voices": st.voices, "audios": st.audios,
            "videos": st.videos, "files": st.files, "emojis": st.emojis,
        },
        "issues": st.issues,
    }
    manifest_path = os.path.join(out_root, "media-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    private_file(manifest_path)
    if st.issues:
        progress("媒体问题 %d 项，详见 media-manifest.json" % len(st.issues))
    return st
