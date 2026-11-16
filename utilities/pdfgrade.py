# -*- coding: utf-8 -*-
"""整份作业批阅：学生作业 + 教师版标准答案一起交给多模态模型。

不切题、不裁剪题目，也不依赖 PDF 文字层。由于所用 OpenAI 兼容网关不支持
PDF 的 file content part（实测返回 400），这里把 PDF 整页渲染成图片，
按 image_url 发送；模型看到的就是完整原卷，逐题输出判定、OCR、标准答案与评语。
"""
import base64
import io
import json
import logging
import os
import re

import pypdfium2 as pdfium

from . import llm

log = logging.getLogger("gradelogy.pdfgrade")

# 提示词分成「正文规则」与「固定输出格式」两段：
# 老师可自定义正文，但输出格式由系统追加，保证程序一定能解析出结构化结果。
DEFAULT_GRADE_PROMPT = (
    "你是阅卷老师。附件依次是：① 学生作业（按页顺序给出的整页扫描图）；"
    "② 教师版标准答案（同样按页顺序给出）。\n"
    "请逐题对照批阅。\n"
    "红线规则：\n"
    "1. 学生作业里的实际书写是唯一评分依据，严禁把标准答案当成学生的作答。\n"
    "2. 看不清就写〔不清〕；无法分辨时必须判「无法判定」，严禁猜测或编造学生写了什么。\n"
    "3. 化学方程式/离子方程式必须查配平、条件、状态符号、可逆号；数值题查有效数字与单位。\n"
    "4. 题号用学生作业里标注的题号；一题有多个小问（如 9.1、9.2、9(1)、9(2)）时合并为一条，"
    "comment 里写清哪一问失分（如「9.1 正确，9.2 漏写单位」）；"
    "未作答判「错」并注明未作答。\n"
    "5. 作业没有标注分值就不要编造分数：score 与 max_score 都输出 0，只给定性判定。\n"
    "6. comment 不超过 60 个汉字，standard_answer 不超过 60 个汉字。\n"
    "7. 学生作业中缺失或与标准答案对不上的题，不要凭空补题。"
)

GRADE_OUTPUT_CONTRACT = (
    "输出格式（必须严格遵守）：只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"题号","ocr":"学生实际作答内容","standard_answer":"标准答案要点",'
    '"verdict":"对|半对|错|无法判定","score":0,"max_score":0,"comment":"给分/扣分说明"}],'
    '"weak_points":[{"no":"题号","type":"失分类型","detail":"具体说明"}],'
    '"evaluation":"整份作业总评（不超过 100 字）","total_score":"如 12/20，没有分值就留空"}'
)

PDF_GRADE_PROMPT = DEFAULT_GRADE_PROMPT + "\n" + GRADE_OUTPUT_CONTRACT

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
    "你是阅卷助理。附件是教师版标准答案的整页扫描图。\n"
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


def _render_pdf_pages(path, label, pages=None):
    """把 PDF 整页渲染为 JPEG 图片 part 列表（不做任何切题/裁剪）。

    pages 为 1 起的页码列表；为空表示整份文件。老师可用它只发送与本讲相关的页，
    减少发给模型的图片数量与 token 消耗。
    """
    parts = []
    doc = pdfium.PdfDocument(path)
    try:
        total = len(doc)
        if pages:
            picked = sorted({p for p in pages if 1 <= p <= total})
            if not picked:
                return None, f"{label}未选择有效页码（共 {total} 页）"
        else:
            picked = list(range(1, total + 1))
        if len(picked) > MAX_PAGES:
            return None, f"{label}选中的 {len(picked)} 页超过 {MAX_PAGES} 页上限，请减少页数后再批阅"
        for n in picked:
            bitmap = doc[n - 1].render(scale=RENDER_SCALE)
            img = bitmap.to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
            parts.append(_image_part("image/jpeg", buf.getvalue()))
            bitmap.close()
    finally:
        doc.close()
    return parts, ""


