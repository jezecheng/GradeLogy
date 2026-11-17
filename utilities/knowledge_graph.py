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

CHUNK_PENDING = "pending"
CHUNK_RUNNING = "running"
CHUNK_SUCCESS = "success"
CHUNK_FAILED = "failed"

RELATIONS = ("prerequisite", "contains", "related")
RELATION_LABEL = {"prerequisite": "前置", "contains": "包含", "related": "相关"}

AUDIT_PENDING = "待确认"
AUDIT_APPLIED = "已应用"
AUDIT_DISCARDED = "已忽略"
AUDIT_FAILED = "失败"
AUDIT_CLEAN = "无需调整"
AUDIT_ROLLED_BACK = "已回滚"
AUDIT_ACTIONS = ("merge_nodes", "reverse_edge", "delete_edge", "drop_node")
AUDIT_ACTION_LABEL = {
    "merge_nodes": "合并同义概念",
    "reverse_edge": "反向前置关系",
    "delete_edge": "删除关系",
    "drop_node": "删除孤立概念",
}
AUDIT_MAX_TOKENS = 32768
AUDIT_MAX_ITEMS = 40
# 推理模型会把大量 token 花在思考上，4096/8192 可能全部耗尽导致正文为空
KG_MAX_TOKENS = 32768
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
            max_tokens=max_tokens, usage_hook=usage_hook, report_running=True)
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


def prerequisite_reachable(db, graph_id, start_id, target_id,
                           exclude_edge_id=None):
    """沿 prerequisite 边判断 start 能否到达 target，用于插入/反向前的防环校验。"""
    if start_id == target_id:
        return True
    exclude = ""
    params = [graph_id, start_id]
    extra = []
    if exclude_edge_id is not None:
        exclude = " AND id<>?"
        extra.append(exclude_edge_id)
    recursive_exclude = ""
    if exclude_edge_id is not None:
        recursive_exclude = " AND e.id<>?"
    sql = (
        "WITH RECURSIVE reach(id) AS ("
        " SELECT dst_id FROM kg_edges WHERE graph_id=? AND relation='prerequisite'"
        " AND src_id=?" + exclude +
        " UNION"
        " SELECT e.dst_id FROM kg_edges e JOIN reach r ON e.src_id=r.id"
        " WHERE e.graph_id=? AND e.relation='prerequisite'" + recursive_exclude +
        ") SELECT 1 FROM reach WHERE id=? LIMIT 1")
    params.extend(extra)
    params.append(graph_id)
    if exclude_edge_id is not None:
        params.append(exclude_edge_id)
    params.append(target_id)
    return db.execute(sql, tuple(params)).fetchone() is not None


def _strong_components(adjacency):
    """Tarjan 强连通分量（迭代实现，避免大图触发递归深度限制）。"""
    index, low, on_stack, stack, result = {}, {}, set(), [], []
    counter = 0
    nodes = set(adjacency)
    for targets in adjacency.values():
        nodes.update(targets)
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            targets = adjacency.get(node, ())
            advanced = False
            for offset in range(position, len(targets)):
                target = targets[offset]
                if target not in index:
                    work.append((node, offset + 1))
                    work.append((target, 0))
                    advanced = True
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            if advanced:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    item = stack.pop()
                    on_stack.discard(item)
                    component.append(item)
                    if item == node:
                        break
                result.append(component)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result


