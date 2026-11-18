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


def lecture_question_stats(db, lecture_id):
    """按题号聚合全班判定，供横向提示词使用（含标签、错误率、典型评语）。"""
    tag_rows = db.execute(
        "SELECT question_no,tag FROM question_tags "
        "WHERE lecture_id=? AND TRIM(tag)<>''", (lecture_id,)).fetchall()
    by_no = _tags_by_no(tag_rows)
    q_rows = db.execute(
        "SELECT q.no,q.verdict,q.ai_comment FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "WHERE s.lecture_id=? AND q.verdict<>'' ORDER BY q.id", (lecture_id,)).fetchall()
    stats = {}
    for q in q_rows:
        no = str(q["no"] or "").strip()
        if not no:
            continue
        item = stats.setdefault(no, {
            "no": no, "tags": [], "total": 0, "right": 0,
            "partial": 0, "wrong": 0, "unknown": 0, "comments": []})
        item["total"] += 1
        key = _VERDICT_FIELD.get(q["verdict"], "")
        if key:
            item[key[2:]] += 1      # n_right -> right
        for tag in _matched_tags(by_no, no):
            if tag not in item["tags"]:
                item["tags"].append(tag)
        comment = str(q["ai_comment"] or "").strip()
        if (q["verdict"] in ("错", "半对") and comment
                and comment not in item["comments"] and len(item["comments"]) < 3):
            item["comments"].append(comment)
    out = []
    for item in stats.values():
        item["error_rate"] = round(
            (item["wrong"] + item["partial"]) * 100 / item["total"], 1)
        out.append(item)
    out.sort(key=lambda x: (-x["error_rate"], _no_key(x["no"])))
    return out


def student_mastery_stats(db, course_id, student_id):
    """按知识点标签聚合该生在本课程各讲的全部判定，用于纵向提示词。"""
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
        "WHERE s.student_id=? AND l.course_id=? AND q.verdict<>'' "
        "ORDER BY l.id,q.id", (student_id, course_id)).fetchall()

    stats = {}
    seen_episode = set()
    for q in q_rows:
        lid = q["lecture_id"]
        no = str(q["no"] or "").strip()
        for tag in _matched_tags(by_lecture.get(lid, {}), no):
            item = stats.setdefault(tag, {
                "tag": tag, "n_total": 0, "n_right": 0, "n_partial": 0,
                "n_wrong": 0, "n_unknown": 0, "lecture_ids": set(),
                "episodes": [], "comments": []})
            item["n_total"] += 1
            field = _VERDICT_FIELD.get(q["verdict"])
            if field:
                item[field] += 1
            item["lecture_ids"].add(lid)
            comment = str(q["ai_comment"] or "").strip()
            if q["verdict"] in ("错", "半对"):
                mark = (tag, lid, no)
                if mark not in seen_episode and len(item["episodes"]) < 8:
                    seen_episode.add(mark)
                    item["episodes"].append({
                        "lecture": titles.get(lid, ""),
                        "lecture_id": lid,
                        "no": no,
                        "verdict": q["verdict"],
                        "comment": comment,
                    })
                if comment and comment not in item["comments"] and len(item["comments"]) < 3:
                    item["comments"].append(comment)

    out = []
    for item in stats.values():
        item["n_lectures"] = len(item.pop("lecture_ids"))
        item["mastery"] = round(
            (item["n_right"] + item["n_partial"] * 0.5) * 100
            / max(item["n_total"], 1), 1)
        out.append(item)
    out.sort(key=lambda x: (x["mastery"], -x["n_wrong"], x["tag"]))
    return out


def student_stats(db, course_id, student_id):
    """把学生的错/半对记录按知识点标签跨讲次聚合，返回薄弱点列表。"""
    rows = [row for row in student_mastery_stats(db, course_id, student_id)
            if row["n_wrong"] or row["n_partial"]]
    rows.sort(key=lambda x: (-x["n_wrong"], -x["n_partial"],
                             -x["n_lectures"], x["tag"]))
    return rows


def student_trend(db, course_id, student_id):
    """按讲次给出该生的判定趋势，供纵向提示词比较近期与早期表现。"""
    rows = db.execute(
        "SELECT l.id,l.title,COUNT(q.id) AS total,"
        "SUM(CASE WHEN q.verdict='对' THEN 1 ELSE 0 END) AS right_n,"
        "SUM(CASE WHEN q.verdict='半对' THEN 1 ELSE 0 END) AS partial_n,"
        "SUM(CASE WHEN q.verdict='错' THEN 1 ELSE 0 END) AS wrong_n "
        "FROM questions q JOIN submissions s ON s.id=q.submission_id "
        "JOIN lectures l ON l.id=s.lecture_id "
        "WHERE s.student_id=? AND l.course_id=? AND q.verdict<>'' "
        "GROUP BY l.id ORDER BY l.id", (student_id, course_id)).fetchall()
    return [{
        "lecture": row["title"], "total": row["total"],
        "right": row["right_n"], "partial": row["partial_n"],
        "wrong": row["wrong_n"],
        "error_rate": round((row["wrong_n"] + row["partial_n"]) * 100
                            / max(row["total"], 1), 1),
    } for row in rows]