def pdf_page_count(path):
    """返回 PDF 页数；非 PDF 或读取失败返回 0。"""
    if not path or not os.path.exists(path):
        return 0
    if os.path.splitext(path)[1].lower() != ".pdf":
        return 0
    try:
        doc = pdfium.PdfDocument(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


def render_page_jpeg(path, page_no, scale=0.9, quality=65):
    """渲染单页为 JPEG 字节，供页码选择界面做缩略图预览；页码越界返回 None。"""
    if not path or not os.path.exists(path) or page_no < 1:
        return None
    doc = pdfium.PdfDocument(path)
    try:
        if page_no > len(doc):
            return None
        bitmap = doc[page_no - 1].render(scale=scale)
        img = bitmap.to_pil().convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        bitmap.close()
        return buf.getvalue()
    finally:
        doc.close()


def _file_parts(path, label, pages=None):
    """把 PDF/图片读成 OpenAI 兼容的 content part 列表；失败返回 (None, 错误)。"""
    if not path or not os.path.exists(path):
        return None, f"{label}文件不存在：{os.path.basename(path or '')}"
    if os.path.getsize(path) > MAX_FILE_MB * 1024 * 1024:
        return None, f"{label}超过 {MAX_FILE_MB}MB，请压缩后再上传"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _render_pdf_pages(path, label, pages)
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


def analyze_answer_pdf(answer_path, api_url, api_key, model, max_tokens=16384,
                       pages=None, usage_hook=None):
    """从教师版标准答案里识别题目清单，返回 (questions, error)。

    每项为 {no, max, hint}，供老师在讲次页勾选本讲作业范围。
    pages 为要发送的页码（1 起）；为空表示整份文件。
    """
    parts, err = _file_parts(answer_path, "标准答案", pages)
    if err:
        return None, err
    content = [{"type": "text", "text": SCOPE_PROMPT},
               {"type": "text", "text": f"教师版标准答案，共 {len(parts)} 页："}]
    content += parts
    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=180, retries=1,
                                  usage_hook=usage_hook)
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
            return None, "AI 输出被截断（题目数太多），请重试。"
        return None, "AI 没有识别出题目清单，请重试；也可先确认标准答案 PDF 是否清晰。"
    return out, ""


TAG_PROMPT = (
    "你是教研助理。附件是教师版标准答案的相关页；下面给出本次作业范围内的题目清单。\n"
    "请给每道题标注知识点标签，用于后续按知识点分析学生的薄弱点。\n"
    "题目清单：\n"
)

TAG_RULES = (
    "\n请只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"题号","category":"主要考查类型","tags":["知识点1","知识点2"]}]}\n'
    "规则：\n"
    "1. tags 用教材/考纲里的通用知识点术语（如 氧化还原反应、离子方程式、化学平衡常数），"
    "每题 1~4 个，按主要程度排序。\n"
    "2. 不要输出题号、分数、难度（如「基础题」「综合题」）这类非知识点标签。\n"
    "3. 只输出清单里的题号，不要补题；同一题有多个小问时合并成一组标签。\n"
    "4. 标准答案看不清时，可结合题目本身与清单里的说明推断；仍无法判断就把 tags 留空。"
)

MAX_TAGS_PER_QUESTION = 4


def _clean_tags(raw):
    """清理模型给出的标签：去重、去空白，过滤题号/分数学非知识点描述。"""
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            item = item.get("tag") or item.get("name") or ""
        text = str(item).strip().strip("#、,，;；")
        if not text or len(text) > 20:
            continue
        if text in out:
            continue
        out.append(text)
    return out[:MAX_TAGS_PER_QUESTION]


def _no_token(value):
    """题号归一化：取前导数字，便于把「3.」「第3题」「3(1)」归到同一题。"""
    m = re.match(r"\s*(?:第\s*)?(\d+)", str(value or ""))
    return m.group(1) if m else str(value or "").strip()


