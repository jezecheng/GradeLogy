# -*- coding: utf-8 -*-
"""教师端「数字助手」的工具集：课程数据只读查询 + 使用文档检索。

同一份工具定义供两处复用：
- mcp_server.py：对外的 MCP（stdio）服务，给外部 AI 客户端调用
- Flask /assistant/chat：页面右下角折叠面板，由后端跑工具调用循环

所有工具都按 teacher_id 做归属过滤，只返回该老师参与的课程。
"""
import json
import os
import re
import sqlite3

import config

DB_PATH = config.DB_PATH
DOCS_DIR = os.path.join(config.BASE_DIR, "docs")

VERDICT_ORDER = ("对", "半对", "错", "无法判定")


# ---------- 基础设施 ----------
def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _cut(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def _course(db, teacher_id, key):
    """按 ID 或名称定位老师参与的课程。"""
    sql = ("SELECT c.* FROM courses c JOIN course_teachers ct "
           "ON ct.course_id=c.id AND ct.teacher_id=? WHERE ")
    key = str(key or "").strip()
    if key.isdigit():
        row = db.execute(sql + "c.id=?", (teacher_id, int(key))).fetchone()
        if row:
            return dict(row)
    row = db.execute(sql + "c.name LIKE ? ORDER BY c.id DESC LIMIT 1",
                     (teacher_id, f"%{key}%")).fetchone()
    return dict(row) if row else None


def _lecture(db, course_id, key):
    """按 ID、完整标题或标题片段定位讲次。"""
    key = str(key or "").strip()
    if key.isdigit():
        row = db.execute("SELECT * FROM lectures WHERE course_id=? AND id=?",
                         (course_id, int(key))).fetchone()
        if row:
            return dict(row)
    row = db.execute("SELECT * FROM lectures WHERE course_id=? AND title LIKE ? "
                     "ORDER BY id DESC LIMIT 1", (course_id, f"%{key}%")).fetchone()
    return dict(row) if row else None


def _enrolled(db, course_id):
    return _rows(db.execute("""
        SELECT u.id, u.username, u.display_name FROM enrollments e
        JOIN users u ON u.id=e.student_id WHERE e.course_id=?
        ORDER BY u.display_name""", (course_id,)))


# ---------- 文档检索 ----------
def _doc_sections():
    files = [os.path.join(config.BASE_DIR, "README.md")]
    if os.path.isdir(DOCS_DIR):
        files += [os.path.join(DOCS_DIR, f) for f in sorted(os.listdir(DOCS_DIR))
                  if f.endswith(".md")]
    sections = []
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        source = os.path.relpath(path, config.BASE_DIR)
        title = text.splitlines()[0].lstrip("# ").strip() if text else ""
        body = []
        for line in text.splitlines():
            if line.startswith("## "):
                sections.append({"source": source, "title": title,
                                 "text": "\n".join(body).strip()})
                title, body = line[3:].strip(), []
            elif line.startswith("# "):
                title = line[2:].strip()
            else:
                body.append(line)
        sections.append({"source": source, "title": title,
                         "text": "\n".join(body).strip()})
    return [s for s in sections if s["text"]]


def _terms(question):
    q = question or ""
    terms = {t.lower() for t in re.findall(r"[A-Za-z0-9_]{2,}", q)}
    han = re.findall(r"[\u4e00-\u9fa5]+", q)
    for run in han:
        if len(run) <= 3:
            terms.add(run)
        else:
            # 长句按 2 字窗口切词，够用于关键词命中
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return [t for t in terms if len(t) >= 2]


def ask_help(question):
    """在 README 与 docs/*.md 里按关键词找最相关的章节。"""
    terms = _terms(question)
    scored = []
    for sec in _doc_sections():
        hay = (sec["title"] + "\n" + sec["text"]).lower()
        score = sum(min(hay.count(t), 6) for t in terms)
        if sec["title"].lower() and any(t in sec["title"].lower() for t in terms):
            score += 4
        if score:
            scored.append((score, sec))
    scored.sort(key=lambda x: (-x[0], x[1]["source"]))
    if not scored:
        titles = sorted({f"{s['source']} · {s['title']}" for s in _doc_sections()})
        return {"found": False, "message": "没有匹配的章节，可参考以下目录：",
                "sections": titles[:30]}
    return {"found": True,
            "sections": [{"source": s["source"], "title": s["title"],
                          "excerpt": _cut(s["text"], 1200)}
                         for _, s in scored[:3]]}


# ---------- 工具实现 ----------
def list_courses(teacher_id):
    db = _db()
    try:
        rows = _rows(db.execute("""
            SELECT c.id, c.name, c.description,
              (SELECT COUNT(*) FROM enrollments e WHERE e.course_id=c.id) AS students,
              (SELECT COUNT(*) FROM lectures h WHERE h.course_id=c.id) AS lectures,
              (SELECT COUNT(*) FROM exams x WHERE x.course_id=c.id) AS exams,
              (SELECT COUNT(*) FROM course_teachers t WHERE t.course_id=c.id) AS teachers
            FROM courses c JOIN course_teachers ct ON ct.course_id=c.id AND ct.teacher_id=?
            ORDER BY c.id DESC""", (teacher_id,)))
        return {"courses": rows}
    finally:
        db.close()


def list_lectures(teacher_id, course):
    db = _db()
    try:
        c = _course(db, teacher_id, course)
        if not c:
            return {"error": f"没有找到课程「{course}」，可先用 list_courses 查看"}
        total = len(_enrolled(db, c["id"]))
        rows = _rows(db.execute("""
            SELECT h.id, h.title, h.deadline, h.created_at,
              (SELECT COUNT(DISTINCT s.student_id) FROM submissions s
                 WHERE s.lecture_id=h.id) AS submitted,
              (SELECT COUNT(*) FROM submissions s
                 WHERE s.lecture_id=h.id AND s.status='已批改') AS graded
            FROM lectures h WHERE h.course_id=?
            ORDER BY h.title COLLATE NOCASE, h.id""", (c["id"],)))
        for r in rows:
            r["enrolled"] = total
        return {"course": c["name"], "lectures": rows}
    finally:
        db.close()


def lecture_status(teacher_id, course, lecture):
    db = _db()
    try:
        c = _course(db, teacher_id, course)
        if not c:
            return {"error": f"没有找到课程「{course}」"}
        h = _lecture(db, c["id"], lecture)
        if not h:
            return {"error": f"课程「{c['name']}」下没有找到讲次「{lecture}」"}
        enrolled = _enrolled(db, c["id"])
        subs = _rows(db.execute("""
            SELECT s.id, s.student_id, s.status, s.score, s.report_published,
                   u.display_name
            FROM submissions s JOIN users u ON u.id=s.student_id
            WHERE s.lecture_id=? ORDER BY s.id""", (h["id"],)))
        submitted_ids = {s["student_id"] for s in subs}

        dist = {v: 0 for v in VERDICT_ORDER}
        per_q, reviewed, total_q = {}, 0, 0
        for s in subs:
            qs = _rows(db.execute(
                "SELECT no, verdict, score, max_score, reviewed, ai_comment "
                "FROM questions WHERE submission_id=? ORDER BY id", (s["id"],)))
            total_q = max(total_q, len(qs))
            for q in qs:
                reviewed += 1 if q["reviewed"] else 0
                v = q["verdict"] if q["verdict"] in dist else "无法判定"
                dist[v] += 1
                item = per_q.setdefault(q["no"], {v: 0 for v in VERDICT_ORDER})
                item[v] += 1

        return {
            "course": c["name"],
            "lecture": h["title"],
            "deadline": h["deadline"] or "不限",
            "enrolled": len(enrolled),
            "submitted": len(submitted_ids),
            "graded": sum(1 for s in subs if s["status"] == "已批改"),
            "reports_published": sum(1 for s in subs if s["report_published"]),
            "questions": total_q,
            "reviewed_rows": reviewed,
            "not_submitted": [u["display_name"] for u in enrolled
                              if u["id"] not in submitted_ids],
            "verdict_distribution": dist,
            "per_question": per_q,
            "lecture_summary": _cut(h.get("summary_md"), 2000) if h.get("summary_md") else "",
        }
    finally:
        db.close()


def find_student(teacher_id, name):
    db = _db()
    try:
        rows = _rows(db.execute("""
            SELECT DISTINCT u.id, u.username, u.display_name,
                   c.id AS course_id, c.name AS course_name,
                   (SELECT COUNT(*) FROM submissions s WHERE s.student_id=u.id) AS submissions
            FROM users u
            JOIN enrollments e ON e.student_id=u.id
            JOIN courses c ON c.id=e.course_id
            JOIN course_teachers ct ON ct.course_id=c.id AND ct.teacher_id=?
            WHERE u.role='student' AND (u.display_name LIKE ? OR u.username LIKE ?)
            ORDER BY u.display_name""",
            (teacher_id, f"%{name}%", f"%{name}%")))
        return {"students": [dict(r) for r in rows]}
    finally:
        db.close()


def student_report(teacher_id, course, student, lecture=None):
    db = _db()
    try:
        c = _course(db, teacher_id, course)
        if not c:
            return {"error": f"没有找到课程「{course}」"}
        stu = db.execute("""
            SELECT u.* FROM users u JOIN enrollments e ON e.student_id=u.id
            WHERE e.course_id=? AND u.role='student'
              AND (u.display_name LIKE ? OR u.username LIKE ?)
            LIMIT 1""", (c["id"], f"%{student}%", f"%{student}%")).fetchone()
        if not stu:
            return {"error": f"课程「{c['name']}」里没有找到学生「{student}」"}
        stu = dict(stu)

        sql = ("SELECT s.*, h.title AS lecture_title FROM submissions s "
               "JOIN lectures h ON h.id=s.lecture_id WHERE s.student_id=? AND h.course_id=?")
        params = [stu["id"], c["id"]]
        if lecture:
            h = _lecture(db, c["id"], lecture)
            if not h:
                return {"error": f"课程「{c['name']}」下没有找到讲次「{lecture}」"}
            sql += " AND s.lecture_id=?"
            params.append(h["id"])
        sub = db.execute(sql + " ORDER BY s.id DESC LIMIT 1", params).fetchone()
        if not sub:
            return {"student": stu["display_name"], "message": "该生还没有提交记录"}
        sub = dict(sub)

        qs = _rows(db.execute(
            "SELECT no, verdict, score, max_score, reviewed, ai_comment "
            "FROM questions WHERE submission_id=? ORDER BY id", (sub["id"],)))
        for q in qs:
            q["ai_comment"] = _cut(q["ai_comment"], 300)

        return {
            "student": stu["display_name"],
            "course": c["name"],
            "lecture": sub["lecture_title"],
            "submitted_at": sub["created_at"],
            "status": sub["status"],
            "score": sub["score"],
            "feedback": _cut(sub["feedback"], 800),
            "report_published": bool(sub["report_published"]),
            "report_markdown": _cut(sub["report_md"], 3000),
            "questions": qs,
        }
    finally:
        db.close()


# ---------- 工具注册表 ----------
def _spec(name, desc, props, required):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    _spec("ask_help", "查询本系统的使用说明：怎么建课、导入学生、设置作业范围、批阅、发布报告等。"
                      "回答操作类问题前优先调用。",
          {"question": {"type": "string", "description": "用户的问题或关键词，如「作业范围」「发布报告」"}},
          ["question"]),
    _spec("list_courses", "列出当前老师参与的课程（含学生数、讲次数、考试数）。",
          {}, []),
    _spec("list_lectures", "列出某门课程下的讲次，含截止时间、已交人数、已批改数。",
          {"course": {"type": "string", "description": "课程名称或 ID"}}, ["course"]),
    _spec("lecture_status", "某讲次作业的整体情况：已交/已批改/已审核人数、未交学生、"
                            "每题的对/半对/错分布、讲次汇总。",
          {"course": {"type": "string", "description": "课程名称或 ID"},
           "lecture": {"type": "string", "description": "讲次标题（可只给片段）或 ID"}},
          ["course", "lecture"]),
    _spec("find_student", "按姓名或用户名查找学生，返回其所在课程。",
          {"name": {"type": "string", "description": "学生姓名或用户名"}}, ["name"]),
    _spec("student_report", "查看某学生在某讲次的批改结果：逐题判定与评语、总分、批改报告。"
                            "不传讲次时取该课程最近一次提交。",
          {"course": {"type": "string", "description": "课程名称或 ID"},
           "student": {"type": "string", "description": "学生姓名或用户名"},
           "lecture": {"type": "string", "description": "讲次标题或 ID，可省略"}},
          ["course", "student"]),
]


