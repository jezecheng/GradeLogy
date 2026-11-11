# -*- coding: utf-8 -*-
"""节点 match_answer：在教师版答案文本中按题号定位答案片段。

POC：python -m utilities.match --no 3.1 --pdf uploads/test_ans.pdf [--window 400]
"""
import argparse
import os
import re

from . import llm


def load_answer_text(answer_pdf, limit=8000, max_pages=5):
    """抽取答案 PDF 的文字层；扫描版（无文字层）返回空串。路径可为绝对或项目根相对。

    limit/max_pages: 控制抽取量。预处理场景（prepare）应放大 limit 并放开页数，
    默认参数服务于在线批改的快速匹配。
    """
    path = answer_pdf if os.path.isabs(answer_pdf) else os.path.join(llm.BASE_DIR, answer_pdf)
    try:
        from pypdf import PdfReader
        pages = PdfReader(path).pages
        if max_pages:
            pages = pages[:max_pages]
        return ("\n".join(p.extract_text() or "" for p in pages) or "")[:limit]
    except ImportError:
        pass
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(path)
        n = min(len(doc), max_pages) if max_pages else len(doc)
        parts = [doc[i].get_textpage().get_text_range() for i in range(n)]
        return "\n".join(parts)[:limit]
    except Exception:
        return ""


def match_answer(no, answer_text, window=400):
    """在教师版答案文本中按题号定位，返回附近的答案片段；找不到返回空串。"""
    if not no or not answer_text:
        return ""
    pat = re.escape(str(no))
    m = re.search(r"(?<![A-Za-z0-9.])" + pat + r"(?![0-9])", answer_text)
    if not m:
        return ""
    start = max(0, m.start() - 15)
    end = min(len(answer_text), m.start() + window)
    return answer_text[start:end].strip()


def main():
    ap = argparse.ArgumentParser(description="POC：在教师版答案中定位某题的答案片段")
    ap.add_argument("--no", required=True, help="题号，如 3.1")
    ap.add_argument("--pdf", required=True, help="教师版答案 PDF")
    ap.add_argument("--window", type=int, default=400, help="截取窗口字数（默认 400）")
    args = ap.parse_args()

    text = load_answer_text(args.pdf)
    if not text.strip():
        raise SystemExit("答案 PDF 没有文字层（可能是扫描版），无法做文本匹配。")
    snippet = match_answer(args.no, text, window=args.window)
    if snippet:
        print(f"题号 {args.no} 的答案片段：\n{snippet}")
    else:
        print(f"未在答案文本中定位到题号 {args.no}")


if __name__ == "__main__":
    main()