def analyze_question_tags(answer_path, questions, api_url, api_key, model,
                          max_tokens=4096, pages=None, usage_hook=None,
                          batch_size=12):
    """给作业范围内每道题生成知识点标签，返回 (rows, error)。

    为避免一次请求题目过多导致输出被截断（finish_reason=length），按
    batch_size 分批调用模型，每批独立请求并把结果合并；没有生成标签的题
    也会保留（tags 为空），由调用方决定是否落库。
    """
    listing = []
    for i, q in enumerate(questions or []):
        if not isinstance(q, dict):
            continue
        no = str(q.get("no", "")).strip()
        if not no:
            continue
        listing.append({"key": str(q.get("key") or f"q{i}"), "no": no,
                        "hint": str(q.get("hint", "")).strip()})
    if not listing:
        return None, "没有可标注的题目"
    if not answer_path:
        return None, "教师版标准答案缺失，无法生成知识点标签"
    parts, err = _file_parts(answer_path, "标准答案", pages)
    if err:
        return None, err

    if batch_size <= 0:
        batch_size = 12
    by_no = {}
    truncated = False
    total_batches = (len(listing) + batch_size - 1) // batch_size
    for start in range(0, len(listing), batch_size):
        batch = listing[start:start + batch_size]
        idx = start // batch_size + 1
        content = [{"type": "text",
                    "text": TAG_PROMPT + json.dumps(batch, ensure_ascii=False) + TAG_RULES},
                   {"type": "text",
                    "text": f"教师版标准答案，共 {len(parts)} 页（第 {idx}/{total_batches} 批）："}]
        content += parts
        reply, finish, err = llm.chat(api_url, api_key, model, content,
                                      max_tokens=max_tokens, timeout=180, retries=1,
                                      usage_hook=usage_hook)
        if err:
            return None, f"AI 标注失败（第 {idx} 批）：{err}"

        data = llm.extract_json(reply)
        raw = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            if finish == "length":
                # 该批输出被截断，跳过这一批（题目标签留空），继续处理后续批次。
                truncated = True
                continue
            return None, "AI 未返回可用的知识点标签，请重试。"
        for item in raw:
            if not isinstance(item, dict):
                continue
            no = str(item.get("no", "")).strip()
            if not no:
                continue
            tags = _clean_tags(item.get("tags"))
            if not tags:
                continue
            by_no[_no_token(no)] = (tags, str(item.get("category", "")).strip())

    rows = []
    for q in listing:
        tags, category = by_no.get(_no_token(q["no"]), ([], ""))
        rows.append({"key": q["key"], "no": q["no"], "category": category, "tags": tags})
    if not any(r["tags"] for r in rows):
        if truncated:
            return None, "AI 输出被截断，未能得到知识点标签，请减少勾选题目后重试。"
        return None, "AI 未能为这些题目生成知识点标签，请重试。"
    if truncated:
        log.warning("知识点标注有批次被截断，仅部分题目拿到标签")
    return rows, ""


def grade_pdf_submission(student_path, answer_path, api_url, api_key, model,
                         extra="", max_tokens=32768, answer_pages=None, prompt="",
                         usage_hook=None):
    """批阅一份作业，返回 {questions, weak_points, evaluation, total_score, errors, error}。

    student_path 为本地绝对路径或路径列表（一次提交多张图片/多页时按顺序合并）；
    answer_path 为本地绝对路径。模型必须是支持图片输入的多模态模型；
    接口按 OpenAI 兼容的 chat/completions 协议传 image_url content part。
    answer_pages 为教师版 PDF 中要发送的页码（1 起），为空表示整份文件。
    prompt 为老师自选的批阅提示词正文，输出格式由系统统一追加；为空用内置默认提示词。
    """
    if not answer_path:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": "请先上传教师版标准答案 PDF，再开始批阅。"}
    student_paths = list(student_path) if isinstance(student_path, (list, tuple)) else [student_path]
    student_parts = []
    for path in student_paths:
        parts, err = _file_parts(path, "学生作业")
        if err:
            return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                    "errors": [], "error": err}
        student_parts += parts
    answer_parts, err = _file_parts(answer_path, "标准答案", answer_pages)
    if err:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": err}

    base = (prompt.strip() + "\n" + GRADE_OUTPUT_CONTRACT) if prompt.strip() else PDF_GRADE_PROMPT
    full_prompt = base + (f"\n补充要求：{extra}" if extra else "")
    content = [{"type": "text", "text": full_prompt},
               {"type": "text", "text": f"① 学生作业，共 {len(student_parts)} 页："}]
    content += student_parts
    content.append({"type": "text", "text": f"② 教师版标准答案，共 {len(answer_parts)} 页："})
    content += answer_parts

    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=GRADE_TIMEOUT,
                                  retries=GRADE_RETRIES, usage_hook=usage_hook)
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


