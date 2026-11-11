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
    '{"ocr":"学生实际写出的完整内容","standard_answer":"标准答案要点",'
    '"verdict":"对|半对|错|无法判定","comment":"给分/扣分说明，指出具体失分点"}\n'
    "判定标准：\n"
    "- 对：答案与关键过程符合标准答案；\n"
    "- 半对：核心思路或部分结论正确，但存在漏项、步骤不完整或局部错误；\n"
    "- 错：关键结论、计算或推理错误；\n"
    "- 无法判定：图像模糊/遮挡导致无法可靠辨认学生写了什么，或裁剪图内容与本题无关。\n"
    "红线规则（必须遵守）：\n"
    "1. 学生实际写了什么才是评分依据；严禁把标准答案当成学生的作答。\n"
    "2. 看不清就写〔不清〕；无法辨认时必须判定为「无法判定」，严禁猜测、严禁为了给出结论而编造内容。\n"
    "3. 不得因为学生结果接近标准答案就反向推断其书写内容。\n"
    "4. 化学方程式/离子方程式查配平、条件、状态符号、可逆号；数值题查有效数字与单位。\n"
    "5. 一题多问分别对照，在 comment 说明哪一问失分；未作答的题判「错」，comment 写\"未作答\"。\n"
    "6. comment 不超过 60 个汉字，standard_answer 不超过 60 个汉字。")

SCORE_HINT = ("本题满分 {max_score} 分，请在上面的 JSON 中额外输出 \"score\": 得分（0 到 {max_score} 之间的数字）。"
              "「无法判定」时 score 输出 null，不要臆造分数。")


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


def grade_question(image_path, answer_text, api_url, api_key, model, extra="",
                   max_tokens=1500, max_score=0):
    """批改一道题。返回 {ocr, standard_answer, verdict, max_score, score, comment, error}。

    max_score: 教师给定的本题满分。为 0 时按「纯定性」批改，绝不返回分数——
    教师版通常没有分值，让模型自己给分就是编造。
    """
    prompt = GRADE_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    if max_score > 0:
        prompt += "\n" + SCORE_HINT.format(max_score=max_score)
    content = [{"type": "text", "text": prompt}]
    if answer_text:
        content.append({"type": "text", "text": f"该题标准答案（供对照）：\n{answer_text}"})
    else:
        content.append({"type": "text",
                        "text": "未在教师版中定位到该题标准答案，按常识与化学规范判定，并在 comment 注明\"未找到标准答案\"。"})
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
    verdict = str(parsed.get("verdict", "")).strip()
    # 分数只接受教师在作业里给定的满分；未给分或「无法判定」时一律留空
    score = None
    if max_score > 0 and verdict and verdict != "无法判定":
        score = _as_num(parsed.get("score"), 0.0)
        score = max(0.0, min(float(max_score), score))
    return {
        "ocr": str(parsed.get("ocr", "")).strip(),
        "standard_answer": str(parsed.get("standard_answer", answer_text)).strip() or answer_text,
        "verdict": verdict,
        "max_score": float(max_score),
        "score": score,
        "comment": str(parsed.get("comment", "")).strip(),
        "error": "",
    }


def main():
    ap = argparse.ArgumentParser(description="POC：单题批改")
    ap.add_argument("--image", required=True, help="题目裁剪图（uploads 文件名或绝对路径）")
    ap.add_argument("--pdf", default="", help="教师版答案 PDF（可选，用于定位标准答案）")
    ap.add_argument("--no", default="", help="题号，配合 --pdf 定位答案片段")
    ap.add_argument("--window", type=int, default=400)
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--max-score", type=float, default=0, help="本题满分；不填则纯定性、不给分")
    ap.add_argument("--teacher", type=int, default=None)
    args = ap.parse_args()
    cfg = llm.resolve_cli_config(args)

    ans = ""
    if args.pdf:
        text = match_mod.load_answer_text(args.pdf)
        ans = match_mod.match_answer(args.no, text, window=args.window) if args.no else text
    result = grade_question(args.image, ans, cfg["api_url"], cfg["api_key"],
                            cfg["model"], cfg["extra"], max_tokens=args.max_tokens,
                            max_score=args.max_score)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
