# -*- coding: utf-8 -*-
"""管理员日志查看：白名单文件与有界增量读取。"""
import os
import re

LOG_NAME_RE = re.compile(r"app\.log(?:\.[1-5])?")
INITIAL_BYTES = 64 * 1024
INITIAL_LINES = 200
MAX_BYTES = 256 * 1024
MAX_LINES = 500


def resolve_log(log_dir, name):
    """只允许当前日志及固定轮转文件，拒绝路径穿越和符号链接。"""
    if not LOG_NAME_RE.fullmatch(name or ""):
        raise FileNotFoundError(name)
    root = os.path.realpath(log_dir)
    candidate = os.path.join(root, name)
    path = os.path.realpath(candidate)
    if (os.path.dirname(path) != root or os.path.basename(path) != name or
            os.path.islink(candidate) or not os.path.isfile(path)):
        raise FileNotFoundError(name)
    return path


def list_logs(log_dir):
    items = []
    for name in ["app.log"] + [f"app.log.{n}" for n in range(1, 6)]:
        try:
            path = resolve_log(log_dir, name)
            stat = os.stat(path)
        except (FileNotFoundError, OSError):
            continue
        items.append({"name": name, "size": stat.st_size, "mtime": stat.st_mtime})
    return items


def _decode(parts):
    return [part.decode("utf-8", errors="replace").rstrip("\r\n") for part in parts]


def read_chunk(log_dir, name, cursor=None):
    """读取一个完整行前缀；cursor 为上次返回的 offset/dev/ino。"""
    path = resolve_log(log_dir, name)
    with open(path, "rb") as f:
        stat = os.fstat(f.fileno())
        size = stat.st_size
        identity = {"dev": stat.st_dev, "ino": stat.st_ino}
        reset = False
        try:
            offset = int(cursor["offset"]) if cursor else 0
            same_file = bool(cursor) and int(cursor["dev"]) == stat.st_dev \
                and int(cursor["ino"]) == stat.st_ino
        except (KeyError, TypeError, ValueError):
            offset, same_file = 0, False
        initial = not cursor
        if initial or not same_file or offset < 0 or offset > size:
            reset = bool(cursor)
            start = max(0, size - INITIAL_BYTES)
        else:
            start = offset
        f.seek(start)
        data = f.read(min(MAX_BYTES, max(0, size - start)))
        if not data:
            return {"name": name, "lines": [], "cursor": {"offset": start, **identity},
                    "size": size, "mtime": stat.st_mtime, "reset": reset,
                    "message": "日志已轮转或截断，已重新读取末尾。" if reset else ""}

        parts = data.splitlines(keepends=True)
        # 初次从文件中部开始时，第一段属于上一行，不展示半行。
        if start > 0 and (initial or reset) and parts:
            parts = parts[1:]
        # 尚未换行的末尾留给下一轮；超长单行则按读取上限截断，避免游标停滞。
        if parts and not data.endswith((b"\n", b"\r")) and len(data) < MAX_BYTES:
            parts = parts[:-1]
        limited = len(parts) > MAX_LINES
        if limited:
            parts = parts[:MAX_LINES]
        consumed = sum(len(part) for part in parts)
        # 即使初次丢弃了中部半行，游标也要越过它。
        if start > 0 and (initial or reset):
            first_end = data.find(b"\n")
            skipped = first_end + 1 if first_end >= 0 else 0
            next_offset = start + skipped + consumed
        else:
            next_offset = start + consumed
        lines = _decode(parts)
        if (initial or reset) and len(lines) > INITIAL_LINES:
            lines = lines[-INITIAL_LINES:]
        return {
            "name": name,
            "lines": lines,
            "cursor": {"offset": next_offset, **identity},
            "size": size,
            "mtime": stat.st_mtime,
            "reset": reset,
            "has_more": next_offset < size and next_offset > start,
            "message": "日志已轮转或截断，已重新读取末尾。" if reset else "",
        }
