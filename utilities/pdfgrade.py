# -*- coding: utf-8 -*-
"""整份作业批阅：学生作业 + 教师版标准答案一起交给支持 PDF 的模型。

不再切题、不再裁剪图片，也不依赖 PDF 文字层；模型直接读取整份 PDF，
逐题输出判定、OCR、标准答案与评语。
"""
import base64
import json
import os

from . import llm

PDF_GRADE_PROMPT = (
    "你是化学竞赛阅卷老师。附件依次是：① 学生作业（PDF 或扫描图片）；② 教师版标准答案（PDF）。\n"
    "请逐题对照批阅，只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"题号","ocr":"学生实际作答内容","standard_answer":"标准答案要点",'
    '"verdict":"对|半对|错|无法判定","score":0,"max_score":0,"comment":"给分/扣分说明"}],'
    '"weak_points":[{"no":"题号","type":"失分类型","detail":"具体说明"}],'
    '"evaluation":"整份作业总评（不超过 100 字）","total_score":"如 12/20，没有分值就留空"}\n'
    "红线规则：\n"
    "1. 学生作业里的实际书写是唯一评分依据，严禁把标准答案当成学生的作答。\n"
    "2. 看不清就写〔不清〕；无法分辨时必须判「无法判定」，严禁猜测或编造学生写了什么。\n"
    "3. 化学方程式/离子方程式必须查配平、条件、状态符号、可逆号；数值题查有效数字与单位。\n"
    "4. 题号用学生作业里标注的题号；一题多问在 comment 说明哪一问失分；未作答判「错」并注明未作答。\n"
    "5. 作业没有标注分值就不要编造分数：score 与 max_score 都输出 0，只给定性判定。\n"
    "6. comment 不超过 60 个汉字，standard_answer 不超过 60 个汉字。\n"
    "7. 学生作业中缺失或与标准答案对不上的题，不要凭空补题。")

MAX_FILE_MB = 20
VERDICTS = ("对", "半对", "错", "无法判定")
_MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _as_num(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _file_part(path, label):
    """把 PDF/图片读成 OpenAI 兼容的 content part；失败返回 (None, 错误)。"""
    if not path or not os.path.exists(path):
        return None, f"{label}文件不存在：{os.path.basename(path or '')}"
    size = os.path.getsize(path)
    if size > MAX_FILE_MB * 1024 * 1024:
        return None, f"{label}超过 {MAX_FILE_MB}MB，请压缩后再上传"
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME.get(ext)
    if not mime:
        return None, f"{label}格式不支持：{ext or '未知'}"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    if ext == ".pdf":
        return {"type": "file",
                "file": {"filename": os.path.basename(path),
                         "file_data": f"data:application/pdf;base64,{b64}"}}, ""
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}}, ""


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


def grade_pdf_submission(student_path, answer_path, api_url, api_key, model,
                         extra="", max_tokens=16000):
    """批阅一份作业，返回 {questions, weak_points, evaluation, total_score, errors, error}。

    student_path / answer_path 为本地绝对路径。模型必须是支持 PDF 输入的多模态模型；
    接口按 OpenAI 兼容的 chat/completions 协议传 file content part。
    """
    if not answer_path:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": "请先上传教师版标准答案 PDF，再开始批阅。"}
    student_part, err = _file_part(student_path, "学生作业")
    if err:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": err}
    answer_part, err = _file_part(answer_path, "标准答案")
    if err:
        return {"questions": [], "weak_points": [], "evaluation": "", "total_score": "",
                "errors": [], "error": err}

    prompt = PDF_GRADE_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    content = [{"type": "text", "text": prompt},
               {"type": "text", "text": "① 学生作业："}, student_part,
               {"type": "text", "text": "② 教师版标准答案："}, answer_part]

    reply, finish, err = llm.chat(api_url, api_key, model, content,
                                  max_tokens=max_tokens, timeout=600)
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
