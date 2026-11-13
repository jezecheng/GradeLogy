# GradeLogy 一键部署指南

## 架构总览

```
本地改代码 → git push main → GitHub Actions SSH → 服务器 git pull + docker compose up
                                      ↓
                              腾讯云轻量 + Docker
                              （gunicorn 单进程 8 线程）
                              /data 持久化 SQLite + 上传件
                                      ↓
                              公网暴露：
                              方案 A: Cloudflare Tunnel（推荐，免费+TLS）
                              方案 B: 裸 IP:5000（简单但 HTTP 明文）
```

## 一次性准备（约 20 分钟）

### 第 1 步：买一台腾讯云轻量服务器

| 项 | 建议 |
|---|---|
| 地域 | **香港**（免备案 + 与 TokenHub 同属腾讯云，延迟低） |
| 配置 | 2C2G 起步，月费约 50~80 元 |
| 镜像 | Ubuntu 22.04 LTS |

买完后记录：
- **公网 IP**（如 `43.136.45.xx`）
- **root 密码**或创建一个 `ubuntu` 用户并加 sudo

腾讯云控制台 → 轻量应用服务器 → 防火墙 → 放行端口 `22`（SSH）、`5000`（应用）、可选 `80/443`。

### 第 2 步：服务器初始化

SSH 登录后，一行命令完成（自动装 Docker、clone 项目、首次启动）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jezecheng/GradeLogy/main/deploy/init-server.sh)
```

脚本会问你：
- SECRET_KEY → 直接回车自动生成（**服务器上的密钥与本地不同，重启本地 app.py 时重新生成即可，不冲突**）
- AI 配置 → 可全部留空，之后在网页「AI 设置」里填

完成后会显示健康检查通过和公网访问地址。验证一下：
```bash
curl http://127.0.0.1:5000/healthz   # 应返回 200
curl http://<你的公网IP>:5000/healthz  # 应返回 200（腾讯云防火墙已放行 5000）
```

### 第 3 步：生成 SSH 密钥对

```bash
ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/gradelogy_deploy -N ""
cat ~/.ssh/gradelogy_deploy.pub   # 复制公钥
```

服务器上：
```bash
cat ~/.ssh/gradelogy_deploy.pub >> ~/.ssh/authorized_keys
```

### 第 4 步：配置 GitHub Secrets

仓库 Settings → Secrets and variables → Actions → New repository secret，填 4 个：

| Secret | 值 |
|---|---|
| `SERVER_HOST` | 服务器公网 IP |
| `SERVER_USER` | `root` 或 `ubuntu` |
| `SERVER_SSH_KEY` | `cat ~/.ssh/gradelogy_deploy` 的完整内容（私钥，以 `-----BEGIN OPENSSH...` 开头） |
| `APP_DIR` | `/srv/gradelogy`（脚本默认的目录） |

（可选）再加 `SECRET_KEY`、`AI_API_URL`、`AI_API_KEY`、`AI_MODEL`——填了会覆盖服务器上 `.env` 的值。

## 后续一键发布（每次）

```bash
git add . && git commit -m "fix: xxx" && git push
```

之后**什么都不用做**。等 20~40 秒，GitHub Actions 自动完成：
- SSH 登录服务器
- `git pull --depth=1`
- `docker compose build && docker compose up -d`
- 健康检查

去 https://github.com/jezecheng/GradeLogy/actions 可以看到实时日志。

---

## 公网暴露：两种方案

### 方案 A：Cloudflare Tunnel（强烈推荐）

完全免费，自动分配域名 + TLS，**不需要买域名、不需要备案**。服务器不用开 5000 端口（只出不进）。

服务器上：
```bash
# 1. 装 cloudflared
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# 2. 登录（浏览器打开，扫码或选 Cloudflare 免费账号）
cloudflared tunnel login

# 3. 创建一个固定隧道
cloudflared tunnel create gradelogy

# 4. 配置（记住隧道 ID，如 12345678-abcd...）
mkdir -p ~/.cloudflared
cat > ~/.cloudflared/config.yml <<EOF
tunnel: gradelogy
credentials-file: ~/.cloudflared/<隧道ID>.json
ingress:
  - hostname: gradelogy.<你的Cloudflare域名>.cfargotunnel.com
    service: http://127.0.0.1:5000
  - service: http_status:404
EOF

# 5. 注册为系统服务 + 开机自启
cloudflared service install
systemctl start cloudflared
```

注册后 Cloudflare 会给一个类似 `gradelogy-xxx.cfargotunnel.com` 的固定域名，HTTPS 自动生效。**学生用这个域名上课。**

腾讯云防火墙可以关掉 5000 端口（隧道只走服务器出站的 443），安全性更高。

### 方案 B：裸 IP + HTTP（简单但不安全）

腾讯云防火墙放行 5000 端口后，学生直接访问 `http://<公网IP>:5000`。

⚠️ **风险**：密码、会话 Cookie、学生上传的作业文件全部 HTTP 明文传输。公网下任何人都能抓包。仅限内部测试或小范围使用。

## 数据备份

所有持久化数据在服务器 `/srv/gradelogy/data/` 里：
- `data.sqlite`（数据库）
- `uploads/`（学生上传的 PDF/图片）
- `.secret_key`（Flask 密钥）

备份命令（每月手动或写 cron）：
```bash
tar czf gradelogy-backup-$(date +%F).tar.gz /srv/gradelogy/data/
# 下载到本地
scp root@<IP>:/srv/gradelogy-backup-*.tar.gz ~/
```

## 常见问题

| 现象 | 处理 |
|---|---|
| Actions 跑失败，SSH 连不上 | 检查服务器安全组 22 端口、服务器上 `sshd` 是否运行、私钥格式是否完整 |
| AI 调用延迟高 | 腾讯云轻量香港与 TokenHub 同属腾讯云，理论上内网；若延迟仍高，换内网接口地址 |
| Docker 国内拉不了镜像 | `docker compose build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim` |
| 服务器内存不够 OOM | 2C2G 跑 20~30 人同时够用；更多人升到 4G |
| 服务器迁移 | 把旧服务器的 `data/` 目录整体拷到新服务器，重跑 `init-server.sh`（选已有目录） |
