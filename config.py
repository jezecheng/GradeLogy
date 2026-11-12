# -*- coding: utf-8 -*-
"""配置中心：所有部署相关参数都从环境变量读取。

本地开发不设任何环境变量时，行为与之前完全一致（数据都在项目目录下）；
Docker 部署时只需挂一个 /data 卷，并设置 DATA_DIR=/data。
"""
import logging
import os
from logging.handlers import RotatingFileHandler


def _env(name, default=""):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = _env("APP_VERSION", "1.0.0")

# 数据根目录：数据库、附件、日志默认都放在这里，容器里挂一个卷即可整体迁移
DATA_DIR = os.path.abspath(_env("DATA_DIR", BASE_DIR))

DB_PATH = os.path.abspath(_env("DB_PATH", os.path.join(DATA_DIR, "data.sqlite")))
STORAGE_DIR = os.path.abspath(_env("STORAGE_DIR", os.path.join(DATA_DIR, "uploads")))
LOG_DIR = os.path.abspath(_env("LOG_DIR", os.path.join(DATA_DIR, "logs")))

HOST = _env("HOST", "0.0.0.0")
PORT = int(_env("PORT", "5000"))
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "20"))
# 备份包解压后的总大小上限（MB），防止恶意 zip 撑爆磁盘
MAX_RESTORE_MB = int(_env("MAX_RESTORE_MB", "2048"))
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
# HTTPS 部署时设为 1：会话 Cookie 只通过加密连接传输（本地 http 访问时保持 0）
COOKIE_SECURE = _env("COOKIE_SECURE", "0").lower() in ("1", "true", "yes", "on")

# 密钥：优先环境变量，其次读文件；都没有时由应用生成并写回文件
SECRET_KEY = _env("SECRET_KEY", "")
SECRET_KEY_FILE = os.path.abspath(_env("SECRET_KEY_FILE",
                                       os.path.join(DATA_DIR, ".secret_key")))

# AI 配置：环境变量优先于数据库 settings，容器部署时无需把 Key 写进数据库
AI_API_URL = _env("AI_API_URL", "")
AI_API_KEY = _env("AI_API_KEY", "")
AI_MODEL = _env("AI_MODEL", "")

# 备份：恢复前快照与每日自动备份都放这里
BACKUP_DIR = os.path.abspath(_env("BACKUP_DIR", os.path.join(DATA_DIR, "backups")))
BACKUP_KEEP = int(_env("BACKUP_KEEP", "7"))
AUTO_BACKUP = _env("AUTO_BACKUP", "1").lower() not in ("0", "false", "no", "off")


def ensure_dirs():
    """确保所有数据目录存在；容器首次启动、只读镜像下都靠它兜底。"""
    for path in (DATA_DIR, STORAGE_DIR, LOG_DIR, BACKUP_DIR,
                 os.path.dirname(DB_PATH)):
        os.makedirs(path, exist_ok=True)


def setup_logging():
    """控制台 + 轮转文件双输出。重复调用只生效一次。"""
    root = logging.getLogger()
    if getattr(root, "_gradelogy_ready", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        file_handler = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"),
                                           maxBytes=5 * 1024 * 1024,
                                           backupCount=5, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        # 日志目录不可写不应阻断启动，控制台输出仍然可用
        root.warning("日志目录不可写，已降级为仅控制台输出：%s", LOG_DIR)
    root._gradelogy_ready = True
