# -*- coding: utf-8 -*-
"""整份作业批阅：学生作业 + 教师版标准答案一起交给多模态模型。

不切题、不裁剪题目，也不依赖 PDF 文字层。由于所用 OpenAI 兼容网关不支持
PDF 的 file content part（实测返回 400），这里把 PDF 整页渲染成图片，
按 image_url 发送；模型看到的就是完整原卷，逐题输出判定、OCR、标准答案与评语。
"""
import base64
import io
import json
import os

import pypdfium2 as pdfium

from . import llm

PDF_GRADE_PROMPT = (
    "你是化学竞赛阅卷老师。附件依次是：① 学生作业（按页顺序给出的整页扫描图）；"
    "② 教师版标准答案（同样按页顺序给出）。\n"
    "请逐题对照批阅，只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"题号","ocr":"学生实际作答内容","standard_answer":"标准答案要点",'
    '"verdict":"对|半对|错|无法判定","score":0,"max_score":0,"comment":"给分/扣分说明"}],'
    '"weak_points":[{"no":"题号","type":"失分类型","detail":"具体说明"}],'
    '"evaluation":"整份作业总评（不超过 100 字）","total_score":"如 12/20，没有分值就留空"}\n'
    "红线规则：\n"
    "1. 学生作业里的实际书写是唯一评分依据，严禁把标准答案当成学生的作答。\n"
    "2. 看不清就写〔不清〕；无法分辨时必须判「无法判定」，严禁猜测或编造学生写了什么。\n"
    "3. 化学方程式/离子方程式必须查配平、条件、状态符号、可逆号；数值题查有效数字与单位。\n"
    "4. 题号用学生作业里标注的题号；一题有多个小问（如 9.1、9.2、9(1)、9(2)）时合并为一条，"
    "comment 里写清哪一问失分（如「9.1 正确，9.2 漏写单位」）；"
    "未作答判「错」并注明未作答。\n"
    "5. 作业没有标注分值就不要编造分数：score 与 max_score 都输出 0，只给定性判定。\n"
    "6. comment 不超过 60 个汉字，standard_answer 不超过 60 个汉字。\n"
    "7. 学生作业中缺失或与标准答案对不上的题，不要凭空补题。")

MAX_FILE_MB = 20
MAX_PAGES = 40          # 单份文件页数上限，避免一次请求图片过多
RENDER_SCALE = 2.0      # 约 144dpi，兼顾手写字辨识度与图片体积
JPEG_QUALITY = 80
GRADE_TIMEOUT = 300     # 单次批阅超时；实测 90~110 秒，留足余量但不至于卡太久
GRADE_RETRIES = 1       # 批阅不重试：失败让老师手动重试，避免一个请求占住线程十几分钟
VERDICTS = ("对", "半对", "错", "无法判定")
_MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".webp": "image/webp"}

SCOPE_PROMPT = (
    "你是化学竞赛阅卷助理。附件是教师版标准答案的整页扫描图。\n"
    "请只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"题号","max":5,"hint":"这题考查什么（不超过 20 字）"}]}\n'
    "规则：\n"
    "1. 按标准答案上标注的题号顺序列出全部题目，题号保持原文写法。\n"
    "2. max 填该题满分；没有标注就填 0，不要估算。\n"
    "3. 带小问的大题按大题合并成一条，不要把 (1)(2) 拆成多题。\n"
    "4. 看不清的题号不要编造。")


