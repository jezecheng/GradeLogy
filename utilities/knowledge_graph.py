# -*- coding: utf-8 -*-
"""学科知识图谱：资料抽取入库、AI 概念/关系抽取、增量合并与出处回收。

存储使用 SQLite 邻接表（kg_nodes / kg_edges），不依赖外部图数据库；
资料文件放在 uploads/kg/<graph_id>/，随现有备份包一起迁移。
"""
import json
import logging
import sqlite3

from utilities import kg_text, llm

log = logging.getLogger("gradelogy.kg")

STATUS_PENDING = "待建图"
STATUS_BUILDING = "建图中"
STATUS_DONE = "完成"
STATUS_FAILED = "失败"

RELATIONS = ("prerequisite", "contains", "related")
RELATION_LABEL = {"prerequisite": "前置", "contains": "包含", "related": "相关"}
# 推理模型会把大量 token 花在思考上，4096/8192 可能全部耗尽导致正文为空
KG_MAX_TOKENS = 32768
BATCH_CHARS = 3000
TRUNCATED_MARK = "模型输出被截断"

KG_BUILD_PROMPT = """你是资深学科教研员与知识图谱构建专家。请从下面的学科资料中抽取概念及其关系，用于构建学科知识图谱。

### 抽取要求
1. 只抽取资料中明确出现的概念、定理、方法、公式或典型迷思概念，不要凭空补充资料之外的内容。
2. 概念名称使用资料中的标准表述；常见同义写法放入 aliases。
3. 关系方向必须明确：
   - prerequisite：from 是学习 to 之前必须具备的前置知识；
   - contains：from 在知识结构上包含 to（上位概念 -> 下位概念）；
   - related：并列、互补或经常一起考查，但无先后依赖。
4. relation 只能取 prerequisite / contains / related；evidence 引用资料中的原句或关键短语作为依据。
5. 宁缺毋滥：资料信息不足时少给关系，不要编造。
6. 控制篇幅：单批最多抽取 25 个概念，每个定义不超过 40 字；关系最多 40 条，只保留有把握的内容。

### 输出格式（严格输出合法 JSON）
{
  "concepts": [
    {"name": "概念名称", "category": "模块/主题分类", "definition": "一句话定义（可留空）",
     "aliases": ["同义写法"], "confidence": 0.0}
  ],
  "relations": [
    {"from": "前置或上位概念", "to": "后续或下位概念",
     "relation": "prerequisite|contains|related", "evidence": "资料依据", "confidence": 0.0}
  ]
}
confidence 取 0~1，表示你对这条抽取结果的把握。"""


def _clamp(value, default=0.6):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def clean_concepts(items):
    result = []
    for item in items or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [str(a).strip() for a in aliases if str(a).strip()]
        result.append({
            "name": name[:120],
            "category": str(item.get("category") or "").strip()[:60],
            "definition": str(item.get("definition") or "").strip()[:300],
            "aliases": aliases[:8],
            "confidence": _clamp(item.get("confidence")),
        })
    return result


def clean_relations(items):
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("from") or "").strip()
        dst = str(item.get("to") or "").strip()
        if not src or not dst or src == dst:
            continue
        relation = str(item.get("relation") or "related").strip()
        if relation not in RELATIONS:
            relation = "related"
        result.append({
            "from": src[:120], "to": dst[:120], "relation": relation,
            "evidence": str(item.get("evidence") or "").strip()[:300],
            "confidence": _clamp(item.get("confidence")),
        })
    return result


