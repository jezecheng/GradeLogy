# -*- coding: utf-8 -*-
"""知识图谱独立迁移命令行：python -m utilities.kg_cli --help。"""
import argparse
import os
import sqlite3
import sys

from utilities.kg_transfer import (
    KGTransferError,
    export_graph,
    import_graph,
    inspect_graph,
)

_ERROR_KINDS = {"material": "材料", "chunk": "文本块", "audit": "体检"}


def _parser():
    parser = argparse.ArgumentParser(description="GradeLogy 知识图谱检查与跨实例迁移")
    parser.add_argument("--db", metavar="PATH", help="SQLite 数据库路径（默认读取 config.DB_PATH）")
    parser.add_argument("--storage", metavar="PATH",
                        help="附件存储根目录（默认读取 config.STORAGE_DIR）")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="查看图谱数量、状态与最近错误")
    status.add_argument("graph_id", type=int, metavar="GID")

    export = commands.add_parser("export", help="导出完整迁移 ZIP")
    export.add_argument("graph_id", type=int, metavar="GID")
    export.add_argument("--output", required=True, metavar="PATH", help="输出 ZIP 路径")

    restore = commands.add_parser("import", help="导入迁移 ZIP并创建新图谱")
    restore.add_argument("package", metavar="PACKAGE", help="迁移 ZIP 路径")
    restore.add_argument("--owner", required=True, metavar="USERNAME", help="新图谱所有者用户名")
    restore.add_argument("--name", metavar="NAME",
                         help="新图谱名称；新迁移包不携带名称，需指定")
    restore.add_argument("--course-id", type=int, metavar="ID", help="可选：显式绑定课程 ID")
    return parser


def _print_status(result):
    graph = result["graph"]
    counts = result["counts"]
    print(f"图谱 #{graph['id']}：{graph['name']}")
    print(f"学科：{graph.get('subject') or '-'}；年级：{graph.get('grade_level') or '-'}")
    print(f"创建者用户 ID：{graph.get('created_by')}；更新时间：{graph.get('updated_at') or '-'}")
    print("数量：" + "；".join(f"{label} {counts[key]}" for key, label in (
        ("nodes", "节点"), ("edges", "关系"), ("materials", "材料"),
        ("chunks", "文本块"), ("audits", "体检"), ("members", "成员"))))
    labels = {"materials": "材料", "chunks": "文本块", "audits": "体检"}
    for key, statuses in result["status_counts"].items():
        print(f"{labels[key]}状态：")
        if not statuses:
            print("  （无记录）")
        for status, count in statuses.items():
            print(f"  {status}：{count}")
    print("最近错误：")
    if not result["recent_errors"]:
        print("  （无）")
    for row in result["recent_errors"]:
        timestamp = f" [{row['timestamp']}]" if row.get("timestamp") else ""
        material = f"（材料 #{row['material_id']}）" if row.get("material_id") else ""
        print(f"  {_ERROR_KINDS.get(row['kind'], row['kind'])} #{row['id']}{material}{timestamp}："
              f"{row.get('error') or ''}")


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.db or not args.storage:
        import config as cfg
        db_path = args.db or cfg.DB_PATH
        storage_path = args.storage or cfg.STORAGE_DIR
    else:
        db_path, storage_path = args.db, args.storage
    db_path = os.path.abspath(os.path.expanduser(db_path))
    storage_path = os.path.abspath(os.path.expanduser(storage_path))

    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        print(f"错误：无法连接数据库 {db_path}：{exc}", file=sys.stderr)
        return 1
    connection.row_factory = sqlite3.Row
    try:
        if args.command == "status":
            _print_status(inspect_graph(connection, args.graph_id))
            return 0
        if args.command == "export":
            result = export_graph(connection, args.graph_id, storage_path, args.output)
            print(f"导出成功：{result['output_path']}")
            print(f"材料 {result['counts']['kg_materials']} 份；"
                  f"节点 {result['counts']['kg_nodes']} 个；关系 {result['counts']['kg_edges']} 条")
            return 0
        if args.command == "import":
            owner = connection.execute(
                "SELECT id FROM users WHERE username=?", (args.owner,)).fetchone()
            if owner is None:
                raise KGTransferError(f"导入者用户名不存在：{args.owner}")
            result = import_graph(
                connection, args.package, storage_path, owner["id"],
                args.course_id, name=args.name)
            print(f"导入成功：新图谱 #{result['graph_id']}")
            print("；".join(f"{label} {result['counts'][key]}" for key, label in (
                ("materials", "材料"), ("chunks", "文本块"), ("nodes", "节点"),
                ("edges", "关系"), ("audits", "体检"), ("members", "恢复成员"))))
            if args.course_id is not None:
                print(f"已绑定课程 #{args.course_id}")
            return 0
        parser.error("未知命令")
    except (KGTransferError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
