# -*- coding: utf-8 -*-
"""将服务端渲染后的批改报告导出为可下载 PDF。"""
from html import escape
from html.parser import HTMLParser
from io import BytesIO
import re
from urllib.parse import urlsplit

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .report import render_markdown


_SUBSCRIPT = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SUPERSCRIPT = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")


def _latex_text(value):
    """把报告常见的轻量 LaTeX 转成适合 PDF 正文阅读的 Unicode 文本。"""
    text = value.strip()
    text = re.sub(r"\\(?:mathrm|text|mathbf|mathit|operatorname|mathsf|mathcal)\s*", "", text)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1/\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    text = re.sub(r"\\xrightarrow(?:\{([^{}]*)\})?", lambda m: "→" + (f"[{m[1]}]" if m[1] else ""), text)
    replacements = {
        r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←",
        r"\leftrightarrow": "↔", r"\times": "×", r"\cdot": "·",
        r"\pm": "±", r"\le": "≤", r"\leq": "≤", r"\ge": "≥",
        r"\geq": "≥", r"\neq": "≠", r"\approx": "≈", r"\Delta": "Δ",
        r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\theta": "θ",
        r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\infty": "∞",
        r"\lg": "lg", r"\log": "log", r"\ln": "ln",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = text.replace(r"\left", "").replace(r"\right", "")
    text = re.sub(r"\\[,;! ]", "", text)

    def script(match):
        table = _SUBSCRIPT if match.group(1) == "_" else _SUPERSCRIPT
        value = match.group(2)
        mapped = value.translate(table)
        # 保留字母指数/下标的可读性；数字和常见符号使用真正的上下标字形。
        return mapped if all(ch in "0123456789+-=()" for ch in value) else match.group(1) + "(" + value + ")"

    text = re.sub(r"([_^])\{([^{}]+)\}", script, text)
    text = re.sub(r"([_^])([A-Za-z0-9+-])", lambda m: m.group(2).translate(
        _SUBSCRIPT if m.group(1) == "_" else _SUPERSCRIPT), text)
    text = text.replace("{", "").replace("}", "")
    return text.replace("\\", "")


def _reportlab_text(value):
    """转义 Paragraph 标记，并将行内公式转换为可读文本。"""
    text = re.sub(r"\$([^$\n]+)\$", lambda m: _latex_text(m.group(1)), value)
    return escape(text, quote=False).replace("\n", "<br/>")


def _register_cjk_font():
    # ReportLab 内置 CID 字体，避免依赖服务器额外安装 CJK 字体文件。
    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        pdfmetrics.registerFontFamily("STSong-Light", normal="STSong-Light",
                                      bold="STSong-Light", italic="STSong-Light",
                                      boldItalic="STSong-Light")


class _RenderedHTMLParser(HTMLParser):
    """把项目自己的渲染 HTML 转成 ReportLab flowables，不接受原始用户 HTML。"""

    INLINE_TAGS = {"strong": "b", "b": "b", "em": "i", "i": "i",
                   "sup": "super", "sub": "sub"}

    def __init__(self, styles, page_width):
        super().__init__(convert_charrefs=True)
        self.styles = styles
        self.page_width = page_width
        self.story = []
        self.buffer = None
        self.block_style = None
        self.table_rows = None
        self.table_row = None
        self.cell_buffer = None
        self.cell_header = False
        self.list_stack = []
        self.quote_depth = 0

    def _begin_block(self, style):
        if self.buffer is None:
            self.buffer = []
            self.block_style = style

    def _finish_block(self):
        if self.buffer is None:
            return
        text = "".join(self.buffer).strip()
        if text:
            style = self.block_style
            if self.quote_depth:
                style = self.styles["PDFQuote"]
            bullet = None
            if self.list_stack:
                kind, index = self.list_stack[-1]
                bullet = f"{index}." if kind == "ol" else "•"
                if kind == "ol":
                    self.list_stack[-1] = (kind, index + 1)
                style = self.styles["PDFList"]
            self.story.append(Paragraph(text, style, bulletText=bullet))
        self.buffer = None
        self.block_style = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "pre":
            self._begin_block(self.styles["PDFCode"])
        elif tag in ("h1", "h2", "h3", "h4", "p", "blockquote", "li"):
            if tag == "blockquote":
                self.quote_depth += 1
            if tag in ("h1", "h2", "h3", "h4"):
                self._begin_block(self.styles["PDFTitle" if tag == "h1" else "PDFHeading"])
            elif tag == "li":
                self._begin_block(self.styles["PDFList"])
            else:
                self._begin_block(self.styles["PDFBody"])
        elif tag in ("ul", "ol"):
            self.list_stack.append((tag, 1))
        elif tag == "hr":
            self.story.append(Spacer(1, 5))
            self.story.append(HRFlowable(width="100%", thickness=0.6,
                                         color=colors.HexColor("#cbd2dc")))
            self.story.append(Spacer(1, 7))
        elif tag == "br":
            target = self.cell_buffer if self.cell_buffer is not None else self.buffer
            if target is not None:
                target.append("<br/>")
        elif tag == "table":
            self.table_rows = []
        elif tag == "tr":
            self.table_row = []
        elif tag in ("td", "th"):
            self.cell_buffer = []
            self.cell_header = tag == "th"
        elif tag in self.INLINE_TAGS:
            target = self.cell_buffer if self.cell_buffer is not None else self.buffer
            if target is not None:
                target.append(f"<{self.INLINE_TAGS[tag]}>")
        elif tag == "a":
            target = self.cell_buffer if self.cell_buffer is not None else self.buffer
            href = attrs.get("href", "")
            try:
                scheme = urlsplit(href).scheme.lower()
            except ValueError:
                scheme = "invalid"
            if target is not None and scheme in ("", "http", "https", "mailto"):
                target.append(f'<link href="{escape(href, quote=True)}" color="#1456f0">')

    def handle_endtag(self, tag):
        if tag in self.INLINE_TAGS:
            target = self.cell_buffer if self.cell_buffer is not None else self.buffer
            if target is not None:
                target.append(f"</{self.INLINE_TAGS[tag]}>")
        elif tag == "a":
            target = self.cell_buffer if self.cell_buffer is not None else self.buffer
            if target is not None:
                target.append("</link>")
        elif tag == "pre":
            self._finish_block()
        elif tag in ("h1", "h2", "h3", "h4", "p", "li"):
            self._finish_block()
        elif tag == "blockquote":
            self._finish_block()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
        elif tag in ("td", "th") and self.cell_buffer is not None:
            cell_style = self.styles["PDFTableHeader" if self.cell_header else "PDFTableCell"]
            self.table_row.append(Paragraph("".join(self.cell_buffer), cell_style))
            self.cell_buffer = None
        elif tag == "tr" and self.table_row is not None:
            self.table_rows.append(self.table_row)
            self.table_row = None
        elif tag == "table" and self.table_rows:
            ncols = max(len(row) for row in self.table_rows)
            for row in self.table_rows:
                row.extend([Paragraph("", self.styles["PDFTableCell"])] * (ncols - len(row)))
            widths = [self.page_width / ncols] * ncols
            table = Table(self.table_rows, colWidths=widths, repeatRows=1,
                          hAlign="LEFT", splitByRow=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#202939")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d5dbe5")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            self.story.extend((table, Spacer(1, 9)))
            self.table_rows = None

    def handle_data(self, data):
        target = self.cell_buffer if self.cell_buffer is not None else self.buffer
        if target is not None:
            target.append(_reportlab_text(data))


def markdown_report_pdf(markdown, *, title="批改报告"):
    """将已保存报告的渲染 HTML 导出成含中文和表格的 PDF 字节。"""
    _register_cjk_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="PDFBody", parent=styles["Normal"], fontName="STSong-Light", fontSize=10,
        leading=16, textColor=colors.HexColor("#263244"), spaceAfter=6,
        alignment=TA_LEFT, wordWrap="CJK"))
    styles.add(ParagraphStyle(
        name="PDFTitle", parent=styles["Title"], fontName="STSong-Light", fontSize=19,
        leading=26, textColor=colors.HexColor("#172033"), spaceAfter=14,
        wordWrap="CJK"))
    styles.add(ParagraphStyle(
        name="PDFHeading", parent=styles["Heading2"], fontName="STSong-Light", fontSize=13,
        leading=19, textColor=colors.HexColor("#263f70"), spaceBefore=10,
        spaceAfter=6, wordWrap="CJK"))
    styles.add(ParagraphStyle(
        name="PDFQuote", parent=styles["PDFBody"], leftIndent=12,
        textColor=colors.HexColor("#86520a"), backColor=colors.HexColor("#fff8e8"),
        borderColor=colors.HexColor("#e7a535"), borderWidth=0.5, borderPadding=6))
    styles.add(ParagraphStyle(
        name="PDFList", parent=styles["PDFBody"], leftIndent=18, firstLineIndent=0,
        bulletIndent=4, spaceAfter=4))
    styles.add(ParagraphStyle(
        name="PDFTableCell", parent=styles["PDFBody"], fontSize=8.5, leading=12,
        spaceAfter=0, wordWrap="CJK"))
    styles.add(ParagraphStyle(
        name="PDFTableHeader", parent=styles["PDFTableCell"], fontName="STSong-Light",
        textColor=colors.HexColor("#202939")))
    styles.add(ParagraphStyle(
        name="PDFCode", parent=styles["PDFBody"], backColor=colors.HexColor("#f4f5f7"),
        leftIndent=8, rightIndent=8, borderPadding=6, spaceBefore=4, spaceAfter=8))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=42, leftMargin=42,
        topMargin=48, bottomMargin=44, title=title, author="GradeLogy")
    parser = _RenderedHTMLParser(styles, doc.width)
    parser.feed(render_markdown(markdown or "", server_mathml=False))
    parser.close()

    def page_chrome(canvas, document):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#7b8493"))
        canvas.drawString(document.leftMargin, 27, "GradeLogy · 批改报告")
        canvas.drawRightString(A4[0] - document.rightMargin, 27, f"第 {document.page} 页")
        canvas.setStrokeColor(colors.HexColor("#e1e5eb"))
        canvas.line(document.leftMargin, 38, A4[0] - document.rightMargin, 38)
        canvas.restoreState()

    doc.build(parser.story or [Paragraph("（报告内容为空）", styles["PDFBody"])],
              onFirstPage=page_chrome, onLaterPages=page_chrome)
    return buffer.getvalue()
