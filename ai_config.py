# -*- coding: utf-8 -*-
"""AI 配置解析：环境变量、多套 API Key、旧版 settings 三者的唯一优先级实现。

Web 页面（app.get_setting）与命令行/MCP（llm.resolve_config）都走这里，
避免同一套优先级在两处各写一遍后逐渐漂移。
"""
import os
import sqlite3

import config

# 环境变量优先于数据库：容器部署时把 Key 放在环境里，库里就不必留明文
ENV_AI_SETTINGS = {
    "ai_api_url": config.AI_API_URL,
    "ai_api_key": config.AI_API_KEY,
    "ai_model": config.AI_MODEL,
}

AI_SETTING_KEYS = ("ai_api_url", "ai_api_key", "ai_model")

# 各 AI 场景的输出上限（max_tokens）：老师可在「AI 设置」页按需调整；
# 未设置时回落到 default。default 与 utilities 里的函数默认值保持一致。
MAX_TOKENS_SETTINGS = {
    "max_tokens_answer_pdf": {"label": "作业范围分析", "default": 32768,
                              "hint": "识别教师版答案中的题目清单与分值"},
    "max_tokens_tagging": {"label": "知识点标注", "default": 32768,
                           "hint": "为每道题生成知识点标签"},
    "max_tokens_grading": {"label": "作业批阅", "default": 32768,
                           "hint": "逐题判定、OCR、标准答案与评语"},
    "max_tokens_summary": {"label": "讲次汇总", "default": 32768,
                           "hint": "每题要点 + 整体总结"},
    "max_tokens_horizontal": {"label": "横向知识点分析", "default": 32768,
                              "hint": "全班共性薄弱知识点"},
    "max_tokens_vertical": {"label": "纵向知识点分析", "default": 32768,
                            "hint": "单个学生跨讲次薄弱点"},
    "max_tokens_kg_build": {"label": "知识图谱构建", "default": 32768,
                            "hint": "从资料抽取概念与关系"},
    "max_tokens_kg_audit": {"label": "知识图谱体检", "default": 32768,
                            "hint": "审校图谱并给出治理建议"},
    "max_tokens_kg_category": {"label": "知识图谱分类重构", "default": 32768,
                               "hint": "把自由分类重映射为分层学科树"},
    "max_tokens_report": {"label": "报告评语", "default": 8192,
                          "hint": "生成总评和逐题图谱评语；输出被截断时可调大"},
}
MAX_TOKENS_KEYS = tuple(MAX_TOKENS_SETTINGS.keys())


def _connect():
    con = sqlite3.connect(config.get("DB_PATH"), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _setting_value(db, teacher_id, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE teacher_id=? AND key=?",
                     (teacher_id, key)).fetchone()
    if row is None:
        return default
    return row[0] if row[0] is not None else default


def _active_key_row(db, teacher_id):
    """当前启用的一套 API Key；表不存在或查询失败时返回 None。"""
    try:
        return db.execute(
            "SELECT base_url,api_key,model FROM ai_api_keys "
            "WHERE teacher_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
            (teacher_id,)).fetchone()
    except sqlite3.Error:
        return None


def get_ai_setting(teacher_id, key, default="", db=None):
    """取单个 AI 配置项；db 传 Flask 请求连接，缺省时临时建连。"""
    if key not in AI_SETTING_KEYS:
        raise ValueError(f"不支持的 AI 配置项：{key}")
    if db is None:
        con = _connect()
        try:
            return get_ai_setting(teacher_id, key, default, db=con)
        finally:
            con.close()

    # 环境变量接管了 Key 时，页面保存的多套配置整体让位，避免半套配置混用
    if ENV_AI_SETTINGS.get("ai_api_key"):
        # 环境密钥接管整套配置；缺项留空并由调用方报错，不借用数据库凭据。
        value = ENV_AI_SETTINGS.get(key) or ""
        return value.rstrip("/") if key == "ai_api_url" else value
    row = _active_key_row(db, teacher_id)
    if row is not None:
        value = {"ai_api_url": row[0], "ai_api_key": row[1], "ai_model": row[2]}[key] or ""
        if key == "ai_api_url":
            value = value.rstrip("/")
        if value:
            return value
    if ENV_AI_SETTINGS.get(key):
        return ENV_AI_SETTINGS[key]
    return _setting_value(db, teacher_id, key, default)


def resolve(teacher_id=None, db=None):
    """解析完整的 AI 配置，返回 {api_url, api_key, model, extra}。

    teacher_id 为空时沿用旧行为：取第一个配置过 Key 的老师，方便命令行直接跑。
    """
    if db is None:
        if not os.path.exists(config.get("DB_PATH")):
            return {"api_url": config.AI_API_URL.rstrip("/"), "api_key": config.AI_API_KEY,
                    "model": config.AI_MODEL, "extra": ""}
        con = _connect()
        try:
            return resolve(teacher_id, db=con)
        finally:
            con.close()

    if teacher_id is None:
        row = db.execute(
            "SELECT teacher_id FROM settings WHERE key='ai_api_key' AND value!='' "
            "ORDER BY teacher_id LIMIT 1").fetchone()
        if not row:
            row = db.execute(
                "SELECT teacher_id FROM ai_api_keys WHERE is_active=1 "
                "ORDER BY teacher_id LIMIT 1").fetchone()
        if not row:
            return {"api_url": config.AI_API_URL.rstrip("/"), "api_key": config.AI_API_KEY,
                    "model": config.AI_MODEL, "extra": ""}
        teacher_id = row[0]

    return {
        "api_url": get_ai_setting(teacher_id, "ai_api_url", db=db).rstrip("/"),
        "api_key": get_ai_setting(teacher_id, "ai_api_key", db=db),
        "model": get_ai_setting(teacher_id, "ai_model", db=db),
        "extra": (_setting_value(db, teacher_id, "ai_prompt") or "").strip(),
    }
