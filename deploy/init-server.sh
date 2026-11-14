#!/usr/bin/env bash
# 新服务器一键初始化（腾讯云轻量 Ubuntu 22.04+ 适用）
#
# 用法：
#   1. ssh root@<服务器IP>
#   2. bash <(curl -fsSL https://raw.githubusercontent.com/jezecheng/GradeLogy/main/deploy/init-server.sh)
#   3. 按提示填 GitHub 仓库地址、部署目录、AI 配置
#
# 脚本完成后，服务器上会：
#   - 装 Docker + Docker Compose v2 + git
#   - clone 项目到 /srv/gradelogy（可改）
#   - 生成 .env 环境变量文件（AI Key / SECRET_KEY 等）
#   - 首次 docker compose up -d 启动容器
#   - 验证 http://127.0.0.1:5000/healthz 返回 200

set -euo pipefail

APP_DIR="${APP_DIR:-/srv/gradelogy}"
GIT_REPO="${GIT_REPO:-https://github.com/jezecheng/GradeLogy.git}"
BRANCH="${BRANCH:-main}"

echo "========== GradeLogy 服务器初始化 =========="
echo "App dir : $APP_DIR"
echo "Repo    : $GIT_REPO (branch $BRANCH)"
echo "============================================"

# ---------- 1. 基础依赖 ----------
if ! command -v docker &>/dev/null; then
  echo "[1/5] 安装 Docker ..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo "[1/5] Docker 已安装: $(docker --version)"
fi
if ! docker compose version &>/dev/null; then
  # 新版 docker 自带 compose 子命令
  docker_ver=$(docker --version)
  echo "[1/5] Docker Compose check ... ($docker_ver)"
fi

if ! command -v git &>/dev/null; then
  apt-get update && apt-get install -y git curl openssl
fi

# ---------- 2. 生成 SECRET_KEY ----------
DEFAULT_SECRET=$(openssl rand -hex 32)
read -rp "SECRET_KEY（直接回车自动生成）: " SECRET_KEY
SECRET_KEY="${SECRET_KEY:-$DEFAULT_SECRET}"

# ---------- 3. AI 配置 ----------
echo ""
echo "AI 配置（可全部留空，之后在网页「AI 设置」里填）："
read -rp "AI_API_URL  [https://tokenhub.tencentmaas.com/v1]: " AI_API_URL
AI_API_URL="${AI_API_URL:-https://tokenhub.tencentmaas.com/v1}"
read -rp "AI_API_KEY  [留空则走网页配置]: " AI_API_KEY
read -rp "AI_MODEL    [留空则走网页配置]: " AI_MODEL

# ---------- 4. Clone 项目 ----------
mkdir -p "$(dirname "$APP_DIR")"
if [ -d "$APP_DIR/.git" ]; then
  echo "[2/5] 已有项目，git pull ..."
  cd "$APP_DIR"
  git fetch --depth=1 origin "$BRANCH"
  git reset --hard "origin/$BRANCH"
else
  echo "[2/5] Clone 项目到 $APP_DIR ..."
  git clone --depth=1 --branch "$BRANCH" "$GIT_REPO" "$APP_DIR"
  cd "$APP_DIR"
fi

# ---------- 5. 写入 .env ----------
echo "[3/5] 写入 .env ..."
cat > .env <<EOF
TZ=Asia/Shanghai
SECRET_KEY=${SECRET_KEY}
AI_API_URL=${AI_API_URL}
AI_API_KEY=${AI_API_KEY}
AI_MODEL=${AI_MODEL}
LOG_LEVEL=INFO
EOF
chmod 600 .env

# ---------- 6. 首次启动 ----------
echo "[4/5] docker compose build + up ..."
export GIT_COMMIT="$(git rev-parse --short HEAD)"
export GIT_BRANCH="$BRANCH"
docker compose build --no-cache
docker compose up -d

# ---------- 7. 健康检查 ----------
echo "[5/5] 等待健康检查 ..."
sleep 10
if curl -sf http://127.0.0.1:5000/healthz >/dev/null; then
  echo ""
  echo "🎉 初始化完成！"
  echo "   本地访问：http://127.0.0.1:5000"
  echo "   公网访问：http://$(curl -s ifconfig.me || echo '<服务器公网IP>'):5000"
  echo ""
  echo "下一步："
  echo "   1. GitHub 仓库 Settings → Secrets，填 SERVER_HOST / SERVER_USER / SERVER_SSH_KEY / APP_DIR"
  echo "   2. 之后每次 git push main → 自动部署"
else
  echo "❌ 健康检查失败，查看日志：docker compose logs"
  exit 1
fi
