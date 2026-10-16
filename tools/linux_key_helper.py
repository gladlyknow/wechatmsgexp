#!/usr/bin/env python3
"""Minimal privileged entry point for reading Linux WeChat process memory.

Only key extraction runs with elevated privileges. Database decryption, reading,
and export must run as the desktop user.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from wx4.key import KeyExtractor  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WeChat Linux key extraction helper")
    parser.add_argument("--db-storage", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--no-passphrase", action="store_true",
                        help="skip the slow passphrase fallback (diagnostics)")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("linux"):
        parser.error("this helper only supports Linux")
    if os.geteuid() != 0:
        parser.error("run this helper through pkexec or sudo")

    storage = os.path.realpath(args.db_storage)
    if not os.path.isdir(storage) or os.path.basename(storage) != "db_storage":
        parser.error("--db-storage must point to an existing db_storage directory")

    key_file = os.path.abspath(args.key_file)
    parent = os.path.realpath(os.path.dirname(key_file))
    if not os.path.isdir(parent):
        parser.error("the key output directory must already exist")
    if os.path.dirname(key_file) != parent:
        parser.error("the key output directory may not be a symlink")

    extractor = KeyExtractor(storage, progress=lambda message: print(message, flush=True))
    extractor.extract(save_to=key_file, try_passphrase=not args.no_passphrase)
    if not extractor.keys:
        print("未找到通过 HMAC 校验的密钥", file=sys.stderr)
        return 2
    print("已验证 %d/%d 个数据库密钥" %
          (len(extractor.keys), len(extractor.salt_index)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
