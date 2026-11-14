# -*- coding: utf-8 -*-
"""课程备份：按「课程 / 讲次 / 学生」结构打包讲义、提交与批改报告。"""
import os
import re
import zipfile

# 上传时自动加的时间戳前缀：20260101120000_ab12cd_原文件名
STORED_NAME_RE = re.compile(r"^\d{14}_[0-9a-f]{6}_(.+)$")
_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_component(value, fallback="未命名"):
    """把课程名、讲次名或用户名变成安全的单层目录名。"""
    text = _BAD_CHARS_RE.sub("_", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    return (text or fallback)[:60]


def display_filename(stored):
    """去掉上传时间戳前缀，让备份内的文件名接近老师/学生原本的文件名。"""
    name = os.path.basename(str(stored or "").replace("\\", "/"))
    match = STORED_NAME_RE.match(name)
    return safe_component(match.group(1) if match else name, "未命名文件")


def _unique(name, used):
    """同一层目录内重名时追加序号，避免 zip 内文件互相覆盖。"""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{stem} ({n}){ext}"
        n += 1
    used.add(candidate)
    return candidate


def _resolve_file(upload_dir, rel):
    """把数据库里的相对路径解析为 uploads 内的真实文件，越界或不存在返回 None。"""
    root = os.path.realpath(upload_dir)
    path = os.path.realpath(os.path.join(root, str(rel or "").replace("\\", "/").lstrip("/")))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None
    return path


def build_course_zip(dest, course_name, lectures, upload_dir):
    """生成课程备份 zip，返回 (写入文件数, 缺失文件数)。

    lectures 结构：
      {"title": 讲次名, "student_pdf": 学生版讲义相对路径, "answer_pdf": 教师版相对路径,
       "students": [{"username": 登录名, "display_name": 姓名,
                     "files": [提交文件相对路径], "report_md": 批改报告}]}
    """
    root = safe_component(course_name, "课程")
    written = missing = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(root + "/", "")
        lecture_names = set()
        for lecture in lectures:
            folder = _unique(safe_component(lecture.get("title"), "未命名讲次"), lecture_names)
            lecture_path = f"{root}/{folder}"
            z.writestr(lecture_path + "/", "")

            for label, key in (("教案-学生版", "student_pdf"), ("教案-教师版", "answer_pdf")):
                src = _resolve_file(upload_dir, lecture.get(key))
                if not src:
                    if lecture.get(key):
                        missing += 1
                    continue
                ext = os.path.splitext(src)[1].lower() or ".pdf"
                z.write(src, f"{lecture_path}/{label}{ext}")
                written += 1

            student_names = set()
            for student in lecture.get("students") or []:
                files = []
                for rel in student.get("files") or []:
                    src = _resolve_file(upload_dir, rel)
                    if not src:
                        missing += 1
                        continue
                    files.append((display_filename(rel), src))
                report = student.get("report_md") or ""
                if not files and not report:
                    continue

                folder_student = _unique(
                    safe_component(student.get("username") or student.get("display_name"), "学生"),
                    student_names)
                student_path = f"{lecture_path}/{folder_student}"
                z.writestr(student_path + "/", "")
                if report:
                    z.writestr(f"{student_path}/批改报告.md", report)
                    written += 1
                used_files = set()
                for name, src in files:
                    z.write(src, f"{student_path}/提交作业/{_unique(name, used_files)}")
                    written += 1
    return written, missing