def call_tool(name, args, teacher_id):
    """执行工具，返回可直接给模型/客户端读的 JSON 文本。"""
    try:
        if name == "ask_help":
            out = ask_help(args.get("question", ""))
        elif name == "list_courses":
            out = list_courses(teacher_id)
        elif name == "list_lectures":
            out = list_lectures(teacher_id, args.get("course", ""))
        elif name == "lecture_status":
            out = lecture_status(teacher_id, args.get("course", ""), args.get("lecture", ""))
        elif name == "find_student":
            out = find_student(teacher_id, args.get("name", ""))
        elif name == "student_report":
            out = student_report(teacher_id, args.get("course", ""),
                                 args.get("student", ""), args.get("lecture"))
        else:
            out = {"error": f"未知工具 {name}"}
    except Exception as e:  # 工具异常不该打断对话
        out = {"error": f"{type(e).__name__}: {e}"}
    return json.dumps(out, ensure_ascii=False)


# ---------- 工具调用循环 ----------
SYSTEM_PROMPT = (
    "你是「教学管理平台」的教师助手。你可以调用工具查询这位老师自己的课程数据和使用文档。\n"
    "规则：\n"
    "1. 涉及具体数据（人数、分数、某题对错、某个学生）时，必须先调用工具，不要凭空猜测。\n"
    "2. 用户问「怎么操作」时先调用 ask_help，再按文档步骤用简洁中文回答。\n"
    "3. 只回答当前老师有权限的课程；工具报错就如实说明，并给出下一步建议。\n"
    "4. 回答控制在 200 字以内，多使用短句和列表；不要输出 JSON、不要编造数据。"
)