def analyze_batch(texts, api_url, api_key, model, max_tokens=KG_MAX_TOKENS,
                  usage_hook=None):
    """让模型从一批资料文本里抽概念与关系，返回 (concepts, relations, error)。"""
    body = "\n\n--- 资料片段 ---\n\n".join(texts)
    prompt = KG_BUILD_PROMPT + "\n\n### 资料正文\n" + body
    try:
        reply, finish, err = llm.chat(
            api_url, api_key, model, [{"type": "text", "text": prompt}],
            max_tokens=max_tokens, usage_hook=usage_hook)
    except Exception as e:  # noqa: BLE001 构建失败记录到资料状态即可
        return [], [], str(e)
    if err:
        return [], [], err
    if finish == "length" or not (reply or "").strip():
        # 推理模型可能把预算全部花在 reasoning 上，正文为空
        return [], [], (f"{TRUNCATED_MARK}（max_tokens={max_tokens}），"
                        "可改用非推理模型或调大输出上限")
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return [], [], "模型未返回合法 JSON"
    return (clean_concepts(data.get("concepts")),
            clean_relations(data.get("relations")), "")


def _batches(rows, limit=BATCH_CHARS):
    batch, size = [], 0
    for row in rows:
        text = row["content"]
        if batch and size + len(text) > limit:
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


def _sources(value):
    return [int(part) for part in (value or "").split(",") if part.strip().isdigit()]


def _sources_text(ids):
    unique = sorted(set(ids))
    return "," + ",".join(str(i) for i in unique) + "," if unique else ""


def _add_source(value, material_id):
    ids = _sources(value)
    if material_id not in ids:
        ids.append(material_id)
    return _sources_text(ids)


def _remove_source(value, material_id):
    ids = [i for i in _sources(value) if i != material_id]
    return _sources_text(ids)


def _alias_map(db, graph_id):
    """把图中已有节点的标准名与别名都映射到节点行，便于增量合并。"""
    mapping = {}
    for row in db.execute(
            "SELECT * FROM kg_nodes WHERE graph_id=? ORDER BY id", (graph_id,)):
        for name in [row["name"]] + _json_list(row["aliases"]):
            key = kg_text.normalize_concept(name)
            if key and key not in mapping:
                mapping[key] = row
    return mapping


def _json_list(text):
    try:
        data = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _upsert_node(db, graph_id, concept, material_id, index):
    key = kg_text.normalize_concept(concept["name"])
    if not key:
        return None
    row = index.get(key)
    aliases = concept["aliases"] + [concept["name"]]
    created = False
    if row is None:
        try:
            cursor = db.execute(
                "INSERT INTO kg_nodes(graph_id,name,normalized,category,definition,"
                "aliases,source_materials,confidence) VALUES(?,?,?,?,?,?,?,?)",
                (graph_id, concept["name"], key, concept["category"],
                 concept["definition"], json.dumps(concept["aliases"], ensure_ascii=False),
                 _sources_text([material_id]), concept["confidence"]))
            row = db.execute("SELECT * FROM kg_nodes WHERE id=?",
                             (cursor.lastrowid,)).fetchone()
            created = True
        except sqlite3.IntegrityError:
            # 同一图谱可能有多份资料在后台并行建图，唯一键冲突后改为合并
            row = db.execute(
                "SELECT * FROM kg_nodes WHERE graph_id=? AND normalized=?",
                (graph_id, key)).fetchone()
    if row is None:
        return None
    if not created:
        merged = _json_list(row["aliases"])
        for alias in aliases:
            if alias and alias != row["name"] and alias not in merged:
                merged.append(alias)
        definition = row["definition"] or concept["definition"]
        db.execute(
            "UPDATE kg_nodes SET aliases=?, definition=?, category=?, "
            "source_materials=?, confidence=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), definition,
             row["category"] or concept["category"],
             _add_source(row["source_materials"], material_id),
             max(row["confidence"] or 0, concept["confidence"]), row["id"]))
        row = db.execute("SELECT * FROM kg_nodes WHERE id=?", (row["id"],)).fetchone()
    index[key] = row
    for alias in aliases:
        alias_key = kg_text.normalize_concept(alias)
        if alias_key:
            index.setdefault(alias_key, row)
    return row


