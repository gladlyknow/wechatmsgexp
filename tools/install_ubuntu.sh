#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
WX4_PYTHON="${WX4_PYTHON:-python3}"

"${WX4_PYTHON}" -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10 或更高版本"'

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "未找到 ffmpeg。请先运行：sudo apt install ffmpeg python3-venv" >&2
  exit 2
fi

if [ ! -x "${PROJECT_DIR}/.venv/bin/python" ]; then
  "${WX4_PYTHON}" -m venv "${PROJECT_DIR}/.venv"
fi

"${PROJECT_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${PROJECT_DIR}/.venv/bin/python" -m pip install -r "${PROJECT_DIR}/requirements-linux-mvp.txt"
mkdir -p "${PROJECT_DIR}/mvp-output"
chmod 700 "${PROJECT_DIR}/mvp-output"

"${PROJECT_DIR}/.venv/bin/python" -m compileall -q "${PROJECT_DIR}/wx4"
echo "安装完成。运行方式见 docs/UBUNTU_OPERATIONS.md"
