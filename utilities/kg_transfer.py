# -*- coding: utf-8 -*-
"""知识图谱独立检查与跨实例迁移；只使用标准库和 SQLite。"""
import hashlib
import json
import ntpath
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

FORMAT = "gradelogy-kg"
VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
MAX_JSON_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 50000
COPY_BUFFER_SIZE = 1024 * 1024
RECENT_ERROR_LIMIT = 10

# 与 utilities/knowledge_graph.py 保持一致；这里不导入该模块，以免间接依赖 config/AI。
STATUS_PENDING = "待建图"
STATUS_BUILDING = "建图中"
CHUNK_PENDING = "pending"
CHUNK_RUNNING = "running"

_DATA_TABLES = {
    "materials": "kg_materials",
    "chunks": "kg_chunks",
    "nodes": "kg_nodes",
    "edges": "kg_edges",
    "audits": "kg_audits",
    "members": "kg_members",
}
_COUNT_TABLES = {"knowledge_graphs": "graph",
                 **{table: key for key, table in _DATA_TABLES.items()}}
_MEMBER_ROLES = {"manage", "edit", "use"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAVEPOINT = "kg_transfer_import"


class KGTransferError(ValueError):
    """迁移包、源数据或文件布局不符合要求。"""


def _one(db, sql, params=()):
    cursor = db.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((item[0] for item in cursor.description), row))


def _many(db, sql, params=()):
    cursor = db.execute(sql, params)
    names = [item[0] for item in cursor.description]
    return [dict(row) if isinstance(row, sqlite3.Row) else dict(zip(names, row))
            for row in cursor.fetchall()]


def _graph(db, graph_id):
    row = _one(db, "SELECT * FROM knowledge_graphs WHERE id=?", (graph_id,))
    if row is None:
        raise KGTransferError(f"知识图谱不存在：{graph_id}")
    return row


def _table_counts(db, graph_id):
    counts = {}
    for key, table in _DATA_TABLES.items():
        counts[key] = _one(
            db, f"SELECT COUNT(*) AS n FROM {table} WHERE graph_id=?", (graph_id,))["n"]
    return counts


def _status_counts(db, table, graph_id):
    rows = _many(
        db, f"SELECT status,COUNT(*) AS count FROM {table} "
            "WHERE graph_id=? GROUP BY status ORDER BY status", (graph_id,))
    return {str(row["status"] or "未标记"): row["count"] for row in rows}


def inspect_graph(db, graph_id):
    """返回图谱概况、各表数量、状态分布和最近错误，不修改数据库。"""
    graph = _graph(db, graph_id)
    errors = []
    queries = (
        ("material", "kg_materials",
         "SELECT id,error,COALESCE(finished_at,started_at,created_at,'') AS timestamp "
         "FROM kg_materials WHERE graph_id=? AND COALESCE(error,'')<>'' ORDER BY id DESC LIMIT ?"),
        ("chunk", "kg_chunks",
         "SELECT id,material_id,error,COALESCE(finished_at,started_at,'') AS timestamp "
         "FROM kg_chunks WHERE graph_id=? AND COALESCE(error,'')<>'' ORDER BY id DESC LIMIT ?"),
        ("audit", "kg_audits",
         "SELECT id,error,COALESCE(applied_at,created_at,'') AS timestamp "
         "FROM kg_audits WHERE graph_id=? AND COALESCE(error,'')<>'' ORDER BY id DESC LIMIT ?"),
    )
    for kind, _table, sql in queries:
        for row in _many(db, sql, (graph_id, RECENT_ERROR_LIMIT)):
            row["kind"] = kind
            errors.append(row)
    errors.sort(key=lambda row: (row.get("timestamp") or "", row["id"]), reverse=True)
    return {
        "graph": graph,
        "counts": _table_counts(db, graph_id),
        "status_counts": {
            "materials": _status_counts(db, "kg_materials", graph_id),
            "chunks": _status_counts(db, "kg_chunks", graph_id),
            "audits": _status_counts(db, "kg_audits", graph_id),
        },
        "recent_errors": errors[:RECENT_ERROR_LIMIT],
    }


def _username_metadata(db, graph, materials, audits, members):
    user_ids = {graph.get("created_by")}
    user_ids.update(row.get("uploaded_by") for row in materials)
    user_ids.update(row.get("created_by") for row in audits)
    user_ids.update(row.get("user_id") for row in members)
    user_ids = sorted(uid for uid in user_ids if isinstance(uid, int) and uid > 0)
    users = {}
    if user_ids:
        placeholders = ",".join("?" for _ in user_ids)
        for row in _many(db, f"SELECT id,username FROM users WHERE id IN ({placeholders})",
                         tuple(user_ids)):
            users[str(row["id"])] = row["username"]
    graph["created_by_username"] = users.get(str(graph.get("created_by")))
    for row in materials:
        row["uploaded_by_username"] = users.get(str(row.get("uploaded_by")))
    for row in audits:
        row["created_by_username"] = users.get(str(row.get("created_by")))
    for row in members:
        row["username"] = users.get(str(row.get("user_id")))
    return users


def _safe_archive_component(value, fallback):
    """只保留单层文件名；原始文件名不参与导入时的存储路径。"""
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return (name or fallback)[:160]


def _material_source_path(upload_dir, graph_id, stored_name):
    """数据库路径必须是 kg/<gid>/<basename>，且任何一级都不能借符号链接逃逸。"""
    value = str(stored_name or "")
    parts = value.split("/")
    if (not value or "\\" in value or "\x00" in value or value.startswith("/")
            or ntpath.splitdrive(value)[0]
            or len(parts) != 3 or parts[:2] != ["kg", str(graph_id)]
            or parts[2] in ("", ".", "..")):
        raise KGTransferError(f"材料 stored_name 路径不安全：{value!r}")
    root = os.path.realpath(os.path.abspath(os.fspath(upload_dir)))
    graph_dir = os.path.abspath(os.path.join(root, "kg", str(graph_id)))
    candidate = os.path.abspath(os.path.join(graph_dir, parts[2]))
    try:
        if os.path.commonpath((root, graph_dir)) != root:
            raise KGTransferError(f"材料目录越界：{value!r}")
    except ValueError as exc:
        raise KGTransferError(f"材料目录越界：{value!r}") from exc
    # realpath 与绝对路径不同，说明图谱目录或文件本身是符号链接。
    if (os.path.realpath(graph_dir) != graph_dir
            or os.path.realpath(candidate) != candidate
            or not os.path.isfile(candidate)):
        raise KGTransferError(f"材料文件不存在或包含不安全的符号链接：{value!r}")
    return candidate