DEFAULT_REPORT_PROMPT = (
    "你是阅卷老师。下面给出某位学生一次作业的逐题判定、失分点与总评。\n"
    "请为学生写一段批改评语。\n"
    "要求：只依据给定的判定，不要编造学生没写的内容；语气客观、以鼓励为主；"
    "improve 最多 3 条，按严重程度排序；没有失分点就给空数组。"
)

REPORT_OUTPUT_CONTRACT = (
    "输出格式（必须严格遵守）：只输出 JSON，不要解释或代码块标记：\n"
    '{"good":"做得好的地方（80~160 字，要具体点出哪几题做得好）",'
    '"improve":["需要改的第 1 处","第 2 处","第 3 处"],'
    '"next":"下一步建议（60~120 字）"}'
)

REPORT_PROMPT = DEFAULT_REPORT_PROMPT + "\n" + REPORT_OUTPUT_CONTRACT


def ai_report_comment(questions, weak_points, evaluation, api_url, api_key, model,
                      max_tokens=2048, prompt="", usage_hook=None):
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
    base = (prompt.strip() + "\n" + REPORT_OUTPUT_CONTRACT) if prompt.strip() else REPORT_PROMPT
    content = [{"type": "text", "text": base + "\n\n数据：\n" + payload}]
    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=120, retries=1,
                                  usage_hook=usage_hook)
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
    "你是教研组组长。下面给出一次作业中全班学生的逐题判定统计，"
    "以及每道题的代表性评语（取多份作业的 ai_comment）。\n"
    "请输出两段内容：\n"
    "1) 每题要点（每题一行短句，5~25 字）：这道题全班主要的错在哪里、或者大家普遍做得好的地方。\n"
    "2) 整体总结（3~6 句，150~300 字）：这次作业全班整体情况、最普遍的几类问题、值得肯定的地方、对下次教学的建议。\n"
    "只输出 JSON：\n"
    '{"per_question":{"题号":"要点",...},'
    '"overall":"整体总结内容"}\n'
    "要求：只依据给定数据，不要编造；每题要点简洁，重点突出；整体总结客观、有教学参考价值。"
)


def ai_lecture_summary(qstats, api_url, api_key, model, max_tokens=4096, usage_hook=None):
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
                                  max_tokens=max_tokens, timeout=180, retries=1,
                                  usage_hook=usage_hook)
    if err:
        return {}
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return {}
    return {"per_question": data.get("per_question", {}),
            "overall": str(data.get("overall", "")).strip()}


# ---------- 基于知识点标签的横向/纵向分析 ----------

DEFAULT_TAG_HORIZONTAL_PROMPT = (
    "你是资深学科教学教研员与数据分析专家。现在需要基于本讲次全体学生的作业批改统计数据，"
    "为授课教师生成一份客观、精准的《讲评课教学诊断报告》。\n\n"
    "### 输入数据\n"
    "- 学科与课程：{subject_and_course}\n"
    "- 作业/讲次名称：{lecture_name}\n"
    "- 参与批改总人数：{total_students}\n"
    "- 题目与知识点统计列表（含题号、关联知识点、全班错误率、典型错误 comment 摘录）：\n"
    "{aggregated_question_stats}\n\n"
    "### 红线与要求\n"
    "1. **严格基于数据**：所有错误率、人数比例、典型错误特征必须严格依据输入统计，"
    "严禁编造未出现的错误类型或主观臆测原因。\n"
    "2. **聚焦共性痛点**：重点剖析错误率较高的题目（如错误率 ≥ 40%），"
    "从“核心概念/原理理解偏差”、“解题思路/逻辑断层”、“审题与作答规范性问题”三个维度"
    "归纳全班共性盲区。\n"
    "3. **知识图谱关联（如有）**：若输入数据中包含知识点关联或前置依赖信息，"
    "需指出高频错题共同指向的底层核心知识模块。\n"
    "4. **可执行性**：教学建议必须具体、可落地，明确指出讲评课的时间分配重点与前置概念补充方向。"
)

