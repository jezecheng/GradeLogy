# -*- coding: utf-8 -*-
"""附件目录组织：批改产生的页面图、题目裁剪图统一存到

    uploads/attachments/<课程名>/<讲次（作业标题）>/

文件命名约定（重跑覆盖旧文件，不再产生随机碎片）：
    整页图   {学生}_p{页码}.jpg
    题目图   {学生}_q_{题号}.jpg
"""
import os
import re
import shutil
from datetime import datetime

import config

BASE_DIR = config.BASE_DIR
UPLOAD_DIR = config.STORAGE_DIR
ATTACH_ROOT = config.ATTACH_ROOT


def safe_name(text, default="未命名"):
    """目录/文件名安全化：保留中英文，把路径分隔符等危险字符压成下划线。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text or "").strip()).strip("._")
    return name or default


def attach_dir(course_name, homework_title):
    """返回 uploads 下的相对目录 attachments/<课程>/<讲次>，并确保目录存在。"""
    rel = os.path.join("attachments", safe_name(course_name), safe_name(homework_title))
    os.makedirs(os.path.join(UPLOAD_DIR, rel), exist_ok=True)
    return rel


def page_name(student, page_no):
    return f"{safe_name(student)}_p{int(page_no)}.jpg"


def question_name(student, no):
    return f"{safe_name(student)}_q_{safe_name(no)}.jpg"


# ---------- 附件浏览与清理（老师端页面用） ----------
def safe_rel(rel):
    """把请求里的相对路径限制在 attachments 根内；越界或非法返回 None。"""
    rel = (rel or "").strip().strip("/")
    target = os.path.normpath(os.path.join(ATTACH_ROOT, rel))
    if target != ATTACH_ROOT and not target.startswith(ATTACH_ROOT + os.sep):
        return None
    return target


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _mtime(path):
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")


def list_dir(rel):
    """列出 attachments 下某目录的 (子目录, 文件)；目录不存在时返回空列表。"""
    target = safe_rel(rel)
    if not target or not os.path.isdir(target):
        return [], []
    dirs, files = [], []
    for name in sorted(os.listdir(target)):
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        if os.path.isdir(full):
            dirs.append({"name": name,
                         "files": sum(len(fs) for _, _, fs in os.walk(full)),
                         "mtime": _mtime(full)})
        else:
            files.append({"name": name, "size": human_size(os.path.getsize(full)),
                          "mtime": _mtime(full)})
    return dirs, files


def remove(rel):
    """删除 attachments 下的文件或目录（不允许删根目录）；成功返回 True。"""
    target = safe_rel(rel)
    if not target or target == ATTACH_ROOT or not os.path.exists(target):
        return False
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    return True
