# -*- coding: utf-8 -*-
"""附件目录组织：批改产生的页面图、题目裁剪图统一存到

    uploads/attachments/<课程名>/<讲次（作业标题）>/

文件命名约定（重跑覆盖旧文件，不再产生随机碎片）：
    整页图   {学生}_p{页码}.jpg
    题目图   {学生}_q_{题号}.jpg
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ATTACH_ROOT = os.path.join(UPLOAD_DIR, "attachments")


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
