# -*- coding: utf-8 -*-
"""知识点标签的聚合统计与展示文本。

标签在设置作业范围时由 AI 生成并写入 question_tags；这里把它与逐题批改结果
（questions）按题号关联起来：

- lecture_stats：同一讲、不同学生的横向聚合，找全班共性问题；
- student_stats：同一学生跨讲次的纵向聚合，找反复出错的知识点。
"""
import re

_VERDICT_FIELD = {"对": "n_right", "半对": "n_partial", "错": "n_wrong",
                  "无法判定": "n_unknown"}


def _no_key(value):
    """题号自然排序：例2 排在例10 前。"""
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", str(value or ""))]


def _leading_digits(value):
    m = re.match(r"\s*(?:第\s*)?(\d+)", str(value or ""))
    return m.group(1) if m else ""


def _same_question(tag_no, question_no):
    """标签题号与批改题号是否指同一题：兼容「3.」「第3题」「3(1)」等写法。"""
    a, b = str(tag_no or "").strip(), str(question_no or "").strip()
    if a == b:
        return True
    da, db = _leading_digits(a), _leading_digits(b)
    return bool(da and da == db)


def _tags_by_no(tag_rows):
    out = {}
    for row in tag_rows:
        no = str(row["question_no"] or "").strip()
        tag = str(row["tag"] or "").strip()
        if not no or not tag:
            continue
        bucket = out.setdefault(no, [])
        if tag not in bucket:
            bucket.append(tag)
    return out


def _matched_tags(by_no, question_no):
    exact = by_no.get(str(question_no or "").strip())
    if exact:
        return exact
    merged = []
    for no, tags in by_no.items():
        if _same_question(no, question_no):
            merged += [t for t in tags if t not in merged]
    return merged


def lecture_tags_by_key(db, lecture_id):
    """讲次页展示用：{scope key 或题号: [标签]}。"""
    rows = db.execute(
        "SELECT question_key,question_no,tag FROM question_tags "
        "WHERE lecture_id=? ORDER BY id", (lecture_id,)).fetchall()
    out = {}
    for row in rows:
        tag = str(row["tag"] or "").strip()
        if not tag:
            continue
        for key in (row["question_key"], row["question_no"]):
            key = str(key or "").strip()
            if not key:
                continue
            bucket = out.setdefault(key, [])
            if tag not in bucket:
                bucket.append(tag)
    return out


