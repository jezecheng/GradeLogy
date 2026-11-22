# -*- coding: utf-8 -*-
"""模型目录与 TokenHub 模型/价目同步。

内置的按「API 入口」分组的模型清单、公开价目页抓取与解析、老师维度的模型清单
同步都收在这里；app.py 只调用 build_model_rows / sync_models_for_teacher 等入口。

依赖方向：本模块只依赖 config / db / utilities.llm，不反向导入 app，避免循环导入。
"""
import gzip
import json
import logging
import os
import re
import ssl
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser

import config as cfg
from db import DB_PATH, database_path
from utilities import llm

try:
    import fcntl  # 仅类 Unix 可用，用于保证每日同步只在一个进程里跑
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger("gradelogy")

# 按「API 入口（基地址）」分组的模型清单。价格单位为元/百万 token，来源为各服务商公开价目；
# 只有腾讯云 TokenHub 的价目已核对，其余入口价格字段留空，页面显示「未收录」。
# TokenHub 的 /v1/models 只返回模型 ID、不返回价格，因此每天同步模型清单时会同时抓取公开价目页，
# 把适合图片批改的多模态/视觉模型价格写入 model_prices；这里是内置兜底清单。
MODEL_CATALOG = [
    {
        "name": "腾讯云 TokenHub",
        "base_url": "https://tokenhub.tencentmaas.com/v1",
        "hosts": ["tokenhub.tencentmaas.com"],
        "price_source": "腾讯云 TokenHub 公开价目",
        "models": [
            {"model": "deepseek-v4.1-flash", "vendor": "DeepSeek", "tags": ["图片理解", "1M 上下文", "性价比高"],
             "price_in": 1.0, "price_out": 4.0, "note": "适合图片作业批改的高性价比模型"},
            {"model": "deepseek-v4.1-flash-1017", "vendor": "DeepSeek", "tags": ["图片理解", "1M 上下文"],
             "price_in": 1.0, "price_out": 4.0, "note": "DeepSeek-V4.1-Flash 版本别名"},
            {"model": "deepseek/deepseek-flash", "vendor": "DeepSeek", "tags": ["图片理解", "原厂直供"],
             "price_in": 1.0, "price_out": 4.0, "note": "原厂直供别名，价格与 deepseek-v4.1-flash 一致"},
            {"model": "deepseek-v4-flash-vision-exp", "vendor": "DeepSeek", "tags": ["图片理解", "视觉实验版"],
             "price_in": 1.0, "price_out": 4.0, "note": "视觉实验版，适合图片批改"},
            {"model": "deepseek/deepseek-v4-flash-vision-exp", "vendor": "DeepSeek", "tags": ["图片理解", "原厂直供"],
             "price_in": 1.0, "price_out": 4.0, "note": "视觉实验版原厂直供别名"},
            {"model": "deepseek-v4-flash-202605", "vendor": "DeepSeek", "tags": ["图片理解"],
             "price_in": 1.0, "price_out": 4.0, "note": "DeepSeek-V4-Flash 正式版别名"},
            {"model": "deepseek/deepseek-v4-flash-0731", "vendor": "DeepSeek", "tags": ["图片理解"],
             "price_in": 1.0, "price_out": 4.0, "note": "DeepSeek-V4-Flash 0731 别名"},
            {"model": "deepseek/deepseek-v4-flash", "vendor": "DeepSeek", "tags": ["图片理解"],
             "price_in": 1.0, "price_out": 4.0, "note": "DeepSeek-V4-Flash 原厂直供别名"},
            {"model": "deepseek-v4-pro", "vendor": "DeepSeek", "tags": ["图片理解", "旗舰", "1M 上下文"],
             "price_in": 12.0, "price_out": 24.0, "note": "推理能力更强，但批阅成本明显更高"},
            {"model": "deepseek-v4-pro-0813", "vendor": "DeepSeek", "tags": ["图片理解", "旗舰"],
             "price_in": 4.5, "price_out": 13.5, "note": "0813 正式版，价格低于默认 deepseek-v4-pro"},
            {"model": "deepseek-v4-pro-202606", "vendor": "DeepSeek", "tags": ["图片理解", "旗舰"],
             "price_in": 4.5, "price_out": 13.5, "note": "DeepSeek-V4-Pro 原厂直供版本别名"},
            {"model": "deepseek/deepseek-v4-pro-0813", "vendor": "DeepSeek", "tags": ["图片理解", "原厂直供"],
             "price_in": 4.5, "price_out": 13.5, "note": "DeepSeek-V4-Pro 0813 原厂直供别名"},
            {"model": "glm-5.3-flash", "vendor": "智谱", "tags": ["图片理解", "最便宜", "128k 上下文"],
             "price_in": 0.8, "price_out": 2.8, "note": "TokenHub 上价格很低的多模态模型"},
            {"model": "glm-5v-turbo", "vendor": "智谱", "tags": ["图片理解", "视觉模型"],
             "price_in": 5.0, "price_out": 22.0, "note": "智谱视觉理解模型，适合图片批改"},
            {"model": "minimax-m3.1-flash", "vendor": "MiniMax", "tags": ["图片理解", "1M 上下文"],
             "price_in": 1.0, "price_out": 4.0, "note": "与 DeepSeek-V4.1-Flash 同价，1M 上下文"},
            {"model": "minimax-m3", "vendor": "MiniMax", "tags": ["图片理解"],
             "price_in": 2.1, "price_out": 8.4, "note": "MiniMax 多模态模型"},
            {"model": "kimi-k3.1-pro", "vendor": "Kimi (月之暗面)", "tags": ["图片+视频", "旗舰", "1M 上下文"],
             "price_in": 13.0, "price_out": 68.0, "note": "支持视频输入，一般作业批阅用不上"},
            {"model": "kimi-k3.1-pro-highspeed", "vendor": "Kimi (月之暗面)", "tags": ["图片+视频", "高速"],
             "price_in": 26.0, "price_out": 136.0, "note": "高速版价格更高，普通批改不推荐"},
            {"model": "hy-vision-2.0-instruct", "vendor": "腾讯混元", "tags": ["图片理解", "多模态理解"],
             "price_in": 7.5, "price_out": 17.5, "note": "混元多模态视觉理解模型"},
            {"model": "hy-vision-1.5-thinking", "vendor": "腾讯混元", "tags": ["图片理解", "多模态理解"],
             "price_in": 3.0, "price_out": 9.0, "note": "混元视觉思考模型"},
            {"model": "hy-vision-video", "vendor": "腾讯混元", "tags": ["图片+视频", "多模态理解"],
             "price_in": 3.0, "price_out": 9.0, "note": "支持视觉和视频理解"},
            {"model": "yt-vita", "vendor": "右糖", "tags": ["图片理解", "多模态理解"],
             "price_in": 1.2, "price_out": 3.5, "note": "多模态理解模型，价格较低"},
        ],
    },
    {
        "name": "智谱 AI",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "hosts": ["open.bigmodel.cn"],
        "price_source": "",
        "models": [
            {"model": "glm-4.6v-flash", "vendor": "智谱", "tags": ["图片理解", "有免费模型"],
             "price_in": None, "price_out": None, "note": "价格以智谱公开价目为准"},
        ],
    },
    {
        "name": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "hosts": ["api.hunyuan.cloud.tencent.com"],
        "price_source": "",
        "models": [
            {"model": "hunyuan-vision", "vendor": "腾讯", "tags": ["图片理解"],
             "price_in": None, "price_out": None, "note": "价格以腾讯云公开价目为准"},
        ],
    },
    {
        "name": "阿里百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hosts": ["dashscope.aliyuncs.com"],
        "price_source": "",
        "models": [
            {"model": "qwen-vl-max", "vendor": "阿里", "tags": ["图片理解", "有免费额度"],
             "price_in": None, "price_out": None, "note": "价格以阿里云公开价目为准"},
        ],
    },
    {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "hosts": ["api.siliconflow.cn"],
        "price_source": "",
        "models": [
            {"model": "Qwen/Qwen2.5-VL-7B-Instruct", "vendor": "Qwen", "tags": ["图片理解", "开源模型"],
             "price_in": None, "price_out": None, "note": "价格以硅基流动公开价目为准"},
        ],
    },
    {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "hosts": ["api.openai.com"],
        "price_source": "",
        "models": [
            {"model": "gpt-4o", "vendor": "OpenAI", "tags": ["图片理解"],
             "price_in": None, "price_out": None, "note": "价格以 OpenAI 公开价目为准"},
        ],
    },
    {
        "name": "本地 Ollama",
        "base_url": "http://localhost:11434/v1",
        "hosts": ["localhost:11434", "127.0.0.1:11434"],
        "price_source": "",
        "models": [
            {"model": "qwen2.5vl:7b", "vendor": "本地", "tags": ["图片理解", "本地运行"],
             "price_in": None, "price_out": None, "note": "本地运行不产生按 token 计费；API Key 随便填"},
        ],
    },
]

