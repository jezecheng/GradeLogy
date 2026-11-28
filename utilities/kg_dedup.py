# -*- coding: utf-8 -*-
"""知识图谱智能去重：跨分类 + 共享邻居结构 + 别名冲突三路召回疑似同义概念，
并发分批送 LLM 判定合并簇，复用 knowledge_graph 的校验/快照/回滚纯图函数。

设计要点（对齐 kg_dedup_archive_plan.md 阶段二）：
- 候选召回先用倒排索引把可能相似的节点对捞出，避免 O(n²) 全图两两比较；
- 候选组之间互不依赖，按 KG_DEDUP_CONCURRENCY 并发判定，单组失败只记录、不中断整体；
- 逐组状态写入 kg_dedup_groups，运行进度写入 kg_dedup_runs.progress，界面可实时展示；
- 已完成候选组持久化判定结果，失败/中断后可从断点续跑，不重复调用模型；
- 模型只输出合并簇（merge_nodes），不自动改图；汇总后整体交
  kg_utils.validate_audit_suggestions 校验（ID 归属 / 冲突 / 前置防环），
  应用时扁平化为 merge_nodes 走同一套 apply_audit + snapshot + restore。
"""
import hashlib
import json
import logging
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext

import config
from utilities import kg_text, llm
from utilities import knowledge_graph as kg_utils
from utilities.time_utils import beijing_wall_now

log = logging.getLogger("gradelogy.kg_dedup")

# 状态常量复用体检取值（仅做语义别名），保证 UI / 路由与体检卡一致。
DEDUP_PENDING = kg_utils.AUDIT_PENDING          # 待确认
DEDUP_APPLIED = kg_utils.AUDIT_APPLIED          # 已应用
DEDUP_DISCARDED = kg_utils.AUDIT_DISCARDED      # 已忽略
DEDUP_FAILED = kg_utils.AUDIT_FAILED            # 失败
DEDUP_CLEAN = kg_utils.AUDIT_CLEAN              # 无需调整
DEDUP_ROLLED_BACK = kg_utils.AUDIT_ROLLED_BACK  # 已回滚

# 去重运行态：进行中 / 服务重启中断。中断后可从未完成候选组继续。
DEDUP_RUNNING = "进行中"
DEDUP_INTERRUPTED = "已中断"
DEDUP_RESUMABLE = (DEDUP_FAILED, DEDUP_INTERRUPTED)

# 候选组逐组状态（与 kg_dedup_groups.status 一致）。
GROUP_PENDING = "pending"
GROUP_RUNNING = "running"
GROUP_DONE = "done"
GROUP_FAILED = "failed"
GROUP_LABEL = {
    GROUP_PENDING: "待处理",
    GROUP_RUNNING: "判定中",
    GROUP_DONE: "已完成",
    GROUP_FAILED: "失败",
}

DEDUP_GROUP_MAX = 40            # 单批送模型的候选节点上限
DEDUP_APPLY_BATCH = 40          # 单次应用（以及界面每页）的合并簇上限
DEDUP_NEIGHBOR_JACCARD = 0.5    # 共享邻居结构召回的 Jaccard 阈值
DEDUP_MAX_TOKENS = 16384
DEDUP_ACTION_LABEL = {"merge_nodes": "合并概念"}

DEDUP_PROMPT = """你是学科知识图谱治理专家。下面是一组被系统判定为「可能重复」的候选概念，
附带每个概念的邻居结构（前置←、延伸→、包含、相关）。请判断其中哪些确实是同一概念的不同写法，
输出需要合并的簇。

### 要求
1. 只在证据充分时合并：名称/别名高度一致，或定义、邻居结构指向同一概念；相邻但不同的概念不要合并。
2. 每个合并簇给出 node_ids（至少两个节点 ID）与 keep_id（保留哪个节点，建议保留置信度最高或名称最规范者）。
3. reason 用不超过 60 字的中文说明合并依据；confidence 取 0~1 表示把握。
4. 没有明确重复时返回空数组，不要勉强合并。
5. 严格输出合法 JSON，不要输出 JSON 之外的解释文字。

### 输出格式（严格输出合法 JSON）
{"groups":[{"node_ids":[1,2],"keep_id":1,"reason":"两者是同一概念的不同写法","confidence":0.92}]}
"""

