# -*- coding: utf-8 -*-
"""节点 grade：单题批改（裁剪图 + 标准答案 → OCR/判分/评语）。

POC：python -m utilities.grade --image uploads/crop_xxx.jpg --pdf uploads/test_ans.pdf --no 3.1
"""
import argparse
import base64
import json
import os

from . import llm
from . import match as match_mod

GRADE_PROMPT = (
    "你是化学竞赛阅卷老师。输入是一道学生手写作答的裁剪图，以及该题的标准答案。\n"
    "请批改本题，只输出 JSON，不要输出解释或代码块标记：\n"
    '{"ocr":"学生写出的完整内容","standard_answer":"标准答案要点",'
    '"verdict":"对|半对|错","max_score":2,"score":2,'
    '"comment":"给分/扣分说明，指出具体失分点"}\n'
    "判分规则：\n"
    "1. 化学方程式/离子方程式必须查配平、条件、状态符号、可逆号；数值题查有效数字与单位。\n"
    "2. 只按学生实际写出的内容判，不得推测未写内容；OCR 严格忠实，看不清写〔不清〕。\n"
    "3. 一题若有多个空，分别对照并在 comment 说明哪一空失分。\n"
    "4. 不给印象分；未作答的题 score=0，comment 写\"未作答\"。\n"
    "5. comment 不超过 60 个汉字，standard_answer 不超过 60 个汉字；只写关键结论，不写完整推导。")


def _as_num(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _image_path(image_path):
    """image_path 可为绝对路径或 uploads 下的文件名。"""
    if os.path.isabs(image_path) and os.path.exists(image_path):
        return image_path
    return os.path.join(llm.UPLOAD_DIR, image_path)


def grade_question(image_path, answer_text, api_url, api_key, model, extra="", max_tokens=1500):
    """批改一道题。返回 {ocr, standard_answer, verdict, max_score, score, comment, error}。"""
    prompt = GRADE_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    content = [{"type": "text", "text": prompt}]
    if answer_text:
        content.append({"type": "text", "text": f"该题标准答案（供对照）：\n{answer_text}"})
    else:
        content.append({"type": "text",
                        "text": "未在教师版中定位到该题标准答案，按常识与化学规范判分。"})
    with open(_image_path(image_path), "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    reply, _finish, err = llm.chat(api_url, api_key, model, content, max_tokens=max_tokens)
    if err:
        return {"ocr": "", "standard_answer": answer_text, "verdict": "",
                "max_score": 0.0, "score": None, "comment": "", "error": err}
    parsed = llm.extract_json(reply) or {}
    if not parsed.get("verdict") and not parsed.get("ocr"):
        # 输出为空/不可解析时不静默吞掉，让上层把该题记为失败
        return {"ocr": "", "standard_answer": answer_text, "verdict": "",
                "max_score": 0.0, "score": None, "comment": "",
                "error": f"模型输出无法解析为批改 JSON（前 120 字：{reply[:120]}）"}
    return {
        "ocr": str(parsed.get("ocr", "")).strip(),
        "standard_answer": str(parsed.get("standard_answer", answer_text)).strip() or answer_text,
        "verdict": str(parsed.get("verdict", "")).strip(),
        "max_score": _as_num(parsed.get("max_score"), 0.0),
        "score": parsed.get("score"),
        "comment": str(parsed.get("comment", "")).strip(),
        "error": "",
    }


def main():
    ap = argparse.ArgumentParser(description="POC：单题批改")
    ap.add_argument("--image", required=True, help="题目裁剪图（uploads 文件名或绝对路径）")
    ap.add_argument("--pdf", default="", help="教师版答案 PDF（可选，用于定位标准答案）")
    ap.add_argument("--no", default="", help="题号，配合 --pdf 定位答案片段")
    ap.add_argument("--window", type=int, default=400)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--teacher", type=int, default=None)
    args = ap.parse_args()
    cfg = llm.resolve_cli_config(args)

    ans = ""
    if args.pdf:
        text = match_mod.load_answer_text(args.pdf)
        ans = match_mod.match_answer(args.no, text, window=args.window) if args.no else text
    result = grade_question(args.image, ans, cfg["api_url"], cfg["api_key"],
                            cfg["model"], cfg["extra"], max_tokens=args.max_tokens)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