# 扁平清单供成本核算（_start_ai_operation）按模型名查价
MODEL_PRESETS = [m for entry in MODEL_CATALOG for m in entry["models"]]


def model_entry_for_url(url):
    """按基地址匹配已收录的入口；无法识别返回 None。

    hosts 里带端口时按 host:port 比较（如本地 Ollama），否则只比主机名。
    """
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:  # 端口非法等
        return None
    if not host:
        return None
    if port and not ((parts.scheme == "https" and port == 443) or
                     (parts.scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    for entry in MODEL_CATALOG:
        if host in entry["hosts"]:
            return entry
    return None


# TokenHub 各站点域名：只有这些入口支持 GET /v1/models 的每日同步
TOKENHUB_SYNC_HOSTS = ("tokenhub.tencentmaas.com", "tokenhub-intl.tencentmaas.com",
                       "tokenhub.tencentmaas.cn", "tokenhub-intl.tencentmaas.cn")


def _is_tokenhub_url(url):
    try:
        host = (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in TOKENHUB_SYNC_HOSTS)


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_available_models(base_url, api_key, timeout=20, usage_hook=None):
    """调用 OpenAI 兼容的 GET /v1/models，返回 [{model_id, display_name, status}]。"""
    payload = llm.get_json(
        (base_url or "").rstrip("/") + "/models", api_key, timeout=timeout,
        usage_hook=usage_hook, stage="model_sync.models")
    rows = []
    for item in payload.get("data") or []:
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        rows.append({
            "model_id": mid,
            "display_name": str(item.get("name") or mid).strip(),
            "status": str(item.get("status") or "").strip(),
        })
    return rows


def _sync_error_text(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "HTTP 401：API Key 无效或无权查询模型列表"
        if exc.code == 403:
            return "HTTP 403：该 Key 无查询模型列表的权限"
        return f"HTTP {exc.code}：{exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"网络不通：{exc.reason}"
    return f"{type(exc).__name__}: {exc}"


TOKENHUB_PRICE_URL = "https://cloud.tencent.com/document/product/1823/130055"
# 作业批改要读取学生卷/答案图片，所以只保留视觉理解或多模态理解模型；
# 纯文本、语音、图片/视频生成、3D、向量等模型即使在 /v1/models 中可用也不展示。
TOKENHUB_GRADE_MODEL_IDS = {
    "deepseek-v4.1-flash", "deepseek-v4.1-flash-1017", "deepseek/deepseek-flash",
    "deepseek-v4-flash-vision-exp", "deepseek/deepseek-v4-flash-vision-exp",
    "deepseek-v4-flash-202605", "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro", "deepseek-v4-pro-0813", "deepseek-v4-pro-202606",
    "deepseek/deepseek-v4-pro-0813", "deepseek/deepseek-v4-pro",
    "glm-5.3-flash", "glm-5v-turbo",
    "minimax-m3.1-flash", "minimax-m3", "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed",
}

# 腾讯云价目页会把同一模型的多个调用参数写在同一个单元格里且不带分隔符，
# 这里按模型家族补齐别名价格，避免某些 /v1/models 中可用的别名显示为未收录。
TOKENHUB_MODEL_ALIAS_GROUPS = [
    {"deepseek-v4.1-flash", "deepseek-v4.1-flash-1017", "deepseek/deepseek-flash",
     "deepseek-v4-flash-vision-exp", "deepseek/deepseek-v4-flash-vision-exp",
     "deepseek-v4-flash-202605", "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash"},
    {"deepseek-v4-pro", "deepseek-v4-pro-0813", "deepseek-v4-pro-202606",
     "deepseek/deepseek-v4-pro-0813", "deepseek/deepseek-v4-pro"},
]


def _urlopen_with_cert(url, timeout=30):
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except Exception:
        pass
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
        "Accept-Encoding": "gzip",
    })
    return urllib.request.urlopen(req, timeout=timeout, context=context)


def _extract_model_ids(text):
    text = text or ""
    ids = []
    # 先按已知模型 ID 切分腾讯云价目页里被拼接在同一单元格的多个别名。
    for mid in sorted(TOKENHUB_GRADE_MODEL_IDS, key=len, reverse=True):
        if mid in text:
            ids.append(mid)
            text = text.replace(mid, " ")
    ids.extend(re.findall(r"[A-Za-z0-9][A-Za-z0-9._/\-:]{0,119}", text))
    return [mid.lower() for mid in ids]


def _parse_price_cell(text):
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else None


class _TokenHubPriceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.rows = []
        self.row = []
        self.cell = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
            self.in_cell = True
        elif tag == "br" and self.in_cell:
            self.cell.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            text = re.sub(r"[^\S\n]+", " ", "".join(self.cell)).strip()
            self.row.append(text)
            self.in_cell = False
        elif tag == "tr":
            self.rows.append(self.row)
        elif tag == "table":
            self.tables.append(self.rows)

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def parse_tokenhub_prices(html):
    """解析 TokenHub 公开价目页，返回 {prices, multimodal_ids}。

    prices[model_id] = {price_in, price_out, name}，单位均为元/百万 token。
    multimodal_ids 来自「多模态理解模型」表，自动视为适合图片批改。
    """
    parser = _TokenHubPriceParser()
    parser.feed(html or "")
    prices = {}
    multimodal_ids = set()
    for rows in parser.tables:
        if len(rows) < 2:
            continue
        header = rows[0]
        joined = "".join(header)
        if "推理输入" not in joined or "推理输出" not in joined:
            continue
        model_col = next((i for i, h in enumerate(header) if "model（调用参数）" in h), 0)
        in_col = next(i for i, h in enumerate(header) if "推理输入" in h)
        out_col = next(i for i, h in enumerate(header) if "推理输出" in h)
        peak_col = next((i for i, h in enumerate(header) if "峰谷" in h), None)
        is_multimodal = model_col == 0 and len(header) == 3
        current_ids = []
        for row in rows[1:]:
            if len(row) <= max(model_col, in_col, out_col):
                continue
            ids = []
            for line in (row[model_col] or "").splitlines():
                ids.extend(_extract_model_ids(line))
            if ids:
                current_ids = ids
            if peak_col is not None and peak_col < len(row) and "高峰" in row[peak_col]:
                continue
            price_in = _parse_price_cell(row[in_col])
            price_out = _parse_price_cell(row[out_col])
            if not current_ids or price_in is None or price_out is None:
                continue
            name = row[0] if row else ""
            for mid in current_ids:
                prices.setdefault(mid, {"price_in": price_in, "price_out": price_out, "name": name})
                if is_multimodal:
                    multimodal_ids.add(mid)
    for group in TOKENHUB_MODEL_ALIAS_GROUPS:
        source = next((prices[mid] for mid in group if mid in prices), None)
        if source:
            for mid in group:
                prices.setdefault(mid, source)
    return {"prices": prices, "multimodal_ids": multimodal_ids}


def fetch_tokenhub_price_page(timeout=30):
    # 腾讯云文档页对部分浏览器默认 Accept 头会返回 JS 挑战页，这里只保留最小请求头。
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
        "Accept-Encoding": "gzip",
    }
    try:
        import requests
        verify = True
        try:
            import certifi
            verify = certifi.where()
        except Exception:
            pass
        resp = requests.get(TOKENHUB_PRICE_URL, headers=headers, timeout=timeout, verify=verify)
        resp.raise_for_status()
        return resp.text
    except ImportError:
        pass
    resp = _urlopen_with_cert(TOKENHUB_PRICE_URL, timeout=timeout)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "ignore")


