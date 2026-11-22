# -*- coding: utf-8 -*-
"""AI 设置与用量路由。

老师端「AI 设置」页（API Key 管理、模型同步、价目截图识别、连通性测试）、老师自用量页
与管理员「AI 用量」页集中在这里。路由只依赖 Flask 与 utilities.llm；设置读写、API Key 操作、
用量聚合等能力由 app.py 在 register() 时注入，避免与 app.py 形成循环导入。

app.py 注入的依赖是惰性包装，调用时才去取模块全局，因此测试中
patch app.get_setting / app._probe_ai_config 依旧会作用到这些路由。
"""

import base64
import json
import re
import tempfile

from flask import (Response, abort, flash, g, redirect, render_template, request,
                   send_file, stream_with_context, url_for)
from werkzeug.utils import secure_filename

from ai_config import MAX_TOKENS_SETTINGS
from utilities import ai_debug, llm

_REQUIRED = (
    "role_required", "get_db", "get_setting", "save_setting", "now_text", "beijing_iso",
    "api_key_rows", "activate_api_key", "build_model_rows", "sync_models_for_teacher",
    "start_ai_operation", "finish_ai_operation", "probe_ai_config", "ai_dashboard_data",
    "model_catalog", "est_tokens_input", "est_tokens_output", "env_ai_settings",
    "ai_operation_label", "ai_status_label",
)

PRICE_IMAGE_PROMPT = """你在帮一个作业批改系统维护大模型价目表。请读取这张价目截图中清晰可读的模型价格。

只输出一个 JSON 对象，不要解释：
{"rows":[{"model":"模型 ID","price_in":输入单价,"price_out":输出单价}],"unclear":["看不清或信息不全的模型"]}

规则：
1. model 必须是图中出现的模型 ID（如 deepseek-v4.1-flash），不要翻译、改写，也不要用你记忆里的模型替代。
2. price_in / price_out 统一换算成「元/百万 token」的数字，不要带单位；若图中单位是元/千 token，乘以 1000 后再填。
3. 只取输入价和输出价；图中没有明确标出的价格不要猜，把该模型放进 unclear。
4. 行被截断、数字模糊、只有单个价格、或是套餐/积分价格而非 token 单价的，全部放进 unclear。
5. 宁可少识别，也不要编造图中没有的模型或价格。"""