_TOKEN_SPLIT = re.compile(r"[\W_]+", re.UNICODE)
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _name_tokens(text):
    """名称/定义的 token 集合：按非词字符切分 + CJK 连续段取二元组，
    用于召回阶段的重叠判定（二元组提升部分重叠的召回，如「氧化还原」与「氧化还原反应」）。"""
    text = (text or "").casefold()
    tokens = set()
    for part in _TOKEN_SPLIT.split(text):
        part = part.strip()
        if not part:
            continue
        tokens.add(part)
        for run in _CJK_RUN.findall(part):
            if len(run) >= 2:
                for i in range(len(run) - 1):
                    tokens.add(run[i:i + 2])
    return tokens


class _UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent.get(self.parent[x], self.parent[x])
            x = self.parent[x]
        return x

    def union(self, a, b):
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_a] = root_b

    def groups(self):
        buckets = {}
        for item in self.parent:
            buckets.setdefault(self.find(item), []).append(item)
        return list(buckets.values())


def _node_context(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "aliases": kg_utils._json_list(row["aliases"]),
        "category": row["category"] or "",
        "definition": row["definition"] or "",
        "confidence": row["confidence"] or 0,
        "normalized": row["normalized"] or "",
    }


def _load_graph_context(db, graph_id):
    """加载全图节点上下文与邻居结构，供候选召回和断点续跑复用。"""
    node_rows = db.execute(
        "SELECT id,name,normalized,category,definition,aliases,confidence "
        "FROM kg_nodes WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
    nodes = {row["id"]: _node_context(row) for row in node_rows}
    if not nodes:
        return nodes
    name_by_id = {nid: ctx["name"] for nid, ctx in nodes.items()}
    edge_rows = db.execute(
        "SELECT src_id,dst_id,relation FROM kg_edges WHERE graph_id=?",
        (graph_id,)).fetchall()
    neighbors = {nid: {"prerequisites": [], "dependents": [],
                       "contains": [], "related": []} for nid in nodes}
    neighbor_set = {nid: set() for nid in nodes}
    for edge in edge_rows:
        src, dst, relation = edge["src_id"], edge["dst_id"], edge["relation"]
        if src not in nodes or dst not in nodes:
            continue
        if relation == "prerequisite":
            # src 是 dst 的前置：dst 的前置含 src，src 的延伸含 dst
            neighbors[dst]["prerequisites"].append(
                {"id": src, "name": name_by_id[src]})
            neighbors[src]["dependents"].append(
                {"id": dst, "name": name_by_id[dst]})
        elif relation == "contains":
            neighbors[src]["contains"].append({"id": dst, "name": name_by_id[dst]})
            neighbors[dst]["contains"].append({"id": src, "name": name_by_id[src]})
        elif relation == "related":
            neighbors[src]["related"].append({"id": dst, "name": name_by_id[dst]})
            neighbors[dst]["related"].append({"id": src, "name": name_by_id[src]})
        neighbor_set[src].add(dst)
        neighbor_set[dst].add(src)
    for nid, ctx in nodes.items():
        ctx["neighbors"] = neighbors[nid]
        ctx["neighbor_ids"] = neighbor_set[nid]
    return nodes


def build_dedup_candidates(db, graph_id):
    """三路召回候选组，避免 O(n²)：别名/名称归一冲突、共享邻居结构（跨分类）、
    同分类名称 token 重叠。候选对经并查集连成组，单组 ≤ DEDUP_GROUP_MAX。

    返回 list[list[node_context]]，node_context 含 id/name/aliases/category/
    definition/confidence 及按 relation 分组的邻居结构。
    """
    nodes = _load_graph_context(db, graph_id)
    if len(nodes) < 2:
        return []

    neighbor_set = {nid: ctx["neighbor_ids"] for nid, ctx in nodes.items()}
    pairs = set()  # (a, b) 且 a < b

    # (a) 别名/名称归一冲突（强信号）：normalized 列本身有唯一约束，
    # 真正的冲突来自「A 的别名归一化等于 B 的名称归一化」或两节点共享别名。
    by_norm = {}
    for nid, ctx in nodes.items():
        key = ctx["normalized"] or kg_text.normalize_concept(ctx["name"])
        if key:
            by_norm.setdefault(key, []).append(nid)
        for alias in ctx["aliases"]:
            alias_key = kg_text.normalize_concept(alias)
            if alias_key:
                by_norm.setdefault(alias_key, []).append(nid)
    for members in by_norm.values():
        uniq = sorted(set(members))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pairs.add((uniq[i], uniq[j]))

    # (b) 共享邻居结构（中信号，跨分类生效）：倒排「邻居 → 节点」，
    # 共享 ≥1 邻居的节点对进入候选，再算邻居集合 Jaccard ≥ 阈值且名称/定义 token 有交集。
    neighbor_index = {}
    for nid, nbrs in neighbor_set.items():
        for nbr in nbrs:
            neighbor_index.setdefault(nbr, set()).add(nid)
    for members in neighbor_index.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if (a, b) in pairs:
                    continue
                set_a, set_b = neighbor_set[a], neighbor_set[b]
                if not set_a or not set_b:
                    continue
                union = len(set_a | set_b)
                if union == 0:
                    continue
                if len(set_a & set_b) / union < DEDUP_NEIGHBOR_JACCARD:
                    continue
                tokens_a = (_name_tokens(nodes[a]["name"])
                            | _name_tokens(nodes[a]["definition"]))
                tokens_b = (_name_tokens(nodes[b]["name"])
                            | _name_tokens(nodes[b]["definition"]))
                if tokens_a & tokens_b:
                    pairs.add((a, b))

    # (c) 同 category 内名称 token 重叠聚类。
    cat_token_index = {}
    for nid, ctx in nodes.items():
        category = ctx["category"] or "未分类"
        for token in _name_tokens(ctx["name"]):
            cat_token_index.setdefault((category, token), set()).add(nid)
    for members in cat_token_index.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add((members[i], members[j]))

    if not pairs:
        return []

    uf = _UnionFind(list(nodes.keys()))
    for a, b in pairs:
        uf.union(a, b)

    result = []
    for group_ids in uf.groups():
        if len(group_ids) < 2:
            continue
        ordered = sorted(group_ids, key=lambda i: (nodes[i]["name"], i))
        # 超大组按名称排序后切块，控制单组送模型规模（不追求 100% 召回）。
        if len(ordered) > DEDUP_GROUP_MAX:
            for start in range(0, len(ordered), DEDUP_GROUP_MAX):
                chunk = ordered[start:start + DEDUP_GROUP_MAX]
                if len(chunk) >= 2:
                    result.append([nodes[i] for i in chunk])
        else:
            result.append([nodes[i] for i in ordered])
    return result


def _format_group_context(group):
    lines = ["### 候选概念组（以下概念可能存在重复，请只合并确实同义的）"]
    for ctx in group:
        parts = [f"#{ctx['id']} {ctx['name']}"]
        if ctx["aliases"]:
            parts.append("别名：" + "、".join(ctx["aliases"]))
        if ctx["category"]:
            parts.append(f"分类：{ctx['category']}")
        parts.append(f"置信度 {round((ctx['confidence'] or 0) * 100)}%")
        if ctx["definition"]:
            parts.append(f"定义：{ctx['definition']}")
        nbr = ctx.get("neighbors") or {}
        nbr_parts = []
        for key, label in (("prerequisites", "前置←"), ("dependents", "延伸→"),
                           ("contains", "包含"), ("related", "相关")):
            items = nbr.get(key) or []
            if items:
                nbr_parts.append(
                    label + "：" + "、".join(f"#{i['id']}{i['name']}" for i in items))
        if nbr_parts:
            parts.append("｜".join(nbr_parts))
        lines.append("｜".join(parts))
    return "\n".join(lines)


def _coerce_ids(value):
    ids = []
    for item in value or []:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    deduped = []
    for i in ids:
        if i not in deduped:
            deduped.append(i)
    return deduped


def analyze_dedup_group(group, api_url, api_key, model, max_tokens, usage_hook=None):
    """判定单个候选组的合并簇，返回 (groups, error)。

    groups 中每项形如 {node_ids, keep_id, reason, confidence}；模型无输出或失败时 groups 为空。
    """
    context = _format_group_context(group)
    try:
        reply, finish, err = llm.chat(
            api_url, api_key, model,
            [{"type": "text", "text": DEDUP_PROMPT + "\n\n" + context}],
            max_tokens=max_tokens, usage_hook=usage_hook, report_running=True,
            stage="kg_dedup")
    except Exception as e:  # noqa: BLE001 单组失败由调用方记录，不中断整体
        return [], str(e)
    if err:
        return [], err
    if finish == "length" or not (reply or "").strip():
        return [], (f"{kg_utils.TRUNCATED_MARK}（max_tokens={max_tokens}），"
                    "可改用非推理模型或调大输出上限")
    data = llm.extract_json(reply)
    if not isinstance(data, dict):
        return [], "模型未返回合法 JSON"
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        return [], "模型返回的 groups 不是数组"
    groups = []
    for item in raw_groups:
        if not isinstance(item, dict):
            continue
        ids = _coerce_ids(item.get("node_ids"))
        if len(ids) < 2:
            continue
        ids_set = set(ids)
        try:
            keep_id = int(item.get("keep_id") or ids[0])
        except (TypeError, ValueError):
            keep_id = ids[0]
        if keep_id not in ids_set:
            keep_id = ids[0]
        groups.append({
            "node_ids": ids,
            "keep_id": keep_id,
            "reason": str(item.get("reason") or "").strip()[:120],
            "confidence": kg_utils._clamp(item.get("confidence"), 0.5),
        })
    return groups, ""


def _enrich_groups(clusters, name_by_id):
    """给簇补上 member_names / keep_name，便于可视化与扁平化。"""
    for cluster in clusters:
        ids = cluster["node_ids"]
        cluster["member_names"] = [name_by_id[i] for i in ids if i in name_by_id]
        cluster["keep_name"] = name_by_id.get(cluster["keep_id"], "")
    return clusters


def flatten_dedup_suggestions(valid_groups):
    """把可视化簇结构扁平化为 apply_audit 可消费的 merge_nodes 建议列表。"""
    result = []
    for group in valid_groups or []:
        if not isinstance(group, dict):
            continue
        suggestion = group.get("suggestion")
        if isinstance(suggestion, dict) and suggestion.get("node_ids"):
            result.append({
                "action": "merge_nodes",
                "node_ids": list(suggestion["node_ids"]),
                "keep_id": suggestion.get("keep_id"),
                "reason": suggestion.get("reason") or "",
            })
            continue
        ids = group.get("node_ids") or []
        if ids:
            result.append({
                "action": "merge_nodes", "node_ids": list(ids),
                "keep_id": group.get("keep_id"),
                "reason": group.get("reason") or "",
            })
    return result


# ---------- 并发执行、进度与断点 ----------

def _resolve_concurrency(value):
    """候选组并发数：显式值优先，其次环境配置；限制在 1~8，避免打满模型限流。"""
    if value:
        try:
            return max(1, min(8, int(value)))
        except (TypeError, ValueError):
            pass
    try:
        return max(1, min(8, int(getattr(config, "KG_DEDUP_CONCURRENCY", 3))))
    except (TypeError, ValueError):
        return 3


def graph_signature(db, graph_id):
    """图谱节点 ID 与边数指纹：断点续跑前校验图谱未发生结构变化。"""
    rows = db.execute(
        "SELECT id FROM kg_nodes WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
    edge_count = db.execute(
        "SELECT COUNT(*) FROM kg_edges WHERE graph_id=?", (graph_id,)).fetchone()[0]
    digest = hashlib.sha1()
    digest.update(f"e{edge_count};".encode())
    for row in rows:
        digest.update(f"n{row[0]};".encode())
    return digest.hexdigest()[:16]


def group_fingerprint(node_ids):
    """单个候选组的稳定指纹，用于断点续跑时匹配已完成结果。"""
    ids = sorted(int(i) for i in node_ids or [])
    return hashlib.sha1(",".join(str(i) for i in ids).encode()).hexdigest()[:16]


def _safe_json_list(value):
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _safe_json_dict(value):
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _build_plan(candidates):
    """把候选组冻结为可持久化的执行计划（索引 + 节点 ID + 成员名 + 指纹）。"""
    plan = []
    for index, group in enumerate(candidates):
        node_ids = [int(ctx["id"]) for ctx in group]
        plan.append({
            "index": index,
            "node_ids": node_ids,
            "member_names": [ctx["name"] for ctx in group],
            "fingerprint": group_fingerprint(node_ids),
        })
    return plan


def load_group_contexts(db, graph_id, id_lists):
    """按节点 ID 列表重建候选组上下文（含邻居结构），用于断点续跑。"""
    nodes = _load_graph_context(db, graph_id)
    groups = []
    for ids in id_lists:
        group = [nodes[nid] for nid in ids if nid in nodes]
        groups.append(group)
    return groups


def _analyze_items(items, *, api_url, api_key, model, max_tokens, usage_hook=None,
                   concurrency=None, task_context=None, known_ids=None,
                   on_group_start=None, on_group_done=None):
    """并发判定候选组，逐组回调状态；回调始终在编排线程执行，便于安全落库。

    items 为 [(plan_index, group_context), ...]；on_group_done(index, clusters, error)
    收到的是已按 known_ids 过滤后的合并簇。
    """
    raw_clusters = []
    errors = []
    if not items:
        return raw_clusters, errors
    concurrency = _resolve_concurrency(concurrency)

    def clean_clusters(clusters):
        result = []
        for cluster in clusters:
            ids = [i for i in cluster["node_ids"]
                   if known_ids is None or i in known_ids]
            if len(ids) < 2:
                continue
            cluster["node_ids"] = ids
            if cluster["keep_id"] not in ids:
                cluster["keep_id"] = ids[0]
            result.append(cluster)
        return result

    def worker(group):
        context = task_context() if task_context else nullcontext()
        with context:
            return analyze_dedup_group(
                group, api_url, api_key, model, max_tokens, usage_hook=usage_hook)

    def handle(index, groups, err):
        clusters = [] if err else clean_clusters(groups)
        if on_group_done:
            on_group_done(index, clusters, err)
        if err:
            errors.append(f"第 {index + 1} 组：{err}")
        else:
            raw_clusters.extend(clusters)

    if concurrency <= 1 or len(items) <= 1:
        for index, group in items:
            if on_group_start:
                on_group_start(index)
            try:
                groups, err = worker(group)
            except Exception as e:  # noqa: BLE001 单组失败不中断整体
                groups, err = [], str(e)
            handle(index, groups, err)
        return raw_clusters, errors

    with ThreadPoolExecutor(max_workers=min(concurrency, len(items)),
                            thread_name_prefix="kg-dedup") as pool:
        pending = iter(items)
        futures = {}

        def submit_next():
            try:
                index, group = next(pending)
            except StopIteration:
                return False
            if on_group_start:
                on_group_start(index)
            futures[pool.submit(worker, group)] = index
            return True

        for _ in range(min(concurrency, len(items))):
            submit_next()
        while futures:
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done:
                index = futures.pop(future)
                try:
                    groups, err = future.result()
                except Exception as e:  # noqa: BLE001
                    groups, err = [], str(e)
                handle(index, groups, err)
                submit_next()
    return raw_clusters, errors


def assemble_dedup_result(db, graph_id, raw_clusters, errors=()):
    """合并重叠簇 → 扁平化为 merge_nodes → 整体校验，返回 (valid, rejected, error)。"""
    if not raw_clusters:
        message = "；".join(errors) if errors else "未发现可合并的重复概念"
        return [], [], message
    name_rows = db.execute(
        "SELECT id,name FROM kg_nodes WHERE graph_id=?", (graph_id,)).fetchall()
    name_by_id = {row["id"]: row["name"] for row in name_rows}

    # 并查集合并重叠簇：A=B、B=C → A=B=C。
    all_ids = set()
    for cluster in raw_clusters:
        all_ids.update(cluster["node_ids"])
    uf = _UnionFind(all_ids)
    for cluster in raw_clusters:
        base = cluster["node_ids"][0]
        for nid in cluster["node_ids"][1:]:
            uf.union(base, nid)

    merged = {}
    for cluster in raw_clusters:
        root = uf.find(cluster["node_ids"][0])
        existing = merged.get(root)
        if existing is None:
            merged[root] = {
                "node_ids": list(cluster["node_ids"]),
                "keep_id": cluster["keep_id"],
                "reason": cluster["reason"],
                "confidence": cluster["confidence"],
            }
            continue
        for nid in cluster["node_ids"]:
            if nid not in existing["node_ids"]:
                existing["node_ids"].append(nid)
        if cluster["confidence"] > existing["confidence"]:
            existing["keep_id"] = cluster["keep_id"]
            existing["confidence"] = cluster["confidence"]
            existing["reason"] = cluster["reason"] or existing["reason"]
        elif not existing["reason"]:
            existing["reason"] = cluster["reason"]
    merged_clusters = list(merged.values())
    for cluster in merged_clusters:
        cluster["node_ids"] = sorted(set(cluster["node_ids"]))
        if cluster["keep_id"] not in cluster["node_ids"]:
            cluster["keep_id"] = cluster["node_ids"][0]
    _enrich_groups(merged_clusters, name_by_id)

    suggestions = flatten_dedup_suggestions(merged_clusters)
    # 去重簇由并查集保证互不重叠，必须全量校验：沿用体检的 40 条上限会让
    # 超出的簇被误判为「未纳入」，导致一次去重只能落地几十个合并。
    valid, _rejected, _ = kg_utils.validate_audit_suggestions(
        db, graph_id, suggestions, max_items=len(suggestions))

    valid_groups = []
    for item in valid:
        ids = item["node_ids"]
        confidence = 0.0
        reason = item.get("reason") or ""
        for cluster in merged_clusters:
            if set(cluster["node_ids"]) == set(ids):
                confidence = cluster.get("confidence", 0.0)
                reason = reason or cluster.get("reason", "")
                break
        valid_groups.append({
            "node_ids": ids,
            "keep_id": item["keep_id"],
            "keep_name": name_by_id.get(item["keep_id"], ""),
            "member_names": [name_by_id[i] for i in ids if i in name_by_id],
            "reason": reason,
            "confidence": confidence,
            "action": "merge_nodes",
            "suggestion": {
                "action": "merge_nodes", "node_ids": list(ids),
                "keep_id": item["keep_id"], "reason": reason,
            },
        })

    # rejected_groups：未通过整体校验的簇，逐条单独校验以获得准确原因与成员。
    valid_keys = {frozenset(item["node_ids"]) for item in valid}
    rejected_groups = []
    for cluster in merged_clusters:
        if frozenset(cluster["node_ids"]) in valid_keys:
            continue
        single = [{"action": "merge_nodes", "node_ids": cluster["node_ids"],
                   "keep_id": cluster["keep_id"], "reason": cluster["reason"]}]
        _v, _r, _ = kg_utils.validate_audit_suggestions(db, graph_id, single)
        reason = "与本批其它合并合并后会产生冲突" if _v else (
            _r[0]["reason"] if _r else "未通过校验")
        rejected_groups.append({
            "node_ids": cluster["node_ids"],
            "keep_id": cluster["keep_id"],
            "member_names": list(cluster.get("member_names") or []),
            "reason": reason,
            "confidence": cluster.get("confidence", 0.0),
        })

    error = "；".join(errors) if errors else ""
    return valid_groups, rejected_groups, error


def run_dedup(db, graph_id, api_url, api_key, model, usage_hook=None,
              max_tokens=DEDUP_MAX_TOKENS, *, concurrency=None, task_context=None):
    """一次性执行智能去重（不落进度记录），供命令行与旧调用方使用。

    逐组并发判定 → 并查集合并重叠簇 → 扁平化 → 整体校验。
    返回 (valid_groups, rejected_groups, error)。单组异常/失败只记录、不中断整体。
    """
    candidates = build_dedup_candidates(db, graph_id)
    if not candidates:
        return [], [], "未发现疑似重复的概念"
    known_ids = {row[0] for row in db.execute(
        "SELECT id FROM kg_nodes WHERE graph_id=?", (graph_id,)).fetchall()}
    raw_clusters, errors = _analyze_items(
        list(enumerate(candidates)), api_url=api_url, api_key=api_key,
        model=model, max_tokens=max_tokens, usage_hook=usage_hook,
        concurrency=concurrency, task_context=task_context, known_ids=known_ids)
    return assemble_dedup_result(db, graph_id, raw_clusters, errors)


# ---------- 持久化运行：进度、断点续跑 ----------

def create_dedup_run(db, graph_id, *, model="", created_by=0, operation_id=None):
    """创建一条「进行中」的去重记录，计划与进度随候选组逐步写入。"""
    cur = db.execute(
        "INSERT INTO kg_dedup_runs(graph_id,status,model,operation_id,created_by,started_at)"
        " VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
        (graph_id, DEDUP_RUNNING, model, operation_id, created_by))
    db.commit()
    return cur.lastrowid


def _progress_payload(rows):
    """把逐组行汇总成界面可直接渲染的进度结构。"""
    counts = {}
    groups = []
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
        names = _safe_json_list(row["member_names"])
        groups.append({
            "index": row["group_index"],
            "status": status,
            "count": len(names),
            "names": names[:3],
            "error": (row["error"] or "")[:80],
            "attempts": row["attempts"] or 0,
        })
    total = len(rows)
    done = counts.get(GROUP_DONE, 0)
    failed = counts.get(GROUP_FAILED, 0)
    processed = done + failed
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "running": counts.get(GROUP_RUNNING, 0),
        "pending": counts.get(GROUP_PENDING, 0),
        "percent": round(processed * 100 / total) if total else 0,
        "updated_at": beijing_wall_now().isoformat(sep=" ", timespec="seconds"),
        "groups": groups,
    }


def _write_progress(db, run_id):
    rows = db.execute(
        "SELECT * FROM kg_dedup_groups WHERE run_id=? ORDER BY group_index",
        (run_id,)).fetchall()
    payload = _progress_payload(rows)
    db.execute("UPDATE kg_dedup_runs SET progress=? WHERE id=?",
               (json.dumps(payload, ensure_ascii=False), run_id))
    db.commit()
    return payload


def _finish_run(db, run_id, status, valid_groups, rejected_groups, error):
    rows = db.execute(
        "SELECT * FROM kg_dedup_groups WHERE run_id=? ORDER BY group_index",
        (run_id,)).fetchall()
    progress = _progress_payload(rows)
    suggestions = flatten_dedup_suggestions(valid_groups)
    db.execute(
        "UPDATE kg_dedup_runs SET status=?,groups=?,suggestions=?,rejected=?,error=?,"
        "progress=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, json.dumps(valid_groups, ensure_ascii=False),
         json.dumps(suggestions, ensure_ascii=False),
         json.dumps(rejected_groups, ensure_ascii=False),
         (error or "")[:500], json.dumps(progress, ensure_ascii=False), run_id))
    db.commit()


