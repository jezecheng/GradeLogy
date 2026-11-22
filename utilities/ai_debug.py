# -*- coding: utf-8 -*-
"""教师 AI 调试载荷的安全落盘、读取与导出。"""
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import zipfile

import config as cfg


_PREVIEW_LIMIT = 256 * 1024


def _root():
    return os.path.abspath(cfg.get("AI_DEBUG_DIR"))


def _operation_dir(operation_id, create=False):
    path = os.path.join(_root(), str(int(operation_id)))
    if create:
        os.makedirs(_root(), mode=0o700, exist_ok=True)
        os.chmod(_root(), 0o700)
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def sanitize_url(value):
    """保留排查所需地址，移除 userinfo、query 和 fragment 中可能的密钥。"""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(value or "")
        netloc = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        return ""


def _payload_extension(data):
    try:
        json.loads(data.decode("utf-8"))
        return ".json"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ".bin"


def save_payload(operation_id, event_id, kind, data, secrets=()):
    """保存安全副本，同时返回线上原始字节的大小和哈希。"""
    if kind not in ("request", "response"):
        raise ValueError("未知载荷类型")
    raw = bytes(data or b"")
    original_sha256 = hashlib.sha256(raw).hexdigest()
    stored = raw
    for secret in secrets:
        if not secret:
            continue
        secret_bytes = str(secret).encode("utf-8")
        if secret_bytes:
            stored = stored.replace(secret_bytes, b"[REDACTED]")
    directory = _operation_dir(operation_id, create=True)
    filename = f"event_{int(event_id)}_{kind}{_payload_extension(raw)}.gz"
    path = os.path.join(directory, filename)
    fd, temporary = tempfile.mkstemp(prefix=".payload-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as raw_file:
            with gzip.GzipFile(fileobj=raw_file, mode="wb") as zipped:
                zipped.write(stored)
            raw_file.flush()
            os.fsync(raw_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return {
        "path": os.path.relpath(path, _root()),
        "size": len(raw),
        "sha256": original_sha256,
        "stored_size": len(stored),
        "stored_sha256": hashlib.sha256(stored).hexdigest(),
        "redacted": stored != raw,
    }


def resolve_payload(operation_id, relative_path):
    if not relative_path or os.path.isabs(relative_path):
        raise FileNotFoundError(relative_path or "空载荷路径")
    directory = _operation_dir(operation_id)
    candidate = os.path.abspath(os.path.join(_root(), relative_path))
    try:
        if os.path.commonpath((directory, candidate)) != directory:
            raise FileNotFoundError(relative_path)
    except ValueError as exc:
        raise FileNotFoundError(relative_path) from exc
    if os.path.islink(candidate) or not os.path.isfile(candidate):
        raise FileNotFoundError(relative_path)
    real_root = os.path.realpath(_root())
    real_directory = os.path.realpath(directory)
    real_candidate = os.path.realpath(candidate)
    if (os.path.commonpath((real_root, real_candidate)) != real_root or
            os.path.commonpath((real_directory, real_candidate)) != real_directory):
        raise FileNotFoundError(relative_path)
    return candidate


def read_preview(operation_id, relative_path, limit=_PREVIEW_LIMIT):
    path = resolve_payload(operation_id, relative_path)
    with gzip.open(path, "rb") as source:
        data = source.read(limit + 1)
    return data[:limit], len(data) > limit


def iter_payload(operation_id, relative_path, chunk_size=64 * 1024):
    path = resolve_payload(operation_id, relative_path)
    with gzip.open(path, "rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


def export_package(zip_path, manifest, operation, events):
    """写入一个 operation 的完整 ZIP；缺失文件会反映在 manifest 中。"""
    manifest = dict(manifest)
    missing = list(manifest.get("missing_files") or [])
    index = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
        bundle.writestr("operation.json", _json_bytes(operation))
        for event in events:
            item = {key: value for key, value in event.items()
                    if key not in ("request_path", "response_path")}
            item["files"] = {}
            for kind in ("request", "response"):
                relative = event.get(f"{kind}_path")
                if not relative:
                    continue
                arcname = f"requests/event_{event['id']}_{kind}{os.path.splitext(relative[:-3])[1]}"
                try:
                    source_path = resolve_payload(operation["id"], relative)
                    with gzip.open(source_path, "rb") as source, bundle.open(arcname, "w") as target:
                        shutil.copyfileobj(source, target, length=64 * 1024)
                    item["files"][kind] = arcname
                except (OSError, FileNotFoundError):
                    missing.append(relative)
            index.append(item)
        bundle.writestr("requests/index.json", _json_bytes(index))
        if missing:
            manifest["complete"] = False
        manifest["missing_files"] = sorted(set(missing))
        bundle.writestr("manifest.json", _json_bytes(manifest))
