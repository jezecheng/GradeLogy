# -*- coding: utf-8 -*-
"""AI 操作与逐次调用用量。

一次用户操作（批阅、汇总、报告…）对应 ai_operations 一行，每次模型请求对应
ai_usage_events 一行；费用按操作开始时的价格与高峰倍率快照结算。

依赖方向：只依赖 db / model_catalog，不反向导入 app。
"""
import json
import logging
from datetime import datetime, timedelta

from db import get_db
from model_catalog import MODEL_PRESETS

_log = logging.getLogger("gradelogy.ai")

# ---------- 闲时批阅：TokenHub 高峰时段价格翻倍 ----------
# 高峰定义（参考腾讯云公开价目说明）：工作日 9:00-12:00, 14:00-18:00
# 闲时窗口：非高峰（包括所有夜间、早高峰前、午间午休、以及周六周日全天）
# 如果当前不在闲时且老师开启了「仅闲时批阅」，则拦截 AI 调用
PEAK_RANGES = [  # (start_hour, start_minute, end_hour, end_minute)，24 小时制
    (9, 0, 12, 0),
    (14, 0, 18, 0),
]

# ---------- AI 操作与逐次调用用量 ----------
AI_OPERATION_LABEL = {
    "scope_analyze": "作业范围分析",
    "question_tagging": "知识点标注",
    "tag_horizontal": "横向知识点分析",
    "tag_vertical": "纵向知识点分析",
    "kg_build": "知识图谱构建",
    "kg_audit": "知识图谱体检",
    "kg_category": "知识图谱分类重构",
    "grade": "作业批阅",
    "lecture_summary": "讲次汇总",
    "report_generate": "批改报告评价",
    "report_comment": "重新生成总评",
    "price_import": "价目截图识别",
}
AI_STATUS_LABEL = {"queued": "排队中", "running": "进行中", "success": "成功", "failed": "失败"}


def _now_in_peak():
    """返回 (是否高峰, 下一次闲时开始时刻的 HH:MM 描述)。"""
    now = datetime.now()
    # 周六=5, 周日=6 全天闲时
    if now.weekday() >= 5:
        return False, None
    h, m = now.hour, now.minute
    for sh, sm, eh, em in PEAK_RANGES:
        if (h, m) >= (sh, sm) and (h, m) < (eh, em):
            # 下一个闲时就是该高峰段的结束时刻
            return True, f"{eh:02d}:{em:02d}"
    return False, None


def _start_ai_operation(teacher_id, operation_type, model, course_id=None,
                        lecture_id=None, submission_id=None, status="running",
                        material_id=None):
    """创建一次用户操作；每次点击/重试都使用新的 operation_id。"""
    preset = next((m for m in MODEL_PRESETS if m["model"] == model), None)
    price_in = preset["price_in"] if preset else None
    price_out = preset["price_out"] if preset else None
    price_source = "MODEL_PRESETS"
    try:  # 公开价目或截图录入的价目优先于内置清单
        row = get_db().execute(
            "SELECT price_in,price_out,source FROM model_prices WHERE model=?", (model,)).fetchone()
        if row is not None and row["price_in"] is not None:
            price_in, price_out = row["price_in"], row["price_out"]
            price_source = row["source"] or "模型价目"
    except Exception:
        pass
    peak = _now_in_peak()[0]
    pricing = {
        "price_in": price_in,
        "price_out": price_out,
        "peak_multiplier": 2 if peak else 1,
        "currency": "CNY",
        "unit": "元/百万 token",
        "source": price_source,
    }
    started_at = datetime.now().isoformat(sep=" ", timespec="milliseconds")
    db = get_db()
    cur = db.execute(
        "INSERT INTO ai_operations(teacher_id,operation_type,course_id,lecture_id,"
        "submission_id,material_id,model,status,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (teacher_id, operation_type, course_id, lecture_id, submission_id, material_id,
         model, status, started_at))
    db.commit()
    operation = {
        "id": cur.lastrowid, "pricing": pricing, "finished": False,
        "teacher": f"#{teacher_id}", "operation_type": operation_type, "model": model,
        "course_id": course_id, "lecture_id": lecture_id,
        "submission_id": submission_id, "material_id": material_id,
    }
    _log.info(
        "AI 操作发起 operation=%s teacher=%s scene=%s model=%s course=%s "
        "lecture=%s submission=%s material=%s status=%s",
        operation["id"], operation["teacher"], operation_type, model,
        course_id or "-", lecture_id or "-", submission_id or "-", material_id or "-", status)
    operation["usage_hook"] = lambda event: _record_ai_usage(operation, event)
    return operation


