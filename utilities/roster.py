# -*- coding: utf-8 -*-
"""名单 CSV 解析与用户名生成：只做「文件 → 结构化数据」，不碰数据库，便于独立 POC。

支持：
- 编码：UTF-8（含 Excel 导出的 BOM）/ GB18030（中文 Excel 默认）
- 管理员批量建号：单列姓名（表头可写「姓名 / name」）
- 课程名单：用户名/账号/登录名、姓名/名字、学籍号/学号
- 用户名自动生成：姓取 zh/ch/sh 两个字母或首字母，名取全拼，如 程诺 → chnuo、张三 → zhsan
- 只负责解析，账号统一由管理员创建

CLI：
    python -m utilities.roster --file students.csv
    python -m utilities.roster --file classes.csv --courses
"""
import argparse
import csv
import io
import re

from pypinyin import lazy_pinyin

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,31}$")
STUDENT_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MAX_ROWS = 1000
MAX_COURSE_ROWS = 2000

USERNAME_HEADERS = {"用户名", "账号", "登录名", "username", "user", "account"}
NAME_HEADERS = {"姓名", "名字", "学生姓名", "name", "display_name"}
NAME_COLUMN_HEADERS = {"姓名", "名字", "学生姓名", "name", "names", "display_name"}
STUDENTNO_HEADERS = {"学籍号", "学号", "student_no", "studentno"}
COURSE_HEADERS = {"课程", "课程名称", "班级", "course"}