def _write_material(archive, source, archive_name):
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, archive.open(archive_name, "w") as dst:
        while True:
            block = src.read(COPY_BUFFER_SIZE)
            if not block:
                break
            size += len(block)
            digest.update(block)
            dst.write(block)
    return size, digest.hexdigest()


_RELATION_TYPES = [
    {"name": "prerequisite", "label": "前置知识",
     "direction": "from 是学习 to 之前必须具备的基础"},
    {"name": "contains", "label": "结构包含",
     "direction": "from 是上位概念，to 是下位概念"},
    {"name": "related", "label": "并列 / 互补 / 常共同考查",
     "direction": "不表达学习先后依赖，from 与 to 可互换理解"},
]

_FIELD_GLOSSARY = {
    "kg_materials": {
        "id": "资料 ID；与 files/<id>/ 下的归档文件一一对应",
        "filename": "原始文件名，仅用于展示",
        "sha256": "原始文件 SHA256，可用于校验文件内容",
        "size": "原始文件字节数",
        "page_count": "PDF 解析页数，文本资料为 0",
        "char_count": "参与建图的字符数",
        "status": "建图状态：待建图 / 建图中 / 完成 / 失败",
    },
    "kg_chunks": {
        "id": "文本块 ID",
        "material_id": "所属资料 ID",
        "page_no": "起始页码，文本资料为 0",
        "chunk_no": "资料内块序号，从 0 开始",
        "content": "送入模型的原文片段",
        "status": "该块建图状态：pending / running / success / failed",
        "extraction_json": "该块的原始抽取结果，包含 concepts 与 relations",
    },
    "kg_nodes": {
        "id": "节点在源实例中的主键；导入时应重新分配，仅用于包内交叉引用",
        "name": "概念、定理、方法或公式的显示名称",
        "normalized": "名称归一化结果，同一图谱内唯一，用于合并同义写法",
        "category": "模块 / 主题分类，可为空",
        "definition": "一句话定义，可为空",
        "aliases": "别名的 JSON 数组字符串",
        "source_materials": "支撑该节点的资料 ID 列表，格式为 ,11,12,",
        "confidence": "模型抽取置信度，0~1；不等同于教师审核后的正确率",
    },
    "kg_edges": {
        "id": "关系在源实例中的主键；导入时应重新分配",
        "src_id": "起点节点 ID；方向含义见 relation_types",
        "dst_id": "终点节点 ID",
        "relation": "关系类型，见 relation_types",
        "evidence": "支撑该关系的资料原句或关键短语",
        "source_materials": "支撑该关系的资料 ID 列表，格式为 ,11,",
        "confidence": "模型抽取置信度，0~1",
    },
    "kg_audits": {
        "status": "AI 体检状态：待确认 / 已应用 / 无需调整 等",
        "suggestions": "AI 体检建议 JSON，可能引用 node_ids / edge_id",
        "snapshot": "应用建议前的图谱快照 JSON，可用于回滚",
    },
    "kg_members": {
        "user_id": "协作者在源实例中的用户 ID；导入时按 username 重新匹配",
        "role": "协作角色：manage / edit / use",
    },
}


def _build_metadata(graph, counts, material_files, scope="full"):
    """生成结构化元数据；不包含原图谱名称。"""
    if scope == "graph":
        provenance = {
            "concepts": "仅包含图谱结构；原始资料未随包导出",
            "source_materials": "已清空：本包不含原始资料，因此不保留材料引用",
            "evidence": "关系边保留的原句或关键短语",
            "confidence": "0~1 的模型自评置信度，不是经过教学审核的正确率",
        }
        guidance = [
            "本包为仅知识图谱包，不包含原始资料、文本块、体检历史与协作成员",
            "导入时必须为新图谱指定名称；原图谱名称已被有意省略",
            "kg_nodes / kg_edges 的 id 是包内局部 ID，导入到新平台时应重新分配",
            "先按 kg_nodes 建节点，再用旧 id 映射重建 kg_edges 的 src_id 与 dst_id",
            "relation 的 from/to 方向必须按 relation_types 的解释保留",
            "source_materials 已清空，无法据此追溯原始资料或重建资料内容",
        ]
    else:
        provenance = {
            "concepts": "由教师上传的资料经模型逐块抽取，再按归一化名称增量合并",
            "source_materials": "节点和关系通过 source_materials 关联支撑它的资料 ID",
            "evidence": "关系边保留资料原句或关键短语，便于人工复核",
            "confidence": "0~1 的模型自评置信度，不是经过教学审核的正确率",
        }
        guidance = [
            "导入时必须为新图谱指定名称；原图谱名称已被有意省略",
            "kg_nodes / kg_edges 的 id 是包内局部 ID，导入到新平台时应重新分配",
            "先按 kg_nodes 建节点，再用旧 id 映射重建 kg_edges 的 src_id 与 dst_id",
            "relation 的 from/to 方向必须按 relation_types 的解释保留",
            "source_materials 中的数字是 kg_materials.id，可映射到 files/ 下的原始资料",
            "kg_chunks 与 kg_audits 是可选的历史信息，可在只需要图结构时忽略",
        ]
    return {
        "format": FORMAT,
        "version": VERSION,
        "scope": scope,
        "document_type": (
            "knowledge_graph_only_package" if scope == "graph"
            else "knowledge_graph_transfer_package"),
        "generated_by": {
            "name": "GradeLogy",
            "description": "从教材、考纲、讲义中抽取学科概念与关系形成的知识图谱",
        },
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "graph_name_omitted": True,
        "graph": {
            "subject": graph.get("subject") or "",
            "grade_level": graph.get("grade_level") or "",
            "description": graph.get("description") or "",
        },
        "counts": counts,
        "relation_types": _RELATION_TYPES,
        "field_glossary": _FIELD_GLOSSARY,
        "provenance": provenance,
        "import_guidance": guidance,
        "files": {
            "manifest": "manifest.json",
            "data": "data.json",
            "readme": "README.md",
            "metadata": "metadata.json",
            "materials": material_files,
        },
    }


