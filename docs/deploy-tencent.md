# 腾讯云轻量服务器部署与自动发布

这份指南面向 **Fork 本项目后，希望部署到自己腾讯云服务器** 的使用者。完成后会得到：

- 一个可通过 `http://服务器IP:5000` 访问的 GradeLogy 实例
- Push 到自己仓库的 `main` 分支后，GitHub Actions 自动更新服务器
- 账号、密码、AI 配置和上传文件在重新构建后仍然保留

以下步骤以 **腾讯云轻量应用服务器 + Ubuntu 22.04 + Docker Compose** 为例。命令中的 `YOUR_NAME`、`YOUR_REPO` 和 `SERVER_IP` 需要替换成自己的值。

> 如果只是在自己的 Windows / Mac 电脑上使用，不需要公网自动发布，请改用 [单机简易部署](deploy-standalone.md)。

## 1. 准备 Fork 和云服务器

1. 在 GitHub 项目页面点击 **Fork**，得到自己的仓库。
2. 在腾讯云购买或创建一台轻量应用服务器：
   - 地域选择离使用者较近的区域
   - 镜像选择 **Ubuntu 22.04**，不必选择应用镜像或容器镜像
   - 小型教学场景建议至少 2 核 2 GB 内存，并预留足够磁盘存放学生 PDF
   - 登录方式选择 SSH 密钥；妥善保存下载的私钥
3. 在腾讯云 **防火墙** 中添加：
   - TCP 22：初期可只允许自己的公网 IP，用于 SSH 管理
   - TCP 5000：测试阶段允许 `0.0.0.0/0`，用于访问网页
4. 用腾讯云提供的私钥登录服务器。Windows 可使用 PowerShell，macOS 可使用“终端”：

```bash
ssh -i /path/to/your-server-key.pem ubuntu@SERVER_IP
```

## 2. 安装 Docker

以下命令使用腾讯云 Docker 软件源，避免 `get.docker.com` 在国内连接被重置。

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

配置镜像加速并启动 Docker：

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io"
  ]
}
EOF
sudo systemctl enable --now docker
sudo systemctl restart docker
sudo usermod -aG docker "$USER"
```

**退出 SSH 并重新登录一次**，让 Docker 用户组生效，然后检查：

```bash
docker --version
docker compose version
```

如果之后仍看到 `permission denied ... /var/run/docker.sock`，通常就是忘记重新登录。

## 3. 让服务器只读拉取自己的 Fork

国内服务器直接走 GitHub HTTPS 可能不稳定。这里使用 **SSH 443 端口 + 只读 Deploy Key**，与后面 GitHub Actions 登录服务器所用的密钥是两回事。

在服务器生成一对专用于拉取代码的密钥：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

复制输出的整行公钥，在自己的 **Fork 仓库**中打开：

**Settings → Deploy keys → Add deploy key**

- Title 可填 `tencent-deploy`
- 粘贴公钥
- 不勾选 **Allow write access**，保持只读

回到服务器，配置 SSH 走 443 端口：

```bash
cat > ~/.ssh/config <<'EOF'
Host github.com
  HostName ssh.github.com
  Port 443
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com
```

看到 `Hi YOUR_NAME!` 即表示认证成功。`ssh -T` 不提供服务端 Shell，因此显示成功后直接结束是正常的。

## 4. 拉取代码并准备持久化配置

将下面的 `YOUR_NAME/YOUR_REPO` 换成自己的 Fork：

```bash
sudo mkdir -p /srv
sudo chown "$USER":"$USER" /srv
git clone git@github.com:YOUR_NAME/YOUR_REPO.git /srv/gradelogy
cd /srv/gradelogy
```

创建 `.env`。`SECRET_KEY` 会随机生成，不需要手工填写；AI 配置可以先留空，之后在网页中设置。

```bash
SECRET_KEY=$(openssl rand -hex 32)
cat > .env <<EOF
TZ=Asia/Shanghai
SECRET_KEY=${SECRET_KEY}
COOKIE_SECURE=0
AI_API_URL=
AI_API_KEY=
AI_MODEL=
LOG_LEVEL=INFO
EOF
chmod 600 .env
```

提前创建数据目录，并把所有者设为容器运行用户的 UID 1000。**这一步不能省略**，否则应用可能因无法创建 `/data/uploads` 而反复重启。

```bash
mkdir -p data
sudo chown -R 1000:1000 data
```

`.env` 和 `data/` 都不会被 Git 跟踪，也不会被打进 Docker 镜像。

## 5. 首次构建与启动

```bash
cd /srv/gradelogy
export GIT_COMMIT="$(git rev-parse --short HEAD)"
export GIT_BRANCH=main
docker compose build
docker compose up -d
sleep 10
curl -fsS http://127.0.0.1:5000/healthz
docker compose ps
```

健康检查返回 200，且容器状态为 `Up` 后，访问：

```text
http://SERVER_IP:5000/login
```

首次登录使用 `admin / admin123`，系统会要求立即修改密码。

> 当前地址是 HTTP，不是 HTTPS。测试可以使用临时数据；正式录入真实学生资料或 AI Key 前，应配置域名和 HTTPS，并将 `.env` 中的 `COOKIE_SECURE` 改为 `1`，再运行 `docker compose up -d`。

## 6. 配置 GitHub Actions 自动部署

Fork **不会继承原仓库的 Actions Secrets**，必须在自己的 Fork 中重新配置。

打开 **Settings → Secrets and variables → Actions → Repository secrets**，添加：

| Secret | 填写内容 |
|---|---|
| `SERVER_HOST` | 自己的服务器公网 IP |
| `SERVER_USER` | `ubuntu` |
| `SERVER_SSH_KEY` | 能登录这台服务器的 SSH 私钥完整内容，包含 BEGIN / END 行 |
| `APP_DIR` | `/srv/gradelogy` |

注意区分两把钥匙：

| 用途 | 公钥放在哪里 | 私钥放在哪里 |
|---|---|---|
| 服务器从 GitHub 拉代码 | Fork 的 Deploy keys | 服务器 `~/.ssh/github_deploy`，不要外传 |
| GitHub Actions 登录服务器 | 服务器 `~/.ssh/authorized_keys` | Fork 的 `SERVER_SSH_KEY` Secret |

### 允许 GitHub Actions 连接 SSH

GitHub Actions 来自 GitHub 云端，不使用你本机的公网 IP。只放行自己的 IP 会导致：

```text
dial tcp ...:22: i/o timeout
```

可选择以下一种方式：

1. **配置较简单**：在密钥登录已验证可用、密码登录已关闭的前提下，允许 TCP 22 来源 `0.0.0.0/0`。
2. **限制更严格**：把 GitHub 官方 `actions` IP 段加入白名单。IP 段可从 `https://api.github.com/meta` 获取，官方调整后需要同步更新。