def _decode(raw):
    """按 UTF-8(BOM) → GB18030 依次尝试，都失败则报错。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 或 GBK 的 CSV")


def _pick(header, names):
    """返回表头中第一个命中的列下标，未命中返回 None。"""
    for i, cell in enumerate(header):
        if (cell or "").strip().lower() in names:
            return i
    return None


# 常见复姓：生成用户名时整体当作「姓」，避免欧阳修 → oyangxiu
COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "司空",
    "鲜于", "令狐", "轩辕", "第五", "拓跋", "呼延", "西门", "东郭", "百里", "微生",
}


def username_from_name(name):
    """按姓名拼音生成用户名：姓取 zh/ch/sh 两字母或首字母，名取全拼。

    程诺 → chnuo；张三 → zhsan；欧阳修 → ouyangxiu；Tom → tom。
    无可用字符时返回空串。
    """
    text = re.sub(r"\s+", "", name or "")
    if not text:
        return ""
    if text[:2] in COMPOUND_SURNAMES:
        surname, given = text[:2], text[2:]
    else:
        surname, given = text[:1], text[1:]
    syllables = lazy_pinyin(surname)
    if len(syllables) > 1:  # 复姓：保留完整拼音（欧阳 → ouyang）
        sur = "".join(syllables).lower()
    else:
        one = (syllables[0] if syllables else "").lower()
        sur = one[:2] if one.startswith(("zh", "ch", "sh")) else one[:1]
    giv = "".join(lazy_pinyin(given)).lower()
    return re.sub(r"[^a-z0-9]", "", sur + giv)


def parse_name_csv(raw):
    """管理员批量建号：解析单列姓名 CSV，返回 (names, errors)。

    首行若为「姓名 / name」之类表头会自动跳过；重复姓名保留（建号时自动加数字区分）。
    """
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    names, errors = [], []
    for line, cells in enumerate(reader, start=1):
        cells = [(c or "").strip() for c in cells]
        if not any(cells):
            continue
        first = cells[0]
        if not names and first.lower() in NAME_COLUMN_HEADERS:
            continue
        if len(names) >= MAX_ROWS:
            errors.append(f"超过单次导入上限 {MAX_ROWS} 行，请拆分文件")
            break
        if len(first) > 32:
            errors.append(f"第 {line} 行：姓名超过 32 字")
            continue
        names.append(first)
    if not names:
        errors.append("文件里没有可用的姓名")
    return names, errors


def parse_csv(raw):
    """解析「用户名,姓名」CSV，返回 (rows, errors)。

    rows: [{"username","name","student_no","line"}]
    表头可含 用户名 / 姓名 / 学籍号；无表头时按「用户名,姓名」。
    """
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows, errors = [], []
    seen = set()
    seen_no = set()
    cols = None  # (username, name, student_no)
    for line, cells in enumerate(reader, start=1):
        cells = [(c or "").strip() for c in cells]
        if not any(cells):
            continue
        if cols is None:
            u = _pick(cells, USERNAME_HEADERS)
            if u is not None:
                cols = (u, _pick(cells, NAME_HEADERS), _pick(cells, STUDENTNO_HEADERS))
                continue
            cols = (0, 1, None)
        if len(rows) >= MAX_ROWS:
            errors.append(f"超过单次导入上限 {MAX_ROWS} 行，请拆分文件")
            break

        def cell(idx):
            return cells[idx] if idx is not None and idx < len(cells) else ""

        username = cell(cols[0])
        name = cell(cols[1]) or username
        student_no = cell(cols[2])
        if not username:
            errors.append(f"第 {line} 行：用户名为空")
            continue
        if not USERNAME_RE.match(username):
            errors.append(f"第 {line} 行：用户名 {username} 不合法（2-32 位字母、数字或 _ . -）")
            continue
        if len(name) > 32:
            errors.append(f"第 {line} 行：姓名过长（最多 32 字）")
            continue
        if student_no and not STUDENT_NO_RE.match(student_no):
            errors.append(f"第 {line} 行：学籍号 {student_no} 不合法（1-32 位字母、数字或 _ . -）")
            continue
        key = username.lower()
        if key in seen:
            errors.append(f"第 {line} 行：用户名 {username} 在文件中重复")
            continue
        if student_no and student_no.lower() in seen_no:
            errors.append(f"第 {line} 行：学籍号 {student_no} 在文件中重复")
            continue
        seen.add(key)
        if student_no:
            seen_no.add(student_no.lower())
        rows.append({"username": username, "name": name, "student_no": student_no, "line": line})
    if cols is None:
        errors.append("文件为空")
    return rows, errors


def parse_course_csv(raw):
    """解析「课程,用户名,姓名」CSV（CLI 预览用），返回 (rows, errors)。

    rows 元素为 {"course","username","name","student_no","line"}。
    """
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows, errors = [], []
    seen = set()
    seen_no = set()
    cols = None  # (course, username, name, student_no)
    for line, cells in enumerate(reader, start=1):
        cells = [(c or "").strip() for c in cells]
        if not any(cells):
            continue
        if cols is None:
            c = _pick(cells, COURSE_HEADERS)
            if c is not None:
                cols = (c, _pick(cells, USERNAME_HEADERS), _pick(cells, NAME_HEADERS),
                        _pick(cells, STUDENTNO_HEADERS))
                continue
            cols = (0, 1, 2, None)
        if len(rows) >= MAX_COURSE_ROWS:
            errors.append(f"超过单次导入上限 {MAX_COURSE_ROWS} 行，请拆分文件")
            break

        def cell(idx):
            return cells[idx] if idx is not None and idx < len(cells) else ""

        course = cell(cols[0])
        username = cell(cols[1])
        name = cell(cols[2]) or username
        student_no = cell(cols[3])
        if not course:
            errors.append(f"第 {line} 行：课程名称为空")
            continue
        if len(course) > 80:
            errors.append(f"第 {line} 行：课程名称超过 80 字")
            continue
        if not username:
            errors.append(f"第 {line} 行：用户名为空")
            continue
        if not USERNAME_RE.match(username):
            errors.append(f"第 {line} 行：用户名 {username} 不合法（2-32 位字母、数字或 _ . -）")
            continue
        if len(name) > 32:
            errors.append(f"第 {line} 行：姓名过长（最多 32 字）")
            continue
        if student_no and not STUDENT_NO_RE.match(student_no):
            errors.append(f"第 {line} 行：学籍号 {student_no} 不合法（1-32 位字母、数字或 _ . -）")
            continue
        key = (course.lower(), username.lower())
        if key in seen:
            errors.append(f"第 {line} 行：课程 {course} 中的用户 {username} 重复")
            continue
        if student_no and student_no.lower() in seen_no:
            errors.append(f"第 {line} 行：学籍号 {student_no} 在文件中重复")
            continue
        seen.add(key)
        if student_no:
            seen_no.add(student_no.lower())
        rows.append({"course": course, "username": username, "name": name,
                     "student_no": student_no, "line": line})
    if cols is None:
        errors.append("文件为空")
    return rows, errors


def main():
    ap = argparse.ArgumentParser(description="名单 CSV 解析预览（不写数据库）")
    ap.add_argument("--file", required=True, help="CSV 文件路径")
    ap.add_argument("--courses", action="store_true", help="按「课程,用户名,姓名」格式解析")
    args = ap.parse_args()
    with open(args.file, "rb") as f:
        rows, errors = parse_course_csv(f.read()) if args.courses else parse_csv(f.read())
    print(f"解析出 {len(rows)} 行，{len(errors)} 条错误")
    for r in rows:
        prefix = f"{r['course']} | " if args.courses else ""
        no = f" | 学籍号 {r['student_no']}" if r["student_no"] else ""
        print(f"  第 {r['line']} 行 | {prefix}{r['username']} | {r['name']}{no}")
    for e in errors:
        print("  !", e)
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