def _post(url, api_key, payload, timeout):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"AI 接口返回 HTTP {e.code}：{e.read().decode()[:200]}")
    except Exception as e:
        raise RuntimeError(f"AI 接口请求失败：{str(e)[:200]}")


def _openai_tools():
    """把 MCP 风格的工具定义转成 OpenAI function calling 需要的结构。"""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["inputSchema"]}}
            for t in TOOLS]


def reply(messages, cfg, teacher_id, max_rounds=4, timeout=90):
    """跑工具调用循环，返回 (回答文本, 调用轨迹, 错误)。cfg 来自 llm.resolve_config。"""
    if not cfg.get("api_url") or not cfg.get("api_key") or not cfg.get("model"):
        return "", [], "尚未配置 AI 接口，请先到「AI 设置」保存地址、Key 与模型"
    url = f"{cfg['api_url']}/chat/completions"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + list(messages)
    trace = []
    try:
        for _ in range(max_rounds):
            data = _post(url, cfg["api_key"],
                         {"model": cfg["model"], "messages": msgs,
                          "max_tokens": 1200, "tools": _openai_tools(),
                          "tool_choice": "auto"},
                         timeout)
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip(), trace, ""
            msgs.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {}).get("name", "")
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                trace.append({"tool": fn, "args": args})
                msgs.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": call_tool(fn, args, teacher_id)})
    except RuntimeError as e:
        return "", trace, str(e)
    return "这个问题需要查太多步，先给你到这里的结论；可以再问我更具体一点。", trace, ""