建议在服务器确认 SSH 已关闭密码和 root 直接登录：

```bash
sudo tee /etc/ssh/sshd_config.d/00-gradelogy-ssh.conf >/dev/null <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
sudo sshd -t && sudo systemctl reload ssh
```

执行后保持原会话，另开一个终端确认仍能使用 `ubuntu` 和私钥登录，再关闭旧会话。

### 手动验证一次，之后自动触发

1. 打开 Fork 的 **Actions → Deploy to server**。
2. 点击 **Run workflow**，选择 `main` 并确认。
3. 确认 `check` 和 `deploy` 两个 Job 都成功，日志末尾出现 `=== deploy ok ===`。

以后：

- Push 到 `main`：自动拉取、构建、重启并检查 `/healthz`
- Push 到其他功能分支：不部署
- 功能分支合并进 `main`：自动部署
- 只添加 Secrets：不会自动补跑旧任务，需要手动运行一次或产生新 Push

## 7. 更新会不会清空账号和 AI Key？

正常自动部署不会清空。服务器执行的是代码更新和容器重建，持久化内容仍在：

| 内容 | 保存位置 | Rebuild 后 |
|---|---|---|
| 用户、密码哈希、课程、成绩、网页 AI 设置 | `data/data.sqlite` | 保留 |
| 学生作业与标准答案 | `data/uploads/` | 保留 |
| 会话密钥及环境变量 AI 配置 | `.env` | 保留 |
| 自动与手动备份 | `data/backups/` | 保留 |

不要把初始化脚本当作日常更新命令，也不要删除 `data/`。日常无需登录服务器，Push 到 `main` 即可。

版本号保存在仓库根目录 `VERSION`；页面会显示 `VERSION_当前提交短哈希` 和提交时间，例如 `v1.0.0_a3fcc50`。正式提升版本时修改 `VERSION`，需要时可打 tag：

```bash
git tag v1.0.0
git push --tags
```

## 8. 常见问题

### 容器反复重启，日志出现 `Permission denied: '/data/uploads'`

```bash
cd /srv/gradelogy
sudo chown -R 1000:1000 data
docker compose restart
```

### 健康检查失败

```bash
cd /srv/gradelogy
docker compose ps
docker compose logs --tail=120
curl -v --max-time 5 http://127.0.0.1:5000/healthz
```

先看日志中的第一段 Python Traceback，不要只根据最后的 `Worker failed to boot` 判断。

### 当前终端没有 Docker 权限

退出 SSH 后重新登录。临时执行单条命令也可以使用：

```bash
sg docker -c "cd /srv/gradelogy && docker compose ps"
```

### Actions 显示成功，但服务器没有更新

展开运行记录，确认不是只有 `check` 成功而 `deploy` 显示 `skipped`。`skipped` 通常表示 `SERVER_HOST` 或 `SERVER_SSH_KEY` 没有配置。

### Actions 报 `Permission denied (publickey)`

检查 `SERVER_USER`、`SERVER_SSH_KEY` 是否匹配，以及对应公钥是否在服务器的 `~/.ssh/authorized_keys` 中。这与 `i/o timeout` 不同，后者应检查防火墙。

### 构建过程下载很慢

项目 Dockerfile 默认使用腾讯云 apt / PyPI 镜像，不要再传入空的 `APT_MIRROR` 或 `PIP_INDEX_URL`。如果基础镜像拉取缓慢，可在 `docker-compose.yml` 中启用已经准备好的 `BASE_IMAGE` 镜像地址。

## 9. 数据备份

管理员可以在网页 **系统维护** 中导出完整备份。也建议定期把服务器上的 `data/` 和 `.env` 复制到其他安全位置；两者包含敏感信息，应按机密文件保管。