def _as_num(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _image_part(mime, data):
    b64 = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _render_pdf_pages(path, label):
    """把 PDF 整页渲染为 JPEG 图片 part 列表（不做任何切题/裁剪）。"""
    parts = []
    doc = pdfium.PdfDocument(path)
    try:
        if len(doc) > MAX_PAGES:
            return None, f"{label}共 {len(doc)} 页，超过 {MAX_PAGES} 页上限，请拆分后再批阅"
        for page in doc:
            bitmap = page.render(scale=RENDER_SCALE)
            img = bitmap.to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
            parts.append(_image_part("image/jpeg", buf.getvalue()))
            bitmap.close()
    finally:
        doc.close()
    return parts, ""


def _file_parts(path, label):
    """把 PDF/图片读成 OpenAI 兼容的 content part 列表；失败返回 (None, 错误)。"""
    if not path or not os.path.exists(path):
        return None, f"{label}文件不存在：{os.path.basename(path or '')}"
    if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
        return None, f"{label}超过 {MAX_FILE_MB}MB，请压缩后再上传"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _render_pdf_pages(path, label)
    mime = _MIME.get(ext)
    if not mime:
        return None, f"{label}格式不支持：{ext or '未知'}"
    with open(path, "rb") as f:
        return [_image_part(mime, f.read())], ""


def _salvage_questions(text):
    """JSON 被截断时，按括号配对抢救 `"questions":[` 里已完整输出的题目对象。"""
    key = text.find('"questions"')
    if key < 0:
        return []
    arr = text.find("[", key)
    if arr < 0:
        return []
    out, depth, obj_at, in_str, esc = [], 0, -1, False, False
    for i in range(arr + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_at = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_at >= 0:
                try:
                    out.append(json.loads(text[obj_at:i + 1]))
                except ValueError:
                    pass
                obj_at = -1
    return out


def _normalize(raw):
    """把模型输出整理成落库需要的字段；坏数据丢弃而不是报错。"""
    questions = []
    for i, q in enumerate(raw or [], 1):
        if not isinstance(q, dict):
            continue
        no = str(q.get("no", "")).strip() or str(i)
        verdict = str(q.get("verdict", "")).strip()
        if verdict not in VERDICTS:
            verdict = "无法判定"
        questions.append({
            "no": no,
            "ocr": str(q.get("ocr", "")).strip(),
            "standard_answer": str(q.get("standard_answer", "")).strip(),
            "verdict": verdict,
            "score": _as_num(q.get("score"), 0.0),
            "max_score": _as_num(q.get("max_score"), 0.0),
            "comment": str(q.get("comment", "")).strip(),
        })
    return questions


def analyze_answer_pdf(answer_path, api_url, api_key, model, max_tokens=16384):
    """从教师版标准答案里识别题目清单，返回 (questions, error)。

    每项为 {no, max, hint}，供老师在讲次页勾选本讲作业范围。
    """
    parts, err = _file_parts(answer_path, "标准答案")
    if err:
        return None, err
    content = [{"type": "text", "text": SCOPE_PROMPT},
               {"type": "text", "text": f"教师版标准答案，共 {len(parts)} 页："}]
    content += parts
    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=180, retries=1)
    if err:
        return None, f"AI 分析失败：{err}"

    data = llm.extract_json(reply)
    raw = data.get("questions") if isinstance(data, dict) else None
    out = []
    for q in raw or []:
        if not isinstance(q, dict):
            continue
        no = str(q.get("no", "")).strip()
        if not no:
            continue
        out.append({"key": f"q{len(out)}", "no": no,
                    "max": _as_num(q.get("max"), 0.0),
                    "hint": str(q.get("hint", "")).strip()})
    if not out:
        if finish == "length":
            return None, "AI 输出被截断（题目数太多），请重试；多次失败可直接跳过作业范围，默认批改全部题目。"
        return None, "AI 没有识别出题目清单，请重试，或直接跳过作业范围（默认批改全部题目）。"
    return out, ""


def grade_pdf_submission(student_path, answer_path, api_url, api_key, model,
                         extra="", max_tokens=32768):
    """批阅一份作业，返回 {questions, weak_points, evaluation, total_score, errors, error}。

    student_path / answer_path 为本地绝对路径。模型必须是支持图片输入的多模态模型；
    接口按 OpenAI 兼容的 chat/completions 协议传 image_url content part。
    """
    if not answer_path:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": "请先上传教师版标准答案 PDF，再开始批阅。"}
    student_parts, err = _file_parts(student_path, "学生作业")
    if err:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": err}
    answer_parts, err = _file_parts(answer_path, "标准答案")
    if err:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": err}

    prompt = PDF_GRADE_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    content = [{"type": "text", "text": prompt},
               {"type": "text", "text": f"① 学生作业，共 {len(student_parts)} 页："}]
    content += student_parts
    content.append({"type": "text", "text": f"② 教师版标准答案，共 {len(answer_parts)} 页："})
    content += answer_parts

    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=GRADE_TIMEOUT,
                                  retries=GRADE_RETRIES)
    if err:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": f"AI 批阅失败：{err}"}

    data = llm.extract_json(reply)
    raw = data.get("questions", []) if isinstance(data, dict) else []
    if not raw and finish == "length":
        raw = _salvage_questions(reply)   # 输出被截断时抢救已完整的题目
    questions = _normalize(raw)
    if not questions:
        if finish == "length":
            return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                    "errors": [], "error": "AI 输出被截断，未能得到完整批阅结果；请调大模型输出上限或分次批阅。"}
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": "AI 未返回可用的逐题批阅结果，请重试。"}

    weak = data.get("weak_points") if isinstance(data, dict) else []
    weak_points = [w for w in (weak or []) if isinstance(w, dict)]
    return {
        "questions": questions,
        "weak_points": weak_points,
        "evaluation": str(data.get("evaluation", "")).strip(),
        "total_score": str(data.get("total_score", "")).strip(),
        "errors": [],
        "error": "",
    }