def prerequisite_cycles(db, graph_id):
    """找出现存前置子图中的环，并给出优先检查的低置信关系。"""
    rows = db.execute(
        "SELECT e.id,e.src_id,e.dst_id,e.confidence,e.evidence,"
        " s.name AS src_name,d.name AS dst_name FROM kg_edges e"
        " JOIN kg_nodes s ON s.id=e.src_id JOIN kg_nodes d ON d.id=e.dst_id"
        " WHERE e.graph_id=? AND e.relation='prerequisite'"
        " ORDER BY e.id", (graph_id,)).fetchall()
    adjacency = {}
    for row in rows:
        adjacency.setdefault(row["src_id"], []).append(row["dst_id"])
    reports = []
    for component in _strong_components(adjacency):
        members = set(component)
        if len(members) < 2:
            continue
        inner = [row for row in rows
                 if row["src_id"] in members and row["dst_id"] in members]
        if not inner:
            continue
        inner.sort(key=lambda row: ((row["confidence"] or 0), -row["id"]))
        weakest = inner[0]
        reports.append({
            "node_names": sorted({row["src_name"] for row in inner} |
                                 {row["dst_name"] for row in inner}),
            "edges": [{
                "id": row["id"], "src_name": row["src_name"],
                "dst_name": row["dst_name"], "confidence": row["confidence"] or 0,
                "evidence": row["evidence"] or "",
            } for row in inner],
            "suggested_edge_id": weakest["id"],
            "suggested_label": (
                f"{weakest['src_name']} → {weakest['dst_name']}"
                f"（置信度 {round((weakest['confidence'] or 0) * 100)}%）"),
        })
    return reports


def merge_extraction(db, graph_id, material_id, concepts, relations, stats=None):
    """把一次抽取结果增量合并进图谱；返回 (节点总数, 关系总数, 新增节点, 新增关系)。

    prerequisite 边写入前先检查是否会与已有前置关系成环，成环则跳过；
    stats 传入 dict 时可读取 skipped_cycles 统计。
    """
    index = _alias_map(db, graph_id)
    skipped_cycles = 0
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
            if rel["relation"] == "prerequisite" and prerequisite_reachable(
                    db, graph_id, dst["id"], src["id"]):
                # 已有 dst -> ... -> src 的路径，再插入 src -> dst 会成环
                skipped_cycles += 1
                continue
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
    if stats is not None:
        stats["skipped_cycles"] = stats.get("skipped_cycles", 0) + skipped_cycles
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


