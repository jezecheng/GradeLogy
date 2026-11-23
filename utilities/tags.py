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


def question_difficulty_map(db, lecture_id):
    """Return effective per-question ratings indexed by scope key and question number."""
    rows = db.execute(
        "SELECT question_key,question_no,difficulty,manual_difficulty "
        "FROM question_assessments WHERE lecture_id=?", (lecture_id,)).fetchall()
    by_key, by_no = {}, {}
    for row in rows:
        manual = row["manual_difficulty"]
        ai = row["difficulty"]
        item = {"rating": manual if manual is not None else ai,
                "ai": ai, "manual": manual}
        key = str(row["question_key"] or "").strip()
        no = str(row["question_no"] or "").strip()
        if key:
            by_key[key] = item
        if no:
            by_no[no] = item
    return {"by_key": by_key, "by_no": by_no}


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
    difficulty_by_no = question_difficulty_map(db, lecture_id)["by_no"]
    for item in out:
        assessment = difficulty_by_no.get(item["no"])
        item["difficulty"] = assessment["rating"] if assessment else None
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
    assessment_rows = db.execute(
        "SELECT a.lecture_id,a.question_no,a.difficulty,a.manual_difficulty "
        "FROM question_assessments a JOIN lectures l ON l.id=a.lecture_id "
        "WHERE l.course_id=?", (course_id,)).fetchall()
    difficulty_by_lecture = {}
    for assessment in assessment_rows:
        rating = (assessment["manual_difficulty"]
                  if assessment["manual_difficulty"] is not None
                  else assessment["difficulty"])
        if rating is not None:
            difficulty_by_lecture.setdefault(assessment["lecture_id"], {})[
                str(assessment["question_no"] or "").strip()] = float(rating)
    by_lecture = {}
    for row in tag_rows:
        by_lecture.setdefault(row["lecture_id"], []).append(row)
    by_lecture = {lid: _tags_by_no(rows) for lid, rows in by_lecture.items()}

    q_rows = db.execute(
        "SELECT l.id AS lecture_id,q.no,q.verdict,q.ai_comment FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "JOIN lectures l ON l.id=s.lecture_id "
        "WHERE s.student_id=? AND l.course_id=? AND TRIM(q.verdict)<>'' "
        "ORDER BY l.id,q.id", (student_id, course_id)).fetchall()

    stats = {}
    seen_episode = set()
    for q in q_rows:
        lid = q["lecture_id"]
        no = str(q["no"] or "").strip()
        verdict = str(q["verdict"] or "").strip()
        for tag in _matched_tags(by_lecture.get(lid, {}), no):
            item = stats.setdefault(tag, {
                "tag": tag, "n_total": 0, "n_right": 0, "n_partial": 0,
                "n_wrong": 0, "n_unknown": 0, "lecture_ids": set(),
                "lecture_breakdown": {}, "difficulty_sum": 0.0,
                "difficulty_count": 0, "episodes": [], "comments": []})
            item["n_total"] += 1
            field = _VERDICT_FIELD.get(verdict)
            if field:
                item[field] += 1
            item["lecture_ids"].add(lid)
            by_lecture = item["lecture_breakdown"].setdefault(lid, {
                "lecture": titles.get(lid, ""), "total": 0,
                "right": 0, "partial": 0, "wrong": 0, "unknown": 0,
                "difficulty_sum": 0.0, "difficulty_count": 0})
            by_lecture["total"] += 1
            difficulty = difficulty_by_lecture.get(lid, {}).get(no)
            if difficulty is not None:
                item["difficulty_sum"] += difficulty
                item["difficulty_count"] += 1
                by_lecture["difficulty_sum"] += difficulty
                by_lecture["difficulty_count"] += 1
            if field:
                by_lecture[field[2:]] += 1
            comment = str(q["ai_comment"] or "").strip()
            if verdict in ("错", "半对"):
                mark = (tag, lid, no)
                if mark not in seen_episode and len(item["episodes"]) < 8:
                    seen_episode.add(mark)
                    item["episodes"].append({
                        "lecture": titles.get(lid, ""),
                        "lecture_id": lid,
                        "no": no,
                        "verdict": verdict,
                        "comment": comment,
                    })
                if comment and comment not in item["comments"] and len(item["comments"]) < 3:
                    item["comments"].append(comment)

    out = []
    for item in stats.values():
        lecture_ids = item.pop("lecture_ids")
        item["n_lectures"] = len(lecture_ids)
        breakdown = item.pop("lecture_breakdown")
        item["lecture_results"] = []
        for lecture_id in sorted(lecture_ids, key=lambda lid: (titles.get(lid, "").casefold(), lid)):
            result = breakdown[lecture_id]
            result["error_rate"] = round(
                (result["wrong"] + result["partial"]) * 100 / max(result["total"], 1), 1)
            result["avg_difficulty"] = (
                round(result["difficulty_sum"] / result["difficulty_count"], 2)
                if result["difficulty_count"] else None)
            result.pop("difficulty_sum")
            result.pop("difficulty_count")
            item["lecture_results"].append(result)
        item["avg_difficulty"] = (
            round(item["difficulty_sum"] / item["difficulty_count"], 2)
            if item["difficulty_count"] else None)
        item.pop("difficulty_sum")
        item.pop("difficulty_count")
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
    """按讲次给出独立作业表现汇总；各讲不视作时间或难度顺序。"""
    rows = db.execute(
        "SELECT l.id,l.title,COUNT(q.id) AS total,"
        "SUM(CASE WHEN TRIM(q.verdict)='对' THEN 1 ELSE 0 END) AS right_n,"
        "SUM(CASE WHEN TRIM(q.verdict)='半对' THEN 1 ELSE 0 END) AS partial_n,"
        "SUM(CASE WHEN TRIM(q.verdict)='错' THEN 1 ELSE 0 END) AS wrong_n,"
        "SUM(CASE WHEN TRIM(q.verdict)='无法判定' THEN 1 ELSE 0 END) AS unknown_n "
        "FROM questions q JOIN submissions s ON s.id=q.submission_id "
        "JOIN lectures l ON l.id=s.lecture_id "
        "WHERE s.student_id=? AND l.course_id=? AND TRIM(q.verdict)<>'' "
        "GROUP BY l.id ORDER BY l.title COLLATE NOCASE,l.id", (student_id, course_id)).fetchall()
    difficulty_rows = db.execute(
        "SELECT l.id AS lecture_id,COALESCE(a.manual_difficulty,a.difficulty) AS difficulty "
        "FROM question_assessments a JOIN lectures l ON l.id=a.lecture_id "
        "WHERE l.course_id=? AND COALESCE(a.manual_difficulty,a.difficulty) IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM questions q JOIN submissions s ON s.id=q.submission_id "
        "WHERE s.student_id=? AND s.lecture_id=l.id AND q.no=a.question_no "
        "AND TRIM(q.verdict)<>'')", (course_id, student_id)).fetchall()
    difficulty_by_lecture = {}
    for item in difficulty_rows:
        difficulty_by_lecture.setdefault(item["lecture_id"], []).append(float(item["difficulty"]))
    return [{
        "lecture": row["title"], "total": row["total"],
        "right": row["right_n"], "partial": row["partial_n"],
        "wrong": row["wrong_n"], "unknown": row["unknown_n"],
        "correct_rate": round(row["right_n"] * 100 / max(row["total"], 1), 1),
        "avg_difficulty": (round(sum(difficulty_by_lecture.get(row["id"], [])) /
                                   len(difficulty_by_lecture[row["id"]]), 2)
                           if difficulty_by_lecture.get(row["id"]) else None),
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
        "WHERE s.student_id=? AND l.course_id=? AND TRIM(q.verdict) IN ('错','半对') "
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
    """把横向诊断和题目难度证据渲染成 Markdown。"""
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
        difficulty_note = str(topic.get("difficulty_observation") or "").strip()
        if difficulty_note:
            lines.append(f"  - 难度表现：{difficulty_note}")
    rated = [row for row in (stats or []) if row.get("difficulty") is not None]
    if rated:
        lines += ["", "## 题目难度与全班表现",
                  "| 题号 | 难度 | 对 | 半对 | 错 | 错/半对比例 |",
                  "|---|---:|---:|---:|---:|---:|"]
        for row in rated:
            lines.append(
                f"| {row.get('no') or '—'} | {row['difficulty']:g} 星 "
                f"| {row.get('right', 0)} | {row.get('partial', 0)} "
                f"| {row.get('wrong', 0)} | {row.get('error_rate', 0)}% |")
    suggestions = result.get("teaching_suggestions") or []
    if suggestions:
        lines += ["", "## 讲评课建议"]
        for i, item in enumerate(suggestions, 1):
            lines.append(f"{i}. {str(item).strip()}")
    return "\n".join(lines)


def render_vertical_md(result, stats=None):
    """把纵向诊断结果渲染成 Markdown（stats 参数保留用于兼容旧调用）。"""
    context = stats if isinstance(stats, dict) else {}
    mastery = context.get("module_mastery_stats") or (stats if isinstance(stats, list) else [])
    trend = context.get("trend_data") or []
    profile = str(result.get("learning_profile") or "").strip()
    patterns = [p for p in (result.get("cross_lecture_patterns") or [])
                if isinstance(p, dict)]
    graph_insights = [p for p in (result.get("knowledge_graph_insights") or [])
                      if isinstance(p, dict)]

    lines = ["## 学情画像", profile or "本次分析基于当前课程已批阅且已关联知识点标签的作业记录。"]
    lines += ["", "## 各讲次作业概览（独立样本，不代表时间趋势）",
              "| 讲次 | 已判定题数 | 平均难度 | 对 | 半对 | 错 | 无法判定 | 错/半对比例 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    if trend:
        for row in trend:
            total = int(row.get("total") or 0)
            error_rate = round((int(row.get("wrong") or 0) + int(row.get("partial") or 0))
                               * 100 / max(total, 1), 1)
            difficulty = (f"{row['avg_difficulty']:g} 星"
                          if row.get("avg_difficulty") is not None else "—")
            lines.append(
                f"| {row.get('lecture') or '未命名讲次'} | {total} | {difficulty} "
                f"| {row.get('right', 0)} "
                f"| {row.get('partial', 0)} | {row.get('wrong', 0)} | {row.get('unknown', 0)} "
                f"| {error_rate}% |")
    else:
        lines.append("| 暂无已批阅讲次数据 | — | — | — | — | — | — | — |")

    lines += ["", "## 值得深挖的跨讲次学习特征"]
    if patterns:
        for item in patterns[:6]:
            title = str(item.get("pattern") or item.get("name") or "学习特征").strip()
            evidence = str(item.get("evidence") or "").strip()
            interpretation = str(item.get("interpretation") or "").strip()
            question = str(item.get("follow_up") or "").strip()
            lines.append(f"### {title}")
            if evidence:
                lines.append(f"- **数据表现：** {evidence}")
            if interpretation:
                lines.append(f"- **可能反映：** {interpretation}")
            if question:
                lines.append(f"- **建议核查：** {question}")
    else:
        lines.append("本次数据暂未支持明确的跨讲次学习特征判断；建议结合后续作业继续观察。")

    lines += ["", "## 知识点掌握与重复失分", "| 知识点 | 掌握度* | 平均难度 | 错 | 半对 | 涉及讲次 | 讲次证据 |",
              "|---|---:|---:|---:|---:|---:|---|"]
    weak_rows = [row for row in mastery if row.get("n_wrong") or row.get("n_partial")]
    weak_rows = sorted(weak_rows, key=lambda row: (-int(row.get("n_wrong") or 0),
                                                    -int(row.get("n_partial") or 0),
                                                    str(row.get("tag") or "")))
    if weak_rows:
        for row in weak_rows[:12]:
            evidence = row.get("lecture_results") or []
            lecture_evidence = []
            for item in evidence:
                if not (item.get("wrong") or item.get("partial")):
                    continue
                difficulty = (f"{item['avg_difficulty']:g}星"
                              if item.get("avg_difficulty") is not None else "难度未评估")
                lecture_evidence.append(
                    f"{item.get('lecture') or '讲次'}：{difficulty}，"
                    f"错{item.get('wrong', 0)}/半对{item.get('partial', 0)}")
            evidence_text = "；".join(lecture_evidence)
            avg_difficulty = (f"{row['avg_difficulty']:g} 星"
                              if row.get("avg_difficulty") is not None else "—")
            lines.append(
                f"| {row.get('tag') or '未命名'} | {row.get('mastery', 0)}% | {avg_difficulty} "
                f"| {row.get('n_wrong', 0)} | {row.get('n_partial', 0)} "
                f"| {row.get('n_lectures', 0)} | {evidence_text or '—'} |")
    else:
        lines.append("| 暂未发现明确薄弱知识点 | — | — | 0 | 0 | 0 | — |")
    lines.append("\n*掌握度按正确题数 + 半对题数的 0.5 权重计算；无法判定题计入题数，不计入掌握得分。")

    lines += ["", "## 知识图谱关联与根因诊断"]
    if graph_insights:
        for item in graph_insights[:8]:
            relation = str(item.get("relation") or "关联").strip()
            evidence = str(item.get("evidence") or "").strip()
            interpretation = str(item.get("interpretation") or "").strip()
            lines.append(f"- **{relation}：** {evidence or '图谱与作业数据存在关联。'}"
                         + (f" {interpretation}" if interpretation else ""))
    root_cause = result.get("root_cause_diagnosis") or []
    if root_cause:
        for item in root_cause[:8]:
            chain = str(item.get("weakness_chain") or "知识点诊断").strip()
            analysis = str(item.get("analysis") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            lines.append(f"- **{chain}**" + (f"：{analysis}" if analysis else ""))
            if evidence:
                lines.append(f"  - 作业证据：{evidence}")
    elif not graph_insights:
        lines.append("暂未发现明确的知识断层；当前图谱/作业数据未形成可核验的前置关系，暂不作因果推断。")

    highlights = result.get("progress_highlights") or []
    lines += ["", "## 稳定优势与掌握较好的知识点"]
    lines += [f"- {str(item).strip()}" for item in highlights] or ["- 暂未发现明显进步点。"]

    habits = result.get("habit_alerts") or []
    if habits:
        lines += ["", "## 作答习惯与非知识性失分"]
        lines += [f"- {str(item).strip()}" for item in habits]
    elif context.get("error_habit_stats"):
        lines += ["", "## 作答习惯与非知识性失分"]
        lines += [f"- {item.get('type')}: {item.get('count')} 次（来自批改评语关键词统计）"
                  for item in context["error_habit_stats"]]
    else:
        lines += ["", "## 作答习惯与非知识性失分", "- 当前评语中暂无稳定重复的习惯性失分证据。"]

    plan = result.get("action_plan") or []
    if plan:
        lines += ["", "## 下一步行动建议"]
        for i, item in enumerate(plan, 1):
            lines.append(f"{i}. {str(item).strip()}")
    else:
        lines += ["", "## 下一步行动建议", "- 暂无足够证据生成个性化计划；建议补充后续作业数据后重新分析。"]
    return "\n".join(lines)


# ---------- 手动编辑标签 ----------
# AI 生成的标签可能不准确；老师可以按题整体替换标签集合，
# 手动写入的行 source='manual'，AI 重新标注时不会被清掉。

MAX_TAG_CHARS = 20
MAX_MANUAL_TAGS = 8
_TAG_SPLIT_RE = re.compile(r"[,，、;；\n]+")


def normalize_tag(raw):
    """去掉首尾空白与常见分隔符，压缩中间空白。"""
    return re.sub(r"\s+", " ", str(raw or "").strip().strip("#、,，;；"))


def split_tags(raw):
    """把逗号/顿号/分号分隔的输入拆成标签列表（保序、去空）。"""
    return [t for t in (normalize_tag(x) for x in _TAG_SPLIT_RE.split(str(raw or ""))) if t]


def replace_question_tags(db, lecture_id, question_key, question_no, raw_tags):
    """整体替换某道题的标签集合，返回 (saved_tags, error)。

    老师手动编辑时以整组提交，避免多次增删产生半途状态；
    同一题重复标签自动去重，超过上限或单标签过长直接拒绝。
    """
    tags = []
    for tag in split_tags(raw_tags):
        if len(tag) > MAX_TAG_CHARS:
            return [], f"标签「{tag[:MAX_TAG_CHARS]}…」超过 {MAX_TAG_CHARS} 字"
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_MANUAL_TAGS:
        return [], f"单题最多 {MAX_MANUAL_TAGS} 个标签"
    key = str(question_key or "").strip()
    no = str(question_no or "").strip()
    if not key and not no:
        return [], "缺少题目标识"
    if key:
        db.execute("DELETE FROM question_tags WHERE lecture_id=?"
                   " AND (question_key=? OR question_no=?)",
                   (lecture_id, key, no))
    else:
        db.execute("DELETE FROM question_tags WHERE lecture_id=? AND question_no=?",
                   (lecture_id, no))
    db.executemany(
        "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
        " VALUES(?,?,?,?,'manual')",
        [(lecture_id, "", no, tag) for tag in tags])
    return tags, ""