def _one_line(value, limit=200):
    text = " ".join(str(value or "").split())
    return text[:limit]


def _render_readme(metadata):
    """把元数据渲染成自然语言说明，便于 LLM 直接阅读。"""
    graph = metadata["graph"]
    counts = metadata["counts"]
    graph_only = metadata.get("scope") == "graph"
    material_count = counts.get("kg_materials", 0)
    chunk_count = counts.get("kg_chunks", 0)
    example_sources = "" if graph_only else ",11,"
    lines = [
        "# GradeLogy 知识图谱迁移包" + ("（仅知识图谱）" if graph_only else ""),
        "",
        "本说明面向人类读者与大语言模型，用自然语言描述这个 ZIP 的内容与语义。",
        "即使不运行 GradeLogy，也可以据此理解、校验或迁移图谱。",
        "",
        "## 1. 包内文件",
        "",
        "| 文件 | 作用 |",
        "| --- | --- |",
        "| `metadata.json` | 结构化元数据：关系语义、字段说明、数量与导入建议 |",
        "| `manifest.json` | 机器校验清单：格式版本、导出时间、各表行数与资料文件 SHA256 |",
        "| `data.json` | 图谱数据：图谱信息、节点与关系"
        + ("（其余数据表为空数组）" if graph_only
           else "、资料、文本块、体检历史与协作成员") + " |",
    ]
    if not graph_only:
        lines.append("| `files/<material_id>/<filename>` | 原始资料文件，按资料 ID 归档 |")
    lines += [
        "| `README.md` | 本说明 |",
        "",
        "## 2. 图谱概况",
        "",
        f"- 学科：{_one_line(graph.get('subject'), 80) or '未填写'}",
        f"- 学段 / 适用年级：{_one_line(graph.get('grade_level'), 40) or '未填写'}",
        f"- 说明：{_one_line(graph.get('description')) or '无'}",
        f"- 概念节点：{counts.get('kg_nodes', 0)} 个",
        f"- 关系边：{counts.get('kg_edges', 0)} 条",
    ]
    if graph_only:
        lines.append("- 包范围：仅知识图谱，不含原始资料文件、文本块、体检历史与协作成员")
    else:
        lines.append(f"- 原始资料：{material_count} 份（文本块 {chunk_count} 个）")
    lines += [
        "",
        "导出时有意省略了原图谱名称，导入方应自行命名。",
        "",
        "## 3. 关系语义与方向",
        "",
        "| relation | 含义 | 方向 |",
        "| --- | --- | --- |",
    ]
    for item in metadata["relation_types"]:
        lines.append(f"| `{item['name']}` | {item['label']} | {item['direction']} |")
    lines += [
        "",
        "## 4. 关键字段",
        "",
        "- `kg_nodes`：每个对象是一个概念或知识点。`name` 为显示名称，"
        "`normalized` 用于同名合并，`aliases` 是 JSON 数组字符串。",
        "- `kg_edges`：每个对象是一条有向关系。`src_id` / `dst_id` 引用 `kg_nodes.id`；"
        "方向必须结合上面的 `relation` 语义解释。",
        "- `confidence`：0~1 的模型自评置信度，不是教师审核后的正确率。",
        "- `evidence`：关系边的资料依据，通常为原句或关键短语。",
    ]
    if graph_only:
        lines.append(
            "- `source_materials` 已清空：本包不包含原始资料，无法据此追溯或重建资料内容。")
    else:
        lines += [
            "- `source_materials` 形如 `,11,12,`，指向 `kg_materials.id`，"
            "可映射到 `files/` 下的原始文件。",
            "- `kg_chunks.extraction_json`：模型对单个文本块的原始抽取结果，"
            "可用于追溯概念的来源上下文。",
            "- `kg_audits` 与 `kg_members` 记录体检历史和协作成员，"
            "他们不是图谱结构本身；迁移到其他平台时可以忽略。",
        ]
    lines += [
        "",
        "## 5. 重建图谱的最小步骤",
        "",
        "1. 读取 `data.json`。",
        "2. 遍历 `kg_nodes` 创建概念节点，忽略旧 `id`，按 `normalized` 去重。",
        "3. 建立旧 `id` 到新节点 ID 的映射。",
        "4. 遍历 `kg_edges` 创建有向关系，用映射重写 `src_id` / `dst_id`，"
        "并按 `relation_types` 保留方向语义。",
    ]
    if graph_only:
        lines.append("5. 本包不包含原始资料与文本块，只能恢复图谱结构，无法恢复资料内容。")
    else:
        lines += [
            "5. 如需追溯出处，把 `source_materials` 中的资料 ID 映射到 "
            "`files/<material_id>/` 下的原始文件。",
            "6. 只关心图结构时，可以忽略 `kg_chunks`、`kg_audits` 与 `kg_members`。",
        ]
    lines += [
        "",
        "## 6. 导入到其他平台的提示",
        "",
        "- 节点唯一性建议使用 `normalized`，而不是显示名称；同一概念可能有别名。",
        "- 关系去重建议使用 `(src_id, dst_id, relation)`。",
    ]
    if graph_only:
        lines.append("- 本包不包含资料与出处信息，无法恢复可重建的原始材料。")
    else:
        lines.append(
            "- 需要保留可解释性时，同时导入 `evidence`、`source_materials` 与 `confidence`。")
    lines += [
        "- 导入 GradeLogy 时，使用系统内「导入迁移包」并填写新图谱名称即可；"
        "导入不会自动调用 AI。",
        "",
        "## 7. 示例",
        "",
        "```json",
        json.dumps({
            "node": {"id": 31, "name": "氧化还原反应", "normalized": "氧化还原反应",
                     "category": "反应类型", "aliases": "[\"氧化还原\"]",
                     "source_materials": example_sources, "confidence": 0.9},
            "edge": {"src_id": 31, "dst_id": 32, "relation": "prerequisite",
                     "evidence": "电子守恒是配平氧化还原反应的基础",
                     "source_materials": example_sources, "confidence": 0.85},
        }, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def export_graph(db, graph_id, upload_dir, output_path, scope="full"):
    """导出指定图谱为 version=2 ZIP，并返回 manifest。

    ``scope="full"`` 导出完整迁移包；``scope="graph"`` 仅导出图谱本身
    （节点与关系），不包含原始资料文件、文本块、体检历史和协作成员，
    节点/关系上的 source_materials 会被清空。

    迁移包不保留原图谱名称，导入时由使用者重新命名；同时写入
    metadata.json 与 README.md，便于其他 LLM 或平台理解包内容。
    """
    if scope not in ("full", "graph"):
        raise KGTransferError(f"未知的导出范围：{scope}")
    graph = _graph(db, graph_id)
    graph["name"] = ""
    data = {"graph": graph}
    for key, table in _DATA_TABLES.items():
        if scope == "graph" and key not in ("nodes", "edges"):
            data[key] = []
            continue
        order = "user_id" if key == "members" else "id"
        data[key] = _many(
            db, f"SELECT * FROM {table} WHERE graph_id=? ORDER BY {order}", (graph_id,))
    if scope == "graph":
        # 没有原始资料时，保留出处 ID 只会产生悬空引用。
        for row in data["nodes"] + data["edges"]:
            row["source_materials"] = ""
        graph["created_by_username"] = None
        data["users"] = {}
    else:
        data["users"] = _username_metadata(
            db, graph, data["materials"], data["audits"], data["members"])

    output = os.path.abspath(os.fspath(output_path))
    parent = os.path.dirname(output)
    os.makedirs(parent, exist_ok=True)
    counts = {"knowledge_graphs": 1}
    counts.update({table: len(data[key]) for key, table in _DATA_TABLES.items()})
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "scope": scope,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "graph_id": graph["id"],
        "counts": counts,
        "materials": [],
    }
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=parent, prefix=".kg-export-", suffix=".tmp", delete=False)
    temporary = handle.name
    handle.close()
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            used_paths = set()
            for material in data["materials"]:
                material_id = material["id"]
                source = _material_source_path(upload_dir, graph_id, material["stored_name"])
                filename = _safe_archive_component(
                    material.get("filename"), f"material_{material_id}")
                archive_path = f"files/{material_id}/{filename}"
                if archive_path in used_paths:
                    raise KGTransferError(f"材料归档路径重复：{archive_path}")
                used_paths.add(archive_path)
                size, sha256 = _write_material(archive, source, archive_path)
                if (int(material.get("size") or 0) != size
                        or str(material.get("sha256") or "").lower() != sha256):
                    raise KGTransferError(
                        f"材料 #{material_id} 的文件大小或 SHA256 与数据库不一致")
                manifest["materials"].append({
                    "material_id": material_id,
                    "archive_path": archive_path,
                    "size": size,
                    "sha256": sha256,
                })
            metadata = _build_metadata(
                graph, counts,
                {str(item["material_id"]): item["archive_path"]
                 for item in manifest["materials"]}, scope)
            readme = _render_readme(metadata)
            archive.writestr(
                "metadata.json",
                json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("README.md", readme.encode("utf-8"))
            archive.writestr(
                "data.json", json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        os.replace(temporary, output)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    result = dict(manifest)
    result["output_path"] = output
    return result


def _validate_archive_name(name):
    if (not name or "\x00" in name or "\\" in name or name.startswith("/")
            or ntpath.isabs(name) or ntpath.splitdrive(name)[0]):
        raise KGTransferError(f"ZIP 包含非法路径：{name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise KGTransferError(f"ZIP 包含路径穿越或重复分隔符：{name!r}")
    if str(PurePosixPath(name)) != name:
        raise KGTransferError(f"ZIP 路径未规范化：{name!r}")


def _read_json(archive, info):
    if info.file_size > MAX_JSON_BYTES:
        raise KGTransferError(f"JSON 文件过大：{info.filename}")
    try:
        with archive.open(info, "r") as source:
            payload = source.read(MAX_JSON_BYTES + 1)
            if source.read(1):
                raise KGTransferError(f"JSON 文件过大：{info.filename}")
    except KGTransferError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise KGTransferError(f"无法读取 ZIP 中的 {info.filename}：{exc}") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise KGTransferError(f"JSON 文件过大：{info.filename}")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise KGTransferError(f"{info.filename} 不是有效的 UTF-8 JSON：{exc}") from exc


def _positive_id(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KGTransferError(f"{label} 必须是正整数，实际为 {value!r}")
    return value


def _row_ids(rows, table):
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise KGTransferError(f"{table} 中存在非对象记录")
        old_id = _positive_id(row.get("id"), f"{table}.id")
        if old_id in seen:
            raise KGTransferError(f"{table} 存在重复 ID：{old_id}")
        seen.add(old_id)
        result.append(old_id)
    return result


def _manifest_counts_valid(manifest, data):
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise KGTransferError("manifest.json 缺少表行数 counts")
    for table, key in _COUNT_TABLES.items():
        expected = 1 if key == "graph" else len(data[key])
        value = counts.get(table)
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            raise KGTransferError(
                f"manifest.json 中 {table} 行数不符：应为 {expected}，实际为 {value!r}")


def _validate_package(archive):
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise KGTransferError(f"ZIP 条目超过 {MAX_ARCHIVE_ENTRIES} 个上限")
    by_name = {}
    total_size = 0
    for info in infos:
        name = info.filename
        _validate_archive_name(name)
        if name in by_name:
            raise KGTransferError(f"ZIP 存在重复条目：{name}")
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if info.is_dir() or file_type == stat.S_IFDIR or file_type == stat.S_IFLNK:
            raise KGTransferError(f"ZIP 不允许目录或符号链接：{name}")
        if file_type not in (0, stat.S_IFREG):
            raise KGTransferError(f"ZIP 包含非普通文件：{name}")
        if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
            raise KGTransferError(f"ZIP 文件大小异常：{name}")
        if name in ("manifest.json", "data.json", "metadata.json") \
                and info.file_size > MAX_JSON_BYTES:
            raise KGTransferError(f"JSON 文件过大：{name}")
        total_size += info.file_size
        if total_size > MAX_TOTAL_BYTES:
            raise KGTransferError("ZIP 解压后总大小超过 2GB 上限")
        by_name[name] = info
    if "manifest.json" not in by_name or "data.json" not in by_name:
        raise KGTransferError("迁移包缺少 manifest.json 或 data.json")

    manifest = _read_json(archive, by_name["manifest.json"])
    data = _read_json(archive, by_name["data.json"])
    if not isinstance(manifest, dict) or not isinstance(data, dict):
        raise KGTransferError("manifest.json 和 data.json 顶层必须是对象")
    version = manifest.get("version")
    if (manifest.get("format") != FORMAT
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version not in SUPPORTED_VERSIONS):
        raise KGTransferError(
            "仅支持 format=gradelogy-kg、version=1 或 2 的迁移包")
    scope = manifest.get("scope", "full")
    if scope not in ("full", "graph"):
        raise KGTransferError("manifest.json 的 scope 无效")
    # metadata.json 是 version=2 新增的可选说明文件；旧包没有也允许导入。
    if "metadata.json" in by_name:
        metadata = _read_json(archive, by_name["metadata.json"])
        if (not isinstance(metadata, dict) or metadata.get("format") != FORMAT
                or metadata.get("version") != version
                or metadata.get("scope", "full") != scope):
            raise KGTransferError(
                "metadata.json 与 manifest.json 的格式、版本或范围不一致")
    if not isinstance(data.get("graph"), dict):
        raise KGTransferError("data.json 缺少 graph 对象")
    graph_id = _positive_id(data["graph"].get("id"), "graph.id")
    if (isinstance(manifest.get("graph_id"), bool)
            or not isinstance(manifest.get("graph_id"), int)
            or manifest.get("graph_id") != graph_id):
        raise KGTransferError("manifest.json 与 data.json 的 graph_id 不一致")
    for key in _DATA_TABLES:
        if not isinstance(data.get(key), list):
            raise KGTransferError(f"data.json 缺少必需的 {key} 数组")
    _manifest_counts_valid(manifest, data)
    for key, table in _DATA_TABLES.items():
        if key == "members":
            for row in data[key]:
                if not isinstance(row, dict):
                    raise KGTransferError("kg_members 中存在非对象记录")
        else:
            _row_ids(data[key], table)

    entries = manifest.get("materials")
    if not isinstance(entries, list) or len(entries) != len(data["materials"]):
        raise KGTransferError("manifest.json 的材料清单与 data.json 不一致")
    material_by_id = {row["id"]: row for row in data["materials"]}
    file_entries = {}
    expected_names = {"manifest.json", "data.json"}
    # version=2 起附带面向人或 LLM 的说明文件；旧包没有也兼容。
    for optional in ("metadata.json", "README.md"):
        if optional in by_name:
            expected_names.add(optional)
    for entry in entries:
        if not isinstance(entry, dict):
            raise KGTransferError("manifest.json 的材料清单存在非对象项")
        material_id = _positive_id(entry.get("material_id"), "material_id")
        if material_id not in material_by_id or material_id in file_entries:
            raise KGTransferError(f"材料清单 ID 无效或重复：{material_id}")
        archive_path = entry.get("archive_path")
        if not isinstance(archive_path, str):
            raise KGTransferError("材料清单缺少 archive_path")
        _validate_archive_name(archive_path)
        if not archive_path.startswith("files/"):
            raise KGTransferError(f"材料归档路径不在 files/ 下：{archive_path}")
        if archive_path in expected_names:
            raise KGTransferError(f"材料归档路径重复：{archive_path}")
        if archive_path not in by_name:
            raise KGTransferError(f"迁移包缺少材料文件：{archive_path}")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise KGTransferError(f"材料 #{material_id} 的 size 无效")
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise KGTransferError(f"材料 #{material_id} 的 sha256 无效")
        material = material_by_id[material_id]
        if material.get("size") != size or material.get("sha256") != sha256:
            raise KGTransferError(f"材料 #{material_id} 在 manifest 与 data 中的元数据不一致")
        if by_name[archive_path].file_size != size:
            raise KGTransferError(f"材料文件大小与清单不符：{archive_path}")
        expected_names.add(archive_path)
        file_entries[material_id] = (archive_path, size, sha256)
    if set(by_name) != expected_names:
        unexpected = sorted(set(by_name) - expected_names)
        raise KGTransferError(f"ZIP 包含非预期文件：{unexpected[:3]}")
    return manifest, data, by_name, file_entries


def _source_ids(value):
    if value in (None, ""):
        return []
    if not isinstance(value, str):
        raise KGTransferError(f"source_materials 必须是字符串：{value!r}")
    result = []
    for part in value.split(","):
        if not part.strip():
            continue
        if not part.strip().isdigit():
            raise KGTransferError(f"source_materials 含非法材料 ID：{value!r}")
        result.append(_positive_id(int(part), "material_id"))
    return result


def _rewrite_sources(value, material_map):
    ids = sorted(set(material_map[old_id] for old_id in _source_ids(value)))
    return "," + ",".join(str(item) for item in ids) + "," if ids else ""


def _parse_audit_json(row, key):
    original = row.get(key)
    if original in (None, ""):
        return None
    if not isinstance(original, str):
        raise KGTransferError(f"体检 #{row.get('id')} 的 {key} 不是 JSON 字符串")
    try:
        return json.loads(original)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise KGTransferError(f"体检 #{row.get('id')} 的 {key} 不是有效 JSON：{exc}") from exc


def _add_scalar_id(container, value, label):
    if value is not None:
        container.add(_positive_id(value, label))


def _add_id_list(container, value, label):
    if value is None:
        return
    if not isinstance(value, list):
        raise KGTransferError(f"{label} 必须是数组")
    for item in value:
        _add_scalar_id(container, item, label)


def _collect_audit_ids(value, node_ids, edge_ids, material_ids):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("node_id", "keep_id", "src_id", "dst_id"):
                _add_scalar_id(node_ids, item, key)
            elif key == "node_ids":
                _add_id_list(node_ids, item, key)
            elif key in ("edge_id", "suggested_edge_id"):
                _add_scalar_id(edge_ids, item, key)
            elif key == "material_id":
                _add_scalar_id(material_ids, item, key)
            elif key == "material_ids":
                _add_id_list(material_ids, item, key)
            elif key == "source_materials":
                material_ids.update(_source_ids(item))
            _collect_audit_ids(item, node_ids, edge_ids, material_ids)
    elif isinstance(value, list):
        for item in value:
            _collect_audit_ids(item, node_ids, edge_ids, material_ids)


def _collect_snapshot_ids(snapshot, node_ids, edge_ids, material_ids):
    if snapshot is None:
        return
    if not isinstance(snapshot, dict):
        raise KGTransferError("体检 snapshot 顶层必须是对象")
    for key in ("nodes", "edges"):
        if key in snapshot and snapshot[key] is not None and not isinstance(snapshot[key], list):
            raise KGTransferError(f"体检 snapshot.{key} 必须是数组")
    for node in snapshot.get("nodes") or []:
        if not isinstance(node, dict):
            raise KGTransferError("体检快照中存在非对象节点")
        _add_scalar_id(node_ids, node.get("id"), "snapshot node id")
        material_ids.update(_source_ids(node.get("source_materials")))
        _collect_audit_ids(node, node_ids, edge_ids, material_ids)
    for edge in snapshot.get("edges") or []:
        if not isinstance(edge, dict):
            raise KGTransferError("体检快照中存在非对象关系")
        _add_scalar_id(edge_ids, edge.get("id"), "snapshot edge id")
        _add_scalar_id(node_ids, edge.get("src_id"), "snapshot src_id")
        _add_scalar_id(node_ids, edge.get("dst_id"), "snapshot dst_id")
        material_ids.update(_source_ids(edge.get("source_materials")))
        _collect_audit_ids(edge, node_ids, edge_ids, material_ids)


def _prepare_audits(audits):
    prepared = []
    node_ids, edge_ids, material_ids = set(), set(), set()
    for row in audits:
        parsed = {key: _parse_audit_json(row, key)
                  for key in ("suggestions", "rejected", "snapshot")}
        for key in ("suggestions", "rejected"):
            _collect_audit_ids(parsed[key], node_ids, edge_ids, material_ids)
        _collect_snapshot_ids(parsed["snapshot"], node_ids, edge_ids, material_ids)
        prepared.append((row, parsed))
    return prepared, node_ids, edge_ids, material_ids


def _make_id_map(db, table, rows, extra_ids=()):
    current_ids = _row_ids(rows, table)
    highest = _one(db, f"SELECT MAX(id) AS value FROM {table}")["value"] or 0
    try:
        sequence = _one(db, "SELECT seq AS value FROM sqlite_sequence WHERE name=?", (table,))
    except sqlite3.OperationalError:
        sequence = None
    if sequence and sequence["value"]:
        highest = max(highest, sequence["value"])
    next_id = int(highest) + 1
    mapping = {}
    # 当前行先分配；仅存在于历史快照/拒绝记录中的 ID 使用其后的保留 ID。
    for old_id in current_ids + sorted(set(extra_ids) - set(current_ids)):
        mapping[old_id] = next_id
        next_id += 1
    return mapping, next_id - 1


def _advance_sequence(db, table, value):
    if not value:
        return
    try:
        db.execute("INSERT OR IGNORE INTO sqlite_sequence(name,seq) VALUES(?,0)", (table,))
        db.execute("UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?",
                   (value, table, value))
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


def _map_scalar(value, mapping, label):
    if value is None:
        return None
    old_id = _positive_id(value, label)
    if old_id not in mapping:
        raise KGTransferError(f"无法重写 {label}={old_id}：缺少 ID 映射")
    return mapping[old_id]


def _map_id_list(value, mapping, label):
    if value is None:
        return None
    if not isinstance(value, list):
        raise KGTransferError(f"{label} 必须是数组")
    return [_map_scalar(item, mapping, label) for item in value]


def _rewrite_audit_value(value, node_map, edge_map, material_map):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("node_id", "keep_id", "src_id", "dst_id"):
                result[key] = _map_scalar(item, node_map, key)
            elif key == "node_ids":
                result[key] = _map_id_list(item, node_map, key)
            elif key in ("edge_id", "suggested_edge_id"):
                result[key] = _map_scalar(item, edge_map, key)
            elif key == "material_id":
                result[key] = _map_scalar(item, material_map, key)
            elif key == "material_ids":
                result[key] = _map_id_list(item, material_map, key)
            elif key == "source_materials":
                result[key] = _rewrite_sources(item, material_map)
            else:
                result[key] = _rewrite_audit_value(item, node_map, edge_map, material_map)
        return result
    if isinstance(value, list):
        return [_rewrite_audit_value(item, node_map, edge_map, material_map)
                for item in value]
    return value


def _rewrite_snapshot(snapshot, node_map, edge_map, material_map):
    if snapshot is None:
        return None
    result = _rewrite_audit_value(snapshot, node_map, edge_map, material_map)
    if "nodes" in result and result["nodes"] is not None:
        result["nodes"] = [
            {**node, "id": _map_scalar(node.get("id"), node_map, "snapshot node id")}
            for node in result["nodes"]]
    if "edges" in result and result["edges"] is not None:
        result["edges"] = [
            {**edge, "id": _map_scalar(edge.get("id"), edge_map, "snapshot edge id")}
            for edge in result["edges"]]
    return result


def _dump_audit_json(original, value):
    if original in (None, ""):
        return original
    return json.dumps(value, ensure_ascii=False)


def _users_by_name(data):
    users = data.get("users") or {}
    if isinstance(users, dict):
        return {str(key): value for key, value in users.items()}
    # 兼容把元数据写成 [{id, username}] 的 version=1 包。
    result = {}
    if isinstance(users, list):
        for row in users:
            if isinstance(row, dict) and "id" in row:
                result[str(row["id"])] = row.get("username")
    return result


def _source_username(row, direct_key, id_key, users):
    return row.get(direct_key) or users.get(str(row.get(id_key)))


def _prepare_import_directory(upload_dir, graph_id):
    root = os.path.realpath(os.path.abspath(os.fspath(upload_dir)))
    os.makedirs(root, exist_ok=True)
    kg_dir = os.path.abspath(os.path.join(root, "kg"))
    os.makedirs(kg_dir, exist_ok=True)
    if os.path.realpath(kg_dir) != kg_dir:
        raise KGTransferError("存储目录中的 kg 不允许是符号链接")
    graph_dir = os.path.join(kg_dir, str(graph_id))
    os.mkdir(graph_dir)
    return graph_dir


def _new_storage_name(filename, used_names):
    match = re.search(r"\.([A-Za-z0-9]{1,10})$", str(filename or ""))
    suffix = "." + match.group(1).lower() if match else ""
    while True:
        name = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(8)}{suffix}"
        if name not in used_names:
            used_names.add(name)
            return name


def _copy_import_file(archive, info, destination, expected_size, expected_sha256):
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, "r") as src, open(destination, "xb") as dst:
            while True:
                block = src.read(COPY_BUFFER_SIZE)
                if not block:
                    break
                size += len(block)
                if size > expected_size or size > MAX_FILE_BYTES:
                    raise KGTransferError(f"材料解压后超过清单大小：{info.filename}")
                digest.update(block)
                dst.write(block)
    except KGTransferError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise KGTransferError(f"读取材料失败 {info.filename}：{exc}") from exc
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise KGTransferError(f"材料文件大小或 SHA256 校验失败：{info.filename}")


def _rollback_import(db, graph_dir, directory_created):
    try:
        db.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
    finally:
        db.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    if directory_created and graph_dir:
        shutil.rmtree(graph_dir, ignore_errors=True)


def import_graph(db, package_path, upload_dir, owner_id, course_id=None, name=None):
    """安全导入迁移包，始终创建新图谱；失败时回滚数据库并清理新文件。

    优先使用调用方传入的 ``name`` 作为新图谱名称；未传入时兼容旧迁移包中
    携带的名称。两者都为空时拒绝导入，避免出现无名图谱。
    """
    owner = _one(db, "SELECT id,username FROM users WHERE id=?", (owner_id,))
    if owner is None:
        raise KGTransferError(f"导入者不存在：{owner_id}")
    owner_id = owner["id"]
    if course_id is not None and _one(db, "SELECT id FROM courses WHERE id=?", (course_id,)) is None:
        raise KGTransferError(f"课程不存在：{course_id}")

    graph_dir = None
    directory_created = False
    savepoint_active = False
    try:
        with zipfile.ZipFile(os.fspath(package_path), "r") as archive:
            _manifest, data, infos, files = _validate_package(archive)
            graph = data["graph"]
            graph_name = (name if name is not None else graph.get("name") or "")
            graph_name = str(graph_name).strip()[:80]
            if not graph_name:
                raise KGTransferError("迁移包未包含图谱名称，请在导入时为新图谱命名")
            materials = data["materials"]
            chunks = data["chunks"]
            nodes = data["nodes"]
            edges = data["edges"]
            audits = data["audits"]
            members = data["members"]
            old_graph_id = graph["id"]
            for key, rows in (("materials", materials), ("chunks", chunks), ("nodes", nodes),
                              ("edges", edges), ("audits", audits), ("members", members)):
                for row in rows:
                    if row.get("graph_id") != old_graph_id:
                        raise KGTransferError(f"{key} 中存在不属于源图谱的记录")
            material_ids = set(_row_ids(materials, "kg_materials"))
            node_ids = set(_row_ids(nodes, "kg_nodes"))
            for chunk in chunks:
                if chunk.get("material_id") not in material_ids:
                    raise KGTransferError(f"文本块 #{chunk['id']} 引用了不存在的材料")
            for edge in edges:
                if edge.get("src_id") not in node_ids or edge.get("dst_id") not in node_ids:
                    raise KGTransferError(f"关系 #{edge['id']} 引用了不存在的节点")

            prepared_audits, audit_node_ids, audit_edge_ids, audit_material_ids = \
                _prepare_audits(audits)
            for row in nodes + edges:
                audit_material_ids.update(_source_ids(row.get("source_materials")))

            db.execute(f"SAVEPOINT {_SAVEPOINT}")
            savepoint_active = True
            cursor = db.execute(
                "INSERT INTO knowledge_graphs(subject,name,description,grade_level,"
                "created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (graph.get("subject") or "", graph_name,
                 graph.get("description") or "", graph.get("grade_level") or "",
                 owner_id, graph.get("created_at"), graph.get("updated_at")))
            new_graph_id = cursor.lastrowid
            if course_id is not None:
                db.execute(
                    "INSERT INTO course_knowledge_graphs(course_id,graph_id) VALUES(?,?)",
                    (course_id, new_graph_id))

            material_map, material_seq = _make_id_map(
                db, "kg_materials", materials, audit_material_ids)
            chunk_map, chunk_seq = _make_id_map(db, "kg_chunks", chunks)
            node_map, node_seq = _make_id_map(db, "kg_nodes", nodes, audit_node_ids)
            edge_map, edge_seq = _make_id_map(db, "kg_edges", edges, audit_edge_ids)
            audit_map, audit_seq = _make_id_map(db, "kg_audits", audits)

            active_users = _many(
                db, "SELECT id,username,role FROM users WHERE active=1")
            by_username = {row["username"]: row for row in active_users}
            users = _users_by_name(data)

            def target_user(row, direct_key, id_key):
                username = _source_username(row, direct_key, id_key, users)
                matched = by_username.get(username)
                return matched["id"] if matched else owner_id

            graph_dir = _prepare_import_directory(upload_dir, new_graph_id)
            directory_created = True
            used_names = set()
            for material in materials:
                old_id = material["id"]
                archive_path, size, sha256 = files[old_id]
                stored = _new_storage_name(material.get("filename"), used_names)
                destination = os.path.join(graph_dir, stored)
                _copy_import_file(archive, infos[archive_path], destination, size, sha256)
                status = material.get("status") or STATUS_PENDING
                running = status == STATUS_BUILDING
                db.execute(
                    "INSERT INTO kg_materials(id,graph_id,filename,stored_name,mime,size,"
                    "sha256,page_count,char_count,status,error,uploaded_by,started_at,"
                    "finished_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (material_map[old_id], new_graph_id, material.get("filename") or "",
                     f"kg/{new_graph_id}/{stored}", material.get("mime") or "", size, sha256,
                     material.get("page_count") or 0, material.get("char_count") or 0,
                     STATUS_PENDING if running else status, material.get("error") or "",
                     target_user(material, "uploaded_by_username", "uploaded_by"),
                     None if running else material.get("started_at"),
                     None if running else material.get("finished_at"),
                     material.get("created_at")))
            _advance_sequence(db, "kg_materials", material_seq)

            for chunk in chunks:
                running = chunk.get("status") == CHUNK_RUNNING
                db.execute(
                    "INSERT INTO kg_chunks(id,graph_id,material_id,page_no,chunk_no,content,"
                    "status,error,extraction_json,started_at,finished_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (chunk_map[chunk["id"]], new_graph_id,
                     material_map[chunk["material_id"]], chunk.get("page_no") or 0,
                     chunk.get("chunk_no") or 0, chunk.get("content") or "",
                     CHUNK_PENDING if running else (chunk.get("status") or CHUNK_PENDING),
                     "" if running else (chunk.get("error") or ""),
                     "" if running else (chunk.get("extraction_json") or ""),
                     None if running else chunk.get("started_at"),
                     None if running else chunk.get("finished_at")))
            _advance_sequence(db, "kg_chunks", chunk_seq)

            for node in nodes:
                db.execute(
                    "INSERT INTO kg_nodes(id,graph_id,name,normalized,category,definition,"
                    "aliases,source_materials,confidence,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (node_map[node["id"]], new_graph_id, node.get("name") or "",
                     node.get("normalized") or "", node.get("category") or "",
                     node.get("definition") or "", node.get("aliases") or "",
                     _rewrite_sources(node.get("source_materials"), material_map),
                     node.get("confidence") or 0, node.get("created_at"),
                     node.get("updated_at")))
            _advance_sequence(db, "kg_nodes", node_seq)

            for edge in edges:
                db.execute(
                    "INSERT INTO kg_edges(id,graph_id,src_id,dst_id,relation,evidence,"
                    "source_materials,confidence,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (edge_map[edge["id"]], new_graph_id, node_map[edge["src_id"]],
                     node_map[edge["dst_id"]], edge.get("relation") or "related",
                     edge.get("evidence") or "",
                     _rewrite_sources(edge.get("source_materials"), material_map),
                     edge.get("confidence") or 0, edge.get("created_at")))
            _advance_sequence(db, "kg_edges", edge_seq)

            for audit, parsed in prepared_audits:
                snapshot = _rewrite_snapshot(
                    parsed["snapshot"], node_map, edge_map, material_map)
                suggestions = _rewrite_audit_value(
                    parsed["suggestions"], node_map, edge_map, material_map)
                rejected = _rewrite_audit_value(
                    parsed["rejected"], node_map, edge_map, material_map)
                db.execute(
                    "INSERT INTO kg_audits(id,graph_id,status,model,error,suggestions,rejected,"
                    "snapshot,operation_id,created_by,created_at,applied_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (audit_map[audit["id"]], new_graph_id, audit.get("status") or "待确认",
                     audit.get("model") or "", audit.get("error") or "",
                     _dump_audit_json(audit.get("suggestions"), suggestions),
                     _dump_audit_json(audit.get("rejected"), rejected),
                     _dump_audit_json(audit.get("snapshot"), snapshot), None,
                     target_user(audit, "created_by_username", "created_by"),
                     audit.get("created_at"), audit.get("applied_at")))
            _advance_sequence(db, "kg_audits", audit_seq)

            member_count = 0
            restored_members = set()
            for member in members:
                username = _source_username(member, "username", "user_id", users)
                matched = by_username.get(username)
                if (not matched or matched["role"] != "teacher"
                        or matched["id"] == owner_id or matched["id"] in restored_members):
                    continue
                role = member.get("role") if member.get("role") in _MEMBER_ROLES else "use"
                db.execute(
                    "INSERT INTO kg_members(graph_id,user_id,role,created_at) VALUES(?,?,?,?)",
                    (new_graph_id, matched["id"], role, member.get("created_at")))
                restored_members.add(matched["id"])
                member_count += 1

            db.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
            savepoint_active = False
            counts = {
                "materials": len(materials), "chunks": len(chunks), "nodes": len(nodes),
                "edges": len(edges), "audits": len(audits), "members": member_count,
            }
            return {"graph_id": new_graph_id, "counts": counts, **counts}
    except zipfile.BadZipFile as exc:
        if savepoint_active:
            _rollback_import(db, graph_dir, directory_created)
        raise KGTransferError(f"不是有效的 ZIP 迁移包：{exc}") from exc
    except Exception:
        if savepoint_active:
            _rollback_import(db, graph_dir, directory_created)
        elif directory_created and graph_dir:
            shutil.rmtree(graph_dir, ignore_errors=True)
        raise
