"""知识图谱资料文本抽取：PDF 按页取文本，Markdown/TXT 直接读取，再切成 AI 可消化的块。"""
import os
import re

import pypdfium2 as pdfium

KG_EXTENSIONS = (".pdf", ".md", ".markdown", ".txt")
# 单份资料最多送入模型的正文字数，避免整本教材一次耗尽额度
MAX_MATERIAL_CHARS = 120000
CHUNK_CHARS = 3000
CHUNK_OVERLAP = 200


def allowed_kg_file(filename):
    return os.path.splitext(filename or "")[1].lower() in KG_EXTENSIONS


def extract_pages(path):
    """返回 [(page_no, text), ...]；PDF 按页，纯文本整体作为第 1 页。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf_pages(path)
    return [(1, _read_text_file(path))]


def _extract_pdf_pages(path):
    pages = []
    doc = pdfium.PdfDocument(path)
    try:
        for index in range(len(doc)):
            page = doc[index]
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range() or ""
            finally:
                textpage.close()
            pages.append((index + 1, text))
    finally:
        doc.close()
    return pages


def _read_text_file(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def chunk_pages(pages, max_chars=CHUNK_CHARS, overlap=CHUNK_OVERLAP):
    """把按页文本切成带页码的块；跨页不合并，便于回溯出处。"""
    chunks = []
    for page_no, text in pages:
        text = _clean(text)
        if not text:
            continue
        step = max(1, max_chars - overlap)
        for start in range(0, len(text), step):
            piece = text[start:start + max_chars].strip()
            if piece:
                chunks.append((page_no, piece))
            if start + max_chars >= len(text):
                break
    return chunks


def _clean(text):
    text = (text or "").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_concept(name):
    """概念归一化：去空白与标点、统一大小写，用于同名/别名合并。"""
    return re.sub(r"[\W_]+", "", (name or "").casefold())