def _record_ai_usage(operation, event):
    """记录单次模型请求；知识图谱请求开始时插入，结束时更新同一行。"""
    pricing = operation["pricing"]
    prompt = event.get("prompt_tokens")
    completion = event.get("completion_tokens")
    cost = None
    if (prompt is not None and completion is not None and
            pricing["price_in"] is not None and pricing["price_out"] is not None):
        cost = ((prompt * pricing["price_in"] + completion * pricing["price_out"])
                * pricing["peak_multiplier"] / 1000000)
    usage = event.get("usage")
    db = get_db()
    values = (event.get("chunk_id"), event.get("attempt"), event.get("provider_request_id"),
              event.get("model") or "", prompt, completion, event.get("total_tokens"),
              event.get("cached_tokens"), event.get("reasoning_tokens"),
              event.get("latency_ms"), event.get("finish_reason") or "", cost,
              json.dumps(pricing, ensure_ascii=False),
              json.dumps(usage, ensure_ascii=False) if usage is not None else "",
              event.get("status") or "error", str(event.get("error") or "")[:1000])
    event_id = event.get("usage_event_id")
    if event_id is not None:
        db.execute(
            "UPDATE ai_usage_events SET chunk_id=?,attempt=?,provider_request_id=?,model=?,"
            "prompt_tokens=?,completion_tokens=?,total_tokens=?,cached_tokens=?,"
            "reasoning_tokens=?,latency_ms=?,finish_reason=?,estimated_cost=?,"
            "pricing_json=?,usage_json=?,status=?,error=? WHERE id=? AND operation_id=?",
            values + (event_id, operation["id"]))
    else:
        cur = db.execute(
            "INSERT INTO ai_usage_events(operation_id,chunk_id,attempt,provider_request_id,model,"
            "prompt_tokens,completion_tokens,total_tokens,cached_tokens,reasoning_tokens,"
            "latency_ms,finish_reason,estimated_cost,pricing_json,usage_json,status,error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (operation["id"],) + values)
        event_id = cur.lastrowid
    db.commit()
    _log.info(
        "AI 请求 operation=%s event=%s teacher=%s scene=%s model=%s status=%s "
        "attempt=%s prompt=%s completion=%s total=%s latency_ms=%s finish=%s error=%s",
        operation["id"], event_id, operation.get("teacher", "-"),
        operation.get("operation_type", "-"), event.get("model") or operation.get("model") or "-",
        event.get("status") or "error", event.get("attempt") or "-",
        prompt if prompt is not None else "-", completion if completion is not None else "-",
        event.get("total_tokens") if event.get("total_tokens") is not None else "-",
        event.get("latency_ms") if event.get("latency_ms") is not None else "-",
        event.get("finish_reason") or "-", str(event.get("error") or "-")[:300])
    return event_id


def _finish_ai_operation(operation, status, error=""):
    if not operation or operation["finished"]:
        return
    get_db().execute(
        "UPDATE ai_operations SET status=?,finished_at=?,error=? WHERE id=?",
        (status, datetime.now().isoformat(sep=" ", timespec="milliseconds"),
         str(error or "")[:1000], operation["id"]))
    get_db().commit()
    _log.info("AI 操作结束 operation=%s teacher=%s scene=%s model=%s status=%s error=%s",
              operation["id"], operation.get("teacher", "-"),
              operation.get("operation_type", "-"), operation.get("model") or "-",
              status, str(error or "-")[:300])
    operation["finished"] = True


def _ai_operation_history(teacher_id=None, lecture_id=None, submission_id=None,
                          operation_id=None, limit=5):
    """操作级聚合供页面与后续 report 复用；部分请求缺 usage 时显式保留未知次数。"""
    where, params = [], []
    if operation_id is not None:
        where.append("o.id=?")
        params.append(operation_id)
    if teacher_id is not None:
        where.append("o.teacher_id=?")
        params.append(teacher_id)
    if lecture_id is not None:
        where.append("o.lecture_id=?")
        params.append(lecture_id)
    if submission_id is not None:
        where.append("o.submission_id=?")
        params.append(submission_id)
    sql = """
        SELECT o.*, COUNT(e.id) AS request_count,
          CASE WHEN COUNT(e.prompt_tokens)>0 THEN SUM(e.prompt_tokens) END AS prompt_tokens,
          CASE WHEN COUNT(e.completion_tokens)>0 THEN SUM(e.completion_tokens) END AS completion_tokens,
          CASE WHEN COUNT(e.total_tokens)>0 THEN SUM(e.total_tokens) END AS total_tokens,
          SUM(CASE WHEN e.id IS NOT NULL AND e.total_tokens IS NULL THEN 1 ELSE 0 END)
            AS unknown_usage_count,
          CASE WHEN COUNT(e.estimated_cost)>0 THEN SUM(e.estimated_cost) END AS estimated_cost,
          SUM(CASE WHEN e.id IS NOT NULL AND e.estimated_cost IS NULL THEN 1 ELSE 0 END)
            AS unknown_cost_count,
          ROUND((julianday(o.finished_at)-julianday(o.started_at))*86400,1) AS elapsed_s
        FROM ai_operations o LEFT JOIN ai_usage_events e ON e.operation_id=o.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY o.id ORDER BY o.id DESC LIMIT ?"
    params.append(limit)
    return get_db().execute(sql, params).fetchall()


def _ai_usage_text(operation):
    """把一次操作的 token、整体耗时和预估费用变成可展示文本。"""
    if not operation:
        return ""
    parts = []
    if operation["request_count"]:
        if operation["total_tokens"] is not None:
            prompt = (f"{operation['prompt_tokens']:,}"
                      if operation["prompt_tokens"] is not None else "未知")
            completion = (f"{operation['completion_tokens']:,}"
                          if operation["completion_tokens"] is not None else "未知")
            parts.append(f"输入 {prompt} / 输出 {completion} / 合计 "
                         f"{operation['total_tokens']:,} token")
            if operation["unknown_usage_count"]:
                parts.append(f"另有 {operation['unknown_usage_count']} 次请求用量未知")
        else:
            parts.append("token 用量未知（服务商未返回 usage）")
    else:
        parts.append("未发起模型请求")
    if operation["elapsed_s"] is not None:
        parts.append(f"耗时 {operation['elapsed_s']:.1f} 秒")
    if operation["estimated_cost"] is not None:
        cost_value = operation["estimated_cost"]
        digits = 4 if 0 <= cost_value < 1 else 2
        cost = f"预估费用 ¥{cost_value:.{digits}f}"
        if operation["unknown_cost_count"]:
            cost += f"，另有 {operation['unknown_cost_count']} 次请求费用未知"
        parts.append(cost)
    elif operation["request_count"]:
        parts.append("预估费用未知（缺少价格或 token 用量）")
    return "本次 AI 消耗：" + "；".join(parts)


def _compact_number(value):
    """监控图表用的紧凑数值；完整数值仍在页面标题或明细表中保留。"""
    if value is None:
        return "—"
    number = float(value)
    units = ((10 ** 12, "T"), (10 ** 9, "G"), (10 ** 6, "M"), (1000, "K"))
    for index, (threshold, suffix) in enumerate(units):
        if abs(number) >= threshold:
            scaled = number / threshold
            # 999999 显示成 1000K 不如直接进位成 1M
            if index and abs(scaled) >= 999.995:
                scaled = number / units[index - 1][0]
                suffix = units[index - 1][1]
            text = f"{scaled:.2f}".rstrip("0").rstrip(".")
            return text + suffix
    return f"{int(number):,}"


def _token_axis_max(value):
    """将柱状图上限取为 1 / 2 / 5 × 10ⁿ，避免顶部柱贴边。"""
    value = int(value or 0)
    magnitude = 1
    while magnitude * 10 <= value:
        magnitude *= 10
    for factor in (1, 2, 5, 10):
        if magnitude * factor >= value:
            return magnitude * factor
    return 1


def _ai_dashboard_data(filters):
    """管理员全局用量：先聚合到操作，再汇总，避免重试放大操作数。"""
    where, params = [], []
    if filters["period"] != "all":
        days = int(filters["period"])
        start = (datetime.now() - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        where.append("o.started_at>=?")
        params.append(start.isoformat(sep=" ", timespec="milliseconds"))
    for key, column in (("teacher_id", "o.teacher_id"), ("course_id", "o.course_id"),
                        ("operation_type", "o.operation_type"), ("model", "o.model"),
                        ("status", "o.status")):
        value = filters.get(key)
        if value not in (None, ""):
            where.append(column + "=?")
            params.append(value)
    query = (filters.get("q") or "").strip()
    if query:
        if query.startswith("#") and query[1:].isdigit():
            where.append("o.id=?")
            params.append(int(query[1:]))
        else:
            where.append("EXISTS (SELECT 1 FROM ai_usage_events search_event "
                         "WHERE search_event.operation_id=o.id "
                         "AND search_event.provider_request_id LIKE ?)")
            params.append("%" + query + "%")

    sql = """
        WITH operation_usage AS (
          SELECT o.*, u.display_name AS teacher_name, u.username AS teacher_username,
            c.name AS course_name,
            COUNT(e.id) AS request_count,
            CASE WHEN COUNT(e.prompt_tokens)>0 THEN SUM(e.prompt_tokens) END AS prompt_tokens,
            CASE WHEN COUNT(e.completion_tokens)>0 THEN SUM(e.completion_tokens) END AS completion_tokens,
            CASE WHEN COUNT(e.total_tokens)>0 THEN SUM(e.total_tokens) END AS total_tokens,
            CASE WHEN COUNT(e.cached_tokens)>0 THEN SUM(e.cached_tokens) END AS cached_tokens,
            CASE WHEN COUNT(e.reasoning_tokens)>0 THEN SUM(e.reasoning_tokens) END AS reasoning_tokens,
            SUM(CASE WHEN e.id IS NOT NULL AND e.total_tokens IS NULL THEN 1 ELSE 0 END)
              AS unknown_usage_count,
            CASE WHEN COUNT(e.estimated_cost)>0 THEN SUM(e.estimated_cost) END AS estimated_cost,
            SUM(CASE WHEN e.id IS NOT NULL AND e.estimated_cost IS NULL THEN 1 ELSE 0 END)
              AS unknown_cost_count,
            SUM(e.latency_ms) AS latency_sum,
            COUNT(e.latency_ms) AS latency_count,
            ROUND((julianday(o.finished_at)-julianday(o.started_at))*86400,1) AS elapsed_s
          FROM ai_operations o
          LEFT JOIN users u ON u.id=o.teacher_id
          LEFT JOIN courses c ON c.id=o.course_id
          LEFT JOIN ai_usage_events e ON e.operation_id=o.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY o.id)"
    db = get_db()
    summary = dict(db.execute(sql + """
        SELECT COUNT(*) AS operation_count,
          COALESCE(SUM(status='success'),0) AS success_count,
          COALESCE(SUM(status='failed'),0) AS failed_count,
          COALESCE(SUM(status='running'),0) AS running_count,
          COALESCE(SUM(request_count),0) AS request_count,
          COALESCE(SUM(request_count=0),0) AS no_request_count,
          SUM(prompt_tokens) AS prompt_tokens,
          SUM(completion_tokens) AS completion_tokens,
          SUM(total_tokens) AS total_tokens,
          SUM(cached_tokens) AS cached_tokens,
          SUM(reasoning_tokens) AS reasoning_tokens,
          COALESCE(SUM(unknown_usage_count),0) AS unknown_usage_count,
          SUM(estimated_cost) AS estimated_cost,
          COALESCE(SUM(unknown_cost_count),0) AS unknown_cost_count,
          CASE WHEN SUM(latency_count)>0
               THEN ROUND(1.0*SUM(latency_sum)/SUM(latency_count)/1000,1) END AS avg_latency_s
        FROM operation_usage""", params).fetchone())
    finished = summary["success_count"] + summary["failed_count"]
    summary["success_rate"] = (round(summary["success_count"] * 1000 / finished) / 10
                               if finished else None)

    def with_bars(rows):
        items = [dict(row) for row in rows]
        peak = max((row["operation_count"] for row in items), default=0)
        for row in items:
            row["bar_pct"] = round(row["operation_count"] * 100 / peak, 1) if peak else 0
        return items

    daily = with_bars(db.execute(sql + """
        SELECT substr(started_at,1,10) AS label, COUNT(*) AS operation_count,
          COALESCE(SUM(request_count),0) AS request_count,
          SUM(total_tokens) AS total_tokens, SUM(estimated_cost) AS estimated_cost
        FROM operation_usage GROUP BY substr(started_at,1,10) ORDER BY label DESC""",
        params).fetchall())
    groups = {}
    dimensions = {
        "operations": ("operation_type", "operation_type"),
        "models": ("COALESCE(NULLIF(model,''),'未指定模型')", "COALESCE(NULLIF(model,''),'未指定模型')"),
        "teachers": ("COALESCE(NULLIF(teacher_name,''),teacher_username,'已删除教师')",
                     "COALESCE(NULLIF(teacher_name,''),teacher_username,'已删除教师')"),
        "courses": ("COALESCE(NULLIF(course_name,''),'未关联课程')",
                    "COALESCE(NULLIF(course_name,''),'未关联课程')"),
    }
    for key, (label, grouping) in dimensions.items():
        groups[key] = with_bars(db.execute(sql + f"""
            SELECT {label} AS label, COUNT(*) AS operation_count,
              COALESCE(SUM(request_count),0) AS request_count,
              SUM(total_tokens) AS total_tokens, SUM(estimated_cost) AS estimated_cost
            FROM operation_usage GROUP BY {grouping}
            ORDER BY operation_count DESC, label""", params).fetchall())
    recent = [dict(row) for row in db.execute(
        sql + " SELECT * FROM operation_usage ORDER BY id DESC LIMIT 20", params).fetchall()]

    # 请求级分析使用服务商实际返回的模型名；业务操作数仍由上面的 operation_usage 决定。
    event_from = """
        FROM ai_operations o
        JOIN ai_usage_events e ON e.operation_id=o.id
        LEFT JOIN users u ON u.id=o.teacher_id
        LEFT JOIN courses c ON c.id=o.course_id
    """
    event_where = (" WHERE " + " AND ".join(where)) if where else ""
    event_model = "COALESCE(NULLIF(e.model,''),NULLIF(o.model,''),'未指定模型')"
    model_rows = [dict(row) for row in db.execute("""
        SELECT """ + event_model + """ AS label, COUNT(*) AS request_count,
          CASE WHEN COUNT(e.total_tokens)>0 THEN SUM(e.total_tokens) END AS total_tokens,
          SUM(CASE WHEN e.total_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_usage_count
    """ + event_from + event_where + """
        GROUP BY """ + event_model + """
        ORDER BY total_tokens IS NULL,total_tokens DESC,request_count DESC,label
    """, params).fetchall()]

    palette = ["#4e6ef2", "#16a394", "#f59e0b", "#8b5cf6", "#ec4899", "#22c55e"]
    other_color = "#98a2b3"
    top_models = model_rows[:6]
    top_labels = {row["label"] for row in top_models}
    token_peak = max((row["total_tokens"] or 0 for row in model_rows), default=0)
    model_ranking = []
    for index, row in enumerate(model_rows[:8]):
        item = dict(row)
        item["rank"] = index + 1
        item["color"] = palette[index] if index < len(palette) else other_color
        item["bar_pct"] = round((row["total_tokens"] or 0) * 100 / token_peak, 1) if token_peak else 0
        model_ranking.append(item)

    trend_rows = [dict(row) for row in db.execute("""
        SELECT substr(o.started_at,1,10) AS day,""" + event_model + """ AS label,
          COUNT(*) AS request_count,
          CASE WHEN COUNT(e.total_tokens)>0 THEN SUM(e.total_tokens) END AS total_tokens
    """ + event_from + event_where + """
        GROUP BY substr(o.started_at,1,10),""" + event_model + """
        ORDER BY day,label
    """, params).fetchall()]
    if filters["period"] == "all":
        day_labels = sorted({row["day"] for row in trend_rows})
    else:
        first_day = datetime.now().date() - timedelta(days=int(filters["period"]) - 1)
        day_labels = [(first_day + timedelta(days=offset)).isoformat()
                      for offset in range(int(filters["period"]))]
    rows_by_day = {}
    for row in trend_rows:
        rows_by_day.setdefault(row["day"], []).append(row)

    trend_models = [{"label": row["label"], "color": palette[index],
                     "total_tokens": row["total_tokens"]}
                    for index, row in enumerate(top_models)]
    if len(model_rows) > len(top_models):
        others = model_rows[len(top_models):]
        other_known = [row["total_tokens"] for row in others if row["total_tokens"] is not None]
        trend_models.append({"label": "其他模型", "color": other_color,
                             "total_tokens": sum(other_known) if other_known else None})

    chart_days = []
    for day in day_labels:
        source_rows = rows_by_day.get(day, [])
        segments = []
        for model in trend_models:
            if model["label"] == "其他模型":
                matched = [row for row in source_rows if row["label"] not in top_labels]
            else:
                matched = [row for row in source_rows if row["label"] == model["label"]]
            known = [row["total_tokens"] for row in matched if row["total_tokens"] is not None]
            segments.append({**model, "value": sum(known) if known else None,
                             "request_count": sum(row["request_count"] for row in matched)})
        known_totals = [segment["value"] for segment in segments if segment["value"] is not None]
        chart_days.append({"label": day, "short_label": day[5:], "segments": segments,
                           "total_tokens": sum(known_totals) if known_totals else None,
                           "request_count": sum(row["request_count"] for row in source_rows)})
    axis_max = _token_axis_max(max((day["total_tokens"] or 0 for day in chart_days), default=0))
    for day in chart_days:
        for segment in day["segments"]:
            segment["height_pct"] = (round(segment["value"] * 100 / axis_max, 3)
                                     if segment["value"] else 0)
    token_trend = {
        "days": chart_days,
        "models": trend_models,
        "ticks": [axis_max * step / 4 for step in range(4, -1, -1)],
        "width": max(660, len(day_labels) * 46),
        "has_tokens": any(day["total_tokens"] is not None for day in chart_days),
    }

    request_logs = [dict(row) for row in db.execute("""
        SELECT e.id,e.operation_id,e.attempt,e.provider_request_id,
          """ + event_model + """ AS model,e.prompt_tokens,e.completion_tokens,e.total_tokens,
          e.cached_tokens,e.reasoning_tokens,e.latency_ms,e.finish_reason,e.estimated_cost,
          e.status AS event_status,e.error AS event_error,
          COALESCE(e.created_at,o.started_at) AS request_time,e.created_at AS event_created_at,
          o.started_at AS operation_started_at,o.operation_type,o.status AS operation_status,
          o.error AS operation_error,u.display_name AS teacher_name,u.username AS teacher_username,
          c.name AS course_name
    """ + event_from + event_where + " ORDER BY e.id DESC LIMIT 50", params).fetchall()]
    for row in request_logs:
        # 事件时间是 UTC；回退到 operation 时使用旧应用本地时间，由显示层按用户时区转换。
        row["request_time_source"] = "UTC" if row["event_created_at"] else "legacy"

    options = {
        "teachers": db.execute(
            "SELECT id,display_name,username FROM users WHERE role='teacher' OR EXISTS "
            "(SELECT 1 FROM ai_operations o WHERE o.teacher_id=users.id) "
            "ORDER BY display_name,username").fetchall(),
        "courses": db.execute("SELECT id,name FROM courses ORDER BY name,id").fetchall(),
        "models": db.execute("SELECT DISTINCT model FROM ai_operations WHERE model<>'' "
                             "ORDER BY model").fetchall(),
        "operations": [{"value": value, "label": label}
                       for value, label in AI_OPERATION_LABEL.items()],
    }
    return {"summary": summary, "daily": daily, "groups": groups,
            "recent": recent, "options": options, "model_ranking": model_ranking,
            "model_ranking_extra": max(0, len(model_rows) - len(model_ranking)),
            "token_trend": token_trend, "request_logs": request_logs}
