# -*- coding: utf-8 -*-
"""本地隐私产物的最小权限辅助函数。"""
from __future__ import annotations

import os


def private_dir(path: str) -> str:
    path = os.path.abspath(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def private_file(path: str) -> str:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