def merge_extraction(db, graph_id, material_id, concepts, relations):
    """把一次抽取结果增量合并进图谱；返回 (节点总数, 关系总数, 新增节点, 新增关系)。"""
    index = _alias_map(db, graph_id)
    nodes_before = db.execute(
        "SELECT COUNT(*) FROM kg_nodes WHERE graph_id=?", (graph_id,)).fetchone()[0]
    edges_before = db.execute(
        "SELECT COUNT(*) FROM kg_edges WHERE graph_id=?", (graph_id,)).fetchone()[0]
    for concept in concepts:
        _upsert_node(db, graph_id, concept, material_id, index)
    for rel in relations:
        src = index.get(kg_text.normalize_concept(rel["from"]))
        dst = index.get(kg_text.normalize_concept(rel["to"]))
        if not src or not dst or src["id"] == dst["id"]:
            continue
        existed = db.execute(
            "SELECT * FROM kg_edges WHERE graph_id=? AND src_id=? AND dst_id=? "
            "AND relation=?", (graph_id, src["id"], dst["id"], rel["relation"])).fetchone()
        if existed:
            db.execute(
                "UPDATE kg_edges SET evidence=?, source_materials=?, confidence=? "
                "WHERE id=?",
                (existed["evidence"] or rel["evidence"],
                 _add_source(existed["source_materials"], material_id),
                 max(existed["confidence"] or 0, rel["confidence"]), existed["id"]))
        else:
            try:
                db.execute(
                    "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,evidence,"
                    "source_materials,confidence) VALUES(?,?,?,?,?,?,?)",
                    (graph_id, src["id"], dst["id"], rel["relation"], rel["evidence"],
                     _sources_text([material_id]), rel["confidence"]))
            except sqlite3.IntegrityError:
                # 并行建图时另一份资料已创建同一条边，改为合并出处即可
                existed = db.execute(
                    "SELECT * FROM kg_edges WHERE graph_id=? AND src_id=? AND dst_id=? "
                    "AND relation=?",
                    (graph_id, src["id"], dst["id"], rel["relation"])).fetchone()
                if existed:
                    db.execute(
                        "UPDATE kg_edges SET source_materials=? WHERE id=?",
                        (_add_source(existed["source_materials"], material_id),
                         existed["id"]))
    db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (graph_id,))
    db.commit()
    nodes_after = db.execute(
        "SELECT COUNT(*) FROM kg_nodes WHERE graph_id=?", (graph_id,)).fetchone()[0]
    edges_after = db.execute(
        "SELECT COUNT(*) FROM kg_edges WHERE graph_id=?", (graph_id,)).fetchone()[0]
    return nodes_after, edges_after, nodes_after - nodes_before, edges_after - edges_before


def extract_material(db, material, abs_path):
    """抽取资料文本并按页切块入库；返回 (块数, 是否因超长截断)。"""
    try:
        pages = kg_text.extract_pages(abs_path)
    except Exception as e:  # noqa: BLE001 解析失败要落状态，便于老师重传
        log.exception("知识图谱资料解析失败 material=%s", material["id"])
        db.execute("UPDATE kg_materials SET status=?, error=? WHERE id=?",
                   (STATUS_FAILED, f"解析失败：{e}", material["id"]))
        db.commit()
        return 0, False
    chunks = kg_text.chunk_pages(pages)
    total_chars = sum(len(text) for _page, text in chunks)
    kept, used = [], 0
    for page_no, text in chunks:
        if used >= kg_text.MAX_MATERIAL_CHARS:
            break
        kept.append((page_no, text))
        used += len(text)
    truncated = total_chars > kg_text.MAX_MATERIAL_CHARS
    db.execute("DELETE FROM kg_chunks WHERE material_id=?", (material["id"],))
    for number, (page_no, text) in enumerate(kept):
        db.execute(
            "INSERT INTO kg_chunks(graph_id,material_id,page_no,chunk_no,content)"
            " VALUES(?,?,?,?,?)",
            (material["graph_id"], material["id"], page_no, number, text))
    if not kept:
        db.execute("UPDATE kg_materials SET status=?, error=?, page_count=?, char_count=? "
                   "WHERE id=?",
                   (STATUS_FAILED, "未抽取到文本，可能是扫描版 PDF，可改用文字版资料",
                    len(pages), 0, material["id"]))
        db.commit()
        return 0, False
    db.execute("UPDATE kg_materials SET page_count=?, char_count=?, status=?, error=? "
               "WHERE id=?",
               (len(pages), min(total_chars, kg_text.MAX_MATERIAL_CHARS),
                STATUS_PENDING, "资料较长，仅使用前 12 万字构建" if truncated else "",
                material["id"]))
    db.commit()
    return len(kept), truncated