def build_material(db, material_id, api_url, api_key, model, usage_hook=None,
                   max_tokens=KG_MAX_TOKENS):
    """逐块持久化抽取结果，重启后续建；最后统一合图以保留跨块关系。"""
    material = db.execute("SELECT * FROM kg_materials WHERE id=?",
                          (material_id,)).fetchone()
    if not material:
        return None
    db.execute("UPDATE kg_materials SET status=?, error='', "
               "started_at=COALESCE(started_at,CURRENT_TIMESTAMP),finished_at=NULL WHERE id=?",
               (STATUS_BUILDING, material_id))
    db.commit()
    rows = db.execute("SELECT * FROM kg_chunks WHERE material_id=? "
                      "ORDER BY chunk_no", (material_id,)).fetchall()
    if not rows:
        db.execute("UPDATE kg_materials SET status=?, error=?, "
                   "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                   (STATUS_FAILED, "没有可用的资料文本，请重新上传", material_id))
        db.commit()
        return None

    for row in rows:
        if row["status"] == CHUNK_SUCCESS and row["extraction_json"]:
            continue

        def chunk_usage(event):
            event["chunk_id"] = row["id"]
            return usage_hook(event) if usage_hook else None

        def analyze_texts(texts):
            """截断时对半拆批；单块则拆正文，避免推理模型一次输出过长。"""
            batch_concepts, batch_relations, error = analyze_batch(
                texts, api_url, api_key, model, max_tokens=max_tokens,
                usage_hook=chunk_usage)
            if error.startswith(TRUNCATED_MARK):
                if len(texts) > 1:
                    left_texts, right_texts = texts[:len(texts) // 2], texts[len(texts) // 2:]
                elif len(texts[0]) > 1000:
                    text = texts[0]
                    middle = len(text) // 2
                    split = max(text.rfind("\n", 0, middle), text.rfind("。", 0, middle))
                    split = split + 1 if split >= middle // 2 else middle
                    left_texts, right_texts = [text[:split]], [text[split:]]
                else:
                    return batch_concepts, batch_relations, [error]
                left = analyze_texts(left_texts)
                right = analyze_texts(right_texts)
                return (left[0] + right[0], left[1] + right[1], left[2] + right[2])
            return batch_concepts, batch_relations, ([error] if error else [])

        db.execute("UPDATE kg_chunks SET status=?,error='',started_at=CURRENT_TIMESTAMP,"
                   "finished_at=NULL WHERE id=?", (CHUNK_RUNNING, row["id"]))
        db.commit()
        chunk_concepts, chunk_relations, chunk_errors = analyze_texts([row["content"]])
        payload = json.dumps({"concepts": chunk_concepts, "relations": chunk_relations},
                             ensure_ascii=False)
        db.execute("UPDATE kg_chunks SET status=?,error=?,extraction_json=?,"
                   "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                   (CHUNK_FAILED if chunk_errors else CHUNK_SUCCESS,
                    "；".join(chunk_errors)[:500], payload, row["id"]))
        db.commit()

    concepts, relations, errors = [], [], []
    saved_rows = db.execute("SELECT status,error,extraction_json FROM kg_chunks "
                            "WHERE material_id=? ORDER BY chunk_no", (material_id,)).fetchall()
    for row in saved_rows:
        if row["extraction_json"]:
            data = json.loads(row["extraction_json"])
            concepts.extend(data.get("concepts") or [])
            relations.extend(data.get("relations") or [])
        if row["status"] == CHUNK_FAILED:
            errors.append(row["error"] or "未知错误")
    if not concepts:
        message = "；".join(errors[:2]) or "模型未抽取到任何概念"
        db.execute("UPDATE kg_materials SET status=?, error=?,"
                   "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                   (STATUS_FAILED, message[:500], material_id))
        db.commit()
        return None
    stats = {}
    nodes, edges, new_nodes, new_edges = merge_extraction(
        db, material["graph_id"], material_id, concepts, relations, stats=stats)
    notes = []
    if errors:
        notes.append(f"部分片段失败：{errors[0]}")
    skipped = stats.get("skipped_cycles", 0)
    if skipped:
        notes.append(f"已跳过 {skipped} 条会成环的前置关系")
    note = "；".join(notes)
    db.execute("UPDATE kg_materials SET status=?, error=?,"
               "finished_at=CURRENT_TIMESTAMP WHERE id=?",
               (STATUS_DONE, note[:500], material_id))
    db.commit()
    return {"nodes": nodes, "edges": edges,
            "new_nodes": new_nodes, "new_edges": new_edges, "errors": len(errors),
            "skipped_cycles": skipped}


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


def graph_payload(db, graph_id):
    """返回全量图谱数据，供前端 ECharts 力导向「知识星系」渲染。

    节点带上连接度 degree 作为星球大小权重，分类 category 作为星团分组。
    """
    rows = db.execute(
        "SELECT id,name,category,definition,confidence FROM kg_nodes "
        "WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
    edge_rows = db.execute(
        "SELECT id,src_id,dst_id,relation,evidence,confidence FROM kg_edges "
        "WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
    degree = {row["id"]: 0 for row in rows}
    for edge in edge_rows:
        if edge["src_id"] in degree:
            degree[edge["src_id"]] += 1
        if edge["dst_id"] in degree:
            degree[edge["dst_id"]] += 1
    category_counts = {}
    for row in rows:
        name = row["category"] or "未分类"
        category_counts[name] = category_counts.get(name, 0) + 1
    categories = [{"name": name, "count": count} for name, count in
                  sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))]
    return {
        "nodes": [{
            "id": row["id"], "name": row["name"],
            "category": row["category"] or "未分类",
            "definition": row["definition"] or "",
            "confidence": row["confidence"] or 0,
            "degree": degree[row["id"]],
        } for row in rows],
        "edges": [{
            "id": edge["id"], "src_id": edge["src_id"], "dst_id": edge["dst_id"],
            "relation": edge["relation"], "evidence": edge["evidence"] or "",
            "confidence": edge["confidence"] or 0,
        } for edge in edge_rows],
        "categories": categories,
        "stats": {"nodes": len(rows), "edges": len(edge_rows),
                  "categories": len(categories)},
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


AUDIT_PROMPT = """你是学科知识图谱的审校专家。请检查下面的概念与关系，指出确实需要修正的问题。

### 可提出的建议（action 只能取这四种）
1. merge_nodes：把明显同义或重复的概念合并。给出 node_ids（至少两个节点 ID）与 keep_id（保留哪个节点）。
2. reverse_edge：前置关系方向反了。给出 edge_id。
3. delete_edge：关系错误、重复或资料中没有依据。给出 edge_id。
4. drop_node：删除没有任何关系、也没有独立价值的孤立概念。给出 node_id。

### 约束
- 只在证据充分时提建议，宁缺毋滥；相邻但不同的概念不要合并。
- 不要提出会让前置关系成环的反向建议；系统会逐条校验，不合规的建议会被丢弃。
- 每条建议都要给出简短的中文理由（reason），不超过 60 字。
- 最多给出 20 条建议；没有明显问题时返回空数组。

### 输出格式（严格输出合法 JSON）
{"suggestions": [
  {"action": "merge_nodes", "node_ids": [1, 2], "keep_id": 1, "reason": "两者是同一概念的不同写法"},
  {"action": "reverse_edge", "edge_id": 12, "reason": "定义说明 A 是学习 B 之前的基础"},
  {"action": "delete_edge", "edge_id": 15, "reason": "资料中没有该关系依据"},
  {"action": "drop_node", "node_id": 9, "reason": "孤立节点且无定义"}
]}
"""


def _audit_catalog(db, graph_id, node_limit=150, edge_limit=250):
    """把图压缩成便于模型判断的编号清单，节点与关系都带 ID。"""
    nodes = db.execute(
        "SELECT id,name,category,definition,confidence FROM kg_nodes WHERE graph_id=? "
        "ORDER BY confidence DESC, id LIMIT ?", (graph_id, node_limit)).fetchall()
    edges = db.execute(
        "SELECT id,src_id,dst_id,relation,evidence,confidence FROM kg_edges "
        "WHERE graph_id=? ORDER BY confidence ASC, id LIMIT ?",
        (graph_id, edge_limit)).fetchall()
    lines = ["### 概念节点"]
    for row in nodes:
        parts = [f"#{row['id']} {row['name']}"]
        if row["category"]:
            parts.append(f"分类：{row['category']}")
        parts.append(f"置信度 {round((row['confidence'] or 0) * 100)}%")
        if row["definition"]:
            parts.append(f"定义：{row['definition']}")
        lines.append("｜".join(parts))
    lines.append("")
    lines.append("### 关系（E 开头是关系 ID，# 是节点 ID）")
    for row in edges:
        label = RELATION_LABEL.get(row["relation"], row["relation"])
        line = (f"E{row['id']}: #{row['src_id']} --{label}--> #{row['dst_id']}"
                f"｜置信度 {round((row['confidence'] or 0) * 100)}%")
        if row["evidence"]:
            line += f"｜依据：{row['evidence']}"
        lines.append(line)
    return "\n".join(lines), len(nodes)


def analyze_graph(db, graph_id, api_url, api_key, model, usage_hook=None,
                  max_tokens=AUDIT_MAX_TOKENS):
    """让模型审校图谱，返回 (可用建议, 被校验拦截的建议, 错误)。"""
    catalog, node_count = _audit_catalog(db, graph_id)
    if not node_count:
        return [], [], "图谱还没有概念，无需体检"
    cycles = prerequisite_cycles(db, graph_id)
    hint = ""
    if cycles:
        hint = "\n\n### 系统检测到的前置环\n" + "\n".join(
            "- " + " / ".join(item["node_names"]) + "；建议优先检查："
            + item["suggested_label"] for item in cycles)
    try:
        reply, finish, err = llm.chat(
            api_url, api_key, model,
            [{"type": "text", "text": AUDIT_PROMPT + "\n\n" + catalog + hint}],
            max_tokens=max_tokens, usage_hook=usage_hook, report_running=True)
    except Exception as e:  # noqa: BLE001 体检失败不影响图谱本身
        return [], [], str(e)
    if err:
        return [], [], err
    if finish == "length" or not (reply or "").strip():
        return [], [], (f"{TRUNCATED_MARK}（max_tokens={max_tokens}），"
                        "可改用非推理模型或调大输出上限")
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return [], [], "模型未返回合法 JSON"
    return validate_audit_suggestions(db, graph_id, data.get("suggestions"))


def _audit_state():
    return {"deleted": set(), "reversed": set(), "merged": [], "dropped": set()}


def _copy_state(state):
    return {"deleted": set(state["deleted"]), "reversed": set(state["reversed"]),
            "merged": list(state["merged"]), "dropped": set(state["dropped"])}


def _prereq_components(edges, state):
    """返回 (find, 成环节点映射)：节点按合并组折叠后，落在非平凡强连通分量的节点。"""
    parent = {}

    def find(node):
        while parent.get(node, node) != node:
            parent[node] = parent.get(parent[node], parent[node])
            node = parent[node]
        return node

    for ids, keep_id in state["merged"]:
        for node in ids:
            root, keep_root = find(node), find(keep_id)
            if root != keep_root:
                parent[root] = keep_root
    adjacency = {}
    for edge_id, edge in edges.items():
        if edge_id in state["deleted"] or edge["relation"] != "prerequisite":
            continue
        src, dst = edge["src_id"], edge["dst_id"]
        if edge_id in state["reversed"]:
            src, dst = dst, src
        if src in state["dropped"] or dst in state["dropped"]:
            continue
        src, dst = find(src), find(dst)
        if src == dst:
            continue
        adjacency.setdefault(src, []).append(dst)
    cyclic = {}
    for index, component in enumerate(_strong_components(adjacency)):
        if len(component) < 2:
            continue
        for node in component:
            cyclic[node] = index
    return find, cyclic


def _introduces_cycle(edges, state, candidate, endpoints):
    """候选改动是否在前置子图中制造了新的环。"""
    base_find, base_cyclic = _prereq_components(edges, state)
    new_find, new_cyclic = _prereq_components(edges, candidate)
    base = base_cyclic.get(base_find(endpoints[0]))
    if base is not None and base == base_cyclic.get(base_find(endpoints[1])):
        return False  # 改动前这两个节点已在同一个环上，不新增
    new = new_cyclic.get(new_find(endpoints[0]))
    return new is not None and new == new_cyclic.get(new_find(endpoints[1]))


def _edge_label(edge, nodes):
    label = RELATION_LABEL.get(edge["relation"], edge["relation"])
    src = nodes.get(edge["src_id"])
    dst = nodes.get(edge["dst_id"])
    return f"{src['name'] if src else edge['src_id']} —{label}→ " \
           f"{dst['name'] if dst else edge['dst_id']}"


def _as_ids(value):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            return []
        if number not in result:
            result.append(number)
    return result


def validate_audit_suggestions(db, graph_id, items):
    """逐条校验模型建议：ID 必须属于本图、合并组不冲突、改动后前置关系不成环。

    返回 (可用建议, 被拦截建议, 错误)；错误恒为空串，保持与调用方一致的返回形态。
    """
    nodes = {row["id"]: row for row in db.execute(
        "SELECT id,name,category,definition,confidence FROM kg_nodes WHERE graph_id=?",
        (graph_id,))}
    edges = {row["id"]: row for row in db.execute(
        "SELECT * FROM kg_edges WHERE graph_id=?", (graph_id,))}
    valid, rejected = [], []
    state = _audit_state()
    for raw in (items or [])[:AUDIT_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip()
        reason = str(raw.get("reason") or "").strip()[:120]
        if action not in AUDIT_ACTIONS:
            rejected.append({"action": action or "未知", "text": "无法识别的建议",
                             "reason": "action 不在允许范围内"})
            continue

        if action == "merge_nodes":
            ids = _as_ids(raw.get("node_ids"))
            try:
                keep_id = int(raw.get("keep_id") or (ids[0] if ids else 0))
            except (TypeError, ValueError):
                keep_id = 0
            if len(ids) < 2:
                rejected.append({"action": action, "text": "合并概念",
                                 "reason": "node_ids 至少需要两个有效节点"})
                continue
            missing = [node for node in ids if node not in nodes]
            if missing:
                rejected.append({"action": action, "text": "合并概念",
                                 "reason": f"节点 {missing} 不属于该图谱"})
                continue
            if keep_id not in ids:
                rejected.append({"action": action, "text": "合并概念",
                                 "reason": "keep_id 必须包含在 node_ids 中"})
                continue
            used = {node for group, _keep in state["merged"] for node in group}
            if used & set(ids) or state["dropped"] & set(ids):
                rejected.append({"action": action, "text": "合并概念",
                                 "reason": "同一概念出现在多条合并或删除建议中"})
                continue
            candidate = _copy_state(state)
            candidate["merged"].append((ids, keep_id))
            base_find, base_cyclic = _prereq_components(edges, state)
            find, cyclic = _prereq_components(edges, candidate)
            if find(keep_id) in cyclic and base_find(keep_id) not in base_cyclic:
                rejected.append({"action": action, "text": "合并概念",
                                 "reason": "合并后前置关系会成环"})
                continue
            state = candidate
            names = [nodes[node]["name"] for node in ids]
            valid.append({
                "action": action, "node_ids": ids, "keep_id": keep_id,
                "reason": reason,
                "label": "合并「" + "」「".join(names) + f"」→「{nodes[keep_id]['name']}」",
            })
            continue

        if action in ("reverse_edge", "delete_edge"):
            try:
                edge_id = int(raw.get("edge_id"))
            except (TypeError, ValueError):
                edge_id = 0
            edge = edges.get(edge_id)
            if not edge:
                rejected.append({"action": action, "text": "关系调整",
                                 "reason": f"关系 {edge_id or '?'} 不属于该图谱"})
                continue
            if action == "reverse_edge" and edge["relation"] != "prerequisite":
                rejected.append({"action": action, "text": _edge_label(edge, nodes),
                                 "reason": "只有前置关系可以反向"})
                continue
            if edge_id in state["deleted"] or edge_id in state["reversed"]:
                rejected.append({"action": action, "text": _edge_label(edge, nodes),
                                 "reason": "同一关系出现在多条建议中"})
                continue
            candidate = _copy_state(state)
            if action == "delete_edge":
                candidate["deleted"].add(edge_id)
                state = candidate
                valid.append({"action": action, "edge_id": edge_id, "reason": reason,
                              "label": "删除「" + _edge_label(edge, nodes) + "」"})
                continue
            candidate["reversed"].add(edge_id)
            if _introduces_cycle(edges, state, candidate,
                                 (edge["src_id"], edge["dst_id"])):
                rejected.append({"action": action, "text": _edge_label(edge, nodes),
                                 "reason": "反向会与现有前置关系形成环"})
                continue
            state = candidate
            src, dst = nodes.get(edge["src_id"]), nodes.get(edge["dst_id"])
            src_name = src["name"] if src else edge["src_id"]
            dst_name = dst["name"] if dst else edge["dst_id"]
            valid.append({
                "action": action, "edge_id": edge_id, "reason": reason,
                "label": f"把「{_edge_label(edge, nodes)}」反转为「{dst_name} → {src_name}」",
            })
            continue

        # drop_node：只允许清理与其他节点完全无关的孤立概念
        try:
            node_id = int(raw.get("node_id"))
        except (TypeError, ValueError):
            node_id = 0
        node = nodes.get(node_id)
        if not node:
            rejected.append({"action": action, "text": "删除孤立概念",
                             "reason": f"节点 {node_id or '?'} 不属于该图谱"})
            continue
        if state["dropped"] & {node_id} or any(
                node_id in group for group, _keep in state["merged"]):
            rejected.append({"action": action, "text": f"删除「{node['name']}」",
                             "reason": "同一概念出现在多条建议中"})
            continue
        related = [edge_id for edge_id, edge in edges.items()
                   if edge_id not in state["deleted"]
                   and node_id in (edge["src_id"], edge["dst_id"])]
        if related:
            rejected.append({"action": action, "text": f"删除「{node['name']}」",
                             "reason": f"该概念还有 {len(related)} 条关系，请先删除关系"})
            continue
        state["dropped"].add(node_id)
        valid.append({"action": action, "node_id": node_id, "reason": reason,
                      "label": f"删除孤立概念「{node['name']}」"})
    return valid, rejected, ""


def snapshot_graph(db, graph_id):
    """保存当前节点与关系，供体检应用后回滚。"""
    nodes = [dict(row) for row in db.execute(
        "SELECT id,name,normalized,category,definition,aliases,source_materials,"
        "confidence,created_at,updated_at FROM kg_nodes WHERE graph_id=? ORDER BY id",
        (graph_id,))]
    edges = [dict(row) for row in db.execute(
        "SELECT id,src_id,dst_id,relation,evidence,source_materials,confidence,"
        "created_at FROM kg_edges WHERE graph_id=? ORDER BY id", (graph_id,))]
    return {"nodes": nodes, "edges": edges}


def _merge_nodes(db, graph_id, node_ids, keep_id):
    """把一批节点合并到 keep 节点，关系重挂并去重。"""
    placeholders = ",".join("?" * len(node_ids))
    rows = db.execute(
        f"SELECT * FROM kg_nodes WHERE graph_id=? AND id IN ({placeholders})",
        (graph_id, *node_ids)).fetchall()
    keep = next((row for row in rows if row["id"] == keep_id), None)
    others = [row for row in rows if row["id"] != keep_id]
    if not keep or not others:
        return
    aliases = _json_list(keep["aliases"])
    sources = _sources(keep["source_materials"])
    definition = keep["definition"] or ""
    category = keep["category"] or ""
    confidence = keep["confidence"] or 0
    for other in others:
        for name in [other["name"]] + _json_list(other["aliases"]):
            if name and name != keep["name"] and name not in aliases:
                aliases.append(name)
        sources.extend(_sources(other["source_materials"]))
        definition = definition or other["definition"] or ""
        category = category or other["category"] or ""
        confidence = max(confidence, other["confidence"] or 0)
        for edge in db.execute(
                "SELECT * FROM kg_edges WHERE graph_id=? AND (src_id=? OR dst_id=?)",
                (graph_id, other["id"], other["id"])).fetchall():
            src = keep_id if edge["src_id"] == other["id"] else edge["src_id"]
            dst = keep_id if edge["dst_id"] == other["id"] else edge["dst_id"]
            if src == dst:
                db.execute("DELETE FROM kg_edges WHERE id=?", (edge["id"],))
                continue
            duplicate = db.execute(
                "SELECT * FROM kg_edges WHERE graph_id=? AND src_id=? AND dst_id=? "
                "AND relation=? AND id<>?",
                (graph_id, src, dst, edge["relation"], edge["id"])).fetchone()
            if duplicate:
                merged_sources = sorted(set(
                    _sources(duplicate["source_materials"])
                    + _sources(edge["source_materials"])))
                db.execute(
                    "UPDATE kg_edges SET source_materials=?, evidence=?, confidence=? "
                    "WHERE id=?",
                    (_sources_text(merged_sources),
                     duplicate["evidence"] or edge["evidence"],
                     max(duplicate["confidence"] or 0, edge["confidence"] or 0),
                     duplicate["id"]))
                db.execute("DELETE FROM kg_edges WHERE id=?", (edge["id"],))
            else:
                db.execute("UPDATE kg_edges SET src_id=?, dst_id=? WHERE id=?",
                           (src, dst, edge["id"]))
        db.execute("DELETE FROM kg_nodes WHERE id=?", (other["id"],))
    db.execute(
        "UPDATE kg_nodes SET aliases=?, definition=?, category=?, source_materials=?,"
        " confidence=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(aliases, ensure_ascii=False), definition, category,
         _sources_text(sources), confidence, keep_id))


def reverse_edge(db, graph_id, edge_id):
    """反向前置关系；若反向边已存在则合并去重。返回是否存在可反向的边。"""
    edge = db.execute("SELECT * FROM kg_edges WHERE id=? AND graph_id=?",
                      (edge_id, graph_id)).fetchone()
    if not edge:
        return False
    mirror = db.execute(
        "SELECT * FROM kg_edges WHERE graph_id=? AND src_id=? AND dst_id=? "
        "AND relation=? AND id<>?",
        (graph_id, edge["dst_id"], edge["src_id"], edge["relation"],
         edge["id"])).fetchone()
    if mirror:
        merged_sources = sorted(set(_sources(mirror["source_materials"])
                                    + _sources(edge["source_materials"])))
        db.execute(
            "UPDATE kg_edges SET source_materials=?, evidence=?, confidence=? WHERE id=?",
            (_sources_text(merged_sources), mirror["evidence"] or edge["evidence"],
             max(mirror["confidence"] or 0, edge["confidence"] or 0), mirror["id"]))
        db.execute("DELETE FROM kg_edges WHERE id=?", (edge["id"],))
    else:
        db.execute("UPDATE kg_edges SET src_id=?, dst_id=? WHERE id=?",
                   (edge["dst_id"], edge["src_id"], edge["id"]))
    return True


def apply_audit(db, graph_id, suggestions):
    """按顺序应用已校验的建议，返回应用前快照。"""
    snapshot = snapshot_graph(db, graph_id)
    for item in suggestions or []:
        action = item.get("action")
        if action == "merge_nodes":
            _merge_nodes(db, graph_id, item.get("node_ids") or [],
                         item.get("keep_id"))
        elif action == "reverse_edge":
            reverse_edge(db, graph_id, item.get("edge_id"))
        elif action == "delete_edge":
            db.execute("DELETE FROM kg_edges WHERE id=? AND graph_id=?",
                       (item.get("edge_id"), graph_id))
        elif action == "drop_node":
            db.execute("DELETE FROM kg_nodes WHERE id=? AND graph_id=?",
                       (item.get("node_id"), graph_id))
    db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (graph_id,))
    db.commit()
    return snapshot


def restore_snapshot(db, graph_id, snapshot):
    """回滚到快照：重建该图的节点与关系（保留原 ID）。"""
    db.execute("DELETE FROM kg_edges WHERE graph_id=?", (graph_id,))
    db.execute("DELETE FROM kg_nodes WHERE graph_id=?", (graph_id,))
    for node in snapshot.get("nodes") or []:
        db.execute(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized,category,definition,"
            "aliases,source_materials,confidence,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (node["id"], graph_id, node["name"], node["normalized"],
             node.get("category") or "", node.get("definition") or "",
             node.get("aliases") or "", node.get("source_materials") or "",
             node.get("confidence") or 0, node.get("created_at"),
             node.get("updated_at")))
    for edge in snapshot.get("edges") or []:
        db.execute(
            "INSERT INTO kg_edges(id,graph_id,src_id,dst_id,relation,evidence,"
            "source_materials,confidence,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (edge["id"], graph_id, edge["src_id"], edge["dst_id"], edge["relation"],
             edge.get("evidence") or "", edge.get("source_materials") or "",
             edge.get("confidence") or 0, edge.get("created_at")))
    db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (graph_id,))
    db.commit()