TAG_HORIZONTAL_OUTPUT_CONTRACT = (
    "### 输出格式（严格输出合法 JSON）\n"
    "{\n"
    '  "overall_summary": "一句话客观概括本讲次全班整体掌握情况与核心问题（100字以内）",\n'
    '  "high_risk_topics": [\n'
    "    {\n"
    '      "topic_name": "薄弱知识点或题号范围",\n'
    '      "error_rate": "错误率百分比",\n'
    '      "common_pitfall": "全班共性错误模式与典型失分点剖析（80字以内）"\n'
    "    }\n"
    "  ],\n"
    '  "teaching_suggestions": [\n'
    '    "针对下一节讲评课的具体教学与重点拆解建议（最多3条，按优先级排序，每条不超过80字）"\n'
    "  ]\n"
    "}"
)

TAG_HORIZONTAL_PROMPT = DEFAULT_TAG_HORIZONTAL_PROMPT + "\n\n" + TAG_HORIZONTAL_OUTPUT_CONTRACT

DEFAULT_TAG_VERTICAL_PROMPT = (
    "你是资深的个性化学习规划师与学情诊断专家。现在需要结合某位学生近 {lecture_count} 讲的"
    "累计作业表现及知识图谱关联数据，为其生成一份《阶段性学情诊断与提分方案》。\n\n"
    "### 输入数据\n"
    "- 学科与课程：{subject_and_course}\n"
    "- 学生姓名：{student_name}\n"
    "- 覆盖讲次范围：{lecture_range}\n"
    "- 各知识模块累计掌握度统计：{module_mastery_stats}\n"
    "- 知识图谱诊断出的“跨讲次关联薄弱链”（前置基础知识点 -> 衍生错题知识点）："
    "{kg_weakness_chains}\n"
    "- 高频错误类型与作答习惯统计：{error_habit_stats}\n"
    "- 近期表现与早期表现对比趋势：{trend_data}\n\n"
    "### 红线与要求\n"
    "1. **数据支撑，严禁空谈**：所有诊断结论必须有输入数据、知识模块名称或具体讲次题号作为支撑，"
    "严禁脱离事实写泛泛而谈的套话。\n"
    "2. **穿透表象找根因**：不要只罗列学生做错了哪些题。必须结合“跨讲次关联薄弱链”，"
    "向学生解释清楚后续综合题卡壳的底层前置知识漏洞。\n"
    "3. **区分“知识漏洞”与“作答习惯”**：明确剥离“核心原理/方法未掌握”导致的硬性失分，"
    "与“审题遗漏、格式不规范、计算粗心、未作答”导致的习惯性失分。\n"
    "4. **语气与激励**：保持客观、专业且富有建设性的鼓励语气，保护学生学习信心。"
)

TAG_VERTICAL_OUTPUT_CONTRACT = (
    "### 输出格式（严格输出合法 JSON）\n"
    "{\n"
    '  "progress_highlights": [\n'
    '    "肯定学生近期掌握较好或进步明显的知识模块/作答习惯（1-2条，每条不超过60字）"\n'
    "  ],\n"
    '  "root_cause_diagnosis": [\n'
    "    {\n"
    '      "weakness_chain": "前置知识点 -> 关联薄弱知识点",\n'
    '      "analysis": "结合具体错题表现，剖析知识断层的根本原因（不超过100字）"\n'
    "    }\n"
    "  ],\n"
    '  "habit_alerts": [\n'
    '    "跨讲次反复出现的非知识性失分习惯（如无明显习惯问题，输出空数组 []）"\n'
    "  ],\n"
    '  "action_plan": [\n'
    '    "未来两周的具体复习与练习路径建议（严格遵循‘先补前置基础 -> 再练综合应用’的递进顺序，'
    '最多3条，每条不超过80字）"\n'
    "  ]\n"
    "}"
)