def build_material(db, material_id, api_url, api_key, model, usage_hook=None):
    """后台任务：读取资料切块 -> 分批 AI 抽取 -> 增量合并。"""
    material = db.execute("SELECT * FROM kg_materials WHERE id=?",
                          (material_id,)).fetchone()
    if not material:
        return None
    db.execute("UPDATE kg_materials SET status=?, error='' WHERE id=?",
               (STATUS_BUILDING, material_id))
    db.commit()
    rows = db.execute("SELECT content FROM kg_chunks WHERE material_id=? "
                      "ORDER BY chunk_no", (material_id,)).fetchall()
    if not rows:
        db.execute("UPDATE kg_materials SET status=?, error=? WHERE id=?",
                   (STATUS_FAILED, "没有可用的资料文本，请重新上传", material_id))
        db.commit()
        return None
    concepts, relations, errors = [], [], []

    def analyze_texts(texts):
        """截断时把批次对半拆开重试，避免推理模型一次输出过长直接失败。"""
        batch_concepts, batch_relations, error = analyze_batch(
            texts, api_url, api_key, model, usage_hook=usage_hook)
        if error.startswith(TRUNCATED_MARK) and len(texts) > 1:
            middle = len(texts) // 2
            left = analyze_texts(texts[:middle])
            right = analyze_texts(texts[middle:])
            return (left[0] + right[0], left[1] + right[1],
                    left[2] + right[2])
        return batch_concepts, batch_relations, ([error] if error else [])

    for texts in _batches(rows):
        batch_concepts, batch_relations, batch_errors = analyze_texts(texts)
        concepts.extend(batch_concepts)
        relations.extend(batch_relations)
        errors.extend(batch_errors)
    if not concepts:
        message = "；".join(errors[:2]) or "模型未抽取到任何概念"
        db.execute("UPDATE kg_materials SET status=?, error=? WHERE id=?",
                   (STATUS_FAILED, message[:500], material_id))
        db.commit()
        return None
    nodes, edges, new_nodes, new_edges = merge_extraction(
        db, material["graph_id"], material_id, concepts, relations)
    note = ""
    if errors:
        note = f"部分片段失败：{errors[0]}"
    db.execute("UPDATE kg_materials SET status=?, error=? WHERE id=?",
               (STATUS_DONE, note[:500], material_id))
    db.commit()
    return {"nodes": nodes, "edges": edges,
            "new_nodes": new_nodes, "new_edges": new_edges, "errors": len(errors)}


def delete_material(db, material):
    """删除资料及其文本，并回收仅由这份资料支撑的节点/边。"""
    material_id = material["id"]
    graph_id = material["graph_id"]
    db.execute("DELETE FROM kg_chunks WHERE material_id=?", (material_id,))
    for edge in db.execute("SELECT * FROM kg_edges WHERE graph_id=?",
                           (graph_id,)).fetchall():
        if material_id not in _sources(edge["source_materials"]):
            continue
        left = _remove_source(edge["source_materials"], material_id)
        if left:
            db.execute("UPDATE kg_edges SET source_materials=? WHERE id=?",
                       (left, edge["id"]))
        else:
            db.execute("DELETE FROM kg_edges WHERE id=?", (edge["id"],))
    for node in db.execute("SELECT * FROM kg_nodes WHERE graph_id=?",
                           (graph_id,)).fetchall():
        if material_id not in _sources(node["source_materials"]):
            continue
        left = _remove_source(node["source_materials"], material_id)
        if left:
            db.execute("UPDATE kg_nodes SET source_materials=? WHERE id=?",
                       (left, node["id"]))
        else:
            db.execute("DELETE FROM kg_edges WHERE src_id=? OR dst_id=?",
                       (node["id"], node["id"]))
            db.execute("DELETE FROM kg_nodes WHERE id=?", (node["id"],))
    db.execute("DELETE FROM kg_materials WHERE id=?", (material_id,))
    db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (graph_id,))
    db.commit()


