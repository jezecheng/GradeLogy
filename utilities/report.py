# -*- coding: utf-8 -*-
"""批改报告的 Markdown 组装与渲染。

build_report_md 负责逐题明细/失分归因/评价的排版，render_markdown 负责把极简
Markdown（标题/表格/列表/粗体）渲染成 HTML，避免引入额外依赖。
"""
import re
from html import escape, unescape
from urllib.parse import urlsplit

try:
    from latex2mathml.converter import convert as _latex_to_mathml
    _HAS_LATEX2MATHML = True
except ImportError:  # 未安装时回退到原始 $...$ 文本，由前端 KaTeX 渲染
    _latex_to_mathml = None
    _HAS_LATEX2MATHML = False

# 判定图标统一用彩色 emoji：⚠ 必须带变体选择符 U+FE0F，否则浏览器会渲染成灰白线框三角
VERDICT_ICON = {"对": "✅", "半对": "⚠️", "错": "❌", "无法判定": "❔"}


def _math_to_mathml(raw):
    """把 ``$...$`` 或 ``$$...$$`` 片段转成 MathML；失败时返回转义后的原文。

    服务端渲染，浏览器原生支持 MathML，不依赖任何 JS/CDN。
    """
    if not _HAS_LATEX2MATHML:
        return None
    is_block = raw.startswith("$$")
    inner = raw[2:-2] if is_block else raw[1:-1]
    try:
        return _latex_to_mathml(inner, display="block" if is_block else "inline")
    except Exception:
        return None


def _md_cell(value):
    """表格单元格里的竖线与换行会破坏 Markdown 表格，统一收敛。"""
    return str(value or "").replace("|", "｜").replace("\n", " ").strip()


