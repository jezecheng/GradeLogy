"""登录会话版本与服务端空闲状态。"""
import hashlib
import os
import secrets
import tempfile
import time
from pathlib import Path

IDLE_TIMEOUT_SECONDS = 45 * 60
IDLE_WARNING_SECONDS = 40 * 60


def _session_hash(session_id):
    """数据库只保存随机会话 ID 的摘要，避免数据库泄露直接暴露 bearer token。"""
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()


def create_auth_session(db, user_id, now=None, idle_timeout=IDLE_TIMEOUT_SECONDS):
    """创建一个独立于教学数据的登录会话，返回仅放在签名 Cookie 中的随机 ID。"""
    now = int(time.time() if now is None else now)
    db.execute("DELETE FROM auth_sessions WHERE last_activity_at<=?",
               (now - idle_timeout,))
    session_id = secrets.token_urlsafe(32)
    db.execute("INSERT INTO auth_sessions(session_hash,user_id,last_activity_at) VALUES(?,?,?)",
               (_session_hash(session_id), user_id, now))
    return session_id, now


def auth_session_activity(db, session_id, user_id, now=None,
                          idle_timeout=IDLE_TIMEOUT_SECONDS):
    """返回有效会话的最后活动时间；超时/无效会话立即撤销并返回 None。"""
    if not session_id:
        return None
    now = int(time.time() if now is None else now)
    key = _session_hash(session_id)
    row = db.execute("SELECT user_id,last_activity_at FROM auth_sessions WHERE session_hash=?",
                     (key,)).fetchone()
    if not row or row["user_id"] != user_id:
        return None
    last_activity = int(row["last_activity_at"])
    if now - last_activity >= idle_timeout:
        db.execute("DELETE FROM auth_sessions WHERE session_hash=?", (key,))
        db.commit()
        return None
    return last_activity


def touch_auth_session(db, session_id, user_id, now=None):
    """更新有效会话活动时间；调用方应先通过 auth_session_activity 校验期限。"""
    if not session_id:
        return None
    now = int(time.time() if now is None else now)
    cur = db.execute(
        "UPDATE auth_sessions SET last_activity_at=MAX(last_activity_at,?) "
        "WHERE session_hash=? AND user_id=?",
        (now, _session_hash(session_id), user_id))
    if cur.rowcount != 1:
        return None
    db.commit()
    return now


def revoke_auth_session(db, session_id):
    """退出时撤销当前设备/浏览器的会话。"""
    if not session_id:
        return
    db.execute("DELETE FROM auth_sessions WHERE session_hash=?", (_session_hash(session_id),))
    db.commit()


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
