# -*- coding: utf-8 -*-
"""从运行中的 Weixin.exe 提取数据库密钥。

微信 4.1+ 不再把可用的 raw key 明文缓存进内存，改成在进程里保留
`com.Tencent.WCDB.Config.Cipher` 配置对象（对象里是一段用固定掩码异或过的
`x'<64位hex密钥><32位hex盐>'` 字符串）。做法：

    1. 枚举所有 Weixin.exe 进程，按内存占用从大到小（主进程最大）
    2. 在可读内存区域里找 "com.Tencent.WCDB.Config.Cipher" 字符串
    3. 找引用该字符串的 {指针,长度} 对，反推出 std::string 节点 -> 配置对象
    4. 读配置对象 +0x88 处的 blob，按固定掩码异或还原
    5. 正则取出 x'...' 字面量，切出 (key, salt) 候选
    6. 用数据库第一页的 HMAC-SHA512 校验，通过才算数

4.0.x 老版本回退路径：直接在全内存里找 `x'<key><salt>'` 字面量。

另有两条不依赖内存的补充路径：
    * 读 `all_users\\login\\<wxid>\\key_info.db`（明文库，但 key_info_data 是加密
      protobuf，本工具只用于诊断，不依赖它）
    * 手工填入 passphrase / 账号密钥（set_key）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import bisect
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

try:
    import pwd
except ImportError:  # Windows
    pwd = None

from .common import KEY_SZ, PAGE_SZ, SALT_SZ, derive_enc_key, verify_enc_key

# ---------------------------------------------------------------------------
# 常量（微信 4.1.11+ 内存布局）
# ---------------------------------------------------------------------------
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
MAX_USER_ADDRESS = 0x0000_8000_0000_0000
CONFIG_BLOB_MAX = 1024
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
WXID_HEX_RE = re.compile(rb"(?:wxid_[0-9a-z]+)[\x00-\x20]*([0-9a-fA-F]{64})")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
kernel32 = ctypes.windll.kernel32 if IS_WINDOWS else None
advapi32 = ctypes.windll.advapi32 if IS_WINDOWS else None


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD),
        ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("_pad2", wt.DWORD),
    ]


# ---------------------------------------------------------------------------
# 进程 / 内存基础操作
# ---------------------------------------------------------------------------
def enable_debug_privilege() -> bool:
    """尝试打开 SeDebugPrivilege（读取其它进程内存时常常需要）。"""
    if not IS_WINDOWS:
        return True
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    h_token = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(h_token)):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(h_token)


def find_weixin_processes() -> list[dict]:
    """列出所有 Weixin.exe 进程，按内存占用降序（主进程内存最大）。"""
    if IS_LINUX:
        out = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                    comm = f.read().strip()
                exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
            except (OSError, UnicodeError):
                continue
            if comm.lower() not in {"wechat", "weixin"} and exe.lower() not in {
                    "wechat", "weixin"}:
                continue
            mem_kb = 0
            try:
                with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            mem_kb = int(line.split()[1])
                            break
            except (OSError, ValueError):
                pass
            out.append({"pid": pid, "name": comm or exe, "mem_kb": mem_kb})
        out.sort(key=lambda x: x["mem_kb"], reverse=True)
        return out
    if not IS_WINDOWS:
        return []
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.strip('"').split('","')
        if len(parts) >= 5:
            try:
                pid = int(parts[1])
                mem_kb = int(parts[4].replace(",", "").replace(" K", "").strip() or 0)
            except ValueError:
                continue
            out.append({"pid": pid, "name": parts[0], "mem_kb": mem_kb})
    out.sort(key=lambda x: x["mem_kb"], reverse=True)
    return out


def _open(pid: int):
    if not IS_WINDOWS:
        return None
    return kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)


def _read(h, addr, size):
    if not IS_WINDOWS:
        return None
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def _regions(h):
    if not IS_WINDOWS:
        return []
    regs = []
    addr = 0
    mbi = _MBI()
    while addr < 0x7FFF_FFFF_FFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def _iter_chunks(regions, read_region, chunk_size=2 * 1024 * 1024, overlap=0):
    """按块读取，overlap 保证跨块的特征串不会被漏掉。"""
    for base, size in regions:
        off = 0
        tail = b""
        tail_base = base
        while off < size:
            cur = min(chunk_size, size - off)
            chunk = read_region(base + off, cur) or b""
            data_base = tail_base if tail else base + off
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + off + cur
            else:
                tail = b""
                tail_base = base + off + cur
            off += cur


def _u64(data, off):
    if off < 0 or off + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, off)[0]


def _xor_repeat(data: bytes, mask: bytes) -> bytes:
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(data))


def _looks_like_key(k: bytes) -> bool:
    return len(k) == KEY_SZ and len(set(k)) >= 15 and k not in {b"\x00" * KEY_SZ, b"\xff" * KEY_SZ}


# ---------------------------------------------------------------------------
# 候选密钥收集
# ---------------------------------------------------------------------------
def _candidates_from_literal_run(run: str) -> list[tuple[str, str | None]]:
    """一段 64~192 位 hex 里滑窗切 (key, salt) 候选。"""
    out = []
    starts = [0]
    if len(run) > 96:
        starts.extend(range(0, len(run) - 63, 32))
        starts.append(len(run) - 64)
    for st in dict.fromkeys(starts):
        if st < 0 or st + 64 > len(run):
            continue
        key_hex = run[st:st + 64]
        try:
            if not _looks_like_key(bytes.fromhex(key_hex)):
                continue
        except ValueError:
            continue
        salt = run[st + 64:st + 96] if st + 96 <= len(run) else None
        out.append((key_hex, salt))
    return out


def _candidates_from_config_blob(blob: bytes) -> list[tuple[str, str | None]]:
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return []
    decoded = _xor_repeat(blob, CONFIG_XOR_MASK)
    out = []
    for m in CONFIG_LITERAL_RE.finditer(decoded):
        out.extend(_candidates_from_literal_run(m.group(1).decode("ascii").lower()))
    return out


def _search_linux_literals(data: bytes) -> list[tuple[str, str | None]]:
    """从 Linux 微信内存块中提取 WCDB ``x'<key><salt>'`` 候选。"""
    out = []
    seen = set()
    for match in HEX_LITERAL_RE.finditer(data):
        run = match.group(1).decode("ascii").lower()
        for candidate in _candidates_from_literal_run(run):
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _linux_regions(pid: int, writable_only: bool = True) -> list[tuple[int, int]]:
    """返回 Linux 进程的可读内存区间。"""
    regions = []
    with open(f"/proc/{pid}/maps", encoding="utf-8") as f:
        for line in f:
            parts = line.split(None, 5)
            if len(parts) < 2 or not parts[1].startswith("r"):
                continue
            if writable_only and not parts[1].startswith("rw"):
                continue
            try:
                start_s, end_s = parts[0].split("-", 1)
                start, end = int(start_s, 16), int(end_s, 16)
            except ValueError:
                continue
            if end > start and end - start < 500 * 1024 * 1024:
                regions.append((start, end - start))
    return regions


def _ptrace_scope() -> str | None:
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 扫描器
# ---------------------------------------------------------------------------
class KeyExtractor:
    """对一个账号的 db_storage 提取全部数据库密钥。"""

    def __init__(self, db_storage: str, progress=None):
        self.db_storage = db_storage
        self.progress = progress or (lambda msg: None)
        self.db_files: list[dict] = []
        self.salt_index: dict[str, list[dict]] = {}
        self.keys: dict[str, str] = {}       # salt_hex -> enc_key_hex
        self.stats: dict = {}
        self._passphrase_attempts = 0

    # -- 收集待解密库，缓存各自的 salt 和第一页（用于校验） -----------------
    def collect(self):
        self.db_files = []
        self.salt_index = {}
        for root, _dirs, files in os.walk(self.db_storage):
            for name in files:
                if not name.endswith(".db"):
                    continue
                path = os.path.join(root, name)
                try:
                    if os.path.getsize(path) < PAGE_SZ:
                        continue
                    with open(path, "rb") as f:
                        page1 = f.read(PAGE_SZ)
                except OSError:
                    continue
                if len(page1) < PAGE_SZ:
                    continue
                salt = page1[:SALT_SZ].hex()
                item = {"rel": os.path.relpath(path, self.db_storage), "path": path,
                        "salt": salt, "page1": page1}
                self.db_files.append(item)
                self.salt_index.setdefault(salt, []).append(item)
        self.progress("共发现 %d 个加密数据库，%d 个不同 salt"
                      % (len(self.db_files), len(self.salt_index)))
        return self.db_files

    def _try_key(self, key_hex: str, salt_hint: str | None,
                 allow_passphrase: bool = False) -> int:
        """校验一个候选密钥，命中则记入 self.keys。返回命中 salt 数。

        候选可能是两种东西：
          * 已经派生好的 32 字节 enc_key（大多数情况，校验很便宜）
          * 32 字节 passphrase，需要用库自己的 salt 再走 256000 轮 PBKDF2
            （部分 4.1+ 版本内存里只剩这个）

        passphrase 路径很贵（每库约 0.1 秒），所以只在显式允许、且剩余 salt
        不多时才尝试，避免把扫描拖成几小时。
        """
        if salt_hint and salt_hint in self.salt_index and salt_hint not in self.keys:
            targets = [salt_hint]
        else:
            targets = [s for s in self.salt_index if s not in self.keys]
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            return 0
        if not _looks_like_key(key):
            return 0
        hit = 0
        for salt in targets:
            if salt in self.keys:
                continue
            page1 = self.salt_index[salt][0]["page1"]
            if verify_enc_key(key, page1):
                self.keys[salt] = key_hex
                hit += 1
                continue
            if allow_passphrase and self._passphrase_attempts < 64:
                self._passphrase_attempts += 1
                derived = derive_enc_key(key, bytes.fromhex(salt))
                if verify_enc_key(derived, page1):
                    self.keys[salt] = derived.hex()
                    hit += 1
        return hit

    # -- 主路径：Config.Cipher ------------------------------------------
    def _scan_process_linux(self, pid: int, allow_passphrase: bool = False) -> dict:
        stats = {"pid": pid, "regions": 0, "bytes": 0,
                 "candidates": 0, "salt_markers": 0, "needle": 0,
                 "refs": 0, "blobs": 0, "graph_nodes": 0,
                 "data_pairs": 0, "hit": 0}
        try:
            regions = _linux_regions(pid)
            stats["regions"] = len(regions)
            fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        except OSError as exc:
            scope = _ptrace_scope()
            hint = "请使用 sudo 只执行 key 子命令"
            if scope is not None:
                hint += f"（ptrace_scope={scope}）"
            return {**stats, "error": f"无法读取 /proc/{pid}/mem: {exc}; {hint}"}

        def read_region(addr, size):
            try:
                return os.pread(fd, size, addr)
            except OSError:
                return None

        seen_candidates = set()

        def try_candidate(key_hex, salt_hex):
            marker = (key_hex, salt_hex, allow_passphrase)
            if marker in seen_candidates:
                return 0
            seen_candidates.add(marker)
            stats["candidates"] += 1
            return self._try_key(key_hex, salt_hex,
                                 allow_passphrase=allow_passphrase)

        try:
            for _base, data in _iter_chunks(regions, read_region, overlap=256):
                if len(self.keys) == len(self.salt_index):
                    break
                stats["bytes"] += len(data)
                for key_hex, salt_hex in _search_linux_literals(data):
                    stats["hit"] += try_candidate(key_hex, salt_hex)

                # 4.1.13+ 有时不再保留完整 x'...' 字面量，但会在配置
                # 对象附近保留 salt 与 raw key。以已知 DB salt 作锚点，只把
                # 附近候选交给 HMAC 校验，不依赖脆弱的固定偏移。
                for salt_hex in self.salt_index:
                    if salt_hex in self.keys:
                        continue
                    salt = bytes.fromhex(salt_hex)
                    for pattern, is_ascii in ((salt, False),
                                              (salt_hex.encode("ascii"), True)):
                        pos = data.find(pattern)
                        while pos >= 0:
                            stats["salt_markers"] += 1
                            left = max(0, pos - 256)
                            right = min(len(data), pos + len(pattern) + 256)
                            window = data[left:right]
                            if is_ascii:
                                for match in re.finditer(rb"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])",
                                                         window):
                                    stats["hit"] += try_candidate(
                                        match.group(0).decode("ascii").lower(), salt_hex)
                            else:
                                # 逐字节滑窗是有意为之：不同构建的 C++ 对齐不同。
                                for off in range(0, max(0, len(window) - KEY_SZ + 1)):
                                    candidate = window[off:off + KEY_SZ]
                                    if _looks_like_key(candidate):
                                        stats["hit"] += try_candidate(candidate.hex(), salt_hex)
                            pos = data.find(pattern, pos + 1)

            # 复用 Windows 4.1.11+ 的 Config.Cipher 对象路径。Linux 版的
            # WCDB 也使用同名配置；此路径失配时只会得到 0 候选。
            if len(self.keys) != len(self.salt_index):
                readable = _linux_regions(pid, writable_only=False)
                readable.sort()
                readable_starts = [base for base, _size in readable]

                def is_readable_address(addr):
                    index = bisect.bisect_right(readable_starts, addr) - 1
                    if index < 0:
                        return False
                    start, size = readable[index]
                    return start <= addr < start + size

                needle_addrs = set()
                for base, data in _iter_chunks(readable, read_region,
                                               overlap=len(CONFIG_CIPHER_NAME) - 1):
                    pos = data.find(CONFIG_CIPHER_NAME)
                    while pos >= 0:
                        needle_addrs.add(base + pos)
                        pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
                stats["needle"] = len(needle_addrs)
                pairs = [struct.pack("<Q", addr) +
                         struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                         for addr in needle_addrs]
                ref_addrs = set()
                for base, data in _iter_chunks(regions, read_region, overlap=0x80):
                    if len(self.keys) == len(self.salt_index):
                        break
                    for pair in pairs:
                        pos = data.find(pair)
                        while pos >= 0:
                            ref_addrs.add(base + pos)
                            node = read_region(base + pos - 0x10, 0x50)
                            if node and len(node) >= 0x40:
                                cfg = _u64(node, 0x28)
                                if 0x10000 <= cfg < MAX_USER_ADDRESS:
                                    obj = read_region(cfg + 0x88, 0x28)
                                    if obj and len(obj) >= 0x18:
                                        dptr, dlen = _u64(obj, 0x8), _u64(obj, 0x10)
                                        if (0 < dlen <= CONFIG_BLOB_MAX and
                                                0x10000 <= dptr < MAX_USER_ADDRESS):
                                            blob = read_region(dptr, int(dlen))
                                            if blob and len(blob) == dlen:
                                                stats["blobs"] += 1
                                                raw_candidates = _search_linux_literals(blob)
                                                masked_candidates = _candidates_from_config_blob(blob)
                                                for key_hex, salt_hex in (raw_candidates +
                                                                          masked_candidates):
                                                    stats["hit"] += try_candidate(
                                                        key_hex, salt_hex)
                            pos = data.find(pair, pos + 1)
                stats["refs"] = len(ref_addrs)

                # Linux 版的 STL/ABI 与 Windows 不同，固定 +0x28/+0x88 布局
                # 不能作为唯一路径。从 Config.Cipher 引用周边的指针出发，
                # 受限遍历两层小对象，只对合理的 (data*, length) 读取候选。
                def scan_small_blob(blob):
                    if not blob or len(blob) > CONFIG_BLOB_MAX:
                        return
                    for key_hex, salt_hex in _search_linux_literals(blob):
                        stats["hit"] += try_candidate(key_hex, salt_hex)
                    for key_hex, salt_hex in _candidates_from_config_blob(blob):
                        stats["hit"] += try_candidate(key_hex, salt_hex)

                    if len(blob) == KEY_SZ and _looks_like_key(blob):
                        stats["hit"] += try_candidate(blob.hex(), None)
                    if len(blob) in (64, 96):
                        try:
                            text = blob.decode("ascii")
                        except UnicodeDecodeError:
                            text = ""
                        if re.fullmatch(r"[0-9a-fA-F]+", text):
                            salt_hint = text[64:96].lower() if len(text) == 96 else None
                            stats["hit"] += try_candidate(text[:64].lower(), salt_hint)

                    for salt_hex in self.salt_index:
                        salt = bytes.fromhex(salt_hex)
                        pos = blob.find(salt)
                        while pos >= 0:
                            stats["salt_markers"] += 1
                            left = max(0, pos - 96)
                            right = min(len(blob), pos + SALT_SZ + 96)
                            for off in range(left, max(left, right - KEY_SZ + 1)):
                                candidate = blob[off:off + KEY_SZ]
                                if _looks_like_key(candidate):
                                    stats["hit"] += try_candidate(candidate.hex(), salt_hex)
                            pos = blob.find(salt, pos + 1)

                queue = []
                queued = set()
                for ref_addr in ref_addrs:
                    around = read_region(max(0, ref_addr - 0x80), 0x180) or b""
                    for off in range(0, len(around) - 7, 8):
                        ptr = _u64(around, off)
                        if is_readable_address(ptr) and ptr not in queued:
                            queued.add(ptr)
                            queue.append((ptr, 0))

                cursor = 0
                max_nodes = 20000
                while (cursor < len(queue) and stats["graph_nodes"] < max_nodes
                       and len(self.keys) != len(self.salt_index)):
                    addr, depth = queue[cursor]
                    cursor += 1
                    obj = read_region(addr, 0x200) or b""
                    if not obj:
                        continue
                    stats["graph_nodes"] += 1
                    scan_small_blob(obj)
                    for off in range(0, len(obj) - 7, 8):
                        ptr = _u64(obj, off)
                        if not is_readable_address(ptr):
                            continue
                        if depth < 2 and ptr not in queued:
                            queued.add(ptr)
                            queue.append((ptr, depth + 1))
                        for len_off in (off - 8, off + 8, off + 16):
                            length = _u64(obj, len_off)
                            if not 1 <= length <= CONFIG_BLOB_MAX:
                                continue
                            blob = read_region(ptr, int(length)) or b""
                            if len(blob) != length:
                                continue
                            stats["data_pairs"] += 1
                            scan_small_blob(blob)
        finally:
            os.close(fd)
        return stats

    def scan_process(self, pid: int) -> dict:
        if IS_LINUX:
            return self._scan_process_linux(pid)
        if not IS_WINDOWS:
            return {"error": "当前平台尚不支持内存密钥提取"}
        h = _open(pid)
        if not h:
            return {"error": "OpenProcess 失败（错误码 %d），请用管理员身份运行"
                             % ctypes.get_last_error()}
        stats = {"pid": pid, "needle": 0, "refs": 0, "nodes": 0,
                 "config_objs": 0, "blobs": 0, "candidates": 0, "hit": 0}
        try:
            regions = _regions(h)
            rd = lambda a, s: _read(h, a, s)

            needle_addrs = set()
            for base, data in _iter_chunks(regions, rd,
                                           overlap=len(CONFIG_CIPHER_NAME) - 1):
                p = data.find(CONFIG_CIPHER_NAME)
                while p >= 0:
                    needle_addrs.add(base + p)
                    p = data.find(CONFIG_CIPHER_NAME, p + 1)
            stats["needle"] = len(needle_addrs)
            if not needle_addrs:
                return stats

            pairs = [struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                     for a in needle_addrs]
            seen_objs = set()
            for base, data in _iter_chunks(regions, rd, overlap=0x80):
                if len(self.keys) == len(self.salt_index):
                    break
                for pat in pairs:
                    pos = data.find(pat)
                    while pos >= 0:
                        if len(self.keys) == len(self.salt_index):
                            break
                        stats["refs"] += 1
                        node = rd(base + pos - 0x10, 0x50)
                        if node and len(node) >= 0x40:
                            if (_u64(node, 0x10) in needle_addrs
                                    and _u64(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                                cfg = _u64(node, 0x28)
                                if 0x10000 <= cfg < MAX_USER_ADDRESS:
                                    stats["nodes"] += 1
                                    seen_objs.add(cfg)
                                    obj = rd(cfg + 0x88, 0x28)
                                    if obj and len(obj) >= 0x18:
                                        dptr, dlen = _u64(obj, 0x8), _u64(obj, 0x10)
                                        if (0 < dlen <= CONFIG_BLOB_MAX
                                                and 0x10000 <= dptr < MAX_USER_ADDRESS):
                                            blob = rd(dptr, int(dlen))
                                            if blob and len(blob) == dlen:
                                                stats["blobs"] += 1
                                                for kh, sh in _candidates_from_config_blob(blob):
                                                    stats["candidates"] += 1
                                                    stats["hit"] += self._try_key(kh, sh)
                        pos = data.find(pat, pos + 1)
            stats["config_objs"] = len(seen_objs)
        finally:
            kernel32.CloseHandle(h)
        return stats

    # -- 外一条：passphrase --------------------------------------------
    def scan_process_passphrase(self, pid: int) -> int:
        """把 Config.Cipher 里的候选当作 passphrase，逐库派生再校验。

        只有少数 4.1+ 版本会出现这种情况，而且每个候选 × 每个库都要跑一次
        256000 轮 PBKDF2（约 0.1 秒），所以总次数做了上限保护。
        """
        if IS_LINUX:
            return self._scan_process_linux(pid, allow_passphrase=True).get("hit", 0)
        if not IS_WINDOWS:
            return 0
        h = _open(pid)
        if not h:
            return 0
        hit = 0
        try:
            regions = _regions(h)
            rd = lambda a, s: _read(h, a, s)
            needle_addrs = set()
            for base, data in _iter_chunks(regions, rd,
                                           overlap=len(CONFIG_CIPHER_NAME) - 1):
                p = data.find(CONFIG_CIPHER_NAME)
                while p >= 0:
                    needle_addrs.add(base + p)
                    p = data.find(CONFIG_CIPHER_NAME, p + 1)
            if not needle_addrs:
                return 0
            pairs = [struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                     for a in needle_addrs]
            for base, data in _iter_chunks(regions, rd, overlap=0x80):
                if self.keys or self._passphrase_attempts >= 64:
                    break
                for pat in pairs:
                    pos = data.find(pat)
                    while pos >= 0:
                        if self.keys or self._passphrase_attempts >= 64:
                            break
                        node = rd(base + pos - 0x10, 0x50)
                        if node and len(node) >= 0x40:
                            if (_u64(node, 0x10) in needle_addrs
                                    and _u64(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                                cfg = _u64(node, 0x28)
                                if 0x10000 <= cfg < MAX_USER_ADDRESS:
                                    obj = rd(cfg + 0x88, 0x28)
                                    if obj and len(obj) >= 0x18:
                                        dptr, dlen = _u64(obj, 0x8), _u64(obj, 0x10)
                                        if (0 < dlen <= CONFIG_BLOB_MAX
                                                and 0x10000 <= dptr < MAX_USER_ADDRESS):
                                            blob = rd(dptr, int(dlen))
                                            if blob and len(blob) == dlen:
                                                for kh, sh in _candidates_from_config_blob(blob):
                                                    hit += self._try_key(kh, sh, True)
                        pos = data.find(pat, pos + 1)
        finally:
            kernel32.CloseHandle(h)
        return hit

    # -- 回退：4.0.x 明文 raw key ---------------------------------------
    def scan_legacy(self, pid: int) -> int:
        if IS_LINUX:
            return 0  # Linux 主扫描已直接处理明文 WCDB literal
        if not IS_WINDOWS:
            return 0
        h = _open(pid)
        if not h:
            return 0
        hit = 0
        try:
            for base, size in _regions(h):
                if len(self.keys) == len(self.salt_index):
                    break
                data = _read(h, base, size)
                if not data:
                    continue
                for m in HEX_LITERAL_RE.finditer(data):
                    run = m.group(1).decode().lower()
                    if len(run) < 96:
                        continue
                    hit += self._try_key(run[:64], run[64:96])
        finally:
            kernel32.CloseHandle(h)
        return hit

    # -- 对外入口 --------------------------------------------------------
    def extract(self, save_to: str | None = None,
                try_passphrase: bool = True) -> dict[str, str]:
        self.collect()
        if IS_WINDOWS:
            enable_debug_privilege()
        procs = find_weixin_processes()
        if not procs:
            raise RuntimeError("未检测到微信进程，请先启动并登录微信")
        self.progress("检测到 %d 个微信进程" % len(procs))
        self.stats["processes"] = procs
        t0 = time.time()
        for p in procs:
            if len(self.keys) == len(self.salt_index):
                break
            self.progress("扫描进程 PID=%d (%d MB) ..." % (p["pid"], p["mem_kb"] // 1024))
            st = self.scan_process(p["pid"])
            self.stats.setdefault("scan", []).append(st)
            if st.get("error"):
                self.progress("  " + st["error"])
            else:
                label = "WCDB literal" if IS_LINUX else "Config.Cipher"
                self.progress("  %s 命中 %d 个密钥（候选 %d）"
                              % (label, st.get("hit", 0), st.get("candidates", 0)))
                if IS_LINUX:
                    self.progress("  扫描 %.1f MB，salt 锚点 %d，Config 特征 %d/"
                                  "引用 %d/blob %d，图节点 %d/数据对 %d"
                                  % (st.get("bytes", 0) / 1024 / 1024,
                                     st.get("salt_markers", 0), st.get("needle", 0),
                                     st.get("refs", 0), st.get("blobs", 0),
                                     st.get("graph_nodes", 0), st.get("data_pairs", 0)))
        if len(self.keys) != len(self.salt_index):
            self.progress("尝试 4.0.x 兼容路径（明文 raw key 扫描）...")
            for p in procs:
                if len(self.keys) == len(self.salt_index):
                    break
                n = self.scan_legacy(p["pid"])
                if n:
                    self.progress("  PID=%d 命中 %d" % (p["pid"], n))
        if try_passphrase and len(self.keys) != len(self.salt_index) and not self.keys:
            # 一条都没命中时才试昂贵的 passphrase 路径（少数 4.1+ 版本内存里
            # 只剩口令，需要按库的 salt 再走 256000 轮 PBKDF2）
            self.progress("尝试 passphrase 路径（较慢）...")
            for p in procs:
                if self.keys:
                    break
                n = self.scan_process_passphrase(p["pid"])
                if n:
                    self.progress("  PID=%d 用 passphrase 派生命中 %d" % (p["pid"], n))
        self.stats["elapsed"] = time.time() - t0
        self.stats["resolved"] = len(self.keys)
        self.stats["total_salts"] = len(self.salt_index)
        if save_to and self.keys:
            self.save(save_to)
        return self.keys

    # -- 结果保存 / 读取 --------------------------------------------------
    def result_payload(self) -> dict:
        out = {
            "_db_storage": self.db_storage,
            "_resolved": len(self.keys),
            "_total": len(self.salt_index),
        }
        for item in self.db_files:
            salt = item["salt"]
            if salt in self.keys:
                out[item["rel"]] = {"enc_key": self.keys[salt], "salt": salt}
        return out

    def save(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".wx4-key-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            # pkexec/sudo 只用于 Linux 读取 /proc/<pid>/mem；密钥文件仍归
            # 原登录用户所有，避免后续解密必须以 root 运行。
            if IS_LINUX and pwd is not None and os.geteuid() == 0:
                uid_text = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
                if uid_text and uid_text.isdigit():
                    uid = int(uid_text)
                    try:
                        gid = pwd.getpwuid(uid).pw_gid
                        os.fchown(fd, uid, gid)
                    except (KeyError, OSError):
                        pass
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = -1
                json.dump(self.result_payload(), f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        self.progress("密钥已保存到私有文件")


# ---------------------------------------------------------------------------
# passphrase / 手工密钥支持
# ---------------------------------------------------------------------------
def keys_from_passphrase(passphrase_hex: str, db_storage: str, progress=None) -> dict:
    """已知 32 字节 passphrase 时，逐库派生并校验密钥。"""
    progress = progress or (lambda m: None)
    if len(passphrase_hex) != 64:
        raise ValueError("passphrase 必须是 64 位十六进制字符串")
    ex = KeyExtractor(db_storage, progress)
    ex.collect()
    ph = bytes.fromhex(passphrase_hex)
    for salt_hex in list(ex.salt_index):
        item = ex.salt_index[salt_hex][0]
        key = derive_enc_key(ph, bytes.fromhex(salt_hex))
        if verify_enc_key(key, item["page1"]):
            ex.keys[salt_hex] = key.hex()
    progress("passphrase 派生命中 %d/%d" % (len(ex.keys), len(ex.salt_index)))
    return ex.keys


def load_keys(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import sys

    storage = sys.argv[1] if len(sys.argv) > 1 else None
    if not storage:
        from . import paths
        acc = paths.find_account()
        storage = acc["db_storage"] if acc else None
    if not storage:
        print("找不到 db_storage，请传入路径")
        raise SystemExit(1)
    ex = KeyExtractor(storage, progress=lambda m: print("[*]", m))
    ex.extract(save_to="wx4_keys.json")
    print(json.dumps({k: v[:16] + "..." for k, v in ex.keys.items()}, indent=2))
