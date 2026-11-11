# -*- coding: utf-8 -*-
"""共享的 OpenAI 兼容调用与教师配置解析（供各 POC 节点复用）。"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "data.sqlite")

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass


def chat(api_url, api_key, model, content, max_tokens=1500):
    """通用 OpenAI 兼容调用，返回 (reply, finish_reason, error)。失败自动重试（含 429 退避）。"""
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        f"{api_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            choice = data["choices"][0]
            return (choice["message"].get("content") or ""),\
                    (choice.get("finish_reason") or "").lower(), ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return "", "", f"HTTP {e.code} {detail}"
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            return "", "", str(e)
    return "", "", "请求多次失败"


def extract_json(text):
    """从模型回复里抠出 JSON 对象（容忍 Markdown 代码块和前后废话）。"""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def resolve_config(teacher_id=None):
    """解析 AI 配置。不传 teacher_id 时，取 settings 表里第一个配了 api_key 的老师，
    方便 POC 命令行不登录也能直接跑。返回 {api_url, api_key, model, extra}。"""
    cfg = {"api_url": "", "api_key": "", "model": "", "extra": ""}
    if not os.path.exists(DB_PATH):
        return cfg
    con = sqlite3.connect(DB_PATH)
    try:
        if teacher_id is None:
            row = con.execute(
                "SELECT teacher_id FROM settings WHERE key='ai_api_key' AND value!='' "
                "ORDER BY teacher_id LIMIT 1").fetchone()
            if not row:
                return cfg
            teacher_id = row[0]
        rows = con.execute("SELECT key, value FROM settings WHERE teacher_id=?",
                           (teacher_id,)).fetchall()
    finally:
        con.close()
    d = dict(rows)
    return {"api_url": (d.get("ai_api_url") or "").rstrip("/"),
            "api_key": d.get("ai_api_key") or "",
            "model": d.get("ai_model") or "",
            "extra": (d.get("ai_prompt") or "").strip()}


def resolve_cli_config(args):
    """CLI 公共入口：--teacher 可选；校验配置完整，不完整则退出并提示。"""
    cfg = resolve_config(getattr(args, "teacher", None))
    if not cfg["api_url"] or not cfg["api_key"]:
        raise SystemExit("未在 settings 表找到可用的 AI 配置（api_url/api_key），"
                         "请先登录系统到「AI 设置」保存，或检查 --teacher 参数。")
    return cfg