def graph_counts(db, graph_id):
    def count(table):
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE graph_id=?",
                          (graph_id,)).fetchone()[0]
    return {"materials": count("kg_materials"), "nodes": count("kg_nodes"),
            "edges": count("kg_edges"), "chunks": count("kg_chunks")}


def list_nodes(db, graph_id, limit=200):
    return db.execute(
        "SELECT * FROM kg_nodes WHERE graph_id=? ORDER BY confidence DESC, id LIMIT ?",
        (graph_id, limit)).fetchall()


def list_edges(db, graph_id, limit=200):
    return db.execute(
        "SELECT e.*, s.name AS src_name, d.name AS dst_name FROM kg_edges e "
        "JOIN kg_nodes s ON s.id=e.src_id JOIN kg_nodes d ON d.id=e.dst_id "
        "WHERE e.graph_id=? ORDER BY e.confidence DESC, e.id LIMIT ?",
        (graph_id, limit)).fetchall()


NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 150, 44, 26, 78, 28
NODE_PALETTE = (("#eef2ff", "#c7d2fe"), ("#ecfdf5", "#a7f3d0"),
                ("#fff7ed", "#fed7aa"), ("#fdf2f8", "#fbcfe8"),
                ("#f0f9ff", "#bae6fd"), ("#fefce8", "#fde68a"))


def _node_color(category):
    if not category:
        return NODE_PALETTE[0]
    return NODE_PALETTE[sum(ord(ch) for ch in category) % len(NODE_PALETTE)]