def lecture_stats(db, lecture_id):
    """把本讲标签与全班逐题判定聚合，返回按问题严重程度排序的标签列表。"""
    tag_rows = db.execute(
        "SELECT question_no,tag FROM question_tags "
        "WHERE lecture_id=? AND TRIM(tag)<>''", (lecture_id,)).fetchall()
    if not tag_rows:
        return []
    by_no = _tags_by_no(tag_rows)
    q_rows = db.execute(
        "SELECT q.submission_id,q.no,q.verdict,q.ai_comment FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "WHERE s.lecture_id=? AND q.verdict<>''", (lecture_id,)).fetchall()

    stats = {}
    for no, tags in by_no.items():
        for tag in tags:
            item = stats.setdefault(tag, {
                "tag": tag, "question_nos": [], "students": set(),
                "n_right": 0, "n_partial": 0, "n_wrong": 0, "n_unknown": 0,
                "sample_comments": []})
            if no not in item["question_nos"]:
                item["question_nos"].append(no)

    counted = set()
    for q in q_rows:
        no = str(q["no"] or "").strip()
        for tag in _matched_tags(by_no, no):
            item = stats[tag]
            item["students"].add(q["submission_id"])
            if no not in item["question_nos"]:
                item["question_nos"].append(no)
            mark = (tag, q["submission_id"], no)
            if mark in counted:
                continue
            counted.add(mark)
            field = _VERDICT_FIELD.get(q["verdict"])
            if field:
                item[field] += 1
            comment = str(q["ai_comment"] or "").strip()
            if (q["verdict"] in ("错", "半对") and comment
                    and comment not in item["sample_comments"]
                    and len(item["sample_comments"]) < 3):
                item["sample_comments"].append(comment)

    out = []
    for item in stats.values():
        item["question_nos"].sort(key=_no_key)
        item["n_students"] = len(item.pop("students"))
        item["n_total"] = (item["n_right"] + item["n_partial"]
                           + item["n_wrong"] + item["n_unknown"])
        out.append(item)
    out.sort(key=lambda x: (-x["n_wrong"], -x["n_partial"],
                            -x["n_students"], x["tag"]))
    return out


def student_stats(db, course_id, student_id):
    """把学生的错/半对记录按知识点标签跨讲次聚合，返回薄弱点列表。"""
    tag_rows = db.execute(
        "SELECT t.lecture_id,t.question_no,t.tag FROM question_tags t "
        "JOIN lectures l ON l.id=t.lecture_id WHERE l.course_id=?",
        (course_id,)).fetchall()
    if not tag_rows:
        return []
    titles = {r["id"]: r["title"] for r in db.execute(
        "SELECT id,title FROM lectures WHERE course_id=?", (course_id,)).fetchall()}
    by_lecture = {}
    for row in tag_rows:
        by_lecture.setdefault(row["lecture_id"], []).append(row)
    by_lecture = {lid: _tags_by_no(rows) for lid, rows in by_lecture.items()}

    q_rows = db.execute(
        "SELECT l.id AS lecture_id,q.no,q.verdict,q.ai_comment FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "JOIN lectures l ON l.id=s.lecture_id "
        "WHERE s.student_id=? AND l.course_id=? AND q.verdict IN ('错','半对') "
        "ORDER BY l.id,q.id", (student_id, course_id)).fetchall()

    stats = {}
    seen_episode = set()
    for q in q_rows:
        lid = q["lecture_id"]
        no = str(q["no"] or "").strip()
        for tag in _matched_tags(by_lecture.get(lid, {}), no):
            item = stats.setdefault(tag, {
                "tag": tag, "n_wrong": 0, "n_partial": 0,
                "lecture_ids": set(), "episodes": []})
            item["lecture_ids"].add(lid)
            if q["verdict"] == "错":
                item["n_wrong"] += 1
            else:
                item["n_partial"] += 1
            mark = (tag, lid, no)
            if mark not in seen_episode and len(item["episodes"]) < 8:
                seen_episode.add(mark)
                item["episodes"].append({
                    "lecture": titles.get(lid, ""),
                    "lecture_id": lid,
                    "no": no,
                    "verdict": q["verdict"],
                    "comment": str(q["ai_comment"] or "").strip(),
                })

    out = []
    for item in stats.values():
        item["n_lectures"] = len(item.pop("lecture_ids"))
        out.append(item)
    out.sort(key=lambda x: (-x["n_wrong"], -x["n_partial"],
                            -x["n_lectures"], x["tag"]))
    return out


def latest_analysis(db, kind, lecture_id=0, student_id=0):
    """取最近一次生成的横向/纵向分析，没有返回 None。"""
    if kind == "horizontal":
        return db.execute(
            "SELECT * FROM tag_analyses WHERE kind='horizontal' AND lecture_id=? "
            "ORDER BY id DESC LIMIT 1", (lecture_id,)).fetchone()
    return db.execute(
        "SELECT * FROM tag_analyses WHERE kind='vertical' AND student_id=? "
        "ORDER BY id DESC LIMIT 1", (student_id,)).fetchone()


def render_horizontal_md(result, stats):
    """把横向分析结果渲染成 Markdown，标签后附带对应判定统计。"""
    by_tag = {s["tag"]: s for s in stats or []}
    lines = ["## 总体判断", str(result.get("summary") or "—").strip(), "",
             "## 共性问题"]
    points = result.get("points") or []
    if not points:
        lines.append("本次数据还不足以归纳共性问题。")
    for point in points:
        tag = str(point.get("tag") or "").strip()
        stat = by_tag.get(tag)
        if stat:
            labels = "、".join(f"第{n}题" for n in stat["question_nos"][:6])
            head = (f"- **{tag}**（{labels}；错 {stat['n_wrong']}、"
                    f"半对 {stat['n_partial']}，涉及 {stat['n_students']} 人）")
        else:
            head = f"- **{tag}**"
        lines.append(head)
        if point.get("problem"):
            lines.append(f"  - 问题：{str(point['problem']).strip()}")
        if point.get("advice"):
            lines.append(f"  - 教学建议：{str(point['advice']).strip()}")
    return "\n".join(lines)


def render_vertical_md(result, stats):
    """把纵向分析结果渲染成 Markdown，标签后附上跨讲次出错记录。"""
    by_tag = {s["tag"]: s for s in stats or []}
    lines = ["## 总体判断", str(result.get("summary") or "—").strip(), "",
             "## 反复出错的知识点"]
    weak = result.get("weak_tags") or []
    if not weak:
        lines.append("暂时没有发现跨讲次反复出错的知识点。")
    for item in weak:
        tag = str(item.get("tag") or "").strip()
        stat = by_tag.get(tag)
        if stat:
            lectures = "、".join(dict.fromkeys(
                e["lecture"] for e in stat["episodes"] if e.get("lecture")))
            head = (f"- **{tag}**（{lectures or '—'}；错 {stat['n_wrong']}、"
                    f"半对 {stat['n_partial']}，涉及 {stat['n_lectures']} 讲）")
        else:
            head = f"- **{tag}**"
        lines.append(head)
        if item.get("trend"):
            lines.append(f"  - 表现：{str(item['trend']).strip()}")
        if item.get("advice"):
            lines.append(f"  - 练习建议：{str(item['advice']).strip()}")
    strengths = str(result.get("strengths") or "").strip()
    if strengths:
        lines += ["", "## 相对优势", strengths]
    return "\n".join(lines)
