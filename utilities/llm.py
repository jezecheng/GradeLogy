# -*- coding: utf-8 -*-
"""共享的 OpenAI 兼容调用与教师配置解析（供各 POC 节点复用）。"""
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request

import config

_log = logging.getLogger("gradelogy.ai")

BASE_DIR = config.BASE_DIR
UPLOAD_DIR = config.STORAGE_DIR
DB_PATH = config.DB_PATH

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass


def chat(api_url, api_key, model, content, max_tokens=1500, timeout=180):
    """通用 OpenAI 兼容调用，返回 (reply, finish_reason, error)。失败自动重试（含 429 退避）。

    每次调用记录模型、耗时与结束原因；限流、失败、输出截断记 warning，便于排查额度与 max_tokens 问题。
    timeout: 单次请求超时秒数；整份 PDF 批阅耗时较长，调用方可调大。
    """
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        f"{api_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    started = time.perf_counter()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            choice = data["choices"][0]
            finish = (choice.get("finish_reason") or "").lower()
            _log.info("AI 调用 model=%s %.1fs finish=%s",
                      model, time.perf_counter() - started, finish or "-")
            if finish == "length":
                _log.warning("AI 输出被截断 model=%s max_tokens=%s，考虑调大上限", model, max_tokens)
            return (choice["message"].get("content") or ""), finish, ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code == 429 and attempt < 2:
                _log.warning("AI 限流 429 model=%s，第 %s 次重试", model, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue
            _log.warning("AI 请求失败 model=%s HTTP %s %s", model, e.code, detail[:120])
            return "", "", f"HTTP {e.code} {detail}"
        except Exception as e:
            if attempt < 2:
                _log.warning("AI 请求异常 model=%s %s，重试", model, str(e)[:120])
                time.sleep(2)
                continue
            _log.warning("AI 请求异常 model=%s %s", model, str(e)[:120])
            return "", "", str(e)
    _log.warning("AI 请求多次失败 model=%s", model)
    return "", "", "请求多次失败"


def extract_json(text):
    """从模型回复里抠出第一个完整 JSON 对象（容忍代码块、前后废话、JSON 后的中文解释）。

    按花括号配对扫描并跳过字符串内的括号，比 find/rfind 更稳：
    模型在 JSON 后面补一句"并说明理由"时也能正确截断。
    """
    if not text:
        return None
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def resolve_config(teacher_id=None):
    """解析 AI 配置。不传 teacher_id 时，取 settings 表里第一个配了 api_key 的老师，
    方便 POC 命令行不登录也能直接跑。返回 {api_url, api_key, model, extra}。"""
    cfg = {"api_url": config.AI_API_URL.rstrip("/"), "api_key": config.AI_API_KEY,
           "model": config.AI_MODEL, "extra": ""}
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
    result = {"api_url": (d.get("ai_api_url") or "").rstrip("/"),
              "api_key": d.get("ai_api_key") or "",
              "model": d.get("ai_model") or "",
              "extra": (d.get("ai_prompt") or "").strip()}
    # 环境变量优先：容器部署时 Key 走 secrets/env，不落数据库
    if config.AI_API_URL:
        result["api_url"] = config.AI_API_URL.rstrip("/")
    if config.AI_API_KEY:
        result["api_key"] = config.AI_API_KEY
    if config.AI_MODEL:
        result["model"] = config.AI_MODEL
    return result


def resolve_cli_config(args):
    """CLI 公共入口：--teacher 可选；校验配置完整，不完整则退出并提示。"""
    cfg = resolve_config(getattr(args, "teacher", None))
    if not cfg["api_url"] or not cfg["api_key"]:
        raise SystemExit("未在 settings 表找到可用的 AI 配置（api_url/api_key），"
                         "请先登录系统到「AI 设置」保存，或检查 --teacher 参数。")
    return cfg