def build_report_md(student_name, lecture_title, date_label, questions, summary, detail,
                    kg_diagnosis=None):
    """生成学生的 Markdown 批改报告：逐题明细 + 失分归因 + 评价 + KG 诊断。

    逐题明细的「批改说明」优先使用老师复核后的 note（人工评语），
    为空时回退到 AI 原始的 ai_comment。
    kg_diagnosis: 知识图谱诊断数据列表，存在时在报告末尾追加 KG 分析部分。
    """
    qs = [dict(q) for q in questions]
    n_total = len(qs)
    right = [q for q in qs if (q.get("verdict") or "") == "对"]
    partial = [q for q in qs if (q.get("verdict") or "") == "半对"]
    wrong = [q for q in qs if (q.get("verdict") or "") == "错"]
    unknown = [q for q in qs if (q.get("verdict") or "") == "无法判定"]
    skipped = [q for q in qs if "未作答" in (q.get("ai_comment") or "")]

    out = [f"# {student_name} · {date_label}作业《{lecture_title}》批改结果", "",
           f"## 一、逐题明细（{n_total} 个独立批改项）", "",
           "| 题号 | 题干/小问 | 判定 | 批改说明 |", "| --- | --- | :---: | --- |"]
    last_parent = None
    for q in qs:
        parent_no = str(q.get("parent_no") or "").strip()
        if parent_no and parent_no != last_parent:
            parent_stem = _md_cell(q.get("parent_stem"))
            out.append(f"| **第 {parent_no} 题** | {parent_stem} | — | 母题（各小问独立判定） |")
            last_parent = parent_no
        icon = VERDICT_ICON.get(q.get("verdict") or "", q.get("verdict") or "—")
        # 优先用老师复核后的评语（note），为空时回退到 AI 原始评语
        comment = (q.get("note") or "").strip() or (q.get("ai_comment") or "").strip()
        stem = _md_cell(q.get("stem"))
        out.append(f"| {_md_cell(q.get('no'))} | {stem} | {icon} | {_md_cell(comment)} |")

    def _nos(items, limit=6):
        names = [str(q.get("no")) for q in items]
        text = "、".join(f"第 {n} 题" for n in names[:limit])
        return text + ("等" if len(names) > limit else "")

    accuracy = round(len(right) * 100 / n_total) if n_total else 0
    stat = [f"正确率 {accuracy}%（完全答对 {len(right)}/{n_total} 个独立批改项）",
            f"统计：{VERDICT_ICON['对']} {len(right)}"]
    stat.append(f"{VERDICT_ICON['错']} {len(wrong)}" + (f"（{_nos(wrong)}）" if wrong else ""))
    stat.append(f"{VERDICT_ICON['半对']} {len(partial)}" + (f"（{_nos(partial)}）" if partial else ""))
    if unknown:
        stat.append(f"{VERDICT_ICON['无法判定']} {len(unknown)}（{_nos(unknown)}）")
    stat.append(f"未作答 {len(skipped)}")
    out += ["", " | ".join(stat), ""]

    weak = (summary or {}).get("weak_points") or []
    out += ["## 二、失分点归因", ""]
    if weak:
        out += ["| 题 | 类型 | 症结 |", "| --- | --- | --- |"]
        for w in weak:
            out.append(f"| {_md_cell(w.get('no'))} | {_md_cell(w.get('type'))} | "
                       f"{_md_cell(w.get('detail'))} |")
    else:
        out.append("本次没有失分点。" if not (wrong or partial) else "AI 未给出失分归类，请参考逐题说明。")

    out += ["", "## 三、评价", ""]
    good = (detail or {}).get("good") or (summary or {}).get("evaluation", "")
    if good:
        out += [good, ""]
    improve = (detail or {}).get("improve") or []
    if improve:
        out += ["需要改的几处：", ""]
        out += [f"{i}. {item}" for i, item in enumerate(improve, 1)]
        out.append("")
    nxt = (detail or {}).get("next") or ""
    if nxt:
        out += [f"**下一步建议：** {nxt}", ""]

    # 知识图谱诊断：以题目为行、关联知识点为列展示掌握状态。
    if kg_diagnosis:
        out += ["## 四、知识图谱诊断", "",
                "> 每行是一道题，按题目关联知识点显示状态；绿色✓表示判定正常，黄色!表示需要关注，红色表示可能存在问题。", ""]
        matrix = kg_diagnosis.get("matrix") if isinstance(kg_diagnosis, dict) else None
        if matrix and matrix.get("columns"):
            headers = ["题号", "题目简述"] + [c["name"] for c in matrix["columns"]] + ["知识点点评"]
            out.append("| " + " | ".join(_md_cell(x) for x in headers) + " |")
            out.append("| " + " | ".join(["---"] * len(headers)) + " |")
            matrix_rows = matrix.get("rows") or []
            columns = matrix["columns"]
            for row in matrix_rows:
                cells = [row.get("no", ""), row.get("brief", "")]
                cells.extend(row.get("cells", {}).get(c["tag"], "—") for c in columns)
                cells.append(row.get("analysis", ""))
                out.append("| " + " | ".join(_md_cell(x) for x in cells) + " |")
            out.append("")
        else:
            # 兼容旧格式的已保存/外部调用数据。
            for d in (kg_diagnosis if isinstance(kg_diagnosis, list) else []):
                p = d["perf"]
                out.append(f"### {d['tag']}（对{p['right']}/半对{p['partial']}/错{p['wrong']}）")
                if d.get("weak_prereqs"):
                    out.append("前置知识薄弱：" + "、".join(x["name"] for x in d["weak_prereqs"]))
                out.append("")
    return "\n".join(out).strip() + "\n"


