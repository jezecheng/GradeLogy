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


def _find(no, text):
    """按题号精确查找位置（3.1 不能命中 3.10，也不能命中 x3.1）。"""
    if not no:
        return None
    return re.search(r"(?<![A-Za-z0-9.])" + re.escape(str(no)) + r"(?![0-9])", text)


def split_sections(answer_text, numbers, max_len=1200):
    """按题号在教师版文本中的出现位置切段，返回 {题号: 本题到下一题之间的原文}。

    比固定字数窗口更可靠：窗口起点是题干、答案在后面的情况也能整段取到。
    同一题号重复出现时只取第一处（讲义不同页重复编号的已知限制）。
    """
    hits = []
    for no in numbers or []:
        m = _find(no, answer_text)
        if m:
            hits.append((m.start(), str(no)))
    hits.sort()
    sections = {}
    for i, (pos, no) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else min(len(answer_text), pos + max_len)
        sections[no] = answer_text[pos:end].strip()[:max_len]
    return sections


def match_answer(no, answer_text, window=400, numbers=None, max_len=1200):
    """在教师版答案文本中按题号定位答案；找不到返回空串。

    numbers: 传入整份卷子的题号列表时，按「本题到下一题」整段切分（推荐）；
             不传则退化为以题号为起点的固定字数窗口。
    """
    if not no or not answer_text:
        return ""
    if numbers:
        sect = split_sections(answer_text, numbers, max_len=max_len).get(str(no))
        if sect:
            return sect
    m = _find(no, answer_text)
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
