# -*- coding: utf-8 -*-
"""节点 prepare：教师版讲义一次性预处理——切题、裁剪题目图、提取标准答案。

产物按题缓存（DB + attachments/<课程>/<讲次>/answer/），批改时直接查，
不必每次批改都重新处理教师版 PDF。

POC：python -m utilities.prepare --pdf uploads/test_ans.pdf [--pages 16]
"""
import argparse
import json
import os

from . import llm, storage
from .detect import detect_questions, pdf_to_pages, crop_bbox
from .match import load_answer_text, split_sections


def prepare_answer_key(answer_pdf, attach_dir, api_url, api_key, model,
                       extra="", batch=4, max_pages=20):
    """对教师版 PDF 做一次预处理。

    answer_pdf: uploads 下的文件名（或绝对路径）。
    attach_dir: 产物输出子目录（相对 uploads），需已存在。
    返回 (items, errors)，items 为 [{no, page, image_path, answer}, ...]。
    """
    if os.path.isabs(answer_pdf) or os.path.exists(answer_pdf):
        pdf_path = answer_pdf
    else:
        pdf_path = os.path.join(llm.UPLOAD_DIR, answer_pdf)
    pages = pdf_to_pages(pdf_path, max_pages=max_pages, subdir=attach_dir, stem="ans")
    if not pages:
        return [], ["教师版 PDF 无法渲染成图片（请确认未加密），或安装 pypdfium2 后重试。"]

    questions, errors = detect_questions(pages, api_url, api_key, model, batch=batch)
    # 教师版通常带文字层；预处理要拿全量文本（20 页讲义可达 2 万字）
    answer_text = load_answer_text(pdf_path, limit=100000, max_pages=None)
    # 一次性按题号把整份答案切段，得到「题号 → 本题到下一题原文」的完整清单，
    # 比逐题固定窗口更不容易把题干误当成答案。
    sections = split_sections(answer_text, [q.get("no") for q in questions])

    items = []
    for q in questions:
        pno = q.get("page") or 1
        page = pages[pno - 1] if 1 <= pno <= len(pages) else pages[0]
        no = q.get("no") or ""
        stem = storage.question_name("ans", no)[:-4] if no else f"ans_q_{len(items) + 1}"
        items.append({
            "no": no,
            "page": pno,
            "image_path": crop_bbox(page, q.get("bbox"), stem, subdir=attach_dir),
            "answer": sections.get(str(no), ""),
        })
    return items, errors


def main():
    ap = argparse.ArgumentParser(description="POC：教师版讲义切题 + 裁图 + 提取标准答案")
    ap.add_argument("--pdf", required=True, help="教师版 PDF（项目根相对路径或绝对路径）")
    ap.add_argument("--pages", type=int, default=20, help="最多渲染页数（默认 20）")
    ap.add_argument("--batch", type=int, default=4, help="每批送模型的页数（默认 4）")
    ap.add_argument("--teacher", type=int, default=None, help="指定 teacher_id 的配置")
    args = ap.parse_args()
    cfg = llm.resolve_cli_config(args)

    attach_dir = storage.attach_dir("_poc", "prepare")  # POC 产物放 attachments/_poc/prepare/
    items, errors = prepare_answer_key(
        args.pdf,
        attach_dir, cfg["api_url"], cfg["api_key"], cfg["model"],
        batch=args.batch, max_pages=args.pages)
    print(f"产物目录: uploads/{attach_dir}/")
    print(json.dumps({"count": len(items),
                      "items": [{**it, "answer": (it["answer"][:80] + "…") if len(it["answer"]) > 80 else it["answer"]}
                                for it in items],
                      "errors": errors}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