def run_is_resumable(status, progress):
    """该去重记录是否还能从未完成候选组继续。"""
    if status in (DEDUP_APPLIED, DEDUP_DISCARDED, DEDUP_ROLLED_BACK):
        return False
    if not isinstance(progress, dict):
        return False
    total = int(progress.get("total") or 0)
    if total <= 0:
        return False
    failed = int(progress.get("failed") or 0)
    done = int(progress.get("done") or 0)
    if failed > 0:
        return True
    return status in DEDUP_RESUMABLE and done < total


def run_dedup_run(db, graph_id, run_id, api_url, api_key, model, usage_hook=None,
                  max_tokens=DEDUP_MAX_TOKENS, concurrency=None,
                  task_context=None):
    """执行一条已创建的持久化去重记录，支持并发判定与断点续跑。

    - 首次执行：生成候选计划并写入 kg_dedup_groups；
    - 续跑：图谱结构未变化时复用已完成组结果，只重跑失败/未完成组；
    - 每次组开始/结束都更新 kg_dedup_runs.progress，界面可实时查看。

    返回 (valid_groups, rejected_groups, error, status)。
    """
    run = db.execute(
        "SELECT * FROM kg_dedup_runs WHERE id=? AND graph_id=?",
        (run_id, graph_id)).fetchone()
    if not run:
        return [], [], "去重记录不存在", DEDUP_FAILED

    plan = _safe_json_list(run["plan"])
    signature = graph_signature(db, graph_id)
    if plan:
        if (run["graph_sig"] or "") != signature:
            message = "图谱内容在去重开始后已变化，无法从断点继续；请重新开始去重。"
            _finish_run(db, run_id, DEDUP_FAILED, [], [], message)
            return [], [], message, DEDUP_FAILED
    else:
        candidates = build_dedup_candidates(db, graph_id)
        if not candidates:
            message = "未发现疑似重复的概念"
            _finish_run(db, run_id, DEDUP_FAILED, [], [], message)
            return [], [], message, DEDUP_FAILED
        plan = _build_plan(candidates)
        db.execute("UPDATE kg_dedup_runs SET plan=?,graph_sig=? WHERE id=?",
                   (json.dumps(plan, ensure_ascii=False), signature, run_id))
        db.commit()

    # 补齐组行（旧记录或异常中断时）；进行中/失败组重置为待处理，已完成组保留结果。
    existing = {row["group_index"] for row in db.execute(
        "SELECT group_index FROM kg_dedup_groups WHERE run_id=?", (run_id,)).fetchall()}
    for item in plan:
        if item["index"] in existing:
            continue
        db.execute(
            "INSERT OR IGNORE INTO kg_dedup_groups(run_id,group_index,fingerprint,"
            "node_ids,member_names,status) VALUES(?,?,?,?,?,?)",
            (run_id, item["index"], item["fingerprint"],
             json.dumps(item["node_ids"], ensure_ascii=False),
             json.dumps(item["member_names"], ensure_ascii=False), GROUP_PENDING))
    db.execute(
        "UPDATE kg_dedup_groups SET status=?, finished_at=NULL, error='' "
        "WHERE run_id=? AND status IN (?,?)",
        (GROUP_PENDING, run_id, GROUP_RUNNING, GROUP_FAILED))
    db.commit()

    rows = db.execute(
        "SELECT * FROM kg_dedup_groups WHERE run_id=? ORDER BY group_index",
        (run_id,)).fetchall()
    done_indexes = {row["group_index"] for row in rows if row["status"] == GROUP_DONE}
    pending_plan = [item for item in plan if item["index"] not in done_indexes]
    contexts = load_group_contexts(
        db, graph_id, [item["node_ids"] for item in pending_plan])
    items = []
    for item, context in zip(pending_plan, contexts):
        if len(context) >= 2:
            items.append((item["index"], context))
        else:  # 节点已不存在等边界：直接视为无合并结果，避免卡在待处理
            db.execute(
                "UPDATE kg_dedup_groups SET status=?,clusters='[]',error='',"
                "finished_at=CURRENT_TIMESTAMP WHERE run_id=? AND group_index=?",
                (GROUP_DONE, run_id, item["index"]))
    db.commit()
    _write_progress(db, run_id)

    known_ids = {row[0] for row in db.execute(
        "SELECT id FROM kg_nodes WHERE graph_id=?", (graph_id,)).fetchall()}

    def on_group_start(index):
        db.execute(
            "UPDATE kg_dedup_groups SET status=?,attempts=attempts+1,"
            "started_at=COALESCE(started_at,CURRENT_TIMESTAMP),finished_at=NULL "
            "WHERE run_id=? AND group_index=?",
            (GROUP_RUNNING, run_id, index))
        db.commit()
        _write_progress(db, run_id)

    def on_group_done(index, clusters, error):
        if error:
            db.execute(
                "UPDATE kg_dedup_groups SET status=?,error=?,finished_at=CURRENT_TIMESTAMP "
                "WHERE run_id=? AND group_index=?",
                (GROUP_FAILED, (error or "")[:500], run_id, index))
        else:
            db.execute(
                "UPDATE kg_dedup_groups SET status=?,clusters=?,error='',"
                "finished_at=CURRENT_TIMESTAMP WHERE run_id=? AND group_index=?",
                (GROUP_DONE, json.dumps(clusters, ensure_ascii=False), run_id, index))
        db.commit()
        _write_progress(db, run_id)

    _analyze_items(
        items, api_url=api_url, api_key=api_key, model=model,
        max_tokens=max_tokens, usage_hook=usage_hook, concurrency=concurrency,
        task_context=task_context, known_ids=known_ids,
        on_group_start=on_group_start, on_group_done=on_group_done)

    rows = db.execute(
        "SELECT * FROM kg_dedup_groups WHERE run_id=? ORDER BY group_index",
        (run_id,)).fetchall()
    all_clusters = []
    errors = []
    for row in rows:
        if row["status"] == GROUP_DONE:
            all_clusters.extend(_safe_json_list(row["clusters"]))
        elif row["status"] == GROUP_FAILED:
            errors.append(f"第 {row['group_index'] + 1} 组：{row['error'] or '未知错误'}")

    if not all_clusters:
        message = "；".join(errors) if errors else "未发现可合并的重复概念"
        status = DEDUP_FAILED if errors else DEDUP_CLEAN
        _finish_run(db, run_id, status, [], [], message)
        return [], [], message, status

    valid, rejected, error = assemble_dedup_result(db, graph_id, all_clusters, errors)
    status = DEDUP_PENDING if valid else (DEDUP_FAILED if errors else DEDUP_CLEAN)
    _finish_run(db, run_id, status, valid, rejected, error)
    return valid, rejected, error, status
