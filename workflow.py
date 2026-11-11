# -*- coding: utf-8 -*-
"""流程执行器：把设计器导出的节点图按拓扑顺序跑成批改结果。

graph 规范格式（设计器保存的就是它）：
    {"nodes": [{"id": "1", "type": "detect", "data": {...}}, ...],
     "connections": [{"from": "1", "to": "2"}, ...]}
也兼容 Drawflow 原生 export 的 {"drawflow": {"Home": {"data": {...}}}}。
"""
from utilities import detect as detect_mod
from utilities import grade as grade_mod
from utilities import match as match_mod
from utilities import storage

# 内置默认流程：detect → match → grade，等价于原 call_ai_segment 的三步流水线
DEFAULT_PIPELINE = {
    "nodes": [
        {"id": "1", "type": "detect", "data": {"batch": 4}},
        {"id": "2", "type": "match", "data": {"window": 400}},
        {"id": "3", "type": "grade", "data": {"max_tokens": 6000}},
    ],
    "connections": [
        {"from": "1", "to": "2"},
        {"from": "2", "to": "3"},
    ],
}


def normalize_graph(graph):
    """把 Drawflow 原生 export 转成 {nodes, connections} 规范格式；已是规范格式则原样返回。"""
    if not isinstance(graph, dict):
        return {"nodes": [], "connections": []}
    if "nodes" in graph:
        return graph
    data = ((graph.get("drawflow") or {}).get("Home") or {}).get("data") or {}
    nodes, connections = [], []
    for nid, n in data.items():
        nodes.append({"id": str(nid), "type": n.get("name", ""), "data": n.get("data") or {}})
        for out in (n.get("outputs") or {}).values():
            for c in out.get("connections") or []:
                connections.append({"from": str(nid), "to": str(c.get("node"))})
    return {"nodes": nodes, "connections": connections}


def topo_sort(nodes, connections):
    """Kahn 拓扑排序。返回 (有序节点 id 列表, 错误信息)；环或引用不存在节点时给出明确错误。"""
    ids = [str(n.get("id")) for n in nodes]
    if len(set(ids)) != len(ids):
        return [], "存在重复的节点 id"
    idset = set(ids)
    for c in connections:
        src, dst = str(c.get("from")), str(c.get("to"))
        if src not in idset or dst not in idset:
            return [], f"连线引用了不存在的节点：{src} → {dst}"
        if src == dst:
            return [], f"节点 {src} 不能连接自己"
    indeg = {i: 0 for i in ids}
    adj = {i: [] for i in ids}
    for c in connections:
        adj[str(c["from"])].append(str(c["to"]))
        indeg[str(c["to"])] += 1
    queue = [i for i in ids if indeg[i] == 0]
    ordered = []
    while queue:
        nid = queue.pop(0)
        ordered.append(nid)
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(ids):
        return [], "流程存在环，无法确定执行顺序"
    return ordered, ""


