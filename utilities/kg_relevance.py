# -*- coding: utf-8 -*-
"""知识图谱无关性分析：分批把概念交给 LLM 判断是否属于本学科的知识或方法，
输出需要摘除的概念（prune_node，连同其关系一起删除），复用 knowledge_graph
的校验、快照与回滚能力。

设计要点：
- 全量节点按 ID 排序后分批（每批 RELEVANCE_BATCH 个），并发送模型判定；
- 每批结果与完成状态写回 kg_relevance_runs.plan，进度写入 progress，
  界面可实时查看逐批进度，中断后可从未完成批次续跑；
- 模型只输出建议摘除的概念，不自动改图；汇总后经 validate_audit_suggestions
  校验，应用时走 apply_audit + snapshot + restore，可回滚。
"""
import json
import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext

from utilities import kg_dedup as kd
from utilities import knowledge_graph as kg_utils
from utilities import llm
from utilities.time_utils import beijing_wall_now

log = logging.getLogger("gradelogy.kg_relevance")

RELEVANCE_PENDING = kg_utils.AUDIT_PENDING          # 待确认
RELEVANCE_APPLIED = kg_utils.AUDIT_APPLIED          # 已应用
RELEVANCE_DISCARDED = kg_utils.AUDIT_DISCARDED      # 已忽略
RELEVANCE_FAILED = kg_utils.AUDIT_FAILED            # 失败
RELEVANCE_CLEAN = kg_utils.AUDIT_CLEAN              # 无需调整
RELEVANCE_ROLLED_BACK = kg_utils.AUDIT_ROLLED_BACK  # 已回滚
RELEVANCE_RUNNING = "进行中"
RELEVANCE_INTERRUPTED = "已中断"
RELEVANCE_RESUMABLE = (RELEVANCE_FAILED, RELEVANCE_INTERRUPTED)

RELEVANCE_BATCH = 40            # 单批送模型的概念数
RELEVANCE_MAX_TOKENS = 16384

RELEVANCE_PROMPT = """你是学科知识图谱治理专家。这张图谱的学科是「{subject}」，
它应该只包含该学科的概念、规律、方法与必要的跨学科前置知识。
下面是一批概念及其分类、定义。请判断哪些概念与该学科的知识或方法无关，需要摘除。

### 需要摘除的典型情况
1. 培训机构、班型、课程体系、集训队、竞赛/高考辅导课程等组织或产品名称；
2. 招生、报名、考试政策、学校院系、人物、企业、地名等非知识性条目；
3. 资料名、书名、章节标题、试卷名称等文献元信息；
4. 明显属于其他学科且对本学科没有支撑作用的内容。

### 不要摘除
- 跨学科但有支撑作用的前置知识（如数学工具、物理量、单位、实验方法）；
- 只是名称宽泛或分类不规范的概念（那属于分类重构或去重的范畴）。

### 输出格式（严格输出合法 JSON，不要解释）
{{"drops":[{{"node_id":1,"reason":"这是培训机构班型，不是学科知识","confidence":0.9}}]}}
没有需要摘除的概念时返回 {{"drops":[]}}。
"""


def _load_nodes(db, graph_id):
    rows = db.execute(
        "SELECT id,name,category,definition,aliases FROM kg_nodes "
        "WHERE graph_id=? ORDER BY id", (graph_id,)).fetchall()
    return [{
        "id": row["id"], "name": row["name"],
        "category": row["category"] or "",
        "definition": row["definition"] or "",
        "aliases": kg_utils._json_list(row["aliases"]),
    } for row in rows]


def _format_batch(nodes, subject):
    lines = [RELEVANCE_PROMPT.format(subject=subject or "未指定"), "", "### 候选概念"]
    for node in nodes:
        parts = [f"#{node['id']} {node['name']}"]
        if node["category"]:
            parts.append(f"分类：{node['category']}")
        if node["aliases"]:
            parts.append("别名：" + "、".join(node["aliases"][:4]))
        if node["definition"]:
            parts.append(f"定义：{node['definition'][:160]}")
        lines.append("｜".join(parts))
    return "\n".join(lines)