_HABIT_KEYWORDS = (
    ("未作答/空白", ("未作答", "空白", "没写", "未写", "空着")),
    ("计算/粗心", ("计算", "算错", "粗心", "数值")),
    ("单位/有效数字", ("单位", "有效数字", "保留")),
    ("审题/看错", ("审题", "看错", "漏看", "题意")),
    ("格式/书写", ("格式", "书写", "不规范", "符号")),
)


def habit_stats(db, course_id, student_id):
    """从错题评语中做关键词统计，区分知识性失分与作答习惯问题。"""
    rows = db.execute(
        "SELECT q.ai_comment FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "JOIN lectures l ON l.id=s.lecture_id "
        "WHERE s.student_id=? AND l.course_id=? AND q.verdict IN ('错','半对') "
        "AND TRIM(q.ai_comment)<>''", (student_id, course_id)).fetchall()
    counts = {}
    for row in rows:
        comment = str(row["ai_comment"])
        for name, words in _HABIT_KEYWORDS:
            if any(word in comment for word in words):
                counts[name] = counts.get(name, 0) + 1
    return [{"type": name, "count": count} for name, count in counts.items()]


def latest_analysis(db, kind, lecture_id=0, student_id=0, course_id=0):
    """取最近一次生成的横向/纵向分析，没有返回 None。"""
    if kind == "horizontal":
        return db.execute(
            "SELECT * FROM tag_analyses WHERE kind='horizontal' AND lecture_id=? "
            "ORDER BY id DESC LIMIT 1", (lecture_id,)).fetchone()
    if kind != "vertical" or not course_id:
        raise ValueError("纵向分析必须指定课程")
    return db.execute(
        "SELECT * FROM tag_analyses WHERE kind='vertical' AND student_id=? AND course_id=? "
        "ORDER BY id DESC LIMIT 1", (student_id, course_id)).fetchone()


def render_horizontal_md(result, stats=None):
    """把横向诊断结果渲染成 Markdown（stats 参数保留用于兼容旧调用）。"""
    lines = ["## 整体掌握情况",
             str(result.get("overall_summary") or "—").strip(), "",
             "## 高风险知识点"]
    topics = result.get("high_risk_topics") or []
    if not topics:
        lines.append("本次数据还不足以归纳高风险知识点。")
    for topic in topics:
        name = str(topic.get("topic_name") or "未命名知识点").strip()
        rate = str(topic.get("error_rate") or "").strip()
        lines.append(f"- **{name}**" + (f"（错误率 {rate}）" if rate else ""))
        pitfall = str(topic.get("common_pitfall") or "").strip()
        if pitfall:
            lines.append(f"  - 共性失分点：{pitfall}")
    suggestions = result.get("teaching_suggestions") or []
    if suggestions:
        lines += ["", "## 讲评课建议"]
        for i, item in enumerate(suggestions, 1):
            lines.append(f"{i}. {str(item).strip()}")
    return "\n".join(lines)


def render_vertical_md(result, stats=None):
    """把纵向诊断结果渲染成 Markdown（stats 参数保留用于兼容旧调用）。"""
    highlights = result.get("progress_highlights") or []
    lines = ["## 进步与亮点"]
    lines += [f"- {str(item).strip()}" for item in highlights] or ["- 暂未发现明显进步点。"]

    lines += ["", "## 根因诊断"]
    root_cause = result.get("root_cause_diagnosis") or []
    if not root_cause:
        lines.append("暂未发现明确的知识断层。")
    for item in root_cause:
        chain = str(item.get("weakness_chain") or "").strip()
        analysis = str(item.get("analysis") or "").strip()
        lines.append(f"- **{chain or '知识断层'}**")
        if analysis:
            lines.append(f"  - {analysis}")

    habits = result.get("habit_alerts") or []
    if habits:
        lines += ["", "## 作答习惯提醒"]
        lines += [f"- {str(item).strip()}" for item in habits]

    plan = result.get("action_plan") or []
    if plan:
        lines += ["", "## 未来两周行动方案"]
        for i, item in enumerate(plan, 1):
            lines.append(f"{i}. {str(item).strip()}")
    return "\n".join(lines)