def layered_graph(db, graph_id, limit=80):
    """按 prerequisite 关系做分层布局，供页面用纯 SVG 渲染。

    layer 越大表示越靠后的衍生知识；成环的节点单独放在最后一层并标记。
    """
    rows = db.execute(
        "SELECT id,name,category,confidence FROM kg_nodes WHERE graph_id=? "
        "ORDER BY id", (graph_id,)).fetchall()
    edge_rows = db.execute(
        "SELECT src_id,dst_id,relation,evidence,confidence FROM kg_edges "
        "WHERE graph_id=?", (graph_id,)).fetchall()
    total_nodes = len(rows)
    prereq = [e for e in edge_rows if e["relation"] == "prerequisite"]
    degree = {}
    for edge in prereq:
        degree[edge["src_id"]] = degree.get(edge["src_id"], 0) + 1
        degree[edge["dst_id"]] = degree.get(edge["dst_id"], 0) + 1
    if len(rows) > limit:
        rows = sorted(rows, key=lambda n: (-degree.get(n["id"], 0), n["id"]))[:limit]
    keep = {n["id"] for n in rows}
    visible_edges = [e for e in edge_rows
                     if e["src_id"] in keep and e["dst_id"] in keep]
    visible_prereq = [e for e in prereq
                      if e["src_id"] in keep and e["dst_id"] in keep]

    outgoing, indegree = {}, {}
    for node_id in keep:
        outgoing[node_id], indegree[node_id] = [], 0
    for edge in visible_prereq:
        outgoing[edge["src_id"]].append(edge["dst_id"])
        indegree[edge["dst_id"]] += 1

    layer = {node_id: 0 for node_id in keep}
    queue = sorted(node_id for node_id, value in indegree.items() if value == 0)
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for target in outgoing[current]:
            layer[target] = max(layer[target], layer[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    has_cycle = visited < len(keep)
    if has_cycle:
        # 成环部分不参与拓扑分层，统一放到末尾，避免无限回溯
        cycle_layer = max(layer.values(), default=0) + 1
        for node_id, value in indegree.items():
            if value > 0:
                layer[node_id] = cycle_layer

    grouped = {}
    for row in rows:
        grouped.setdefault(layer[row["id"]], []).append(row)
    for items in grouped.values():
        items.sort(key=lambda n: (n["category"] or "", n["name"]))
    max_layer = max(grouped, default=0)
    max_count = max((len(items) for items in grouped.values()), default=0)
    width = max(PAD * 2 + max_count * (NODE_W + GAP_X) - GAP_X, 320)
    height = PAD * 2 + (max_layer + 1) * (NODE_H + GAP_Y) - GAP_Y

    placed = {}
    for level in sorted(grouped):
        items = grouped[level]
        row_width = len(items) * (NODE_W + GAP_X) - GAP_X
        start_x = (width - row_width) / 2
        for index, row in enumerate(items):
            fill, stroke = _node_color(row["category"])
            placed[row["id"]] = {
                "id": row["id"], "name": row["name"],
                "category": row["category"] or "",
                "x": round(start_x + index * (NODE_W + GAP_X), 1),
                "y": round(PAD + level * (NODE_H + GAP_Y), 1),
                "w": NODE_W, "h": NODE_H,
                "cx": round(start_x + index * (NODE_W + GAP_X) + NODE_W / 2, 1),
                "cy": round(PAD + level * (NODE_H + GAP_Y) + NODE_H / 2, 1),
                "fill": fill, "stroke": stroke,
            }

    paths = []
    for edge in visible_edges:
        source, target = placed[edge["src_id"]], placed[edge["dst_id"]]
        if source["y"] == target["y"]:
            path = (f"M {source['cx']} {source['cy']} "
                    f"C {source['cx']} {source['cy'] - 34}, "
                    f"{target['cx']} {target['cy'] - 34}, "
                    f"{target['cx']} {target['cy']}")
        else:
            bend = max(24, abs(target["y"] - source["y"]) / 3)
            path = (f"M {source['cx']} {source['y'] + NODE_H} "
                    f"C {source['cx']} {source['y'] + NODE_H + bend}, "
                    f"{target['cx']} {target['y'] - bend}, "
                    f"{target['cx']} {target['y']}")
        paths.append({"path": path, "relation": edge["relation"],
                      "evidence": edge["evidence"] or ""})
    return {
        "nodes": [placed[row["id"]] for row in rows],
        "edges": paths,
        "width": round(width), "height": round(height),
        "layers": max_layer + 1,
        "truncated": total_nodes > limit,
        "hidden_nodes": max(0, total_nodes - limit),
        "has_cycle": has_cycle,
    }


def concept_neighbors(db, graph_id, node_id):
    """概念本身与它直接相邻的前置、延伸、包含/相关节点。"""
    node = db.execute("SELECT * FROM kg_nodes WHERE id=? AND graph_id=?",
                      (node_id, graph_id)).fetchone()
    if not node:
        return None
    rows = db.execute(
        "SELECT e.*, s.name AS src_name, d.name AS dst_name FROM kg_edges e "
        "JOIN kg_nodes s ON s.id=e.src_id JOIN kg_nodes d ON d.id=e.dst_id "
        "WHERE e.graph_id=? AND (e.src_id=? OR e.dst_id=?) "
        "ORDER BY e.confidence DESC, e.id", (graph_id, node_id, node_id)).fetchall()
    prerequisites, dependents, others = [], [], []
    for edge in rows:
        incoming = edge["dst_id"] == node_id
        item = {"id": edge["src_id"] if incoming else edge["dst_id"],
                "name": edge["src_name"] if incoming else edge["dst_name"],
                "evidence": edge["evidence"] or "", "confidence": edge["confidence"],
                "relation": edge["relation"]}
        if edge["relation"] == "prerequisite":
            (prerequisites if incoming else dependents).append(item)
        else:
            item["direction"] = "←" if incoming else "→"
            others.append(item)
    return {"node": node, "prerequisites": prerequisites,
            "dependents": dependents, "others": others}

