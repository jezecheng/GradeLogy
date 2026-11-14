#!/usr/bin/env bash
# 群晖 NAS 定时拉取更新：配合 DSM「任务计划」每分钟跑一次即可实现 push 后自动更新。
#
# 前置：项目已 clone 到 APP_DIR（默认 /volume1/docker/gradelogy），且已安装 Container Manager。
# 用法：
#   bash /volume1/docker/gradelogy/deploy/nas-update.sh
# 环境变量：
#   APP_DIR  项目目录，默认 /volume1/docker/gradelogy
#   BRANCH   跟踪分支，默认 main
#
# 行为：发现远程有新提交才重建容器；没有变化时静默退出，避免刷日志。

set -euo pipefail

APP_DIR="${APP_DIR:-/volume1/docker/gradelogy}"
BRANCH="${BRANCH:-main}"
LOCK_DIR="$APP_DIR/.update.lock"

log() { echo "[$(date '+%F %T')] $*"; }

if [ ! -d "$APP_DIR/.git" ]; then
  log "错误：$APP_DIR 不是 git 仓库，请先用 SSH clone 项目"
  exit 1
fi

# 同一时间只允许一个更新进程（任务计划间隔短于构建耗时时会重叠）
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$APP_DIR"
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  log "错误：未找到 docker compose，请在套件中心安装 Container Manager"
  exit 1
fi

# 首次运行时生成会话密钥；.env 不在 git 里，更新不会覆盖
if [ ! -f .env ]; then
  if command -v openssl >/dev/null 2>&1; then
    SECRET=$(openssl rand -hex 32)
  else
    SECRET=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
  fi
  printf 'TZ=Asia/Shanghai\nSECRET_KEY=%s\nLOG_LEVEL=INFO\n' "$SECRET" > .env
  chmod 600 .env
  log "已生成 .env（含 SECRET_KEY）"
fi

git fetch --depth=1 origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  # 代码没变，但容器可能还没启动（首次部署或 NAS 重启后）
  if docker ps --format '{{.Names}}' | grep -qx gradelogy; then
    exit 0
  fi
  log "代码无变化，容器未运行，开始启动"
else
  log "发现新提交 ${LOCAL:0:7} -> ${REMOTE:0:7}，开始更新"
  git reset --hard "origin/$BRANCH"
fi

export GIT_COMMIT="$(git rev-parse --short HEAD)"
export GIT_BRANCH="$BRANCH"
"${DC[@]}" build
"${DC[@]}" up -d

sleep 8
if curl -fsS http://127.0.0.1:5000/healthz >/dev/null; then
  log "更新完成：$(git rev-parse --short HEAD)"
else
  log "健康检查失败，请执行 docker compose logs --tail=100 查看"
  exit 1
fi