def analyze_relevance_batch(nodes, subject, api_url, api_key, model, max_tokens,
                            usage_hook=None):
    """判定一批概念中需要摘除的项，返回 (drops, error)。"""
    try:
        reply, finish, err = llm.chat(
            api_url, api_key, model,
            [{"type": "text", "text": _format_batch(nodes, subject)}],
            max_tokens=max_tokens, usage_hook=usage_hook, report_running=True,
            stage="kg_relevance")
    except Exception as e:  # noqa: BLE001 单批失败只记录，不中断整体
        return [], str(e)
    if err:
        return [], err
    if finish == "length" or not (reply or "").strip():
        return [], f"{kg_utils.TRUNCATED_MARK}（max_tokens={max_tokens}）"
    data = llm.extract_json(reply)
    if not isinstance(data, dict) or not isinstance(data.get("drops"), list):
        return [], "模型未返回合法的 drops 数组"
    drops = []
    for item in data["drops"]:
        if not isinstance(item, dict):
            continue
        try:
            node_id = int(item.get("node_id"))
        except (TypeError, ValueError):
            continue
        drops.append({
            "node_id": node_id,
            "reason": str(item.get("reason") or "").strip()[:120],
            "confidence": kg_utils._clamp(item.get("confidence"), 0.5),
        })
    return drops, ""


def create_relevance_run(db, graph_id, *, model="", created_by=0, operation_id=None):
    cur = db.execute(
        "INSERT INTO kg_relevance_runs(graph_id,status,model,operation_id,created_by,"
        "started_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
        (graph_id, RELEVANCE_RUNNING, model, operation_id, created_by))
    db.commit()
    return cur.lastrowid


def _progress_payload(plan):
    groups = []
    counts = {}
    for item in plan:
        if item.get("done"):
            status = "failed" if item.get("error") else "done"
        elif item.get("running"):
            status = "running"
        else:
            status = "pending"
        counts[status] = counts.get(status, 0) + 1
        groups.append({
            "index": item["index"], "status": status,
            "count": len(item.get("node_ids") or []),
            "names": (item.get("names") or [])[:3],
            "error": (item.get("error") or "")[:80],
        })
    total = len(plan)
    done, failed = counts.get("done", 0), counts.get("failed", 0)
    return {
        "total": total, "done": done, "failed": failed,
        "running": counts.get("running", 0), "pending": counts.get("pending", 0),
        "percent": round((done + failed) * 100 / total) if total else 0,
        "updated_at": beijing_wall_now().isoformat(sep=" ", timespec="seconds"),
        "groups": groups,
    }


def _save_plan(db, run_id, plan):
    progress = _progress_payload(plan)
    db.execute("UPDATE kg_relevance_runs SET plan=?,progress=? WHERE id=?",
               (json.dumps(plan, ensure_ascii=False),
                json.dumps(progress, ensure_ascii=False), run_id))
    db.commit()
    return progress


def run_is_resumable(status, progress):
    if status in (RELEVANCE_APPLIED, RELEVANCE_DISCARDED, RELEVANCE_ROLLED_BACK):
        return False
    if not isinstance(progress, dict):
        return False
    total = int(progress.get("total") or 0)
    if total <= 0:
        return False
    if int(progress.get("failed") or 0) > 0:
        return True
    return (status in RELEVANCE_RESUMABLE
            and int(progress.get("done") or 0) < total)


def assemble_relevance_result(db, graph_id, drops, errors=()):
    """把模型输出的摘除建议汇总、校验，返回 (items, rejected, error)。"""
    known = {row["id"]: row for row in db.execute(
        "SELECT id,name,category FROM kg_nodes WHERE graph_id=?", (graph_id,))}
    merged = {}
    for drop in drops or []:
        node_id = drop.get("node_id")
        if node_id not in known:
            continue
        existing = merged.get(node_id)
        if existing is None or drop.get("confidence", 0) > existing["confidence"]:
            merged[node_id] = {
                "node_id": node_id,
                "reason": drop.get("reason") or "",
                "confidence": drop.get("confidence") or 0,
            }
    suggestions = [{"action": "prune_node", "node_id": node_id,
                    "reason": item["reason"]}
                   for node_id, item in merged.items()]
    if not suggestions:
        message = "；".join(errors) if errors else "未发现与学科无关的概念"
        return [], [], message
    valid, _rejected, _ = kg_utils.validate_audit_suggestions(
        db, graph_id, suggestions, max_items=len(suggestions))
    valid_ids = {item["node_id"] for item in valid}
    items = []
    for item in valid:
        node = known[item["node_id"]]
        extra = merged[item["node_id"]]
        items.append({
            "node_id": item["node_id"], "name": node["name"],
            "category": node["category"] or "", "reason": extra["reason"],
            "confidence": extra["confidence"], "action": "prune_node",
            "edge_count": item.get("edge_count") or 0,
        })
    rejected = []
    for node_id, extra in merged.items():
        if node_id in valid_ids:
            continue
        rejected.append({
            "node_id": node_id, "name": known[node_id]["name"],
            "reason": extra["reason"] or "未通过校验",
            "confidence": extra["confidence"],
        })
    return items, rejected, "；".join(errors) if errors else ""