TAG_VERTICAL_PROMPT = DEFAULT_TAG_VERTICAL_PROMPT + "\n\n" + TAG_VERTICAL_OUTPUT_CONTRACT


def _prompt_value(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _compose_tag_prompt(template, contract, values):
    """拼装分析提示词：自定义提示词同样追加系统输出契约，并替换数据占位符。

    自定义提示词若删掉了部分占位符，未注入的数据仍会附在末尾，避免模型无数据可用。
    """
    base = (template.strip() + "\n" + contract
            if template.strip() else contract)
    text = base
    missing = []
    for key, value in values.items():
        token = "{" + key + "}"
        if token in text:
            text = text.replace(token, _prompt_value(value))
        else:
            missing.append(f"- {key}：{_prompt_value(value)}")
    if missing:
        text += "\n\n数据：\n" + "\n".join(missing)
    return text


def ai_horizontal_analysis(context, api_url, api_key, model,
                           max_tokens=4096, prompt="", usage_hook=None):
    """按讲次生成全班教学诊断；context 提供提示词所需的各项数据。

    context 键：subject_and_course / lecture_name / total_students /
    aggregated_question_stats。
    """
    values = {
        "subject_and_course": context.get("subject_and_course", ""),
        "lecture_name": context.get("lecture_name", ""),
        "total_students": str(context.get("total_students", 0)),
        "aggregated_question_stats": context.get("aggregated_question_stats") or [],
    }
    text = _compose_tag_prompt(prompt or DEFAULT_TAG_HORIZONTAL_PROMPT,
                               TAG_HORIZONTAL_OUTPUT_CONTRACT, values)
    reply, finish, err = llm.chat(api_url, api_key, model,
                                  [{"type": "text", "text": text}],
                                  max_tokens=max_tokens, timeout=180, retries=1,
                                  usage_hook=usage_hook)
    if err:
        return {}
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return {}
    summary = str(data.get("overall_summary") or data.get("summary") or "").strip()
    topics = [t for t in (data.get("high_risk_topics") or []) if isinstance(t, dict)][:8]
    suggestions = data.get("teaching_suggestions") or []
    if isinstance(suggestions, str):
        suggestions = [suggestions]
    suggestions = [str(s).strip() for s in suggestions if str(s).strip()][:3]
    if not summary and not topics and not suggestions:
        return {}
    return {"overall_summary": summary, "high_risk_topics": topics,
            "teaching_suggestions": suggestions}


def ai_vertical_analysis(context, api_url, api_key, model,
                         max_tokens=4096, prompt="", usage_hook=None):
    """按学生跨讲次生成阶段诊断；context 提供提示词所需的各项数据。"""
    values = {
        "lecture_count": str(context.get("lecture_count", 0)),
        "subject_and_course": context.get("subject_and_course", ""),
        "student_name": context.get("student_name", ""),
        "lecture_range": context.get("lecture_range", ""),
        "module_mastery_stats": context.get("module_mastery_stats") or [],
        "kg_weakness_chains": context.get("kg_weakness_chains", ""),
        "error_habit_stats": context.get("error_habit_stats") or [],
        "trend_data": context.get("trend_data") or [],
    }
    text = _compose_tag_prompt(prompt or DEFAULT_TAG_VERTICAL_PROMPT,
                               TAG_VERTICAL_OUTPUT_CONTRACT, values)
    reply, finish, err = llm.chat(api_url, api_key, model,
                                  [{"type": "text", "text": text}],
                                  max_tokens=max_tokens, timeout=180, retries=1,
                                  usage_hook=usage_hook)
    if err:
        return {}
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return {}
    highlights = _str_list(data.get("progress_highlights"), 2)
    root_cause = [r for r in (data.get("root_cause_diagnosis") or [])
                  if isinstance(r, dict)][:6]
    habits = _str_list(data.get("habit_alerts"), 5)
    plan = _str_list(data.get("action_plan"), 3)
    if not highlights and not root_cause and not habits and not plan:
        return {}
    return {"progress_highlights": highlights, "root_cause_diagnosis": root_cause,
            "habit_alerts": habits, "action_plan": plan}


def _str_list(value, limit):
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in (value or []) if str(item).strip()][:limit]