def _as_num(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _build_summary(questions, errors):
    """总分/满分/对错分布由后端按题汇总（与 app.py 的 _build_summary 同口径）。"""
    return {
        "total": sum(_as_num(q.get("score")) for q in questions),
        "max_total": sum(_as_num(q.get("max_score")) for q in questions),
        "right": [q["no"] for q in questions if q.get("verdict") == "对"],
        "partial": [q["no"] for q in questions if q.get("verdict") == "半对"],
        "wrong": [q["no"] for q in questions if q.get("verdict") == "错"],
        "weak_points": [],
        "evaluation": "",
        "errors": errors,
    }


def _run_detect(state, cfg, params, errors):
    if not state.get("pages"):
        errors.append("detect 节点缺少输入 pages")
        return
    questions, errs = detect_mod.detect_questions(
        state["pages"], cfg["api_url"], cfg["api_key"], cfg["model"],
        params.get("extra") or cfg.get("extra", ""),
        batch=int(params.get("batch") or 4))
    state["questions"] = questions
    errors.extend(errs)
    if not questions:
        errors.append("detect 节点未识别到任何题目")


def _run_match(state, _cfg, params, errors):
    questions = state.get("questions") or []
    if not questions:
        errors.append("match 节点缺少输入 questions")
        return
    window = int(params.get("window") or 400)
    answer_text = state.get("answer_text") or ""
    answer_key = state.get("answer_key") or {}  # 预处理缓存的标准答案（prepare 节点产物）
    answers = {}
    for q in questions:
        ans = answer_key.get(q.get("no")) or match_mod.match_answer(q.get("no"), answer_text, window=window)
        q["standard_answer"] = ans
        answers[q.get("no")] = ans
    state["answers"] = answers


def _run_grade(state, cfg, params, errors):
    questions = state.get("questions") or []
    if not questions:
        errors.append("grade 节点缺少输入 questions")
        return
    pages = state.get("pages") or []
    answers = state.get("answers") or {}
    # 附件组织：裁剪图存到 attachments/<课程>/<讲次>/，命名 {学生}_q_{题号}.jpg，重跑覆盖
    attach_dir = state.get("attach_dir") or ""
    student = state.get("student") or ""
    # glm-5v-turbo 等思考模型的 reasoning 会吃掉 token 配额，1500 时输出为空，至少给 6000
    max_tokens = int(params.get("max_tokens") or 6000)
    extra = params.get("extra") or cfg.get("extra", "")
    graded = []
    for i, q in enumerate(questions, 1):
        pno = q.get("page") or 1
        page = pages[pno - 1] if 1 <= pno <= len(pages) else (pages[0] if pages else "")
        no = q.get("no") or str(i)
        stem = storage.question_name(student, no)[:-4] if student else f"q{i}"
        img = detect_mod.crop_bbox(page, q.get("bbox"), stem, subdir=attach_dir) or page
        ans = answers.get(q.get("no"), q.get("standard_answer", ""))
        result = grade_mod.grade_question(
            img, ans, cfg["api_url"], cfg["api_key"], cfg["model"],
            extra=extra, max_tokens=max_tokens)
        if result.get("error"):
            errors.append(f"第 {q.get('no')} 题批改失败：{result['error']}")
        graded.append({
            "no": q.get("no"), "page": pno, "bbox": q.get("bbox"), "image_path": img,
            "ocr": result.get("ocr", ""),
            "standard_answer": result.get("standard_answer") or ans,
            "max_score": _as_num(result.get("max_score")),
            "score": result.get("score"),
            "verdict": result.get("verdict", ""),
            "comment": result.get("comment", ""),
        })
    state["graded"] = graded


NODE_RUNNERS = {"detect": _run_detect, "match": _run_match, "grade": _run_grade}


def run_pipeline(graph, state, cfg):
    """按拓扑顺序执行流程图。

    state: {"pages": [整页图片文件名...], "answer_text": "...", "sid": .., "file_path": ..}
    cfg:   {"api_url", "api_key", "model", "extra"}
    返回 {"questions", "pages", "summary", "errors", "error"}，结构与 call_ai_segment 一致。
    """
    graph = normalize_graph(graph)
    nodes = graph.get("nodes") or []
    ordered, err = topo_sort(nodes, graph.get("connections") or [])
    if err:
        return {"questions": [], "pages": state.get("pages") or [], "summary": {},
                "errors": [], "error": f"流程不合法：{err}"}
    if not nodes:
        return {"questions": [], "pages": state.get("pages") or [], "summary": {},
                "errors": [], "error": "流程为空，请先在「流程设计」里添加节点。"}

    nmap = {str(n.get("id")): n for n in nodes}
    errors = []
    for nid in ordered:
        node = nmap[nid]
        runner = NODE_RUNNERS.get(node.get("type"))
        if not runner:
            errors.append(f"未知节点类型：{node.get('type')}（节点 {nid}）")
            continue
        runner(state, cfg, node.get("data") or {}, errors)

    questions = state.get("graded") or state.get("questions") or []
    if not questions and not errors:
        errors.append("流程执行完毕但没有产出任何题目")
    return {"questions": questions, "pages": state.get("pages") or [],
            "summary": _build_summary(questions, errors), "errors": errors, "error": ""}