def run_relevance_run(db, graph_id, run_id, api_url, api_key, model, usage_hook=None,
                      max_tokens=RELEVANCE_MAX_TOKENS, concurrency=None,
                      task_context=None):
    """执行一条无关性分析记录，支持分批并发、进度展示与断点续跑。

    返回 (items, rejected, error, status)。
    """
    run = db.execute(
        "SELECT * FROM kg_relevance_runs WHERE id=? AND graph_id=?",
        (run_id, graph_id)).fetchone()
    if not run:
        return [], [], "无关性分析记录不存在", RELEVANCE_FAILED

    def finish(status, items, rejected, error):
        db.execute(
            "UPDATE kg_relevance_runs SET status=?,suggestions=?,rejected=?,error=?,"
            "progress=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, json.dumps(items, ensure_ascii=False),
             json.dumps(rejected, ensure_ascii=False), (error or "")[:500],
             json.dumps(_progress_payload(plan), ensure_ascii=False), run_id))
        db.commit()

    try:
        plan = json.loads(run["plan"] or "[]")
    except (TypeError, ValueError):
        plan = []
    if not isinstance(plan, list):
        plan = []

    nodes = _load_nodes(db, graph_id)
    node_by_id = {node["id"]: node for node in nodes}
    if plan:
        if (run["graph_sig"] or "") != kd.graph_signature(db, graph_id):
            message = "图谱内容在分析开始后已变化，无法从断点继续；请重新开始分析。"
            finish(RELEVANCE_FAILED, [], [], message)
            return [], [], message, RELEVANCE_FAILED
    else:
        if not nodes:
            message = "图谱还没有概念，无需分析"
            plan = []
            finish(RELEVANCE_CLEAN, [], [], message)
            return [], [], message, RELEVANCE_CLEAN
        plan = []
        for start in range(0, len(nodes), RELEVANCE_BATCH):
            chunk = nodes[start:start + RELEVANCE_BATCH]
            plan.append({
                "index": len(plan),
                "node_ids": [node["id"] for node in chunk],
                "names": [node["name"] for node in chunk],
                "done": False, "running": False, "drops": [], "error": "",
            })
        db.execute("UPDATE kg_relevance_runs SET plan=?,graph_sig=? WHERE id=?",
                   (json.dumps(plan, ensure_ascii=False),
                    kd.graph_signature(db, graph_id), run_id))
        db.commit()

    subject = db.execute("SELECT subject FROM knowledge_graphs WHERE id=?",
                         (graph_id,)).fetchone()
    subject = subject["subject"] if subject else ""
    concurrency = kd._resolve_concurrency(concurrency)
    pending = [item for item in plan if not item.get("done") or item.get("error")]
    for item in plan:
        item["running"] = False
    _save_plan(db, run_id, plan)

    def worker(item):
        batch_nodes = [node_by_id[nid] for nid in item["node_ids"]
                       if nid in node_by_id]
        context = task_context() if task_context else nullcontext()
        with context:
            return analyze_relevance_batch(
                batch_nodes, subject, api_url, api_key, model, max_tokens,
                usage_hook=usage_hook)

    def handle(item, drops, error):
        item["done"] = True
        item["running"] = False
        item["drops"] = drops
        item["error"] = (error or "")[:500]
        _save_plan(db, run_id, plan)

    def run_one(item):
        item["running"] = True
        _save_plan(db, run_id, plan)
        try:
            drops, error = worker(item)
        except Exception as e:  # noqa: BLE001
            drops, error = [], str(e)
        handle(item, drops, error)

    if concurrency <= 1 or len(pending) <= 1:
        for item in pending:
            run_one(item)
    else:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(pending)),
                                thread_name_prefix="kg-relevance") as pool:
            iterator = iter(pending)
            futures = {}

            def submit_next():
                try:
                    item = next(iterator)
                except StopIteration:
                    return False
                item["running"] = True
                _save_plan(db, run_id, plan)
                futures[pool.submit(worker, item)] = item
                return True

            for _ in range(min(concurrency, len(pending))):
                submit_next()
            while futures:
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    item = futures.pop(future)
                    try:
                        drops, error = future.result()
                    except Exception as e:  # noqa: BLE001
                        drops, error = [], str(e)
                    handle(item, drops, error)
                    submit_next()

    drops = []
    errors = []
    for item in plan:
        if item.get("error"):
            errors.append(f"第 {item['index'] + 1} 批：{item['error']}")
        drops.extend(item.get("drops") or [])
    items, rejected, error = assemble_relevance_result(db, graph_id, drops, errors)
    if items:
        status = RELEVANCE_PENDING
    elif errors:
        status = RELEVANCE_FAILED
    else:
        status = RELEVANCE_CLEAN
    finish(status, items, rejected, error)
    return items, rejected, error, status
