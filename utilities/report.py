# -*- coding: utf-8 -*-
"""批改报告的 Markdown 组装与渲染。

build_report_md 负责逐题明细/失分归因/评价的排版，render_markdown 负责把极简
Markdown（标题/表格/列表/粗体）渲染成 HTML，避免引入额外依赖。
"""
import re

# 判定图标统一用彩色 emoji：⚠ 必须带变体选择符 U+FE0F，否则浏览器会渲染成灰白线框三角
VERDICT_ICON = {"对": "✅", "半对": "⚠️", "错": "❌", "无法判定": "❔"}


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
           f"## 一、逐题明细（{n_total} 题）", "",
           "| # | 题目 | 判定 | 批改说明 |", "| ---: | --- | :---: | --- |"]
    for i, q in enumerate(qs, 1):
        icon = VERDICT_ICON.get(q.get("verdict") or "", q.get("verdict") or "—")
        # 优先用老师复核后的评语（note），为空时回退到 AI 原始评语
        comment = (q.get("note") or "").strip() or (q.get("ai_comment") or "").strip()
        out.append(f"| {i} | {_md_cell(q.get('no'))} | {icon} | {_md_cell(comment)} |")

    def _nos(items, limit=6):
        names = [str(q.get("no")) for q in items]
        text = "、".join(f"第 {n} 题" for n in names[:limit])
        return text + ("等" if len(names) > limit else "")

    stat = [f"统计：{VERDICT_ICON['对']} {len(right)}"]
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

    # 知识图谱诊断：将 KG 的前置知识断层分析直接写入 MD 报告
    if kg_diagnosis:
        out += ["## 四、知识图谱诊断", "",
                "> 基于本课程挂载的知识图谱，分析薄弱知识点的关联概念与前置知识断层。", ""]
        for d in kg_diagnosis:
            p = d["perf"]
            out.append(f"### {d['tag']}")
            out.append("")
            # 用表格展示判定分布，更清晰
            out.append("| 判定 | 次数 |")
            out.append("| :---: | ---: |")
            if p["right"]:
                out.append(f"| ✓ 对 | {p['right']} |")
            if p["partial"]:
                out.append(f"| ◐ 半对 | {p['partial']} |")
            if p["wrong"]:
                out.append(f"| ✗ 错 | {p['wrong']} |")
            out.append("")
            if d.get("definition"):
                out.append(f"> **概念定义**：{d['definition']}")
                out.append("")
            if d.get("weak_prereqs"):
                out.append("**⚠ 前置知识断层**")
                out.append("")
                out.append(f"你在「{d['tag']}」上失分，而它的前置概念也掌握不牢，**建议优先补强这些前置概念**：")
                out.append("")
                for j, x in enumerate(d["weak_prereqs"], 1):
                    line = f"{j}. **{x['name']}**"
                    if x.get("perf"):
                        line += f"（对{x['perf']['right']}/半对{x['perf']['partial']}/错{x['perf']['wrong']}）"
                    if x.get("evidence"):
                        line += f" — {x['evidence']}"
                    out.append(line)
                out.append("")
            if d.get("untested_prereqs"):
                names = "、".join(x["name"] for x in d["untested_prereqs"])
                out.append(f"其他前置知识（本次未涉及，如不熟悉可一并复习）：{names}")
                out.append("")
            out.append("---")
            out.append("")
    return "\n".join(out).strip() + "\n"


def _overall_text(detail, fallback=""):
    """把 AI 返回的三段式评价拼成纯文本总评，供逐题核对页的文本框直接编辑。"""
    if not detail:
        return fallback or ""
    parts = []
    good = (detail.get("good") or "").strip()
    if good:
        parts.append(good)
    improve = [str(x).strip() for x in (detail.get("improve") or []) if str(x).strip()]
    if improve:
        parts.append("需要改的几处：\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(improve, 1)))
    nxt = (detail.get("next") or "").strip()
    if nxt:
        parts.append("下一步建议：" + nxt)
    return "\n\n".join(parts) or (fallback or "")


def render_markdown(md):
    """把报告用的极简 Markdown（标题/表格/列表/粗体）渲染成 HTML，避免引入额外依赖。"""
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def inline(text):
        s = esc(text)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    lines = (md or "").splitlines()
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
                        + "".join(f"<p>{inline(x)}</p>" for x in quotes if x)
                        + "</blockquote>")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", line.strip()):
            html.append("<hr>")
            i += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            html.append(f"<h{min(level, 4)}>{inline(line[level:].strip())}</h{min(level, 4)}>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            rows = []
            # 保存表头行（否则第一行数据会被误标为 <th>）
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html.append("<table class='report-table'>")
            for r, cells in enumerate(rows):
                tag = "th" if r == 0 else "td"
                html.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            html.append("</table>")
            continue
        if re.match(r"^\d+\.\s", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
                items.append(re.sub(r"^\d+\.\s", "", lines[i]))
                i += 1
            html.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:])
                i += 1
            html.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue
        html.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(html)
