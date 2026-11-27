"""知识图谱资料文本抽取：PDF 按页取文本，Word 文档按正文分页，Markdown/TXT 直接读取，再切成 AI 可消化的块。"""
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

import pypdfium2 as pdfium

from .pdfium_runtime import PDFIUM_LOCK, close_pdfium_resource

KG_EXTENSIONS = (".pdf", ".doc", ".docx", ".md", ".markdown", ".txt")
# 单份资料最多送入模型的正文字数，避免整本教材一次耗尽额度
MAX_MATERIAL_CHARS = 120000
CHUNK_CHARS = 3000
CHUNK_OVERLAP = 200

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = "{%s}" % _W_NS
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_WORD_IDENT = 0xA5EC
_PAGE_BREAK = "\f"


def allowed_kg_file(filename):
    return os.path.splitext(filename or "")[1].lower() in KG_EXTENSIONS


def extract_pages(path):
    """返回 [(page_no, text), ...]；PDF 按页，Word 按分页符，纯文本整体作为第 1 页。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf_pages(path)
    if ext == ".docx":
        return _split_pages(_extract_docx_text(path))
    if ext == ".doc":
        return _split_pages(_extract_doc_text(path))
    return [(1, _read_text_file(path))]


def _extract_pdf_pages(path):
    pages = []
    # Text extraction can run in a background worker while teachers are
    # rendering/counting PDFs in request threads; share the same PDFium lock.
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(path)
        try:
            for index in range(len(doc)):
                page = doc[index]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range() or ""
                finally:
                    close_pdfium_resource(textpage, "textpage", path)
                pages.append((index + 1, text))
        finally:
            close_pdfium_resource(doc, "document", path)
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


def _split_pages(text):
    """按分页符切页；没有分页符时整篇作为第 1 页。"""
    parts = str(text or "").split(_PAGE_BREAK)
    while len(parts) > 1 and not parts[-1].strip():
        parts.pop()
    return [(index + 1, part) for index, part in enumerate(parts)]


def _extract_docx_text(path):
    """读取 DOCX 正文：段落、换行、制表符与分页符，忽略域代码与制表位定义。"""
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 100 * 1024 * 1024:
                raise ValueError("DOCX 正文过大，请拆分后上传")
            xml = archive.read(info)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ValueError("不是有效的 DOCX 文档，请确认文件未损坏") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("DOCX 文档内容无法解析，请另存后重新上传") from exc
    parts = []

    def walk(node):
        tag = node.tag
        if tag in (_W + "tabs", _W + "instrText"):
            return
        if tag == _W + "t":
            parts.append(node.text or "")
        elif tag == _W + "tab":
            parts.append("\t")
        elif tag == _W + "br":
            parts.append(_PAGE_BREAK if node.get(_W + "type") == "page" else "\n")
        elif tag == _W + "cr":
            parts.append("\n")
        elif tag == _W + "lastRenderedPageBreak":
            parts.append(_PAGE_BREAK)
        for child in node:
            walk(child)
        if tag == _W + "p":
            parts.append("\n")
        elif tag == _W + "tc":
            parts.append("\t")

    walk(root)
    return "".join(parts)


def _extract_doc_text(path):
    """读取 Word 97-2003 二进制文档；少数软件导出的 .doc 实际是 DOCX，按内容识别。"""
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
        raise ValueError("服务器缺少 DOC 解析组件，请上传 DOCX 或 PDF") from exc
    if not olefile.isOleFile(path):
        if zipfile.is_zipfile(path):
            return _extract_docx_text(path)
        with open(path, "rb") as fh:
            if fh.read(8) == _OLE_MAGIC:
                raise ValueError("DOC 文档结构无法识别，请另存为 DOCX 后上传")
        raise ValueError("不是有效的 Word 文档，请另存为 DOCX 后上传")
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("WordDocument"):
            raise ValueError("不是 Word 97-2003 文档，请另存为 DOCX 后上传")
        word = ole.openstream("WordDocument").read()
        if len(word) < 0x0C:
            raise ValueError("Word 文档内容不完整")
        flags = struct.unpack_from("<H", word, 0x0A)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        table = ole.openstream(table_name).read() if ole.exists(table_name) else b""
    return _word97_text(word, table)


def _word97_text(word, table):
    """按 FIB 与分片表（CLX/Pcdt）还原 Word 97-2003 正文。"""
    if len(word) < 0x01AA:
        raise ValueError("Word 文档内容不完整")
    ident = struct.unpack_from("<H", word, 0)[0]
    if ident != _WORD_IDENT:
        raise ValueError("不是 Word 97-2003 文档，请另存为 DOCX 后上传")
    nfib = struct.unpack_from("<H", word, 2)[0]
    if nfib < 0x00C1:
        raise ValueError("暂不支持 Word 6.0/95 文档，请另存为 DOCX 后上传")
    flags = struct.unpack_from("<H", word, 0x0A)[0]
    if flags & 0x0100:
        raise ValueError("Word 文档已加密，请解除密码后重新上传")
    ccp_text = struct.unpack_from("<I", word, 0x4C)[0]
    fc_clx, lcb_clx = struct.unpack_from("<II", word, 0x01A2)
    if lcb_clx <= 0:
        raise ValueError("Word 文档缺少正文信息")
    clx = b""
    if table and fc_clx + lcb_clx <= len(table):
        clx = table[fc_clx:fc_clx + lcb_clx]
    elif fc_clx + lcb_clx <= len(word):
        clx = word[fc_clx:fc_clx + lcb_clx]
    if not clx:
        raise ValueError("Word 文档缺少正文信息")
    return _clean_word_text(_decode_piece_table(word, _find_piece_table(clx), ccp_text))


def _find_piece_table(clx):
    """跳过属性修改块，取出最后一个分片表 Pcdt。"""
    pos = 0
    while pos < len(clx):
        kind = clx[pos]
        if kind == 0x01:
            if pos + 3 > len(clx):
                break
            size = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + size
        elif kind == 0x02:
            if pos + 5 > len(clx):
                break
            size = struct.unpack_from("<I", clx, pos + 1)[0]
            start = pos + 5
            return clx[start:start + size]
        else:
            break
    raise ValueError("Word 文档缺少分片信息，请另存为 DOCX 后上传")


def _decode_piece_table(word, plc, ccp_text=0):
    """按 CP 分片还原正文；压缩分片为单字节 cp1252，其余为 UTF-16LE。"""
    if len(plc) < 16 or (len(plc) - 4) % 12:
        raise ValueError("Word 文档分片信息异常，请另存为 DOCX 后上传")
    count = (len(plc) - 4) // 12
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    limit = ccp_text if ccp_text else cps[-1]
    pcd_base = 4 * (count + 1)
    parts = []
    for index in range(count):
        cp_start, cp_end = cps[index], cps[index + 1]
        if cp_start >= limit:
            break
        chars = min(cp_end, limit) - cp_start
        if chars <= 0:
            continue
        offset, = struct.unpack_from("<I", plc, pcd_base + index * 8 + 2)
        compressed = bool(offset & 0x40000000)
        offset &= 0x3FFFFFFF
        if compressed:
            offset //= 2
            raw = word[offset:offset + chars]
            parts.append(raw.decode("cp1252", "replace"))
        else:
            raw = word[offset:offset + chars * 2]
            parts.append(raw.decode("utf-16-le", "replace"))
        if cp_end >= limit:
            break
    return "".join(parts)


def _clean_word_text(text):
    """把 Word 控制字符转成可读文本；丢弃域指令，保留域结果。"""
    out = []
    fields = []
    for char in text:
        if char == "\x13":
            fields.append("instr")
            continue
        if char == "\x14":
            if fields:
                fields[-1] = "result"
            continue
        if char == "\x15":
            if fields:
                fields.pop()
            continue
        if "instr" in fields:
            continue
        if char == "\r":
            out.append("\n")
        elif char == "\x07":
            out.append("\t")
        elif char == "\x0b":
            out.append("\n")
        elif char == "\x0c":
            out.append(_PAGE_BREAK)
        elif char == "\x1e":
            out.append("-")
        elif char in ("\x1f", "\x01", "\x02", "\x05", "\x08"):
            continue
        elif char == "\xa0":
            out.append(" ")
        elif char < " " and char not in ("\n", "\t", _PAGE_BREAK):
            continue
        else:
            out.append(char)
    return "".join(out)


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
