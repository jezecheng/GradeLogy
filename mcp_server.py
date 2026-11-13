# -*- coding: utf-8 -*-
"""GradeLogy MCP 服务器（stdio）：把课程数据以只读工具的形式提供给 AI 客户端。

用法：在 MCP 客户端（Trae / Claude Desktop / Cursor）里配置

    {
      "mcpServers": {
        "gradelogy": {
          "command": "/path/to/.venv/bin/python",
          "args": ["/path/to/mcp_server.py"],
          "env": { "GRADELOGY_TEACHER": "teacher1" }
        }
      }
    }

只对老师账号开放：启动时校验 users.role='teacher'，否则直接退出。
所有查询都按该老师参与的课程过滤，不提供任何写操作。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from utilities import assistant

SERVER_INFO = {"name": "gradelogy", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"


def _teacher_id():
    username = os.environ.get("GRADELOGY_TEACHER", "").strip()
    if not username:
        sys.stderr.write("缺少环境变量 GRADELOGY_TEACHER（老师登录名），例如 GRADELOGY_TEACHER=teacher1\n")
        sys.exit(1)
    if not os.path.exists(config.DB_PATH):
        sys.stderr.write(f"数据库不存在：{config.DB_PATH}\n")
        sys.exit(1)
    con = sqlite3.connect(config.DB_PATH)
    try:
        row = con.execute("SELECT id, role FROM users WHERE username=?", (username,)).fetchone()
    finally:
        con.close()
    if not row:
        sys.stderr.write(f"账号不存在：{username}\n")
        sys.exit(1)
    if row[1] != "teacher":
        sys.stderr.write(f"该 MCP 服务器只对老师开放，{username} 的角色是 {row[1]}\n")
        sys.exit(1)
    return row[0]


def _send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(req, teacher_id):
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": assistant.TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        text = assistant.call_tool(name, args, teacher_id)
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}],
            "isError": False}}
    if method.startswith("notifications/"):
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"未实现的方法：{method}"}}


def main():
    teacher_id = _teacher_id()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "JSON 解析失败"}})
            continue
        try:
            resp = _handle(req, teacher_id)
        except Exception as e:  # 单次失败不应让整个服务退出
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if resp:
            _send(resp)


if __name__ == "__main__":
    main()
