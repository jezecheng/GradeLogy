# -*- coding: utf-8 -*-
"""知识图谱资料归档减容。

把已建图资料的源文件从 uploads/ 移到独立归档目录，仅保留建图历史
（元数据、文本块、节点、关系、体检记录）；归档文件不进系统备份，
但可随时还原。归档绝不调用 delete_material，因此不会回收节点/边。

本模块只依赖标准库 + config（取归档/上传根目录的默认值）；路径解析
与文件移动逻辑集中在此，所有读资料源文件的入口都应走 material_file_path。
"""
import os
import shutil

import config
from utilities.knowledge_graph import STATUS_DONE, STATUS_FAILED

# 允许归档的资料状态：建图中的资料正在被后台写入，归档会破坏建图流程。
ARCHIVABLE_STATUSES = (STATUS_DONE, STATUS_FAILED)


def _basename(stored_name):
    """从 stored_name（形如 kg/<gid>/<file>）取最后一段文件名。"""
    return str(stored_name or "").replace("\\", "/").rsplit("/", 1)[-1]


def material_file_path(material, archive_root=None, upload_root=None):
    """返回资料源文件的当前绝对路径。

    archived_at 非空时文件已移到归档目录，否则仍在 uploads 下。
    只做路径拼接，不校验文件是否存在——调用方按需用 os.path.exists 判断，
    以便对「恢复后归档文件缺失」做优雅降级。
    """
    material = _row_to_dict(material)
    if archive_root is None:
        archive_root = config.ARCHIVE_DIR
    if upload_root is None:
        upload_root = config.STORAGE_DIR
    graph_id = material["graph_id"]
    basename = _basename(material.get("stored_name"))
    if material.get("archived_at"):
        return os.path.join(archive_root, "kg", str(graph_id), basename)
    return os.path.join(upload_root, "kg", str(graph_id), basename)


def _move_file(src, target):
    """把 src 移到 target；目标已存在时先删，避免 shutil.move 抛错。"""
    if os.path.abspath(src) == os.path.abspath(target):
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        os.remove(target)
    shutil.move(src, target)


def _resolve_roots(archive_root, upload_root):
    return (archive_root if archive_root is not None else config.ARCHIVE_DIR,
            upload_root if upload_root is not None else config.STORAGE_DIR)


def archive_material(db, material, commit=True, archive_root=None, upload_root=None):
    """把单份资料的源文件移入归档目录，并标记 archived_at。

    仅允许状态为「完成 / 失败」的资料归档；建图中 / 待建图拒绝。
    不删除 kg_chunks / kg_nodes / kg_edges / kg_materials 行。
    返回 (True, "") 或 (False, 错误说明)。
    """
    archive_root, upload_root = _resolve_roots(archive_root, upload_root)
    material = _row_to_dict(material)
    material_id = material["id"]
    status = material.get("status") or ""
    if status not in ARCHIVABLE_STATUSES:
        return (False,
                f"资料 #{material_id} 状态为「{status}」，仅已完成或失败的资料可归档")
    if material.get("archived_at"):
        return (False, f"资料 #{material_id} 已归档，无需重复归档")
    src = material_file_path(material, archive_root, upload_root)
    if not os.path.isfile(src):
        return (False, f"资料 #{material_id} 源文件不存在，无法归档")
    target = material_file_path(
        {**dict(material), "archived_at": "1"},  # 强制按归档路径拼接目标
        archive_root, upload_root)
    try:
        _move_file(src, target)
    except OSError as exc:
        return (False, f"资料 #{material_id} 归档失败：{exc}")
    db.execute("UPDATE kg_materials SET archived_at=CURRENT_TIMESTAMP WHERE id=?",
               (material_id,))
    if commit:
        db.commit()
    return (True, "")


