# -*- coding: utf-8 -*-
"""附件落盘与文件访问白名单。

上传校验（扩展名 + 文件头魔数）、按课程/讲次/学生分目录存储，以及学生可见文件
白名单都收在这里；app.py 的 /files 路由只负责登录与鉴权。

依赖方向：只依赖 config / db，不反向导入 app。
"""
import json
import os
import secrets
from datetime import datetime

from werkzeug.utils import secure_filename

import config as cfg
from db import get_db

ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}

# 文件头魔数：只靠扩展名过滤，改名的 HTML/脚本也能传进来，这里按内容再判一次
MAGIC_HEADS = {
    "pdf": (b"%PDF",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),  # 需再检查 8~12 字节是否为 WEBP
}


def file_list(value):
    """submissions.file_path 兼容单文件与 JSON 数组（一次提交多张图片/多页）。"""
    if not value:
        return []
    text = str(value)
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return [text]
        return [str(p) for p in data if p] if isinstance(data, list) else [text]
    return [text]


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def _upload_rel_dir(course_id, lecture_id, student_id=0):
    """附件在 uploads/ 下的相对目录：course/lecture[/student]。"""
    parts = [f"course_{course_id}", f"lecture_{lecture_id}"]
    if student_id:
        parts.append(f"student_{student_id}")
    return "/".join(parts)


def _validate_upload(file_storage):
    """校验扩展名与文件头，返回安全的落盘文件名（此时尚未写入磁盘）。"""
    orig = secure_filename(file_storage.filename or "upload")
    ext = orig.rsplit(".", 1)[1].lower() if "." in orig else "bin"
    if ext not in MAGIC_HEADS:
        raise ValueError("只支持 png / jpg / webp / pdf 文件")
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    if not any(head.startswith(sig) for sig in MAGIC_HEADS[ext]) or \
            (ext == "webp" and head[8:12] != b"WEBP"):
        raise ValueError("文件内容与扩展名不符，请重新导出后再上传")
    size = file_storage.content_length
    if size is None:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
    if size > cfg.get("MAX_UPLOAD_MB") * 1024 * 1024:
        raise ValueError(f"单个文件不能超过 {cfg.get("MAX_UPLOAD_MB")}MB")
    return f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_{orig or 'file.' + ext}"


def save_upload(file_storage, course_id, lecture_id, student_id=0, filename=None):
    """按 course/lecture[/student] 分层保存附件，返回相对路径（用 / 分隔）。"""
    fname = filename or _validate_upload(file_storage)
    rel_dir = _upload_rel_dir(course_id, lecture_id, student_id)
    abs_dir = os.path.join(cfg.get("STORAGE_DIR"), *rel_dir.split("/"))
    os.makedirs(abs_dir, exist_ok=True)
    file_storage.save(os.path.join(abs_dir, fname))
    return f"{rel_dir}/{fname}"


def _student_files(uid):
    """学生可访问的文件白名单：自己的提交原件 + 自己提交下的题目裁剪图 + 所选课程的讲次学生版讲义。

    教师版答案、未选课程的材料、他人作业一律不在集合内，避免知道路径就能下载。
    """
    db = get_db()
    paths = set()
    for r in db.execute("SELECT file_path FROM submissions WHERE student_id=?", (uid,)):
        for p in file_list(r["file_path"]):
            paths.add(os.path.normpath(p).replace("\\", "/"))
    for r in db.execute("SELECT q.image_path FROM questions q "
                        "JOIN submissions s ON s.id=q.submission_id "
                        "WHERE s.student_id=? AND q.image_path != ''", (uid,)):
        paths.add(os.path.normpath(r["image_path"]).replace("\\", "/"))
    for r in db.execute("""
            SELECT h.student_pdf FROM lectures h
            JOIN enrollments e ON e.course_id=h.course_id AND e.student_id=?
            WHERE h.student_pdf != ''""", (uid,)):
        paths.add(os.path.normpath(r["student_pdf"]).replace("\\", "/"))
    return paths


def can_read_file(db, user, filename):
    """按数据库中的资源归属授权；文件名和目录名本身不授予权限。"""
    if user["role"] == "admin":
        return True
    wanted = os.path.normpath(filename).replace("\\", "/")
    if user["role"] == "student":
        return wanted in _student_files(user["id"])
    if user["role"] != "teacher":
        return False
    uid = user["id"]
    scope = ("SELECT c.id FROM courses c WHERE c.teacher_id=? OR EXISTS "
             "(SELECT 1 FROM course_teachers ct WHERE ct.course_id=c.id AND ct.teacher_id=?)")
    paths = set()
    for row in db.execute(
            f"SELECT student_pdf,answer_pdf FROM lectures WHERE course_id IN ({scope})",
            (uid, uid)):
        paths.update(p for p in row if p)
    for row in db.execute(
            f"SELECT s.file_path FROM submissions s JOIN lectures l ON l.id=s.lecture_id "
            f"WHERE l.course_id IN ({scope})", (uid, uid)):
        paths.update(file_list(row["file_path"]))
    for row in db.execute(
            f"SELECT q.image_path FROM questions q JOIN submissions s ON s.id=q.submission_id "
            f"JOIN lectures l ON l.id=s.lecture_id WHERE l.course_id IN ({scope})",
            (uid, uid)):
        if row["image_path"]:
            paths.add(row["image_path"])
    for row in db.execute(
            "SELECT m.stored_name FROM kg_materials m JOIN knowledge_graphs k ON k.id=m.graph_id "
            "WHERE k.created_by=? OR EXISTS (SELECT 1 FROM kg_members km "
            "WHERE km.graph_id=k.id AND km.user_id=?)", (uid, uid)):
        paths.add(row["stored_name"])
    return wanted in {os.path.normpath(p).replace("\\", "/") for p in paths}
