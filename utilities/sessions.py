"""实例级会话版本：不随教学数据库或备份包恢复。"""
import os
import secrets
import tempfile
from pathlib import Path


def session_epoch(data_dir):
    """旧实例未发生恢复时兼容已有会话；首次恢复后必须重新登录。"""
    try:
        return (Path(data_dir) / ".session_epoch").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "0"


def revoke_sessions(data_dir):
    """原子更新版本，所有进程在下一次请求拒绝旧 Cookie。"""
    target = Path(data_dir) / ".session_epoch"
    fd, path = tempfile.mkstemp(prefix=".session_epoch-", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_hex(32))
            f.flush()
            os.fsync(f.fileno())
        os.replace(path, target)
    finally:
        if os.path.exists(path):
            os.unlink(path)