def _image_mime(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _valid_model_name(name):
    return bool(re.fullmatch(r"[A-Za-z0-9._/\-:]{1,120}", name or ""))


def _valid_api_base_url(value):
    return bool(re.fullmatch(r"https?://[^\s]{3,500}", value or ""))


def register(registry, deps):
    """把 AI 相关路由登记到 registry（endpoint 仍取视图函数名）。"""
    missing = [name for name in _REQUIRED if name not in deps]
    if missing:
        raise RuntimeError(f"ai_routes 缺少依赖：{', '.join(missing)}")

    role_required = deps["role_required"]
    get_db = deps["get_db"]
    get_setting = deps["get_setting"]
    save_setting = deps["save_setting"]
    now_text = deps["now_text"]
    beijing_iso = deps["beijing_iso"]
    api_key_rows = deps["api_key_rows"]
    activate_api_key = deps["activate_api_key"]
    build_model_rows = deps["build_model_rows"]
    sync_models_for_teacher = deps["sync_models_for_teacher"]
    start_ai_operation = deps["start_ai_operation"]
    finish_ai_operation = deps["finish_ai_operation"]
    probe_ai_config = deps["probe_ai_config"]
    ai_dashboard_data = deps["ai_dashboard_data"]
    model_catalog = deps["model_catalog"]
    est_tokens_input = deps["est_tokens_input"]
    est_tokens_output = deps["est_tokens_output"]
    env_ai_settings = deps["env_ai_settings"]
    ai_operation_label = deps["ai_operation_label"]
    ai_status_label = deps["ai_status_label"]

    # ---------- 管理员：AI 用量 ----------
    @registry.route("/admin/ai-usage")
    @role_required("admin")
    def admin_ai_usage():
        period = request.args.get("period", "30")
        period_labels = {"1": "今天", "7": "近 7 天", "30": "近 30 天",
                         "90": "近 90 天", "all": "全部时间"}
        if period not in period_labels:
            period = "30"

        def optional_id(name):
            try:
                value = int(request.args.get(name, ""))
                return value if value > 0 else ""
            except (TypeError, ValueError):
                return ""

        operation_type = request.args.get("operation_type", "")
        if operation_type not in ai_operation_label:
            operation_type = ""
        status = request.args.get("status", "")
        if status not in ai_status_label:
            status = ""
        dimension_labels = {"operation_type": "使用场景", "teacher": "用户",
                            "course": "课程", "model": "模型"}
        dimension = request.args.get("dimension", "operation_type")
        if dimension not in dimension_labels:
            dimension = "operation_type"
        filters = {
            "period": period,
            "teacher_id": optional_id("teacher_id"),
            "course_id": optional_id("course_id"),
            "operation_type": operation_type,
            "model": request.args.get("model", "").strip()[:200],
            "status": status,
            "q": request.args.get("q", "").strip()[:200],
            "dimension": dimension,
        }
        period_links = {value: url_for("admin_ai_usage", **{**filters, "period": value})
                        for value in period_labels}
        dimension_links = {value: url_for("admin_ai_usage", **{**filters, "dimension": value})
                           for value in dimension_labels}
        return render_template("admin_ai_usage.html", filters=filters,
                               period_labels=period_labels, period_links=period_links,
                               dimension_labels=dimension_labels,
                               dimension_links=dimension_links,
                               **ai_dashboard_data(filters))

    @registry.route("/ai-usage")
    @role_required("teacher")
    def teacher_ai_usage():
        """教师查看自己的模型请求与逐请求用量明细。"""
        db = get_db()
        teacher_id = g.user["id"]
        summary = db.execute("""
            SELECT COUNT(e.id) AS request_count,
              SUM(e.prompt_tokens) AS prompt_tokens,
              SUM(e.completion_tokens) AS completion_tokens,
              SUM(e.total_tokens) AS total_tokens,
              SUM(e.cached_tokens) AS cached_tokens,
              SUM(e.reasoning_tokens) AS reasoning_tokens,
              SUM(e.estimated_cost) AS estimated_cost
            FROM ai_operations o
            LEFT JOIN ai_usage_events e ON e.operation_id=o.id
            WHERE o.teacher_id=?
        """, (teacher_id,)).fetchone()
        operations = db.execute("SELECT COUNT(*) FROM ai_operations WHERE teacher_id=?",
                                (teacher_id,)).fetchone()[0]
        page_size = 100
        pages = max(1, (summary["request_count"] + page_size - 1) // page_size)
        page = request.args.get("page", 1, type=int) or 1
        page = max(1, min(page, pages))
        operation_rows = db.execute("""
            SELECT o.*,COUNT(e.id) AS request_count,
              SUM(CASE WHEN e.request_path<>'' OR e.response_path<>'' THEN 1 ELSE 0 END)
                AS payload_event_count,
              SUM(CASE WHEN e.debug_error<>'' THEN 1 ELSE 0 END) AS debug_error_count,
              c.name AS course_name,l.title AS lecture_title,u.display_name AS student_name
            FROM ai_operations o
            LEFT JOIN ai_usage_events e ON e.operation_id=o.id
            LEFT JOIN courses c ON c.id=o.course_id
            LEFT JOIN submissions s ON s.id=o.submission_id
            LEFT JOIN lectures l ON l.id=COALESCE(o.lecture_id,s.lecture_id)
            LEFT JOIN users u ON u.id=s.student_id
            WHERE o.teacher_id=?
            GROUP BY o.id ORDER BY o.id DESC LIMIT 100
        """, (teacher_id,)).fetchall()
        requests = db.execute("""
            SELECT e.id,e.operation_id,e.chunk_id,e.attempt,e.provider_request_id,
              COALESCE(NULLIF(e.model,''),NULLIF(o.model,''),'未指定模型') AS model,
              e.prompt_tokens,e.completion_tokens,e.total_tokens,e.cached_tokens,
              e.reasoning_tokens,e.latency_ms,e.estimated_cost,e.finish_reason,
              e.pricing_json,e.usage_json,e.status AS event_status,e.error AS event_error,
              e.created_at,o.operation_type,o.status AS operation_status,
              o.started_at,o.finished_at,o.error AS operation_error,c.name AS course_name,
              l.title AS lecture_title,u.display_name AS student_name
            FROM ai_usage_events e
            JOIN ai_operations o ON o.id=e.operation_id
            LEFT JOIN courses c ON c.id=o.course_id
            LEFT JOIN submissions s ON s.id=o.submission_id
            LEFT JOIN lectures l ON l.id=COALESCE(o.lecture_id,s.lecture_id)
            LEFT JOIN users u ON u.id=s.student_id
            WHERE o.teacher_id=?
            ORDER BY e.id DESC LIMIT ? OFFSET ?
        """, (teacher_id, page_size, (page - 1) * page_size)).fetchall()
        return render_template("teacher_ai_usage.html", summary=summary,
                               operations=operations, operation_rows=operation_rows,
                               requests=requests,
                               page=page, pages=pages, page_size=page_size,
                               ai_operation_label=ai_operation_label,
                               ai_status_label=ai_status_label)

    # ---------- 老师：AI 操作调试日志 ----------
    def owned_debug_operation(operation_id):
        row = get_db().execute("""
            SELECT o.*,c.name AS course_name,l.title AS lecture_title,
              u.display_name AS student_name
            FROM ai_operations o
            LEFT JOIN courses c ON c.id=o.course_id
            LEFT JOIN submissions s ON s.id=o.submission_id
            LEFT JOIN lectures l ON l.id=COALESCE(o.lecture_id,s.lecture_id)
            LEFT JOIN users u ON u.id=s.student_id
            WHERE o.id=? AND o.teacher_id=?
        """, (operation_id, g.user["id"])).fetchone()
        if row is None:
            abort(404)
        return row

    def debug_events(operation_id):
        return get_db().execute(
            "SELECT * FROM ai_usage_events WHERE operation_id=? "
            "ORDER BY COALESCE(call_index,id),attempt,id", (operation_id,)).fetchall()

    @registry.route("/ai-usage/operations/<oid:operation_id>/debug")
    @role_required("teacher")
    def teacher_ai_operation_debug(operation_id):
        """新窗口查看一次操作的全部已保存请求、响应和最终业务结果。"""
        operation = owned_debug_operation(operation_id)
        events = debug_events(operation_id)
        result_pretty = ""
        if operation["result_json"]:
            try:
                result_pretty = json.dumps(json.loads(operation["result_json"]),
                                           ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                result_pretty = operation["result_json"]
        return render_template(
            "teacher_ai_debug.html", operation=operation, events=events,
            result_pretty=result_pretty,
            ai_operation_label=ai_operation_label, ai_status_label=ai_status_label)

    @registry.route("/ai-usage/events/<oid:event_id>/raw/<kind>")
    @role_required("teacher")
    def teacher_ai_event_raw(event_id, kind):
        """原文预览限 256 KiB；download=1 时流式下载完整安全副本。"""
        if kind not in ("request", "response"):
            abort(404)
        event = get_db().execute("""
            SELECT e.* FROM ai_usage_events e
            JOIN ai_operations o ON o.id=e.operation_id
            WHERE e.id=? AND o.teacher_id=?
        """, (event_id, g.user["id"])).fetchone()
        if event is None or not event[f"{kind}_path"]:
            abort(404)
        operation_id = event["operation_id"]
        relative_path = event[f"{kind}_path"]
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        try:
            if request.args.get("download") == "1":
                ai_debug.resolve_payload(operation_id, relative_path)
                extension = ".json" if relative_path.endswith(".json.gz") else ".bin"
                filename = f"ai_event_{event_id}_{kind}{extension}"
                headers["Content-Disposition"] = f'attachment; filename="{filename}"'
                return Response(
                    stream_with_context(ai_debug.iter_payload(operation_id, relative_path)),
                    content_type="application/octet-stream", headers=headers,
                    direct_passthrough=True)
            data, truncated = ai_debug.read_preview(operation_id, relative_path)
        except (OSError, FileNotFoundError):
            abort(404)
        text = data.decode("utf-8", "replace")
        if truncated:
            text += "\n\n[预览已截断；请下载此原文或完整 ZIP 查看剩余内容。]"
        return Response(text, content_type="text/plain; charset=utf-8", headers=headers)

    @registry.route("/ai-usage/operations/<oid:operation_id>/debug.zip")
    @role_required("teacher")
    def teacher_ai_operation_debug_zip(operation_id):
        """流式生成完整 ZIP，不把 base64 等大载荷塞入数据库或详情 HTML。"""
        operation = owned_debug_operation(operation_id)
        events = debug_events(operation_id)
        operation_data = dict(operation)
        for field in ("started_at", "finished_at"):
            if operation_data.get(field):
                operation_data[field] = beijing_iso(operation_data[field], "legacy")
        if operation_data.get("result_json"):
            try:
                operation_data["result"] = json.loads(operation_data["result_json"])
            except (TypeError, ValueError):
                pass
        event_data = []
        for row in events:
            item = dict(row)
            if item.get("created_at"):
                item["created_at"] = beijing_iso(item["created_at"], "UTC")
            for source, target in (("usage_json", "usage"),
                                   ("pricing_json", "pricing"),
                                   ("debug_json", "debug")):
                if not item.get(source):
                    continue
                try:
                    item[target] = json.loads(item[source])
                except (TypeError, ValueError):
                    pass
            event_data.append(item)
        debug_failed = bool(operation["debug_error"] or any(
            row["debug_error"] for row in events))
        manifest = {
            "format": "gradelogy-ai-debug-v1",
            "generated_at": now_text(),
            "timezone": "Asia/Shanghai",
            "operation_id": operation_id,
            "event_count": len(events),
            "complete": not debug_failed,
            "missing_files": [],
            "notes": [
                "导出包中的操作、请求和事件时间均为北京时间（UTC+08:00）。",
                "请求和响应为安全副本：仅将配置 API Key 的精确出现替换为 [REDACTED]。",
                "非流式请求不存在可伪造的逐 token 中间状态；每轮完整响应均保存在对应事件中。",
                "历史操作可能没有原始载荷；网络失败时没有服务商响应正文属于正常情况。",
                "日志包含真实提示词、学生作答与图片 base64，仅应发给可信开发人员。",
            ],
        }
        if debug_failed:
            manifest["notes"].append("部分调试载荷写入失败，详见 operation/event 的 debug_error。")
        bundle = tempfile.TemporaryFile()
        try:
            ai_debug.export_package(bundle, manifest, operation_data, event_data)
            bundle.seek(0)
        except Exception:
            bundle.close()
            raise
        response = send_file(
            bundle, mimetype="application/zip", as_attachment=True,
            download_name=f"ai_operation_{operation_id}_debug.zip", max_age=0)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.call_on_close(bundle.close)
        return response

    # ---------- 老师：AI 设置 ----------
    def settings_context(extra=None):
        conf = {k: get_setting(g.user["id"], k)
                for k in ("ai_api_url", "ai_api_key", "ai_model", "ai_demo",
                          "ai_offpeak_only", "ai_auto_grade_attempts")}
        # 各场景 max_tokens：未设置时显示注册表默认值（占位符），保存时才写入 settings
        max_tokens = {
            key: get_setting(g.user["id"], key, "") or str(cfg["default"])
            for key, cfg in MAX_TOKENS_SETTINGS.items()
        }
        view = build_model_rows(g.user["id"], conf["ai_api_url"], get_db())
        api_keys = api_key_rows(g.user["id"])
        ctx = {
            "cfg": conf,
            "env_keys": [k for k, v in env_ai_settings.items() if v],
            "model_catalog": model_catalog,
            "model_view": view,
            "model_rows": view["rows"],
            "est_tokens_input": est_tokens_input,
            "est_tokens_output": est_tokens_output,
            "price_import": None,
            "api_keys": api_keys,
            "active_api_key": next((k for k in api_keys if k["is_active"]), None),
            "api_configured": bool(conf["ai_api_key"]),
            # 只有环境变量接管了 Key 才禁用页面管理；仅注入 AI_API_URL 时仍可保存多套配置
            "api_env_managed": bool(env_ai_settings.get("ai_api_key")),
            "max_tokens_settings": MAX_TOKENS_SETTINGS,
            "max_tokens": max_tokens,
        }
        if extra:
            ctx.update(extra)
        return ctx

    @registry.route("/settings", methods=["GET", "POST"])
    @role_required("teacher")
    def settings_page():
        if request.method == "POST":
            if request.form.get("demo_only"):
                save_setting(g.user["id"], "ai_demo", "1" if request.form.get("ai_demo") else "")
            else:
                # 模型由每套 API 配置单独保存，这里维护批阅开关与各场景 max_tokens
                save_setting(g.user["id"], "ai_demo", "1" if request.form.get("ai_demo") else "")
                save_setting(g.user["id"], "ai_offpeak_only",
                             "1" if request.form.get("ai_offpeak_only") else "")
                raw_limit = (request.form.get("ai_auto_grade_attempts") or "").strip()
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError):
                    limit = 0
                save_setting(g.user["id"], "ai_auto_grade_attempts",
                             str(limit) if limit > 0 else "")
                for key in MAX_TOKENS_SETTINGS:
                    raw = (request.form.get(key) or "").strip()
                    if raw:
                        try:
                            value = int(raw)
                            if value > 0:
                                save_setting(g.user["id"], key, str(value))
                            else:
                                save_setting(g.user["id"], key, "")
                        except (TypeError, ValueError):
                            save_setting(g.user["id"], key, "")
                    else:
                        save_setting(g.user["id"], key, "")
            get_db().commit()
            flash("已保存")
            return redirect(url_for("settings_page"))
        return render_template("settings.html", **settings_context())

    @registry.route("/settings/api-keys/add", methods=["POST"])
    @role_required("teacher")
    def settings_api_key_add():
        if env_ai_settings.get("ai_api_key"):
            flash("API Key 已由环境变量注入，页面上的 Key 管理不会生效")
            return redirect(url_for("settings_page"))
        name = (request.form.get("name") or "").strip()[:60]
        base_url = (request.form.get("base_url") or "").strip().rstrip("/")
        api_key = (request.form.get("api_key") or "").strip()
        model = (request.form.get("model") or "").strip()[:120]
        if not _valid_api_base_url(base_url):
            flash("接口基地址需要以 http:// 或 https:// 开头")
            return redirect(url_for("settings_page"))
        if not api_key:
            flash("请填写 API Key")
            return redirect(url_for("settings_page"))
        db = get_db()
        cur = db.execute(
            "INSERT INTO ai_api_keys(teacher_id,name,base_url,api_key,model) VALUES(?,?,?,?,?)",
            (g.user["id"], name or "未命名 Key", base_url, api_key, model))
        # 勾选「保存后立即使用」或当前还没有可用 Key 时，新 Key 直接生效
        if request.form.get("use_now") or not any(k["is_active"] for k in api_key_rows(g.user["id"])):
            activate_api_key(g.user["id"], cur.lastrowid)
        db.commit()
        flash("已保存 API Key")
        return redirect(url_for("settings_page"))

    @registry.route("/settings/api-keys/<int:key_id>/activate", methods=["POST"])
    @role_required("teacher")
    def settings_api_key_activate(key_id):
        if env_ai_settings.get("ai_api_key"):
            flash("API Key 已由环境变量注入，页面上的 Key 管理不会生效")
            return redirect(url_for("settings_page"))
        if activate_api_key(g.user["id"], key_id):
            get_db().commit()
            flash("已切换当前使用的 AI API")
        else:
            flash("未找到该 API Key")
        return redirect(url_for("settings_page"))

    @registry.route("/settings/api-keys/<int:key_id>/model", methods=["POST"])
    @role_required("teacher")
    def settings_api_key_model(key_id):
        """每套 API 配置单独保存模型，切换 Key 时互不影响。"""
        if env_ai_settings.get("ai_api_key"):
            flash("API Key 已由环境变量注入，模型请通过环境变量配置")
            return redirect(url_for("settings_page"))
        model = (request.form.get("model") or "").strip()[:120]
        db = get_db()
        row = db.execute("SELECT is_active FROM ai_api_keys WHERE id=? AND teacher_id=?",
                         (key_id, g.user["id"])).fetchone()
        if row is None:
            flash("未找到该 API Key")
            return redirect(url_for("settings_page"))
        db.execute("UPDATE ai_api_keys SET model=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (model, key_id))
        if row["is_active"] and not env_ai_settings.get("ai_model"):
            save_setting(g.user["id"], "ai_model", model)
        db.commit()
        flash("已保存该配置的模型")
        return redirect(url_for("settings_page"))

    @registry.route("/settings/api-keys/<int:key_id>/delete", methods=["POST"])
    @role_required("teacher")
    def settings_api_key_delete(key_id):
        if env_ai_settings.get("ai_api_key"):
            flash("API Key 已由环境变量注入，页面上的 Key 管理不会生效")
            return redirect(url_for("settings_page"))
        db = get_db()
        row = db.execute("SELECT is_active FROM ai_api_keys WHERE id=? AND teacher_id=?",
                         (key_id, g.user["id"])).fetchone()
        if row is None:
            flash("未找到该 API Key")
            return redirect(url_for("settings_page"))
        db.execute("DELETE FROM ai_api_keys WHERE id=?", (key_id,))
        if row["is_active"]:
            # 删除当前 Key 后自动切换到最近保存的一条，避免批改突然无法调用
            nxt = db.execute("SELECT id FROM ai_api_keys WHERE teacher_id=? "
                             "ORDER BY id DESC LIMIT 1", (g.user["id"],)).fetchone()
            db.commit()
            if nxt:
                activate_api_key(g.user["id"], nxt["id"])
            else:
                save_setting(g.user["id"], "ai_api_url", "")
                save_setting(g.user["id"], "ai_api_key", "")
                save_setting(g.user["id"], "ai_model", "")
        db.commit()
        flash("已删除该 API Key")
        return redirect(url_for("settings_page"))

    @registry.route("/settings/models/sync", methods=["POST"])
    @role_required("teacher")
    def settings_models_sync():
        """按当前配置立即拉一次 GET /v1/models，刷新可用模型。"""
        count, err = sync_models_for_teacher(g.user["id"], force=True)
        if err:
            flash(f"模型同步失败：{err}")
        elif count == 0:
            flash("接口返回的模型列表为空，已保留上次结果")
        else:
            flash(f"已同步 {count} 个可用模型")
        return redirect(url_for("settings_page"))

    @registry.route("/settings/prices/extract", methods=["POST"])
    @role_required("teacher")
    def settings_prices_extract():
        """上传价目截图，调用当前视觉模型抽出模型单价，先给用户确认再入库。"""
        file = request.files.get("price_image")
        if file is None or not file.filename:
            flash("请先选择一张价目截图")
            return redirect(url_for("settings_page"))
        raw = file.read()
        if len(raw) > 8 * 1024 * 1024:
            flash("截图超过 8MB，请压缩后再上传")
            return redirect(url_for("settings_page"))
        mime = _image_mime(raw)
        if not mime:
            flash("仅支持 PNG / JPG / WebP 截图")
            return redirect(url_for("settings_page"))

        conf = llm.resolve_config(g.user["id"])
        if get_setting(g.user["id"], "ai_demo") == "1":
            flash("演示模式无法识别截图，请先切换到真实接口")
            return redirect(url_for("settings_page"))
        if not (conf["api_url"] and conf["api_key"] and conf["model"]):
            flash("请先配置并保存真实接口的基地址、API Key 和模型")
            return redirect(url_for("settings_page"))

        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode()
        operation = start_ai_operation(g.user["id"], "price_import", conf["model"])
        reply, finish, err = llm.chat(
            conf["api_url"], conf["api_key"], conf["model"],
            [{"type": "text", "text": PRICE_IMAGE_PROMPT},
             {"type": "image_url", "image_url": {"url": data_url}}],
            max_tokens=32768, timeout=120, retries=2, usage_hook=operation["usage_hook"])
        if err:
            finish_ai_operation(operation, "failed", err)
            flash(f"截图识别失败：{err}")
            return redirect(url_for("settings_page"))
        payload = llm.extract_json(reply)
        if not isinstance(payload, dict):
            finish_ai_operation(operation, "failed", "模型未返回可解析的 JSON")
            flash("模型没有返回可解析的价目 JSON，请换一张更清晰的截图或稍后重试")
            return redirect(url_for("settings_page"))

        rows, seen = [], set()
        for item in payload.get("rows") or []:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "").strip()
            try:
                price_in, price_out = float(item.get("price_in")), float(item.get("price_out"))
            except (TypeError, ValueError):
                continue
            if not _valid_model_name(model) or model in seen:
                continue
            if not (0 <= price_in <= 100000 and 0 <= price_out <= 100000):
                continue
            seen.add(model)
            rows.append({"model": model, "price_in": round(price_in, 4), "price_out": round(price_out, 4)})
        unclear = [str(x)[:60] for x in (payload.get("unclear") or [])][:20]
        finish_ai_operation(operation, "success")

        if not rows:
            flash("没有识别到清晰的价格行，请确认截图包含模型名和输入/输出单价")
            return redirect(url_for("settings_page"))
        return render_template("settings.html", **settings_context({
            "price_import": {"rows": rows, "unclear": unclear,
                             "filename": secure_filename(file.filename) or "价目截图"},
        }))

    @registry.route("/settings/prices/save", methods=["POST"])
    @role_required("teacher")
    def settings_prices_save():
        """保存用户在预览里确认过的价目；按模型 ID 全局生效。"""
        models = request.form.getlist("model")
        pins = request.form.getlist("price_in")
        pouts = request.form.getlist("price_out")
        db = get_db()
        now = now_text()
        saved = 0
        for model, pin, pout in zip(models, pins, pouts):
            model = model.strip()
            if not _valid_model_name(model):
                continue
            try:
                price_in, price_out = float(pin), float(pout)
            except (TypeError, ValueError):
                continue
            if not (0 <= price_in <= 100000 and 0 <= price_out <= 100000):
                continue
            db.execute(
                "INSERT INTO model_prices(model,price_in,price_out,unit,source,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET "
                "price_in=excluded.price_in, price_out=excluded.price_out, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (model, price_in, price_out, "元/百万 token", "截图识别", now))
            saved += 1
        db.commit()
        flash(f"已更新 {saved} 个模型的价格（对所有老师生效）" if saved else "没有可保存的价格行")
        return redirect(url_for("settings_page"))

    @registry.route("/settings/test", methods=["POST"])
    @role_required("teacher")
    def settings_test():
        """发一个最小请求，验证当前生效的接口地址、Key 与模型是否可用。"""
        flash(probe_ai_config(get_setting(g.user["id"], "ai_api_url"),
                              get_setting(g.user["id"], "ai_api_key"),
                              get_setting(g.user["id"], "ai_model")))
        return redirect(url_for("settings_page"))

    @registry.route("/settings/api-keys/<int:key_id>/test", methods=["POST"])
    @role_required("teacher")
    def settings_api_key_test(key_id):
        """单独测试某套配置，不受当前选中项影响，方便逐套完成对接验证。"""
        row = get_db().execute(
            "SELECT name,base_url,api_key,model FROM ai_api_keys WHERE id=? AND teacher_id=?",
            (key_id, g.user["id"])).fetchone()
        if row is None:
            flash("未找到该 API Key")
            return redirect(url_for("settings_page"))
        model = row["model"] or get_setting(g.user["id"], "ai_model")
        flash(f"「{row['name']}」" + probe_ai_config(row["base_url"], row["api_key"], model))
        return redirect(url_for("settings_page"))