def restore_material_archive(db, material, commit=True,
                             archive_root=None, upload_root=None):
    """把已归档资料的源文件移回 uploads，并清空 archived_at。

    归档文件缺失时返回明确错误（恢复备份后常见，调用方应优雅提示）。
    返回 (True, "") 或 (False, 错误说明)。
    """
    archive_root, upload_root = _resolve_roots(archive_root, upload_root)
    material = _row_to_dict(material)
    material_id = material["id"]
    if not material.get("archived_at"):
        return (False, f"资料 #{material_id} 未归档，无需还原")
    src = material_file_path(material, archive_root, upload_root)
    if not os.path.isfile(src):
        return (False, "归档文件不存在，无法还原")
    target = material_file_path(
        {**dict(material), "archived_at": ""},
        archive_root, upload_root)
    try:
        _move_file(src, target)
    except OSError as exc:
        return (False, f"资料 #{material_id} 还原失败：{exc}")
    db.execute("UPDATE kg_materials SET archived_at='' WHERE id=?", (material_id,))
    if commit:
        db.commit()
    return (True, "")


def _row_to_dict(row):
    if isinstance(row, dict):
        return row
    return {key: row[key] for key in row.keys()}


def bulk_archive_materials(db, graph_id, material_ids, restore=False,
                           archive_root=None, upload_root=None):
    """批量归档或还原，整体事务一致性。

    先校验全部 id 属于该图且满足前置条件（归档需状态合法 + 源文件存在；
    还原需已归档 + 归档文件存在），再逐条执行；任一条失败则回滚数据库
    并返回 (success_count, [失败说明])。单条函数以 commit=False 调用，
    全部成功后统一 commit。
    """
    archive_root, upload_root = _resolve_roots(archive_root, upload_root)
    raw_ids = [i for i in (material_ids or []) if str(i).strip().lstrip("-").isdigit()]
    ids = []
    for item in raw_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    if not ids:
        return (0, ["未选择任何资料"])
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT * FROM kg_materials WHERE id IN ({placeholders})", ids).fetchall()
    materials = [_row_to_dict(r) for r in rows]
    found = {m["id"] for m in materials}
    failures = [f"资料不存在：{mid}" for mid in ids if mid not in found]
    owned = []
    for m in materials:
        if m["graph_id"] != graph_id:
            failures.append(f"资料 #{m['id']} 不属于该图谱")
        else:
            owned.append(m)
    if failures:
        return (0, failures)

    # 前置校验：把能预见的失败提前挡住，尽量保证后续移动全部成功。
    for m in owned:
        if restore:
            if not m.get("archived_at"):
                return (0, [f"资料 #{m['id']} 未归档，无需还原"])
            if not os.path.isfile(material_file_path(m, archive_root, upload_root)):
                return (0, [f"资料 #{m['id']} 归档文件不存在，无法还原"])
        else:
            status = m.get("status") or ""
            if status not in ARCHIVABLE_STATUSES:
                return (0, [f"资料 #{m['id']} 状态为「{status}」，"
                            "仅已完成或失败的资料可归档"])
            if m.get("archived_at"):
                return (0, [f"资料 #{m['id']} 已归档，无需重复归档"])
            if not os.path.isfile(material_file_path(m, archive_root, upload_root)):
                return (0, [f"资料 #{m['id']} 源文件不存在，无法归档"])

    success = 0
    failures = []
    for m in owned:
        if restore:
            ok, err = restore_material_archive(
                db, m, commit=False, archive_root=archive_root, upload_root=upload_root)
        else:
            ok, err = archive_material(
                db, m, commit=False, archive_root=archive_root, upload_root=upload_root)
        if ok:
            success += 1
        else:
            failures.append(err)
            break
    if failures:
        db.rollback()
        return (success, failures)
    db.commit()
    return (success, [])


def archive_savings(materials):
    """已归档资料的 size 总和（字节），供 UI 显示节省空间。"""
    total = 0
    for m in materials:
        if m.get("archived_at"):
            try:
                total += int(m.get("size") or 0)
            except (TypeError, ValueError):
                pass
    return total
