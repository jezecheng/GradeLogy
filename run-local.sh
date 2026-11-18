#!/bin/sh
# Start GradeLogy with an isolated local data directory.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    echo "缺少本地 Python 环境，请先安装项目依赖。" >&2
    exit 1
fi

export DATA_DIR="${DATA_DIR:-$PROJECT_DIR/data/local}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-5000}"
export AUTO_BACKUP="${AUTO_BACKUP:-0}"
export AUTO_MODEL_SYNC="${AUTO_MODEL_SYNC:-0}"
export AUTO_GRADE="${AUTO_GRADE:-0}"

cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/app.py"
