# -*- coding: utf-8 -*-
"""共享的 OpenAI 兼容调用与教师配置解析（供各 POC 节点复用）。"""
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

import config
from utilities.ai_debug import sanitize_url

_log = logging.getLogger("gradelogy.ai")

BASE_DIR = config.BASE_DIR
UPLOAD_DIR = config.STORAGE_DIR

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass


def _usage_int(value):
    """token 字段缺失或不是数字时保持未知（None），不能误记成 0。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _emit_usage(hook, event):
    """用量落库失败不应触发模型请求重试，也不应覆盖原始调用结果。"""
    if not hook:
        return None
    try:
        return hook(event)
    except Exception:
        _log.exception("AI 用量记录失败 model=%s attempt=%s",
                       event.get("model"), event.get("attempt"))
        return None


def _usage_event(usage, model, attempt, latency_ms, finish_reason="", status="success",
                 error="", provider_request_id=None, response_model=""):
    """把 OpenAI 兼容 usage 整理为稳定的内部结构，同时保留原始 usage 供后续对账。"""
    usage = usage if isinstance(usage, dict) else None
    prompt = _usage_int(usage.get("prompt_tokens")) if usage else None
    completion = _usage_int(usage.get("completion_tokens")) if usage else None
    total = _usage_int(usage.get("total_tokens")) if usage else None
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    prompt_details = usage.get("prompt_tokens_details") if usage else None
    completion_details = usage.get("completion_tokens_details") if usage else None
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(completion_details, dict):
        completion_details = {}
    return {
        "attempt": attempt,
        "provider_request_id": provider_request_id,
        "model": response_model or model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": _usage_int(prompt_details.get("cached_tokens")),
        "reasoning_tokens": _usage_int(completion_details.get("reasoning_tokens")),
        "latency_ms": latency_ms,
        "finish_reason": finish_reason,
        "usage": usage,
        "status": status,
        "error": error,
    }


def _safe_headers(headers):
    """只保留排查有用的响应头，避免 Set-Cookie 等秘密进入日志。"""
    if not headers:
        return {}
    allowed = {"content-type", "x-request-id", "retry-after"}
    result = {}
    for key in headers.keys():
        lower = key.lower()
        if lower in allowed or lower.startswith("x-ratelimit-"):
            result[lower] = headers.get(key)
    return result


def _debug_fields(event, *, call_key, request_kind, stage, url, method,
                  api_key, request_body=None, response_body=None, http_status=None,
                  response_headers=None, exception=None):
    event.update({
        "call_key": call_key,
        "request_kind": request_kind,
        "stage": stage,
        "http_status": http_status,
        "debug": {
            "url": sanitize_url(url),
            "method": method,
            "response_headers": response_headers or {},
            "exception": ({"type": type(exception).__name__, "message": str(exception)}
                          if exception is not None else None),
        },
        "_debug_secrets": (api_key,),
    })
    if request_body is not None:
        event["request_body"] = request_body
    if response_body is not None:
        event["response_body"] = response_body
    return event


def chat_completion(api_url, api_key, payload, timeout=180, retries=3,
                    usage_hook=None, report_running=False, stage="", log_events=True):
    """发送完整 OpenAI payload，返回 (message, finish_reason, error) 并保留逐次原始现场。"""
    del report_running  # 兼容旧参数；所有带 hook 的请求都在发送前登记 running。
    model = payload.get("model") or ""
    body = json.dumps(payload).encode()
    url = f"{api_url}/chat/completions"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    started = time.perf_counter()
    call_key = uuid.uuid4().hex
    for attempt in range(retries):
        attempt_started = time.perf_counter()
        response_body = None
        response_headers = {}
        http_status = None

        def event(usage=None, finish="", status="success", error="", exception=None,
                  provider_request_id=None, response_model=""):
            latency = round((time.perf_counter() - attempt_started) * 1000)
            item = _usage_event(
                usage, model, attempt + 1, latency if status != "running" else None,
                finish, status=status, error=error,
                provider_request_id=provider_request_id, response_model=response_model)
            return _debug_fields(
                item, call_key=call_key, request_kind="chat_completion", stage=stage,
                url=url, method="POST", api_key=api_key,
                request_body=body, response_body=response_body, http_status=http_status,
                response_headers=response_headers, exception=exception)

        event_id = _emit_usage(usage_hook, event(status="running"))

        def record(item):
            if event_id is not None:
                item["usage_event_id"] = event_id
            _emit_usage(usage_hook, item)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response_body = resp.read()
                response_headers = _safe_headers(resp.headers)
                http_status = getattr(resp, "status", None) or resp.getcode()
                data = json.loads(response_body)
                header_request_id = resp.headers.get("x-request-id")
            choice = data["choices"][0]
            message = choice["message"]
            finish = (choice.get("finish_reason") or "").lower()
            record(event(
                data.get("usage"), finish=finish,
                provider_request_id=data.get("id") or header_request_id,
                response_model=data.get("model") or ""))
            if log_events:
                _log.info("AI 调用 model=%s %.1fs finish=%s",
                          model, time.perf_counter() - started, finish or "-")
            if finish == "length" and log_events:
                _log.warning("AI 输出被截断 model=%s max_tokens=%s，考虑调大上限",
                             model, payload.get("max_tokens"))
            return message, finish, ""
        except urllib.error.HTTPError as e:
            response_body = e.read()
            response_headers = _safe_headers(e.headers)
            http_status = e.code
            detail = response_body.decode("utf-8", "replace")
            error = f"HTTP {e.code} {detail[:200]}"
            record(event(
                status="error", error=error, exception=e,
                provider_request_id=e.headers.get("x-request-id") if e.headers else None))
            if e.code == 429 and attempt < retries - 1:
                if log_events:
                    _log.warning("AI 限流 429 model=%s，第 %s 次重试", model, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue
            if log_events:
                _log.warning("AI 请求失败 model=%s HTTP %s %s", model, e.code, detail[:120])
            return {}, "", error
        except Exception as e:
            record(event(status="error", error=str(e), exception=e))
            if attempt < retries - 1:
                if log_events:
                    _log.warning("AI 请求异常 model=%s %s，重试", model, str(e)[:120])
                time.sleep(2)
                continue
            if log_events:
                _log.warning("AI 请求异常 model=%s %s", model, str(e)[:120])
            return {}, "", str(e)
    if log_events:
        _log.warning("AI 请求多次失败 model=%s", model)
    return {}, "", "请求多次失败"


def chat(api_url, api_key, model, content, max_tokens=32768, timeout=180, retries=3,
         usage_hook=None, report_running=False, stage=""):
    """通用 OpenAI 兼容调用，返回 (reply, finish_reason, error)。失败自动重试（含 429 退避）。"""
    message, finish, error = chat_completion(
        api_url, api_key,
        {"model": model, "messages": [{"role": "user", "content": content}],
         "max_tokens": max_tokens},
        timeout=timeout, retries=retries, usage_hook=usage_hook,
        report_running=report_running, stage=stage)
    return (message.get("content") or ""), finish, error


def get_json(url, api_key, timeout=20, usage_hook=None, stage=""):
    """记录一次无 body 的鉴权 GET；成功返回 JSON，失败保留现场后继续抛出原异常。"""
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    started = time.perf_counter()
    response_body = None
    response_headers = {}
    http_status = None
    call_key = uuid.uuid4().hex

    def event(status="success", error="", exception=None, usage=None, provider_request_id=None):
        item = _usage_event(
            usage, "", 1, round((time.perf_counter() - started) * 1000)
            if status != "running" else None,
            status=status, error=error, provider_request_id=provider_request_id)
        return _debug_fields(
            item, call_key=call_key, request_kind="models", stage=stage,
            url=url, method="GET", api_key=api_key, response_body=response_body,
            http_status=http_status, response_headers=response_headers, exception=exception)

    event_id = _emit_usage(usage_hook, event(status="running"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read()
            response_headers = _safe_headers(resp.headers)
            http_status = getattr(resp, "status", None) or resp.getcode()
            data = json.loads(response_body.decode("utf-8"))
            provider_request_id = data.get("id") or resp.headers.get("x-request-id") \
                if isinstance(data, dict) else resp.headers.get("x-request-id")
        item = event(provider_request_id=provider_request_id)
        if event_id is not None:
            item["usage_event_id"] = event_id
        _emit_usage(usage_hook, item)
        return data
    except urllib.error.HTTPError as e:
        response_body = e.read()
        response_headers = _safe_headers(e.headers)
        http_status = e.code
        item = event(status="error", error=f"HTTP {e.code} {e.reason}", exception=e,
                     provider_request_id=e.headers.get("x-request-id") if e.headers else None)
        if event_id is not None:
            item["usage_event_id"] = event_id
        _emit_usage(usage_hook, item)
        raise
    except Exception as e:
        item = event(status="error", error=str(e), exception=e)
        if event_id is not None:
            item["usage_event_id"] = event_id
        _emit_usage(usage_hook, item)
        raise


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
    """解析 AI 配置。不传 teacher_id 时，取第一个配了 api_key 的老师，
    方便 POC 命令行不登录也能直接跑。返回 {api_url, api_key, model, extra}。

    优先级规则统一在 ai_config 中实现，页面、批改、命令行共用一份。
    """
    from ai_config import resolve

    return resolve(teacher_id)


def resolve_cli_config(args):
    """CLI 公共入口：--teacher 可选；校验配置完整，不完整则退出并提示。"""
    cfg = resolve_config(getattr(args, "teacher", None))
    if not cfg["api_url"] or not cfg["api_key"]:
        raise SystemExit("未在 settings 表找到可用的 AI 配置（api_url/api_key），"
                         "请先登录系统到「AI 设置」保存，或检查 --teacher 参数。")
    return cfg