REPORT_PROMPT = (
    "你是化学竞赛阅卷老师。下面给出某位学生一次作业的逐题判定、失分点与总评。\n"
    "请为学生写一段批改评语，只输出 JSON，不要解释或代码块标记：\n"
    '{"good":"做得好的地方（80~160 字，要具体点出哪几题做得好）",'
    '"improve":["需要改的第 1 处","第 2 处","第 3 处"],'
    '"next":"下一步建议（60~120 字）"}\n'
    "要求：只依据给定的判定，不要编造学生没写的内容；语气客观、以鼓励为主；"
    "improve 最多 3 条，按严重程度排序；没有失分点就给空数组。"
)


def ai_report_comment(questions, weak_points, evaluation, api_url, api_key, model,
                      max_tokens=2048):
    """根据逐题结果生成报告里的「评价」部分；失败返回 {}，不影响报告其余内容。"""
    def _field(q, *names):
        for name in names:
            if hasattr(q, "get"):
                value = q.get(name)
            else:
                value = q[name] if name in q.keys() else None
            if value is not None:
                return value
        return ""

    brief = [{"no": _field(q, "no"), "verdict": _field(q, "verdict"),
              "comment": _field(q, "ai_comment", "comment"), "note": _field(q, "note")}
             for q in (questions or [])]
    payload = json.dumps({"questions": brief, "weak_points": weak_points or [],
                          "evaluation": evaluation or ""}, ensure_ascii=False)
    content = [{"type": "text", "text": REPORT_PROMPT + "\n\n数据：\n" + payload}]
    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=120, retries=1)
    if err:
        return {}
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return {}
    improve = data.get("improve")
    if isinstance(improve, str):
        improve = [improve]
    improve = [str(x).strip() for x in (improve or []) if str(x).strip()][:3]
    return {"good": str(data.get("good", "")).strip(),
            "improve": improve,
            "next": str(data.get("next", "")).strip()}


# ---------- 讲次级别的整体汇总 ----------

LECTURE_SUMMARY_PROMPT = (
    "你是化学竞赛教研组组长。下面给出一次作业中全班学生的逐题判定统计，"
    "以及每道题的代表性评语（取多份作业的 ai_comment）。\n"
    "请输出两段内容：\n"
    "1) 每题要点（每题一行短句，5~25 字）：这道题全班主要的错在哪里、或者大家普遍做得好的地方。\n"
    "2) 整体总结（3~6 句，150~300 字）：这次作业全班整体情况、最普遍的几类问题、值得肯定的地方、对下次教学的建议。\n"
    "只输出 JSON：\n"
    '{"per_question":{"题号":"要点",...},'
    '"overall":"整体总结内容"}\n'
    "要求：只依据给定数据，不要编造；每题要点简洁，重点突出；整体总结客观、有教学参考价值。"
)


def ai_lecture_summary(qstats, api_url, api_key, model, max_tokens=4096):
    """根据全班逐题判定与评语，生成讲次汇总。失败返回 {}。"""
    payload = []
    for row in qstats or []:
        comments = (row["all_comments"] or "").split("||SPLIT||")
        comments = [c.strip() for c in comments if c and c.strip()][:5]
        payload.append({
            "no": row["no"],
            "total": row["n_right"] + row["n_partial"] + row["n_wrong"] + row["n_unknown"],
            "right": row["n_right"],
            "partial": row["n_partial"],
            "wrong": row["n_wrong"],
            "unknown": row["n_unknown"],
            "sample_comments": comments,
        })
    content = [{"type": "text", "text": LECTURE_SUMMARY_PROMPT + "\n\n数据：\n" +
                json.dumps(payload, ensure_ascii=False)}]
    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=180, retries=1)
    if err:
        return {}
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return {}
    return {"per_question": data.get("per_question", {}),
            "overall": str(data.get("overall", "")).strip()}