def fetch_tokenhub_prices(timeout=30):
    return parse_tokenhub_prices(fetch_tokenhub_price_page(timeout=timeout))


def tokenhub_suitable_model_ids(price_data=None):
    ids = set(TOKENHUB_GRADE_MODEL_IDS)
    if price_data:
        ids |= set(price_data.get("multimodal_ids") or set())
    return ids


def sync_models_for_teacher(teacher_id, force=False, fetch_models=None, fetch_prices=None,
                            usage_hook=None):
    """把某老师配置入口的可用模型同步进缓存。

    返回 (模型数, 错误文本)：未到期时模型数为 -1 且无错误。
    fetch_models / fetch_prices 允许调用方注入，便于 app 层保留可替换的 patch 点。
    """
    fetch_models = fetch_models or fetch_available_models
    fetch_prices = fetch_prices or fetch_tokenhub_prices
    conf = llm.resolve_config(teacher_id)
    base_url, api_key = conf["api_url"].rstrip("/"), conf["api_key"]
    if not base_url or not api_key:
        return 0, "未配置接口基地址或 API Key"
    if not _is_tokenhub_url(base_url):
        return 0, "当前入口不是腾讯云 TokenHub，跳过自动同步"

    con = sqlite3.connect(database_path(), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if not force:
            st = con.execute(
                "SELECT MAX(fetched_at) AS last FROM model_sync WHERE teacher_id=? AND base_url=?",
                (teacher_id, base_url)).fetchone()
            if st and st["last"]:
                try:
                    last = datetime.strptime(st["last"], "%Y-%m-%d %H:%M:%S")
                    if datetime.now() - last < timedelta(hours=cfg.get("MODEL_SYNC_HOURS")):
                        return -1, ""
                except ValueError:
                    pass
        try:
            if usage_hook is None:
                models = fetch_models(base_url, api_key)
            else:
                models = fetch_models(base_url, api_key, usage_hook=usage_hook)
        except Exception as exc:  # 网络/鉴权失败只记录，保留上次结果，下一轮继续重试
            msg = _sync_error_text(exc)[:300]
            log.warning("模型清单同步失败 teacher=%s: %s", teacher_id, msg)
            con.execute(
                "INSERT INTO model_sync_state(teacher_id,base_url,fetched_at,model_count,error) "
                "VALUES(?,?,?,0,?) ON CONFLICT(teacher_id,base_url) DO UPDATE SET "
                "error=excluded.error",
                (teacher_id, base_url, _now_text(), msg))
            con.commit()
            return 0, msg

        price_data = None
        try:
            price_data = fetch_prices(timeout=20)
        except Exception as exc:
            log.warning("TokenHub 公开价目同步失败 teacher=%s: %s",
                        teacher_id, _sync_error_text(exc))
        suitable_ids = tokenhub_suitable_model_ids(price_data)
        models = [m for m in models if m["model_id"] in suitable_ids]

        now = _now_text()
        if price_data:
            for mid, item in price_data["prices"].items():
                if mid not in suitable_ids:
                    continue
                con.execute(
                    "INSERT INTO model_prices(model,price_in,price_out,unit,source,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET "
                    "price_in=excluded.price_in, price_out=excluded.price_out, "
                    "unit=excluded.unit, source=excluded.source, updated_at=excluded.updated_at",
                    (mid, item["price_in"], item["price_out"], "元/百万 token",
                     "TokenHub 公开价目", now))

        con.execute("DELETE FROM model_sync WHERE teacher_id=? AND base_url=?",
                    (teacher_id, base_url))
        con.executemany(
            "INSERT INTO model_sync(teacher_id,base_url,model_id,display_name,status,fetched_at) "
            "VALUES(?,?,?,?,?,?)",
            [(teacher_id, base_url, m["model_id"], m["display_name"], m["status"], now)
             for m in models])
        con.execute(
            "INSERT INTO model_sync_state(teacher_id,base_url,fetched_at,model_count,error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(teacher_id,base_url) DO UPDATE SET "
            "fetched_at=excluded.fetched_at, model_count=excluded.model_count, error=''",
            (teacher_id, base_url, now, len(models), ""))
        con.commit()
        return len(models), ""
    finally:
        con.close()


def build_model_rows(teacher_id, base_url, con=None):
    """把「静态清单 + 接口同步结果 + 截图录入价目」合并成设置页展示的一行行模型。"""
    own = con is None
    if own:
        con = sqlite3.connect(database_path(), timeout=10)
        con.row_factory = sqlite3.Row
    try:
        entry = model_entry_for_url(base_url)
        synced, state = {}, None
        if teacher_id:
            synced = {r["model_id"]: dict(r) for r in con.execute(
                "SELECT * FROM model_sync WHERE teacher_id=? AND base_url=?",
                (teacher_id, base_url))}
            st = con.execute(
                "SELECT * FROM model_sync_state WHERE teacher_id=? AND base_url=?",
                (teacher_id, base_url)).fetchone()
            state = dict(st) if st else None
        prices = {r["model"]: dict(r) for r in con.execute(
            "SELECT model,price_in,price_out,unit,source,updated_at FROM model_prices")}
    finally:
        if own:
            con.close()

    static = [dict(m) for m in (entry["models"] if entry else [])]
    rows, seen = [], set()

    def add(base, sync_row):
        mid = base.get("model") or sync_row["model_id"]
        seen.add(mid)
        override = prices.get(mid) or {}
        price_in = override.get("price_in")
        price_out = override.get("price_out")
        if price_in is None:
            price_in = base.get("price_in")
        if price_out is None:
            price_out = base.get("price_out")
        rows.append({
            "model": mid,
            "display_name": (sync_row or {}).get("display_name") or mid,
            "vendor": base.get("vendor") or "",
            "tags": list(base.get("tags") or []),
            "note": base.get("note") or "",
            "price_in": price_in,
            "price_out": price_out,
            "price_source": (override.get("source") or "") if override.get("price_in") is not None else "",
            "price_updated_at": override.get("updated_at") or "",
            "known": bool(base),
            "status": (sync_row or {}).get("status") or "",
        })

    for m in static:
        add(m, synced.get(m["model"]))
    for mid in sorted(synced):
        if mid not in seen:
            add({}, synced[mid])

    return {
        "rows": rows,
        "entry": entry,
        "state": state,
        "title": entry["name"] if entry else "当前接口",
        "synced": bool(synced),
        "sync_supported": _is_tokenhub_url(base_url),
        "has_unknown": any(not r["known"] for r in rows),
        "price_sources": sorted({r["price_source"] for r in rows if r["price_source"]}),
    }


def _model_sync_loop():
    """每小时醒一次，按 MODEL_SYNC_HOURS 节流逐个老师同步可用模型。"""
    while True:
        try:
            con = sqlite3.connect(database_path(), timeout=10)
            try:
                ids = [r[0] for r in con.execute(
                    "SELECT id FROM users WHERE role='teacher' AND active=1")]
            finally:
                con.close()
            for tid in ids:
                try:
                    sync_models_for_teacher(tid)
                except Exception:
                    log.exception("模型清单同步异常 teacher=%s", tid)
        except Exception:
            log.exception("模型清单自动同步循环异常")
        time.sleep(3600)


_sync_thread_started = False
_sync_lock_file = None


def start_model_sync():
    """启动每日模型清单同步线程；多进程部署时只让抢到文件锁的进程跑。"""
    global _sync_thread_started, _sync_lock_file
    if _sync_thread_started or not cfg.get("AUTO_MODEL_SYNC"):
        return
    _sync_thread_started = True
    try:
        os.makedirs(cfg.get("DATA_DIR"), exist_ok=True)
        _sync_lock_file = open(os.path.join(cfg.get("DATA_DIR"), ".model_sync.lock"), "w")
        fcntl.flock(_sync_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _sync_thread_started = False   # 别的进程已经在同步
        if _sync_lock_file:
            _sync_lock_file.close()
            _sync_lock_file = None
        return
    from flask import current_app, has_app_context
    application = current_app._get_current_object() if has_app_context() else None
    def run():
        if application is None:
            _model_sync_loop()
        else:
            with application.app_context():
                _model_sync_loop()
    threading.Thread(target=run, daemon=True, name="model-sync").start()


# 一份典型作业（约 26 页图片 + 答案）的估算 token 量
# 144dpi 的 A4 页经视觉模型切块后每页约 2000-2500 token
EST_TOKENS_INPUT = 60000   # 输入：学生卷 + 答案约 46 页 × 约 1300 token/页
EST_TOKENS_OUTPUT = 4000   # 输出：逐题 JSON + 评语