def render_markdown(md, *, server_mathml=True):
    """把报告用的极简 Markdown（标题/表格/列表/粗体）渲染成 HTML。

    LaTeX 处理：行内 ``$...$`` 与块级 ``$$...$$`` 在转义阶段用占位符保护，
    最终由服务端 ``latex2mathml`` 转成 MathML 直接嵌入 HTML，浏览器原生渲染，
    不依赖任何前端 JS 或 CDN。若未安装 latex2mathml，则回退为原始 ``$...$``
    文本，交由前端 KaTeX 渲染。
    """
    # 1) 先把所有数学片段抽出来，用占位符替换，避免被转义/被加粗等正则误伤
    math_store = {}

    def _stash(m):
        key = f"\x00MATH{len(math_store)}\x00"
        math_store[key] = m.group(0)
        return key

    # 块级 $$...$$（允许跨行），必须在行内 $ 之前处理
    text = re.sub(r"\$\$[\s\S]+?\$\$", _stash, md or "")
    # 行内 $...$（不跨行、不含 $）
    text = re.sub(r"\$[^$\n]+\$", _stash, text)

    esc = lambda s: escape(str(s), quote=True)

    def link(match):
        label, url = match.group(1), unescape(match.group(2))
        # 链接属性和链接协议分别校验，阻止事件属性及 javascript/data 等 URL。
        try:
            parts = urlsplit(url)
        except ValueError:
            return label
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
            return label
        if parts.scheme.lower() not in ("http", "https", "mailto", ""):
            return label
        if not parts.scheme and (url.startswith("//") or "\\" in url):
            return label
        return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'

    def inline(text):
        s = esc(text)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return s

    def _restore(s):
        """把占位符换成渲染后的 MathML；失败则回退为转义后的原始数学文本。"""
        for key, raw in math_store.items():
            if key not in s:
                continue
            mathml = _math_to_mathml(raw) if server_mathml else None
            replacement = mathml if mathml is not None else esc(raw)
            s = s.replace(esc(key), replacement)
            s = s.replace(key, replacement)
        return s

    lines = text.splitlines()
    html, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # 跳过结束标记
            html.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            continue
        if line.startswith(">"):
            quotes = []
            while i < len(lines) and lines[i].startswith(">"):
                quotes.append(lines[i].lstrip(">").strip())
                i += 1
            html.append("<blockquote>"
                        + "".join(f"<p>{_restore(inline(x))}</p>" for x in quotes if x)
                        + "</blockquote>")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", line.strip()):
            html.append("<hr>")
            i += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            html.append(f"<h{min(level, 4)}>{_restore(inline(line[level:].strip()))}"
                        f"</h{min(level, 4)}>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            rows = []
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html.append("<table class='report-table'>")
            for r, cells in enumerate(rows):
                tag = "th" if r == 0 else "td"
                html.append("<tr>"
                            + "".join(f"<{tag}>{_restore(inline(c))}</{tag}>" for c in cells)
                            + "</tr>")
            html.append("</table>")
            continue
        if re.match(r"^\d+\.\s", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
                items.append(re.sub(r"^\d+\.\s", "", lines[i]))
                i += 1
            html.append("<ol>" + "".join(f"<li>{_restore(inline(x))}</li>" for x in items)
                        + "</ol>")
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:])
                i += 1
            html.append("<ul>" + "".join(f"<li>{_restore(inline(x))}</li>" for x in items)
                        + "</ul>")
            continue
        html.append(f"<p>{_restore(inline(line))}</p>")
        i += 1
    body = "\n".join(html)
    # 把「知识图谱诊断」整段包裹进 .kg-sourced，与普通报告区块视觉区分
    body = re.sub(
        r"(<h2>.*?知识图谱诊断.*?</h2>)",
        r'<div class="kg-sourced">\1',
        body, count=1)
    # 若打开了 kg-sourced，在下一个 h2 前补上闭合标签
    if '<div class="kg-sourced">' in body:
        idx = body.find('<div class="kg-sourced">')
        rest = body[body.find('</h2>', idx) + 5:]
        nxt = rest.find('<h2>')
        if nxt >= 0:
            body = body[:body.find('</h2>', idx) + 5 + nxt] + '</div>' + rest[nxt:]
        else:
            body += '</div>'
    return body
