# -*- coding: utf-8 -*-
"""教学管理平台
结构：课程 → 讲次（作业 + AI 批阅）/ 考试（只录分数）
角色：admin（用户管理）/ teacher（课程、讲次、考试、批改、AI 设置）/ student（提交与查看批改）
"""
import difflib
import fcntl
import hashlib
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl  # 仅类 Unix 可用，用于保证自动备份只在一个进程里跑
except ImportError:  # pragma: no cover
    fcntl = None

from flask import (Flask, abort, current_app, flash, g, has_app_context, has_request_context, jsonify, redirect,
                   render_template, request, send_file, send_from_directory, session, url_for)
from itsdangerous import BadSignature, URLSafeSerializer
from markupsafe import Markup
from werkzeug.routing import BaseConverter, ValidationError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.local import LocalProxy

import ai_routes
import config as cfg
import db as database
from ai_config import AI_SETTING_KEYS, ENV_AI_SETTINGS, MAX_TOKENS_SETTINGS, get_ai_setting
from ai_usage import (AI_OPERATION_LABEL, AI_STATUS_LABEL, _ai_dashboard_data,
                      _ai_operation_history, _ai_usage_text, _compact_number,
                      _finish_ai_operation, _now_in_peak, _start_ai_operation)
from db import DB_PATH, close_db, get_db
from grading_service import GradingQueue, latest_operation
from model_catalog import (EST_TOKENS_INPUT, EST_TOKENS_OUTPUT, MODEL_CATALOG,
                           _now_text, build_model_rows, fetch_available_models,
                           fetch_tokenhub_prices, parse_tokenhub_prices,
                           start_model_sync, tokenhub_suitable_model_ids)
from model_catalog import sync_models_for_teacher as _sync_models_for_teacher
from utilities import assistant, course_export, deletion, llm, logview, roster, storage
from utilities import ai_task_queue
from utilities import knowledge_graph as kg_utils
from utilities import kg_text
from utilities import kg_transfer
from utilities import tags as tag_utils
from utilities.teacher_views import workload
from utilities.report import _overall_text, build_report_md, render_markdown
from utilities.sessions import (
    IDLE_TIMEOUT_SECONDS, auth_session_activity, create_auth_session,
    revoke_auth_session, revoke_sessions, session_epoch, touch_auth_session)
from utilities.uploads import (_student_files, _validate_upload, allowed_file, file_list,
                               save_upload, can_read_file)
from utilities.pdfgrade import (DEFAULT_GRADE_PROMPT, DEFAULT_REPORT_PROMPT,
                                DEFAULT_TAG_HORIZONTAL_PROMPT,
                                DEFAULT_TAG_VERTICAL_PROMPT,
                                ai_horizontal_analysis, ai_lecture_summary,
                                ai_report_comment, ai_vertical_analysis,
                                analyze_answer_pdf, analyze_question_tags,
                                grade_pdf_submission, pdf_page_count,
                                render_page_jpeg)

BASE_DIR = cfg.BASE_DIR
UPLOAD_DIR = cfg.get("STORAGE_DIR")
BACKUP_DIR = cfg.get("BACKUP_DIR")
BACKUP_RE = re.compile(r"gradelogy_\d{8}_\d{6}\.zip")
# 课程备份包：course_<课程ID>_<生成时间>[_序号].zip，保留 24 小时
COURSE_EXPORT_RE = re.compile(r"course_(\d+)_(\d{8}_\d{6})(?:_(\d+))?\.zip")
COURSE_EXPORT_KEEP_HOURS = 24

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底，避免 HTTPS 调用 AI 时报 SSL 错误
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

ROLE_LABEL = {"admin": "管理员", "teacher": "老师", "student": "学生"}
DISPLAY_TIMEZONE_OPTIONS = (
    "Asia/Shanghai", "UTC", "Asia/Singapore", "Asia/Tokyo", "Europe/London",
    "Europe/Berlin", "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "Australia/Sydney",
)

DEFAULT_PASSWORDS = ("admin123", "123456")


# ---------- 闲时批阅：TokenHub 高峰时段价格翻倍 ----------
# 高峰定义（参考腾讯云公开价目说明）：工作日 9:00-12:00, 14:00-18:00
# 闲时窗口：非高峰（包括所有夜间、早高峰前、午间午休、以及周六周日全天）
# 如果当前不在闲时且老师开启了「仅闲时批阅」，则拦截 AI 调用
def _offpeak_block_if_enabled(teacher_id):
    """如果老师开启了「仅闲时批阅」且当前是高峰，返回提示文案；否则返回 None。"""
    if get_setting(teacher_id, "ai_offpeak_only") != "1":
        return None
    peak, end_hint = _now_in_peak()
    if not peak:
        return None
    return (f"当前是腾讯云 TokenHub 高峰时段（价格约翻倍），"
            f"已开启「仅闲时批阅」。建议 {end_hint} 之后再调用 AI。")

def load_secret_key():
    """密钥持久化，避免每次重启把所有人都登出（容器里通过 SECRET_KEY 环境变量注入）。"""
    path = cfg.get("SECRET_KEY_FILE")
    if os.path.exists(path):
        with open(path) as f:
            key = f.read().strip()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)  # 密钥只允许当前用户读写
    except OSError:
        pass
    return key


def _db_path():
    return current_app.config.get("DB_PATH", DB_PATH) if has_app_context() else DB_PATH


def _upload_dir():
    return current_app.config.get("GRADELOGY_SETTINGS", {}).get("STORAGE_DIR", UPLOAD_DIR) if has_app_context() else UPLOAD_DIR


def _backup_dir():
    return current_app.config.get("GRADELOGY_SETTINGS", {}).get("BACKUP_DIR", BACKUP_DIR) if has_app_context() else BACKUP_DIR


def _setup_tmp_root():
    return os.path.join(cfg.get("DATA_DIR"), "tmp_setup")


class AppRoutes:
    """延迟注册路由与请求钩子，由 create_app() 绑定到具体的 app 实例。

    endpoint 一律取视图函数名，因此 url_for("...") 的写法与拆分前完全一致。
    """

    def __init__(self):
        self._items = []

    def route(self, rule, **options):
        def decorator(fn):
            self._items.append(("route", (rule,), options, fn))
            return fn
        return decorator

    def before_request(self, fn):
        self._items.append(("before_request", (), {}, fn))
        return fn

    def after_request(self, fn):
        self._items.append(("after_request", (), {}, fn))
        return fn

    def errorhandler(self, code):
        def decorator(fn):
            self._items.append(("errorhandler", (code,), {}, fn))
            return fn
        return decorator

    def context_processor(self, fn):
        self._items.append(("context_processor", (), {}, fn))
        return fn

    def template_filter(self, name):
        def decorator(fn):
            self._items.append(("template_filter", (name,), {}, fn))
            return fn
        return decorator

    def apply(self, app):
        for kind, args, options, fn in self._items:
            if kind == "route":
                app.add_url_rule(args[0], endpoint=fn.__name__, view_func=fn, **options)
            elif kind == "before_request":
                app.before_request(fn)
            elif kind == "after_request":
                app.after_request(fn)
            elif kind == "errorhandler":
                app.register_error_handler(args[0], fn)
            elif kind == "context_processor":
                app.context_processor(fn)
            elif kind == "template_filter":
                app.template_filter(args[0])(fn)


_routes = AppRoutes()


class SignedIdConverter(BaseConverter):
    """把内部自增 ID 签名为不可读的 URL 片段，路由内仍拿到 int。

    只做「不暴露可枚举 ID + 防篡改」，鉴权仍由各路由的归属校验负责；
    伪造或过期的 URL 直接 404。
    """

    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = r"[A-Za-z0-9_\-\.]+"
        self.signer = url_map.gradelogy_signer

    def to_python(self, value):
        try:
            return int(self.signer.loads(value))
        except BadSignature:
            raise ValidationError()

    def to_url(self, value):
        return self.signer.dumps(int(value))


log = logging.getLogger("gradelogy.system")
user_log = logging.getLogger("gradelogy.user")
kg_log = logging.getLogger("gradelogy.kg")
grading_log = logging.getLogger("gradelogy.grading")
security_log = logging.getLogger("gradelogy.security")

APP_STARTED_AT = datetime.now()


def _read_git_info():
    """读取版本库信息：容器依赖构建参数，本地开发直接读 .git 和提交时间。"""
    commit, branch = cfg.GIT_COMMIT, cfg.GIT_BRANCH
    commit_date = cfg.GIT_COMMIT_DATE
    if commit:
        return commit[:7], branch, commit_date
    try:
        git_dir = os.path.join(BASE_DIR, ".git")
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            branch = branch or ref.rsplit("/", 1)[-1]
            with open(os.path.join(git_dir, ref), encoding="utf-8") as f:
                commit = f.read().strip()
        else:
            commit = head
        if commit and not commit_date:
            try:
                commit_date = subprocess.check_output(
                    ["git", "-C", BASE_DIR, "show", "-s", "--format=%cd",
                     "--date=format:%Y-%m-%d %H:%M", "HEAD"],
                    text=True, stderr=subprocess.DEVNULL, timeout=3,
                ).strip()
            except (OSError, subprocess.SubprocessError):
                pass
    except OSError:
        pass
    return commit[:7], branch, commit_date


APP_GIT_COMMIT, APP_GIT_BRANCH, APP_GIT_DATE = _read_git_info()

# 「帮助」弹窗里的本地文档：按角色控制可见范围，避免学生看到管理员操作说明
HELP_DOCS = {
    "student": ("学生手册", ("student", "teacher", "admin")),
    "teacher": ("老师手册", ("teacher", "admin")),
    "admin": ("管理员手册", ("admin",)),
}


@_routes.context_processor
def _inject_app_meta():
    user = getattr(g, "user", None)
    role = user["role"] if user else ""
    return {
        "app_version": cfg.APP_VERSION,
        "app_commit": APP_GIT_COMMIT,
        "app_branch": APP_GIT_BRANCH,
        "app_version_updated": _legacytime(APP_GIT_DATE),
        "app_python": platform.python_version(),
        "app_started": _legacytime(APP_STARTED_AT),
        "display_timezone": _display_timezone_name(),
        "display_timezone_options": DISPLAY_TIMEZONE_OPTIONS,
        "help_docs": [{"slug": slug, "title": title}
                      for slug, (title, roles) in HELP_DOCS.items() if role in roles],
    }


def _json_or_none(value):
    """模板里把 ai_summary 的 JSON 字符串转成 dict；空值或坏数据一律返回 None。"""
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def accuracy_text(n_right, n_partial, n_wrong):
    """作业正确率 = 完全答对题数 / 已判定题数；半对、无法判定不计为正确。"""
    judged = (n_right or 0) + (n_partial or 0) + (n_wrong or 0)
    if not judged:
        return ""
    return f"{round(n_right * 100 / judged)}%"


def _question_no_key(value):
    """题号按自然顺序排序，让例2排在例10前，同时兼容小问与文字前缀。"""
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", str(value or ""))]


def _accuracy_by_submission(db, sub_ids):
    """按提交 ID 批量计算正确率，返回 {submission_id: '75%'}。"""
    ids = [int(i) for i in sub_ids if i]
    if not ids:
        return {}
    rows = db.execute(
        "SELECT submission_id, "
        "SUM(CASE WHEN verdict='对' THEN 1 ELSE 0 END) AS n_right, "
        "SUM(CASE WHEN verdict='半对' THEN 1 ELSE 0 END) AS n_partial, "
        "SUM(CASE WHEN verdict='错' THEN 1 ELSE 0 END) AS n_wrong "
        f"FROM questions WHERE submission_id IN ({','.join('?' * len(ids))}) "
        "GROUP BY submission_id", ids).fetchall()
    return {r["submission_id"]: accuracy_text(r["n_right"], r["n_partial"], r["n_wrong"])
            for r in rows}


def _md_safe(md):
    """模板里直接用 {{ hw.summary_md|markdown_safe }} 渲染 Markdown 片段。
    返回 Markup 对象，防止 Jinja2 自动转义 HTML 标签。"""
    if not md:
        return ""
    return Markup(render_markdown(md))


def _is_valid_timezone(name):
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _zone(name):
    return ZoneInfo(name if _is_valid_timezone(name) else "Asia/Shanghai")


def _display_timezone_name():
    """当前请求的用户显示时区；无设置、非法旧值和未登录场景回退上海。"""
    if not has_request_context():
        return cfg.get("DISPLAY_TIMEZONE") if _is_valid_timezone(cfg.get("DISPLAY_TIMEZONE")) else "Asia/Shanghai"
    cached = getattr(g, "_display_timezone_name", None)
    if cached:
        return cached
    user = getattr(g, "user", None)
    preferred = get_setting(user["id"], "display_timezone") if user else ""
    for name in (preferred, cfg.get("DISPLAY_TIMEZONE"), "Asia/Shanghai"):
        if _is_valid_timezone(name):
            g._display_timezone_name = name
            return name
    return "Asia/Shanghai"  # pragma: no cover - 最后候选始终有效


def _parse_display_time(value, source):
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        if source == "UTC":
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=_zone(cfg.get("LEGACY_TIMEZONE") if source == "legacy" else source))
    return dt


def _format_display_time(value, source="UTC", fmt="%Y-%m-%d %H:%M"):
    if not value:
        return ""
    dt = _parse_display_time(value, source)
    if dt is None:
        return str(value)
    return dt.astimezone(_zone(_display_timezone_name())).strftime(fmt)


@_routes.template_filter("localtime")
def _localtime(value, source="UTC"):
    """UTC 或明确来源的时间统一转为当前用户显示时区。"""
    return _format_display_time(value, source)


@_routes.template_filter("legacytime")
def _legacytime(value):
    """旧应用本地 naive 时间按部署来源时区解释，避免二次加 8。"""
    return _format_display_time(value, "legacy")


@_routes.template_filter("datetime_local")
def _datetime_local(value):
    return _format_display_time(value, "legacy", "%Y-%m-%dT%H:%M")


def _deadline_storage(value):
    """用户本地截止时间转回旧存储口径，历史数据无需迁移。"""
    if not value:
        return ""
    dt = _parse_display_time(value, _display_timezone_name())
    if dt is None:
        return value
    return dt.astimezone(_zone(cfg.get("LEGACY_TIMEZONE"))).strftime("%Y-%m-%dT%H:%M")


def csrf_token():
    """为当前会话生成并复用 CSRF token；模板里用 csrf_token() 写入表单隐藏域。"""
    token = session.get("_csrf")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf"] = token
    return token


# 无需 CSRF 校验的端点：健康检查（只读）、静态资源
CSRF_EXEMPT = {"healthz", "static"}


def _ajax_request():
    return (request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.is_json
            or request.accept_mimetypes["application/json"] > 0)


def _unauthenticated_response(expired=False):
    """Give fetch clients a detectable 401, while preserving normal page redirects."""
    if expired:
        flash("登录状态已失效，请重新登录。")
    if _ajax_request():
        response = jsonify(ok=False, error="登录已失效，请重新登录。")
        response.status_code = 401
        if expired:
            response.headers["X-Session-Expired"] = "1"
        return response
    return redirect(url_for("login", next=request.path))


def _is_document_navigation():
    if request.method not in ("GET", "HEAD"):
        return False
    if request.endpoint in {"static", "healthz", "session_status", "admin_logs_tail",
                            "grading_queue", "lecture_grading_status",
                            "submission_grading_status", "knowledge_graph_status"}:
        return False
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return False
    destination = request.headers.get("Sec-Fetch-Dest")
    if destination:
        return destination == "document"
    # Fetch's default Accept is */* (which is compatible with HTML but is not a
    # document navigation); require an explicit HTML media type for old browsers.
    return "text/html" in request.headers.get("Accept", "").lower()

# ---------- 数据库 ----------
def _migrate_upload_layout(con):
    """把旧版平铺在 uploads/ 下的附件迁入分层目录，并更新数据库引用。

    新结构：
      course_<课程>/lecture_<讲次>/              教师版、学生版讲义
      course_<课程>/lecture_<讲次>/student_<学号>/ 学生提交的作业
    已是新结构（含 "/"）或磁盘上找不到的文件保持原样，交给一致性检查报告。
    该函数幂等：重复执行不会重复移动文件。
    """
    def move(name, course_id, lecture_id, student_id=0):
        rel = str(name or "").replace("\\", "/").strip("/")
        if not rel or "/" in rel:
            return name
        src = os.path.join(_upload_dir(), rel)
        if not os.path.isfile(src):
            return name
        parts = [f"course_{course_id}", f"lecture_{lecture_id}"]
        if student_id:
            parts.append(f"student_{student_id}")
        target_dir = os.path.join(_upload_dir(), *parts)
        os.makedirs(target_dir, exist_ok=True)
        stem, ext = os.path.splitext(rel)
        dst_name, n = rel, 1
        while os.path.exists(os.path.join(target_dir, dst_name)):
            dst_name = f"{stem}_{n}{ext}"
            n += 1
        shutil.move(src, os.path.join(target_dir, dst_name))
        return f"{'/'.join(parts)}/{dst_name}"

    moved = 0
    for r in con.execute("SELECT id, course_id, answer_pdf, student_pdf FROM lectures"):
        lid, cid = r[0], r[1]
        answer, student = move(r[2], cid, lid), move(r[3], cid, lid)
        if answer != r[2] or student != r[3]:
            con.execute("UPDATE lectures SET answer_pdf=?, student_pdf=? WHERE id=?",
                        (answer, student, lid))
            moved += 1
    for r in con.execute("""
            SELECT s.id, s.file_path, s.student_id, h.course_id, h.id
            FROM submissions s JOIN lectures h ON h.id=s.lecture_id"""):
        paths = file_list(r[1])
        new_paths = [move(p, r[3], r[4], r[2]) for p in paths]
        if new_paths != paths:
            stored = (new_paths[0] if len(new_paths) == 1
                      else json.dumps(new_paths, ensure_ascii=False))
            con.execute("UPDATE submissions SET file_path=? WHERE id=?", (stored, r[0]))
            moved += 1
    if moved:
        log.info("附件目录结构迁移：更新 %s 条记录", moved)


def init_db(recover_interrupted=False):
    """兼容入口：建表与迁移已收敛到 db.py，附件目录与提示词仍由本层提供。"""
    database.init_db(
        upload_migrator=_migrate_upload_layout,
        default_prompts=(("grade", "默认批改提示词", DEFAULT_GRADE_PROMPT),
                         ("report", "默认报告提示词", DEFAULT_REPORT_PROMPT),
                         ("tag_horizontal", "默认横向知识点分析提示词",
                          DEFAULT_TAG_HORIZONTAL_PROMPT),
                         ("tag_vertical", "默认纵向知识点分析提示词",
                          DEFAULT_TAG_VERTICAL_PROMPT)),
        default_passwords=DEFAULT_PASSWORDS, recover_interrupted=recover_interrupted)





def get_setting(teacher_id, key, default=""):
    # AI 配置的优先级（环境变量 / 多套 Key / 旧版 settings）统一由 ai_config 解析
    if key in AI_SETTING_KEYS:
        return get_ai_setting(teacher_id, key, default, db=get_db())
    row = get_db().execute("SELECT value FROM settings WHERE teacher_id=? AND key=?",
                           (teacher_id, key)).fetchone()
    return row["value"] if row else default


def save_setting(teacher_id, key, value):
    get_db().execute(
        "INSERT INTO settings(teacher_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(teacher_id,key) DO UPDATE SET value=excluded.value",
        (teacher_id, key, value))


def _get_max_tokens(teacher_id, key):
    """读取老师为某 AI 场景配置的 max_tokens；未设置或非法时回落到注册表默认值。"""
    default = MAX_TOKENS_SETTINGS[key]["default"]
    raw = get_setting(teacher_id, key)
    if raw == "":
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# ---------- 老师：多组 AI API Key ----------
def _mask_api_key(value):
    """列表里只展示 Key 首尾，避免明文出现在页面和截图里。"""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


def _api_key_rows(teacher_id):
    rows = get_db().execute(
        "SELECT id,name,base_url,api_key,model,is_active,created_at FROM ai_api_keys "
        "WHERE teacher_id=? ORDER BY is_active DESC, id DESC", (teacher_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["masked_key"] = _mask_api_key(item["api_key"])
        result.append(item)
    return result


def _activate_api_key(teacher_id, key_id):
    """指定当前使用的 Key：同步写回 settings，让批改、同步、测试连接沿用同一份配置。"""
    db = get_db()
    row = db.execute("SELECT base_url,api_key,model FROM ai_api_keys "
                     "WHERE id=? AND teacher_id=?", (key_id, teacher_id)).fetchone()
    if row is None:
        return False
    db.execute("UPDATE ai_api_keys SET is_active=0 WHERE teacher_id=?", (teacher_id,))
    db.execute("UPDATE ai_api_keys SET is_active=1, updated_at=CURRENT_TIMESTAMP "
               "WHERE id=?", (key_id,))
    save_setting(teacher_id, "ai_api_url", row["base_url"] or "")
    save_setting(teacher_id, "ai_api_key", row["api_key"])
    if row["model"]:
        save_setting(teacher_id, "ai_model", row["model"])
    return True


# ---------- 提示词（批改 / 报告 / 知识点分析）----------
PROMPT_KINDS = ("grade", "report", "tag_horizontal", "tag_vertical")
PROMPT_KIND_LABEL = {"grade": "批改", "report": "报告",
                     "tag_horizontal": "横向分析", "tag_vertical": "纵向分析"}
PROMPT_PLACEHOLDER_HINTS = {
    "grade": "",
    "report": "",
    "tag_horizontal": ("可用占位符：{subject_and_course}、{lecture_name}、"
                       "{total_students}、{aggregated_question_stats}"),
    "tag_vertical": ("可用占位符：{lecture_count}、{subject_and_course}、"
                     "{student_name}、{lecture_range}、{module_mastery_stats}、"
                     "{kg_weakness_chains}、{error_habit_stats}、{trend_data}"),
}


def _prompt_content(kind, prompt_id):
    """取提示词正文；0 或已失效时返回空串，由调用方回落到系统内置默认提示词。"""
    if not prompt_id:
        return ""
    row = get_db().execute("SELECT content FROM prompts WHERE id=? AND kind=?",
                           (prompt_id, kind)).fetchone()
    return (row["content"] or "").strip() if row else ""


def _prompt_options(course_id, kind):
    """课程可选的提示词：系统内置 + 本课程所有管理老师自建的。"""
    return get_db().execute("""
        SELECT p.id, p.name, p.is_default, u.display_name AS owner
        FROM prompts p LEFT JOIN users u ON u.id=p.teacher_id
        WHERE p.kind=? AND (p.is_default=1 OR p.teacher_id IN
              (SELECT teacher_id FROM course_teachers WHERE course_id=?))
        ORDER BY p.is_default DESC, p.id""", (kind, course_id)).fetchall()


# ---------- 访问日志 ----------
@_routes.before_request
def _mark_request_start():
    g.request_started = time.perf_counter()


@_routes.after_request
def _access_log(resp):
    """每个请求写入一条访问日志；静态资源与健康检查跳过，避免轮询刷屏。"""
    if request.endpoint == "knowledge_graph_status":
        kg_log.debug("KG 状态轮询 path=%s status=%s ip=%s",
                     request.path, resp.status_code, request.remote_addr or "-")
        return resp
    if request.endpoint in ("static", "healthz", "admin_logs_tail",
                            "session_status", "session_activity"):
        return resp
    started = getattr(g, "request_started", None)
    cost_ms = (time.perf_counter() - started) * 1000 if started else 0.0
    user = g.get("user")
    who = f"{user['username']}#{user['id']}" if user else "-"
    log.info("HTTP %s %s %s %.0fms user=%s ip=%s", request.method,
             request.path, resp.status_code, cost_ms, who, request.remote_addr or "-")
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and user
            and 200 <= resp.status_code < 400):
        objects = ",".join(f"{key}={value}" for key, value in
                           sorted((request.view_args or {}).items())) or "-"
        user_log.info(
            "用户写操作 user=%s#%s role=%s method=%s endpoint=%s path=%s "
            "objects=%s status=%s ip=%s",
            user["username"], user["id"], user["role"], request.method,
            request.endpoint, request.path, objects, resp.status_code,
            request.remote_addr or "-")
    # 批改是长任务，单独看；其余接口超过 3 秒视为慢请求
    if cost_ms >= 3000 and request.endpoint != "submission_grade":
        log.warning("慢请求 %s %s %.0fms", request.method, request.path, cost_ms)
    return resp


@_routes.after_request
def security_headers(resp):
    """统一补安全响应头：防 MIME 嗅探、防被第三方站点 iframe 嵌套、限制外链与内联资源来源。"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "frame-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


# ---------- 登录与权限 ----------
@_routes.before_request
def load_user():
    g.session_expired = False
    g.session_last_activity_at = None
    uid = session.get("uid")
    g.user = None
    if uid:
        if session.get("auth_epoch", "0") != session_epoch(cfg.get("DATA_DIR")):
            session.clear()
            g.session_expired = True
            return
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None or not g.user["active"]:
            # 账号被删除或停用后，已登录的会话立即失效
            session.clear()
            g.user = None
            g.session_expired = True
            return

        db = get_db()
        sid = session.get("sid")
        timeout = int(current_app.config.get(
            "SESSION_IDLE_TIMEOUT_SECONDS", IDLE_TIMEOUT_SECONDS))
        if sid:
            last_activity = auth_session_activity(db, sid, uid, idle_timeout=timeout)
            if last_activity is None:
                session.clear()
                g.user = None
                g.session_expired = True
                return
        else:
            # 一次性兼容旧版签名 Cookie：升级后的首个有效请求开始记录服务端空闲时间。
            # 之后每个会话都必须有服务端记录；缺失/撤销的 sid 不会被重新签发。
            sid, last_activity = create_auth_session(db, uid, idle_timeout=timeout)
            db.commit()
            session["sid"] = sid
            session.modified = True

        if _is_document_navigation():
            last_activity = touch_auth_session(db, sid, uid)
            if last_activity is None:
                session.clear()
                g.user = None
                g.session_expired = True
                return
            session.modified = True
        g.session_last_activity_at = last_activity


@_routes.before_request
def limit_body_size():
    """按端点限制请求体大小：恢复备份与知识图谱资料走大上限，其余用 MAX_UPLOAD_MB。

    必须排在 CSRF 校验之前：CSRF 会解析表单，超大请求应在解析前就拒绝。
    """
    if request.endpoint in ("admin_restore", "knowledge_graph_import"):
        cap = cfg.get("MAX_RESTORE_MB")
    elif request.endpoint == "knowledge_graph_upload":
        cap = cfg.get("KG_MAX_UPLOAD_MB")
    else:
        cap = cfg.get("MAX_UPLOAD_MB")
    if (request.content_length or 0) > cap * 1024 * 1024:
        abort(413, description=f"上传内容超过 {cap}MB 限制。")


@_routes.before_request
def csrf_protect():
    """所有写操作必须带本次会话的 CSRF token（表单隐藏域 _csrf 或请求头 X-CSRF-Token）。"""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if getattr(g, "session_expired", False):
        return _unauthenticated_response(expired=True)
    if request.endpoint in CSRF_EXEMPT:
        return None
    if request.endpoint == "session_activity" and not g.user:
        return _unauthenticated_response(expired=True)
    expect = session.get("_csrf") or ""
    got = request.headers.get("X-CSRF-Token") or request.form.get("_csrf") or ""
    if not expect or not got or not secrets.compare_digest(expect, got):
        security_log.warning("CSRF 校验失败 method=%s path=%s ip=%s",
                             request.method, request.path, request.remote_addr)
        abort(400, description="页面已过期或请求来源不可信，请刷新页面后重试。")
    # 只有通过 CSRF 验证的用户写操作才续期；匿名登录和无效请求不算用户活动。
    if g.user and request.endpoint != "session_activity":
        last_activity = touch_auth_session(get_db(), session.get("sid"), g.user["id"])
        if last_activity is None:
            session.clear()
            g.user = None
            g.session_expired = True
            return _unauthenticated_response(expired=True)
        g.session_last_activity_at = last_activity
        session.modified = True
    return None


@_routes.before_request
def require_password_change():
    """仍在使用初始密码的账号，先改密码才能访问其他页面。"""
    if not g.user or not g.user["must_change_password"]:
        return None
    if request.endpoint in {"change_password", "logout", "static", "login", "healthz"}:
        return None
    flash("当前账号仍在使用初始密码，请先设置新密码。")
    return redirect(url_for("change_password"))


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return _unauthenticated_response(expired=getattr(g, "session_expired", False))
        return f(*a, **kw)
    return wrapper


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not g.user:
                return _unauthenticated_response(expired=getattr(g, "session_expired", False))
            if g.user["role"] not in roles:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


@_routes.errorhandler(400)
def bad_request(e):
    msg = getattr(e, "description", "") or "请求无效。"
    return render_template("error.html", code=400, msg=msg), 400


@_routes.errorhandler(413)
def too_large(e):
    security_log.warning("上传内容过大 method=%s path=%s size=%s ip=%s",
                         request.method, request.path, request.content_length or 0,
                         request.remote_addr or "-")
    msg = getattr(e, "description", "") or "上传内容过大。"
    return render_template("error.html", code=413, msg=msg), 413


@_routes.errorhandler(403)
def forbidden(_):
    user = g.get("user")
    security_log.warning("访问拒绝 user=%s method=%s path=%s ip=%s",
                         f"{user['username']}#{user['id']}" if user else "-",
                         request.method, request.path, request.remote_addr or "-")
    return render_template("error.html", code=403, msg="你没有访问该页面的权限。"), 403


@_routes.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, msg="页面或资源不存在。"), 404


def _safe_next(target):
    """只接受站内绝对路径，挡掉 //evil.com 这类协议相对跳转。"""
    return target if target and target.startswith("/") and not target.startswith("//") else ""


@_routes.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and not user["active"]:
            flash("该账号已停用，请联系管理员")
        elif user and check_password_hash(user["password_hash"], password):
            session.clear()
            db = get_db()
            sid, last_activity = create_auth_session(
                db, user["id"],
                idle_timeout=int(current_app.config["SESSION_IDLE_TIMEOUT_SECONDS"]))
            session["uid"] = user["id"]
            session["auth_epoch"] = session_epoch(cfg.get("DATA_DIR"))
            session["sid"] = sid
            session.permanent = True
            db.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (user["id"],))
            db.commit()
            g.session_last_activity_at = last_activity
            user_log.info("登录成功 user=%s#%s role=%s ip=%s",
                          username, user["id"], user["role"], request.remote_addr or "-")
            nxt = _safe_next(request.args.get("next"))
            return redirect(nxt or url_for("index"))
        security_log.warning("登录失败 user=%s ip=%s", username or "-", request.remote_addr or "-")
        flash("用户名或密码错误")
    return render_template("login.html")


@_routes.route("/session/status", methods=["GET"])
@login_required
def session_status():
    """恢复前台/休眠标签时校验会话，不把状态轮询视为用户活动。"""
    return jsonify(ok=True,
                   lastActivityAt=g.session_last_activity_at,
                   idleTimeoutSeconds=current_app.config["SESSION_IDLE_TIMEOUT_SECONDS"],
                   warningSeconds=current_app.config["SESSION_IDLE_WARNING_SECONDS"])


@_routes.route("/session/activity", methods=["POST"])
@login_required
def session_activity():
    """仅由用户输入事件或“继续使用”操作调用，服务器确认后续期。"""
    last_activity = touch_auth_session(get_db(), session.get("sid"), g.user["id"])
    if last_activity is None:
        session.clear()
        g.user = None
        g.session_expired = True
        return _unauthenticated_response(expired=True)
    session.modified = True
    g.session_last_activity_at = last_activity
    return jsonify(ok=True,
                   lastActivityAt=last_activity,
                   idleTimeoutSeconds=current_app.config["SESSION_IDLE_TIMEOUT_SECONDS"],
                   warningSeconds=current_app.config["SESSION_IDLE_WARNING_SECONDS"])


@_routes.route("/logout")
def logout():
    if g.user:
        user_log.info("退出登录 user=%s#%s ip=%s",
                      g.user["username"], g.user["id"], request.remote_addr or "-")
        revoke_auth_session(get_db(), session.get("sid"))
    if request.args.get("reason") == "idle":
        flash("因连续无操作，已自动退出登录。")
    session.clear()
    return redirect(url_for("login"))


@_routes.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old", "")
        new = request.form.get("new", "")
        if not check_password_hash(g.user["password_hash"], old):
            flash("原密码不正确")
        elif len(new) < 6:
            flash("新密码至少 6 位")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            user_log.info("修改密码 user=%s#%s ip=%s", g.user["username"], g.user["id"],
                          request.remote_addr or "-")
            flash("密码已更新")
            return redirect(url_for("index"))
    return render_template("password.html")


@_routes.route("/preferences/timezone", methods=["POST"])
@login_required
def display_timezone_save():
    name = request.form.get("display_timezone", "").strip()[:100]
    if not _is_valid_timezone(name):
        flash("请输入有效的 IANA 时区名称，例如 Asia/Shanghai")
    else:
        save_setting(g.user["id"], "display_timezone", name)
        get_db().commit()
        flash(f"显示时区已保存为 {name}")
    return redirect(url_for("change_password"))


@_routes.route("/")
def index():
    if not g.user:
        return redirect(url_for("login"))
    return redirect(url_for({"admin": "admin_users", "teacher": "courses",
                             "student": "student_home"}[g.user["role"]]))


@_routes.route("/help/<name>")
@login_required
def help_doc(name):
    """站内渲染 docs/*.md，内网/离线部署也能看使用说明。"""
    meta = HELP_DOCS.get(name)
    if not meta or g.user["role"] not in meta[1]:
        abort(404)
    path = os.path.join(BASE_DIR, "docs", f"{name}.md")
    try:
        with open(path, encoding="utf-8") as f:
            body = render_markdown(f.read())
    except OSError:
        abort(404)
    return render_template("help.html", doc_title=meta[0], body=body)


@_routes.route("/student_home")
@_routes.route("/teacher_home")
@_routes.route("/teacher")
def home_alias():
    """兼容手输的首页地址，统一按角色跳转到各自首页。"""
    return redirect(url_for("index"))


# ---------- 管理员：用户管理 ----------
USER_SORT_COLUMNS = {"username": "u.username", "role": "u.role", "active": "u.active",
                     "created_at": "u.created_at", "last_login_at": "u.last_login_at"}
USER_SORT_DEFAULT = "created_at"


def _admin_user_query():
    """按查询参数筛选 / 排序账号列表，返回 (users, filters)。"""
    args = request.args
    q = (args.get("q") or "").strip()
    role = args.get("role") if args.get("role") in ("admin", "teacher", "student") else ""
    active = args.get("active") if args.get("active") in ("0", "1") else ""
    created = args.get("created") if args.get("created") in ("today", "7d", "30d") else ""
    login = args.get("login") if args.get("login") in ("today", "7d", "30d", "never") else ""
    sort = args.get("sort") if args.get("sort") in USER_SORT_COLUMNS else USER_SORT_DEFAULT
    order = "asc" if args.get("order") == "asc" else "desc"

    where, params = [], []
    if q:
        where.append("u.username LIKE ?")
        params.append(f"%{q}%")
    if role:
        where.append("u.role=?")
        params.append(role)
    if active:
        where.append("u.active=?")
        params.append(int(active))
    if created == "today":
        where.append("date(u.created_at,'localtime')=date('now','localtime')")
    elif created in ("7d", "30d"):
        where.append("u.created_at >= datetime('now', ?)")
        params.append("-7 day" if created == "7d" else "-30 day")
    if login == "never":
        where.append("COALESCE(u.last_login_at,'')=''")
    elif login == "today":
        where.append("date(u.last_login_at,'localtime')=date('now','localtime')")
    elif login in ("7d", "30d"):
        where.append("u.last_login_at >= datetime('now', ?)")
        params.append("-7 day" if login == "7d" else "-30 day")

    sql = ["SELECT u.*,",
           "(SELECT COUNT(*) FROM course_teachers ct WHERE ct.teacher_id=u.id) AS n_courses,",
           "(SELECT COUNT(*) FROM enrollments e WHERE e.student_id=u.id) AS n_enrolled",
           "FROM users u"]
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append(f"ORDER BY {USER_SORT_COLUMNS[sort]} {order.upper()}, u.id {order.upper()}")
    users = get_db().execute(" ".join(sql), params).fetchall()
    filters = {"q": q, "role": role, "active": active, "created": created,
               "login": login, "sort": sort, "order": order}
    return users, filters


@_routes.route("/admin/users")
@role_required("admin")
def admin_users():
    users, filters = _admin_user_query()
    return render_template("admin_users.html", users=users, filters=filters,
                           pristine=_is_pristine())


@_routes.route("/admin/logs")
@role_required("admin")
def admin_logs_page():
    files = []
    for item in logview.list_logs(cfg.get("LOG_DIR")):
        item = dict(item)
        item["size_text"] = storage.human_size(item["size"])
        item["mtime_text"] = datetime.fromtimestamp(item["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
        files.append(item)
    selected = request.args.get("name", "app.log")
    names = {item["name"] for item in files}
    if selected not in names:
        selected = files[0]["name"] if files else ""
    resp = render_template("admin_logs.html", files=files, selected=selected)
    return resp, 200, {"Cache-Control": "no-store"}


@_routes.route("/admin/logs/tail")
@role_required("admin")
def admin_logs_tail():
    name = request.args.get("name", "app.log")
    cursor = None
    if "offset" in request.args:
        try:
            cursor = {key: int(request.args[key]) for key in ("offset", "dev", "ino")}
        except (KeyError, TypeError, ValueError):
            abort(400, description="日志游标无效。")
        if cursor["offset"] < 0:
            abort(400, description="日志游标无效。")
    try:
        result = logview.read_chunk(cfg.get("LOG_DIR"), name, cursor)
    except FileNotFoundError:
        abort(404)
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@_routes.route("/admin/logs/download/<name>")
@role_required("admin")
def admin_logs_download(name):
    try:
        path = logview.resolve_log(cfg.get("LOG_DIR"), name)
    except FileNotFoundError:
        security_log.warning("非法日志下载 user=%s#%s name=%s ip=%s",
                             g.user["username"], g.user["id"], name,
                             request.remote_addr or "-")
        abort(404)
    resp = send_file(path, as_attachment=True, download_name=name,
                     mimetype="text/plain; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@_routes.route("/admin_users")
def admin_users_alias():
    """兼容手输下划线的地址。"""
    return redirect(url_for("admin_users"))


@_routes.route("/admin/users/create", methods=["POST"])
@role_required("admin")
def admin_user_create():
    username = request.form.get("username", "").strip()
    name = request.form.get("display_name", "").strip() or username
    student_no = request.form.get("student_no", "").strip()
    role = request.form.get("role", "student")
    password = request.form.get("password", "") or "123456"
    if not username:
        flash("用户名不能为空")
    elif role not in ("teacher", "student", "admin"):
        flash("角色不合法")
    elif student_no and not roster.STUDENT_NO_RE.match(student_no):
        flash("学籍号不合法（1-32 位字母、数字或 _ . -）")
    elif len(name) > 32:
        flash("姓名最多 32 字")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO users(username,password_hash,display_name,role,"
                       "student_no,must_change_password) VALUES(?,?,?,?,?,?)",
                       (username, generate_password_hash(password), name, role,
                        student_no, 1 if password in DEFAULT_PASSWORDS else 0))
            db.commit()
            log.info("创建账号 operator=%s#%s user=%s role=%s",
                     g.user["username"], g.user["id"], username, role)
            flash(f"已创建{ROLE_LABEL[role]}账号 {username}，初始密码 {password}")
        except sqlite3.IntegrityError:
            flash(f"用户名 {username} 已存在")
    return redirect(url_for("admin_users"))


def _unique_username(db, base, used):
    """按拼音基名生成不重复用户名：已存在则依次追加 2、3、4…"""
    candidate, n = base, 1
    while candidate in used or db.execute(
            "SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base}{n}"
    used.add(candidate)
    return candidate


@_routes.route("/admin/users/import", methods=["POST"])
@role_required("admin")
def admin_users_import():
    """批量建号：CSV 单列姓名，按拼音自动生成用户名。"""
    role = request.form.get("role", "student")
    if role not in ("teacher", "student"):
        flash("批量导入只支持老师或学生账号")
        return redirect(url_for("admin_users"))
    f = request.files.get("file")
    if not f or not f.filename:
        flash("请选择 CSV 文件")
        return redirect(url_for("admin_users"))
    if not f.filename.lower().endswith((".csv", ".txt")):
        flash("只支持 .csv 文件（Excel 可另存为 CSV）")
        return redirect(url_for("admin_users"))
    raw = f.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        flash("文件超过 1MB，请拆分后再导入")
        return redirect(url_for("admin_users"))
    try:
        names, errors = roster.parse_name_csv(raw)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("admin_users"))
    if not names:
        flash("没有可导入的姓名：" + ("；".join(errors[:3]) if errors else "文件为空"))
        return redirect(url_for("admin_users"))

    db = get_db()
    used, created = set(), []
    password = "123456"
    for name in names:
        base = roster.username_from_name(name) or "user"
        username = _unique_username(db, base, used)
        db.execute("INSERT INTO users(username,password_hash,display_name,role,"
                   "must_change_password) VALUES(?,?,?,?,1)",
                   (username, generate_password_hash(password), name, role))
        created.append({"name": name, "username": username})
    db.commit()
    log.info("批量建号 operator=%s#%s role=%s count=%s",
             g.user["username"], g.user["id"], role, len(created))
    return render_template("admin_users_import_result.html", created=created,
                           errors=errors, role=role, password=password)



@_routes.route("/admin/users/<int:uid>/reset", methods=["POST"])
@role_required("admin")
def admin_user_reset(uid):
    password = request.form.get("password", "").strip() or "123456"
    db = get_db()
    db.execute("UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
               (generate_password_hash(password), 1 if password in DEFAULT_PASSWORDS else 0, uid))
    db.commit()
    log.info("重置密码 operator=%s#%s uid=%s", g.user["username"], g.user["id"], uid)
    flash(f"密码已重置为 {password}")
    return redirect(url_for("admin_users"))


@_routes.route("/admin/users/<int:uid>/edit", methods=["POST"])
@role_required("admin")
def admin_user_edit(uid):
    """修改账号主数据（姓名、学籍号），由管理员统一维护。"""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        abort(404)
    display_name = request.form.get("display_name", "").strip()
    student_no = request.form.get("student_no", "").strip()
    if not display_name:
        flash("姓名不能为空")
    elif len(display_name) > 32:
        flash("姓名最多 32 字")
    elif student_no and not roster.STUDENT_NO_RE.match(student_no):
        flash("学籍号不合法（1-32 位字母、数字或 _ . -）")
    else:
        db.execute("UPDATE users SET display_name=?, student_no=? WHERE id=?",
                   (display_name, student_no, uid))
        db.commit()
        log.info("修改账号资料 operator=%s#%s uid=%s", g.user["username"], g.user["id"], uid)
        flash(f"已更新 {user['username']} 的资料")
    return redirect(url_for("admin_users"))


@_routes.route("/admin/users/<int:uid>/toggle", methods=["POST"])
@role_required("admin")
def admin_user_toggle(uid):
    """启用 / 停用账号。停用后无法登录，也不会出现在课程的可选学生里。"""
    if uid == g.user["id"]:
        flash("不能停用当前登录的管理员账号")
        return redirect(url_for("admin_users"))
    db = get_db()
    user = db.execute("SELECT username, role, active FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        abort(404)
    active = 0 if user["active"] else 1
    db.execute("UPDATE users SET active=? WHERE id=?", (active, uid))
    db.commit()
    log.info("切换账号状态 operator=%s#%s user=%s active=%s",
             g.user["username"], g.user["id"], user["username"], active)
    if active:
        flash(f"已启用 {user['username']}，该账号可以重新登录")
    elif user["role"] == "teacher":
        n = db.execute("SELECT COUNT(*) FROM course_teachers WHERE teacher_id=?", (uid,)).fetchone()[0]
        flash(f"已停用 {user['username']}，该老师已登录的会话会立即失效；"
              f"其参与的 {n} 门课程数据保留，可在课程里移除或转交")
    else:
        flash(f"已停用 {user['username']}，该账号无法登录，课程名单与选课候选中不再显示")
    return redirect(url_for("admin_users"))


@_routes.route("/admin/users/<int:uid>/delete", methods=["POST"])
@role_required("admin")
def admin_user_delete(uid):
    if uid == g.user["id"]:
        flash("不能删除当前登录的管理员账号")
        return redirect(url_for("admin_users"))
    db = get_db()
    target = db.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
    deletion.delete_user(db, uid, g.user["id"])
    db.commit()
    log.info("删除账号 operator=%s#%s user=%s role=%s uid=%s",
             g.user["username"], g.user["id"],
             target["username"] if target else "-",
             target["role"] if target else "-", uid)
    flash("账号已删除")
    return redirect(url_for("admin_users"))


# ---------- 老师：课程 ----------
def own_course(cid):
    """课程归属校验：创建者与共同管理老师都能进入，管理员不受限。"""
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    if not course:
        abort(404)
    if g.user["role"] != "admin":
        member = db.execute("SELECT 1 FROM course_teachers WHERE course_id=? AND teacher_id=?",
                            (cid, g.user["id"])).fetchone()
        if not member:
            abort(403)
    return course


def is_course_owner(course):
    """课程创建者（或管理员）才能增删共同管理老师。"""
    return g.user["role"] == "admin" or course["teacher_id"] == g.user["id"]


@_routes.route("/courses")
@role_required("teacher")
def courses():
    rows = get_db().execute("""
        SELECT c.*,
          (SELECT COUNT(*) FROM enrollments e JOIN users u ON u.id=e.student_id
            WHERE e.course_id=c.id AND u.active=1) AS n_students,
          (SELECT COUNT(*) FROM lectures h WHERE h.course_id=c.id) AS n_lectures,
          (SELECT COUNT(*) FROM exams x WHERE x.course_id=c.id) AS n_exams,
          (SELECT COUNT(*) FROM course_teachers t WHERE t.course_id=c.id) AS n_teachers,
          (c.teacher_id=?) AS is_owner
        FROM courses c
        JOIN course_teachers ct ON ct.course_id=c.id AND ct.teacher_id=?
        ORDER BY c.id DESC""", (g.user["id"], g.user["id"])).fetchall()
    work = workload(get_db(), [row["id"] for row in rows])
    return render_template("courses.html", courses=rows, work=work)


@_routes.route("/course/create", methods=["POST"])
@role_required("teacher")
def course_create():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        cid = db.execute("INSERT INTO courses(name,description,teacher_id) VALUES(?,?,?)",
                         (name, request.form.get("description", "").strip(),
                          g.user["id"])).lastrowid
        db.execute("INSERT OR IGNORE INTO course_teachers(course_id,teacher_id) VALUES(?,?)",
                   (cid, g.user["id"]))
        db.commit()
    return redirect(url_for("courses"))


@_routes.route("/course/<oid:cid>/edit", methods=["POST"])
@role_required("teacher", "admin")
def course_edit(cid):
    course = own_course(cid)
    if not is_course_owner(course):
        abort(403)
    name = request.form.get("name", "").strip()
    if not name:
        flash("课程名称不能为空")
        return redirect(url_for("course_detail", cid=cid))
    db = get_db()
    db.execute("UPDATE courses SET name=?, description=? WHERE id=?",
               (name, request.form.get("description", "").strip(), cid))
    db.commit()
    flash("课程信息已更新")
    return redirect(url_for("course_detail", cid=cid))


def _prune_course_exports():
    """清理超过 24 小时的课程备份包。"""
    if not os.path.isdir(cfg.get("EXPORT_DIR")):
        return
    cutoff = time.time() - COURSE_EXPORT_KEEP_HOURS * 3600
    for name in os.listdir(cfg.get("EXPORT_DIR")):
        path = os.path.join(cfg.get("EXPORT_DIR"), name)
        if COURSE_EXPORT_RE.fullmatch(name) and os.path.isfile(path) \
                and os.path.getmtime(path) < cutoff:
            try:
                os.remove(path)
            except OSError:
                log.warning("课程备份清理失败：%s", path)


def _course_exports(cid):
    """列出本课程仍可下载的备份包，新的在前，最多 10 个。"""
    _prune_course_exports()
    items = []
    if not os.path.isdir(cfg.get("EXPORT_DIR")):
        return items
    for name in sorted(os.listdir(cfg.get("EXPORT_DIR")), reverse=True):
        match = COURSE_EXPORT_RE.fullmatch(name)
        path = os.path.join(cfg.get("EXPORT_DIR"), name)
        if not match or int(match.group(1)) != cid or not os.path.isfile(path):
            continue
        items.append({
            "name": name,
            "size": storage.human_size(os.path.getsize(path)),
            "created": datetime.strptime(match.group(2), "%Y%m%d_%H%M%S")
            .strftime("%Y-%m-%d %H:%M:%S")})
    return items[:10]


def _course_export_lectures(cid):
    """整理课程备份所需的讲次、学生提交与批改报告。"""
    db = get_db()
    lectures = db.execute(
        "SELECT id,title,student_pdf,answer_pdf FROM lectures WHERE course_id=? "
        "ORDER BY title COLLATE NOCASE, id", (cid,)).fetchall()
    rows = db.execute("""
        SELECT s.lecture_id, s.student_id, s.file_path, s.report_md,
               u.username, u.display_name
        FROM submissions s
        JOIN users u ON u.id=s.student_id
        JOIN lectures h ON h.id=s.lecture_id
        WHERE h.course_id=? ORDER BY s.id""", (cid,)).fetchall()
    by_lecture = {}
    for row in rows:
        students = by_lecture.setdefault(row["lecture_id"], {})
        student = students.setdefault(row["student_id"], {
            "username": row["username"], "display_name": row["display_name"],
            "files": [], "report_md": ""})
        student["files"].extend(file_list(row["file_path"]))
        if row["report_md"]:
            student["report_md"] = row["report_md"]  # 多次提交时取最新报告
    return [{"title": lecture["title"], "student_pdf": lecture["student_pdf"],
             "answer_pdf": lecture["answer_pdf"],
             "students": list(by_lecture.get(lecture["id"], {}).values())}
            for lecture in lectures]


@_routes.route("/course/<oid:cid>")
@role_required("teacher", "admin")
def course_detail(cid):
    db = get_db()
    course = own_course(cid)
    students = db.execute("""
        SELECT u.id, u.username, u.display_name, u.student_no FROM enrollments e
        JOIN users u ON u.id=e.student_id
        WHERE e.course_id=? AND u.active=1 ORDER BY u.display_name""",
        (cid,)).fetchall()
    candidates = db.execute("""
        SELECT u.id, u.username, u.display_name, u.student_no FROM users u
        WHERE u.role='student' AND u.active=1 AND u.id NOT IN
              (SELECT student_id FROM enrollments WHERE course_id=?)
        ORDER BY u.display_name""", (cid,)).fetchall()
    lectures = db.execute("""
        SELECT h.*,
          (SELECT COUNT(*) FROM submissions s WHERE s.lecture_id=h.id) AS n_subs,
          (SELECT COUNT(*) FROM enrollments e JOIN users u ON u.id=e.student_id
            WHERE e.course_id=h.course_id AND u.active=1) AS n_students
        FROM lectures h WHERE h.course_id=?
        ORDER BY h.title COLLATE NOCASE, h.id""", (cid,)).fetchall()
    plans = db.execute("SELECT * FROM lesson_plans WHERE course_id=? ORDER BY updated_at DESC",
                       (cid,)).fetchall()
    exams = db.execute("""
        SELECT e.*,
          (SELECT COUNT(*) FROM exam_scores s WHERE s.exam_id=e.id AND s.score!='') AS n_scores
        FROM exams e WHERE e.course_id=? ORDER BY e.id DESC""", (cid,)).fetchall()
    teachers = db.execute("""
        SELECT u.id, u.username, u.display_name,
               (u.id=c.teacher_id) AS is_owner
        FROM course_teachers ct JOIN users u ON u.id=ct.teacher_id
        JOIN courses c ON c.id=ct.course_id
        WHERE ct.course_id=? ORDER BY is_owner DESC, u.display_name""", (cid,)).fetchall()
    teacher_candidates = db.execute("""
        SELECT u.id, u.username, u.display_name FROM users u
        WHERE u.role='teacher' AND u.active=1 AND u.id NOT IN
              (SELECT teacher_id FROM course_teachers WHERE course_id=?)
        ORDER BY u.display_name""", (cid,)).fetchall()
    kg_options = [row for row in db.execute("""
        SELECT g.id, g.subject, g.name, g.grade_level,
          (SELECT COUNT(*) FROM kg_nodes n WHERE n.graph_id=g.id) AS n_nodes
        FROM knowledge_graphs g ORDER BY g.subject, g.name COLLATE NOCASE""")
        if row["id"] in _kg_accessible_ids(db, g.user)]
    assigned_kg_ids = {r["graph_id"] for r in db.execute(
        "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=?", (cid,))}
    return render_template("course.html", course=course, students=students,
                           work=workload(db, [cid], by_lecture=True),
                           candidates=candidates, lectures=lectures, exams=exams,
                           plans=plans, teachers=teachers,
                           teacher_candidates=teacher_candidates,
                           kg_options=kg_options, assigned_kg_ids=assigned_kg_ids,
                           grade_prompts=_prompt_options(cid, "grade"),
                           report_prompts=_prompt_options(cid, "report"),
                           tag_horizontal_prompts=_prompt_options(cid, "tag_horizontal"),
                           tag_vertical_prompts=_prompt_options(cid, "tag_vertical"),
                           exports=_course_exports(cid),
                           can_manage_teachers=is_course_owner(course))


@_routes.route("/course/<oid:cid>/export", methods=["POST"])
@role_required("teacher", "admin")
def course_export_start(cid):
    """生成整个课程的备份 zip 并直接下载，服务器保留 24 小时。"""
    course = own_course(cid)
    _prune_course_exports()
    os.makedirs(cfg.get("EXPORT_DIR"), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name, n = f"course_{cid}_{stamp}.zip", 2
    path = os.path.join(cfg.get("EXPORT_DIR"), name)
    while os.path.exists(path):
        name = f"course_{cid}_{stamp}_{n}.zip"
        path = os.path.join(cfg.get("EXPORT_DIR"), name)
        n += 1
    try:
        written, missing = course_export.build_course_zip(
            path, course["name"], _course_export_lectures(cid), _upload_dir())
    except Exception:  # noqa: BLE001 导出失败不应留下半截文件
        log.exception("课程备份导出失败 course=%s", cid)
        try:
            os.remove(path)
        except OSError:
            pass
        flash("导出失败，请稍后重试")
        return redirect(url_for("course_detail", cid=cid))
    log.info("课程备份导出 course=%s files=%s missing=%s operator=%s",
             cid, written, missing, g.user["username"])
    download = f"{course_export.safe_component(course['name'], '课程')}_课程备份_{stamp}.zip"
    return send_file(path, as_attachment=True, download_name=download)


@_routes.route("/course/<oid:cid>/export/<name>")
@role_required("teacher", "admin")
def course_export_file(cid, name):
    """下载 24 小时内生成的课程备份包，仅限本课程老师。"""
    course = own_course(cid)
    match = COURSE_EXPORT_RE.fullmatch(name or "")
    if not match or int(match.group(1)) != cid:
        abort(404)
    path = os.path.join(cfg.get("EXPORT_DIR"), name)
    if not os.path.isfile(path):
        abort(404)
    download = (f"{course_export.safe_component(course['name'], '课程')}"
                f"_课程备份_{match.group(2)}.zip")
    return send_file(path, as_attachment=True, download_name=download)


@_routes.route("/course/<oid:cid>/teachers", methods=["POST"])
@role_required("teacher")
def course_teacher_add(cid):
    """把另一名老师加入课程共同管理（仅创建者或管理员可操作）。"""
    course = own_course(cid)
    if not is_course_owner(course):
        abort(403)
    tid = request.form.get("teacher_id", "")
    db = get_db()
    target = db.execute("SELECT id, display_name FROM users WHERE id=? AND role='teacher'",
                        (tid,)).fetchone()
    if not target:
        flash("请选择有效的老师账号")
    else:
        db.execute("INSERT OR IGNORE INTO course_teachers(course_id,teacher_id) VALUES(?,?)",
                   (cid, target["id"]))
        db.commit()
        log.info("课程加老师 operator=%s#%s course=%s teacher=%s#%s",
                 g.user["username"], g.user["id"], cid, target["display_name"], target["id"])
        flash(f"已把 {target['display_name']} 加入课程共同管理")
    return redirect(url_for("course_detail", cid=cid))


@_routes.route("/course/<oid:cid>/teachers/<oid:tid>/delete", methods=["POST"])
@role_required("teacher")
def course_teacher_remove(cid, tid):
    course = own_course(cid)
    if not is_course_owner(course):
        abort(403)
    if int(tid) == course["teacher_id"]:
        flash("不能移除课程创建者")
        return redirect(url_for("course_detail", cid=cid))
    db = get_db()
    db.execute("DELETE FROM course_teachers WHERE course_id=? AND teacher_id=?", (cid, tid))
    db.commit()
    flash("已移除该共同管理老师")
    return redirect(url_for("course_detail", cid=cid))


@_routes.route("/course/<oid:cid>/delete", methods=["POST"])
@role_required("teacher")
def course_delete(cid):
    course = own_course(cid)
    if not is_course_owner(course):
        flash("只有课程创建者可以删除课程")
        return redirect(url_for("course_detail", cid=cid))
    db = get_db()
    deletion.delete_course(db, cid)
    db.commit()
    return redirect(url_for("courses"))


@_routes.route("/course/<oid:cid>/enroll", methods=["POST"])
@role_required("teacher")
def course_enroll(cid):
    db = get_db()
    student_ids = request.form.getlist("student_ids")
    # 保留旧版单值表单，避免现存书签、集成或旧客户端失效。
    if "student_ids" not in request.form:
        student_ids = request.form.getlist("student_id")

    added = already_enrolled = unavailable = 0
    # 锁住选课关系和课程教师关系的写入窗口，避免权限撤销或重复 POST
    # 在验证与插入之间造成越权/重复计数。
    db.execute("BEGIN IMMEDIATE")
    own_course(cid)
    for sid in student_ids:
        try:
            student_id = int(sid)
        except (TypeError, ValueError):
            unavailable += 1
            continue
        target = db.execute(
            "SELECT 1 FROM users WHERE id=? AND role='student' AND active=1",
            (student_id,)).fetchone()
        if not target:
            # 不区分不存在、停用或不是学生，避免泄漏账号状态。
            unavailable += 1
            continue
        if db.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?",
                      (cid, student_id)).fetchone():
            already_enrolled += 1
            continue
        inserted = db.execute(
            "INSERT OR IGNORE INTO enrollments(course_id,student_id) VALUES(?,?)",
            (cid, student_id)).rowcount
        if inserted:
            added += 1
        else:
            # 主键约束让并发/重复提交仍然幂等；冲突只计为已存在。
            already_enrolled += 1
    db.commit()
    summary = (f"批量加入结果：新增 {added} 人，已在课程中 {already_enrolled} 人，"
               f"不可用 {unavailable} 项。")
    if not student_ids:
        summary += "请选择至少一名学生后重试。"
    flash(summary)
    return redirect(url_for("course_detail", cid=cid))


@_routes.route("/course/<oid:cid>/unenroll/<oid:sid>", methods=["POST"])
@role_required("teacher")
def course_unenroll(cid, sid):
    """把学生移出课程：数据库层面删除该生在本课程内的全部记录（不可恢复）。"""
    own_course(cid)
    db = get_db()
    student = db.execute("SELECT username, display_name FROM users WHERE id=?", (sid,)).fetchone()
    if not student:
        abort(404)
    n_subs, n_questions, n_scores = deletion.unenroll(db, cid, sid)
    db.commit()
    log.info("移出学生 course=%s operator=%s#%s student=%s#%s 提交=%s 题目=%s 成绩=%s",
             cid, g.user["username"], g.user["id"], student["username"], sid,
             n_subs, n_questions, n_scores)
    flash(f"已把 {student['display_name']} 移出课程，"
          f"并删除其在本课程的 {n_subs} 份提交、{n_scores} 条考试成绩（不可恢复）")
    return redirect(url_for("course_detail", cid=cid))


# ---------- 老师：讲次 ----------
@_routes.route("/course/<oid:cid>/lecture/create", methods=["POST"])
@role_required("teacher")
def lecture_create(cid):
    own_course(cid)
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("course_detail", cid=cid))
    # 讲次 id 决定附件目录，先校验文件，建好讲次后再落盘
    pending = {}
    try:
        for field in ("answer_pdf", "student_pdf"):
            f = request.files.get(field)
            if f and f.filename and allowed_file(f.filename):
                pending[field] = (f, _validate_upload(f))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("course_detail", cid=cid))

    db = get_db()
    hid = db.execute("INSERT INTO lectures(course_id,title,description,deadline) "
                     "VALUES(?,?,?,?)",
                     (cid, title, request.form.get("description", "").strip(),
                      _deadline_storage(request.form.get("deadline", "")))).lastrowid
    paths = {"answer_pdf": "", "student_pdf": ""}
    for field, (f, fname) in pending.items():
        paths[field] = save_upload(f, cid, hid, filename=fname)
    db.execute("UPDATE lectures SET answer_pdf=?, student_pdf=? WHERE id=?",
               (paths["answer_pdf"], paths["student_pdf"], hid))
    db.commit()
    return redirect(url_for("course_detail", cid=cid))


def _page_list(value):
    """把 "1,3,5-7" 形式的页码串解析成升序页码列表；空值返回 []（表示整份文件）。"""
    out = set()
    for part in re.split(r"[,，、\s]+", str(value or "")):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-~－]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.update(range(a, b + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(p for p in out if p >= 1)


@_routes.route("/lecture/<oid:hid>")
@role_required("teacher")
def lecture_detail(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    course = own_course(hw["course_id"])
    subs = db.execute("""
        SELECT s.*, u.display_name, u.username,
          (SELECT COUNT(*) FROM questions q WHERE q.submission_id=s.id) AS n_questions,
          (SELECT COUNT(*) FROM questions q WHERE q.submission_id=s.id AND q.reviewed=1) AS n_reviewed
        FROM submissions s
        JOIN users u ON u.id=s.student_id
        WHERE s.lecture_id=? ORDER BY s.created_at DESC""", (hid,)).fetchall()
    students = db.execute("""
        SELECT u.id, u.display_name, u.username FROM enrollments e
        JOIN users u ON u.id=e.student_id
        WHERE e.course_id=? AND u.active=1 ORDER BY u.display_name""", (course["id"],)).fetchall()

    # ---------- 每题统计：对/半对/错/未作答/总人数 ----------
    qstats = db.execute("""
        SELECT q.no,
          SUM(CASE WHEN q.verdict='对' THEN 1 ELSE 0 END)   AS n_right,
          SUM(CASE WHEN q.verdict='半对' THEN 1 ELSE 0 END) AS n_partial,
          SUM(CASE WHEN q.verdict='错' THEN 1 ELSE 0 END)   AS n_wrong,
          SUM(CASE WHEN q.verdict='无法判定' THEN 1 ELSE 0 END) AS n_unknown,
          (SELECT COUNT(*) FROM submissions s WHERE s.lecture_id=? AND
            EXISTS(SELECT 1 FROM questions q2 WHERE q2.submission_id=s.id)) AS n_total_subs,
          GROUP_CONCAT(CASE WHEN q.ai_comment THEN q.ai_comment ELSE NULL END, '||SPLIT||') AS all_comments
        FROM questions q
        JOIN submissions s ON s.id=q.submission_id
        WHERE s.lecture_id=?
        GROUP BY q.no
    """, (hid, hid)).fetchall()
    qstats.sort(key=lambda row: _question_no_key(row["no"]))

    # 顶层汇总
    n_subs = len(subs)
    n_graded = sum(1 for s in subs if s["n_questions"] and s["n_questions"] > 0)
    n_reviewed = sum(1 for s in subs if s["n_questions"] and s["n_reviewed"] >= s["n_questions"])

    # 教师版 PDF 页数与已选页码：只把这些页发给模型，避免整份答案浪费 token
    answer_total = pdf_page_count(os.path.join(_upload_dir(), hw["answer_pdf"])) if hw["answer_pdf"] else 0
    answer_picked = _page_list(hw["answer_pages"])

    acc_by = _accuracy_by_submission(db, [s["id"] for s in subs])
    grade_jobs = {r["submission_id"]: r for r in db.execute(
        "SELECT o.submission_id,o.status,o.error FROM ai_operations o "
        "JOIN (SELECT submission_id,MAX(id) AS max_id FROM ai_operations "
        "WHERE operation_type='grade' GROUP BY submission_id) m ON m.max_id=o.id "
        "JOIN submissions s ON s.id=o.submission_id WHERE s.lecture_id=?", (hid,))}
    tag_by_key = tag_utils.lecture_tags_by_key(db, hid)
    tag_stats = tag_utils.lecture_stats(db, hid)
    lecture_weak_tags = [s["tag"] for s in tag_stats
                         if s.get("n_wrong", 0) > 0 or s.get("n_partial", 0) > 0]
    lecture_kg_chains = _kg_weakness_chains(db, course["id"], lecture_weak_tags)
    return render_template("lecture.html", hw=hw, course=course, subs=subs, students=students,
                           qstats=qstats, n_subs=n_subs, n_graded=n_graded, n_reviewed=n_reviewed,
                           answer_total=answer_total, answer_picked=answer_picked,
                           grade_jobs=grade_jobs,
                           grading_active=any(j["status"] in ("queued", "running")
                                              for j in grade_jobs.values()),
                           acc_by=acc_by, tag_by_key=tag_by_key, tag_stats=tag_stats,
                           lecture_kg_chains=lecture_kg_chains,
                           tag_analysis=tag_utils.latest_analysis(
                               db, "horizontal", lecture_id=hid),
                           ai_operations=_ai_operation_history(lecture_id=hid, limit=8))


@_routes.route("/lecture/<oid:hid>/upload-for", methods=["POST"])
@role_required("teacher")
def lecture_upload_for(hid):
    """老师代学生上传：把扫描件记到指定学生的提交里，后续照样走 AI 批阅。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    course = own_course(hw["course_id"])
    student_id = request.form.get("student_id", type=int)
    enrolled = db.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?",
                          (course["id"], student_id)).fetchone()
    if not student_id or not enrolled:
        flash("请选择本课程的学生")
        return redirect(url_for("lecture_detail", hid=hid))
    files = [f for f in (request.files.getlist("files") or request.files.getlist("file"))
             if f and f.filename]
    if not files:
        flash("请选择要上传的作业图片或 PDF")
        return redirect(url_for("lecture_detail", hid=hid))
    if len(files) > 20:
        flash("一次最多上传 20 个文件，请合并或分次上传")
        return redirect(url_for("lecture_detail", hid=hid))
    try:
        prepared = [(f, _validate_upload(f)) for f in files]
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("lecture_detail", hid=hid))
    names = [save_upload(f, course["id"], hid, student_id, filename=fname)
             for f, fname in prepared]
    stored = names[0] if len(names) == 1 else json.dumps(names, ensure_ascii=False)
    cur = db.execute("INSERT INTO submissions(lecture_id,student_id,file_path) VALUES(?,?,?)",
                     (hid, student_id, stored))
    db.commit()
    student = db.execute("SELECT display_name FROM users WHERE id=?", (student_id,)).fetchone()
    log.info("老师代传作业 lecture=%s student=%s files=%s operator=%s",
             hid, student_id, len(names), g.user["username"])
    if _auto_enqueue_submission(cur.lastrowid):
        flash(f"已替 {student['display_name']} 上传 {len(names)} 个文件，已自动进入批改队列。")
    else:
        flash(f"已替 {student['display_name']} 上传 {len(names)} 个文件，可点「AI 批阅」开始批改")
    return redirect(url_for("lecture_detail", hid=hid))



@_routes.route("/lecture/<oid:hid>/edit", methods=["POST"])
@role_required("teacher")
def lecture_edit(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    title = request.form.get("title", "").strip()
    if title:
        db.execute("UPDATE lectures SET title=?, description=?, deadline=? WHERE id=?",
                   (title, request.form.get("description", "").strip(),
                    _deadline_storage(request.form.get("deadline", "")), hid))
        db.commit()
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/answer", methods=["POST"])
@role_required("teacher")
def lecture_answer(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    f = request.files.get("answer_pdf")
    if f and f.filename and allowed_file(f.filename):
        try:
            name = save_upload(f, hw["course_id"], hid)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("lecture_detail", hid=hid))
        with _scope_analysis_lock:
            _advance_scope_analysis_generation(hid)
            db.execute("UPDATE lectures SET answer_pdf=?, answer_pages='', "
                        "scope_candidates='', scope_selected='' WHERE id=?", (name, hid))
            db.commit()
        flash("教师版答案已更新；旧页码和题目范围已清除，请重新生成题目清单。")
    return redirect(url_for("lecture_detail", hid=hid))


def _answer_pages_note(picked, total):
    """页码保存结果的提示文案，成功生成题目清单后会拼在前面。"""
    if not picked:
        return f"已清除页码限制：将发送教师版 PDF 的全部 {total} 页。"
    if len(picked) == total:
        return f"已保存页码：全部 {total} 页（与不限制相同）。"
    return f"已保存页码：{len(picked)}/{total} 页，只发送这些页。"


# 同一讲的答案/页码每次变更都会推进代次。队列任务写回时同时校验代次和
# 数据库中的答案配置，避免任务迟到，或配置改回原值后仍写入过期清单。
_scope_analysis_lock = threading.RLock()
_scope_analysis_generations = {}


def _advance_scope_analysis_generation(lecture_id):
    _scope_analysis_generations[lecture_id] = (
        _scope_analysis_generations.get(lecture_id, 0) + 1)
    return _scope_analysis_generations[lecture_id]


def _course_label(course):
    """课程名称 + 简介，用作分析提示词里的「学科与课程」。"""
    name = course["name"] or ""
    desc = (course["description"] or "").strip() if "description" in course.keys() else ""
    return f"{name}（{desc}）" if desc else name


def _ai_endpoint(teacher_id):
    """取当前可用的 AI 配置；不可用时返回 (\"\", \"\", \"\", 原因)。"""
    if get_setting(teacher_id, "ai_demo") == "1":
        return "", "", "", "演示模式不会调用 AI"
    block = _offpeak_block_if_enabled(teacher_id)
    if block:
        return "", "", "", block
    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    model = get_setting(teacher_id, "ai_model")
    if not api_url or not api_key or not model:
        return "", "", "", "尚未配置 AI 接口"
    return api_url, api_key, model, ""


def _generate_question_tags(hw, keys):
    """异步给作业范围内勾选的题目生成知识点标签。

    返回 (标签数或 None, 错误提示)；入队成功时返回 (None, None)，
    调用方只需提示「已加入后台队列」。
    """
    db = get_db()
    try:
        cands = json.loads(hw["scope_candidates"] or "[]")
    except (TypeError, ValueError):
        cands = []
    by_key = {str(c.get("key")): c for c in cands if isinstance(c, dict)}
    picked = [by_key[str(k)] for k in keys if str(k) in by_key]
    if not picked:
        return 0, "没有找到勾选的题目，请重新生成题目清单"
    if not hw["answer_pdf"]:
        return 0, "教师版标准答案缺失，无法生成知识点标签"
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        return 0, err
    max_tokens = _get_max_tokens(g.user["id"], "max_tokens_tagging")
    answer_pdf = hw["answer_pdf"]
    answer_pages = hw["answer_pages"]
    lecture_id = hw["id"]
    scope_selected = hw["scope_selected"] or ""
    generation = _scope_analysis_generations.get(lecture_id, 0)
    course_id = hw["course_id"]
    teacher_id = g.user["id"]
    # 优先沿用课程图谱已有节点名，减少生成后又匹配不上图谱的情况
    kg_node_names = [n["name"] for n in _course_kg_nodes(db, course_id)]

    def runner(database, operation):
        try:
            rows, run_err = analyze_question_tags(
                os.path.join(_upload_dir(), answer_pdf), picked,
                api_url, api_key, model, pages=_page_list(answer_pages),
                usage_hook=operation["usage_hook"], max_tokens=max_tokens,
                candidate_nodes=kg_node_names)
        except Exception as e:  # noqa: BLE001
            _finish_ai_operation(operation, "failed", str(e))
            log.exception("生成知识点标签失败 lecture=%s", lecture_id)
            return
        if run_err:
            _finish_ai_operation(operation, "failed", run_err)
            return
        with _scope_analysis_lock:
            current = database.execute(
                "SELECT answer_pdf,answer_pages,scope_selected FROM lectures WHERE id=?",
                (lecture_id,)).fetchone()
            try:
                current_scope = json.loads(current["scope_selected"] or "[]") if current else []
            except (TypeError, ValueError):
                current_scope = []
            if (not current or _scope_analysis_generations.get(lecture_id, 0) != generation
                    or (current["answer_pdf"] or "") != answer_pdf
                    or (current["answer_pages"] or "") != answer_pages
                    or set(map(str, current_scope)) != set(map(str, keys))):
                _finish_ai_operation(
                    operation, "failed", "答案、页码或作业范围已更改，已忽略过期知识点标签")
                return
            database.execute("DELETE FROM question_tags WHERE lecture_id=? AND source='ai'",
                             (lecture_id,))
            # 老师手动维护的标签优先：重新标注时不再重复写入同名标签。
            # 手动标签按题号保存（题目清单重建后 q0/q1 可能指向别的题），所以按题号比对。
            manual = {(str(r["question_no"]), r["tag"]) for r in database.execute(
                "SELECT question_no,tag FROM question_tags"
                " WHERE lecture_id=? AND source='manual'", (lecture_id,))}
            count = 0
            for row in rows:
                seen = set()
                for tag in row["tags"]:
                    if tag in seen or (str(row["no"]), tag) in manual:
                        continue
                    seen.add(tag)
                    database.execute(
                        "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,"
                        "category,source) VALUES(?,?,?,?,?,'ai')",
                        (lecture_id, row["key"], row["no"], tag, row["category"]))
                    count += 1
            _finish_ai_operation(operation, "success")
            log.info("生成知识点标签 lecture=%s 题数=%s 标签数=%s", lecture_id, len(rows), count)

    operation, queue_err = _ai_task_queue.submit(
        teacher_id, "question_tagging", model=model, runner=runner,
        course_id=course_id, lecture_id=lecture_id,
        dedup_key=f"question_tagging|{lecture_id}")
    if queue_err:
        return 0, queue_err
    return None, None


@_routes.route("/lecture/<oid:hid>/answer/pages", methods=["POST"])
@role_required("teacher")
def lecture_answer_pages(hid):
    """保存教师要发送给模型的页码，并异步用这些页生成题目清单。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    total = pdf_page_count(os.path.join(_upload_dir(), hw["answer_pdf"])) if hw["answer_pdf"] else 0
    if not total:
        flash("请先上传教师版标准答案 PDF，再选择页码。")
        return redirect(url_for("lecture_detail", hid=hid))
    picked = sorted({int(v) for v in request.form.getlist("page")
                     if v.isdigit() and 1 <= int(v) <= total})
    stored_pages = ",".join(str(p) for p in picked)
    with _scope_analysis_lock:
        current = db.execute(
            "SELECT answer_pdf,answer_pages FROM lectures WHERE id=?", (hid,)).fetchone()
        if not current or current["answer_pdf"] != hw["answer_pdf"]:
            flash("教师版答案已更新，请重新选择页码。")
            return redirect(url_for("lecture_detail", hid=hid))
        changed = (current["answer_pages"] or "") != stored_pages
        if changed:
            generation = _advance_scope_analysis_generation(hid)
            db.execute("UPDATE lectures SET answer_pages=?, scope_candidates='', "
                       "scope_selected='' WHERE id=?", (stored_pages, hid))
        else:
            generation = _scope_analysis_generations.get(hid, 0)
            db.execute("UPDATE lectures SET answer_pages=? WHERE id=?",
                       (stored_pages, hid))
        db.commit()
    note = _answer_pages_note(picked, total)
    if changed:
        note += " 已清除旧题目范围，待新清单生成后重新选择。"
    teacher_id = g.user["id"]
    if get_setting(teacher_id, "ai_demo") == "1":
        flash(f"{note} 演示模式不会调用 AI，无法生成题目清单。")
        return redirect(url_for("lecture_detail", hid=hid))
    block = _offpeak_block_if_enabled(teacher_id)
    if block:
        flash(f"{note} {block}")
        return redirect(url_for("lecture_detail", hid=hid))
    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    model = get_setting(teacher_id, "ai_model")
    if not api_url or not api_key or not model:
        flash(f"{note} 尚未配置 AI 接口，无法生成题目清单。")
        return redirect(url_for("lecture_detail", hid=hid))
    max_tokens = _get_max_tokens(teacher_id, "max_tokens_answer_pdf")
    answer_pdf = hw["answer_pdf"]
    course_id = hw["course_id"]

    def runner(database, operation):
        try:
            questions, err = analyze_answer_pdf(
                os.path.join(_upload_dir(), answer_pdf), api_url, api_key, model,
                pages=picked, usage_hook=operation["usage_hook"], max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            _finish_ai_operation(operation, "failed", str(e))
            log.exception("生成题目清单失败 lecture=%s", hid)
            return
        if err:
            _finish_ai_operation(operation, "failed", err)
            return
        if not err:
            with _scope_analysis_lock:
                current_generation = _scope_analysis_generations.get(hid, 0)
                cursor = None
                if current_generation == generation:
                    cursor = database.execute(
                        "UPDATE lectures SET scope_candidates=?, scope_selected='' "
                        "WHERE id=? AND COALESCE(answer_pdf,'')=? "
                        "AND COALESCE(answer_pages,'')=?",
                        (json.dumps(questions, ensure_ascii=False), hid,
                         answer_pdf or "", stored_pages))
                if cursor is None or cursor.rowcount != 1:
                    _finish_ai_operation(
                        operation, "failed", "答案文件或页码已更改，已忽略过期题目清单")
                    return
                _finish_ai_operation(operation, "success")
            log.info("作业范围题目清单生成成功 lecture=%s 题目=%s", hid, len(questions))

    log.info("分析作业范围 lecture=%s answer_pdf=%s pages=%s", hid, answer_pdf, picked)
    operation, queue_err = _ai_task_queue.submit(
        teacher_id, "scope_analyze", model=model, runner=runner,
        course_id=course_id, lecture_id=hid,
        dedup_key=f"scope_analyze|{hid}|{generation}")
    if queue_err:
        flash(f"{note} {queue_err}")
    else:
        flash(f"{note} 题目清单生成已加入后台队列，可离开当前页面。")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/answer/page/<int:no>.jpg")
@role_required("teacher")
def lecture_answer_preview(hid, no):
    """页码选择界面的缩略图：按需渲染教师版 PDF 的某一页。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    if not hw["answer_pdf"]:
        abort(404)
    data = render_page_jpeg(os.path.join(_upload_dir(), hw["answer_pdf"]), no)
    if not data:
        abort(404)
    resp = send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=3600)
    return resp


@_routes.route("/lecture/<oid:hid>/student-pdf", methods=["POST"])
@role_required("teacher")
def lecture_student_pdf(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    f = request.files.get("student_pdf")
    if f and f.filename and allowed_file(f.filename):
        try:
            name = save_upload(f, hw["course_id"], hid)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("lecture_detail", hid=hid))
        db.execute("UPDATE lectures SET student_pdf=? WHERE id=?", (name, hid))
        db.commit()
        flash("学生版讲义已更新，学生端可预览")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/scope/save", methods=["POST"])
@role_required("teacher")
def lecture_scope_save(hid):
    """保存勾选的作业范围；至少勾选一道题，AI 批阅只处理勾选的题目。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    keys = [k.strip() for k in request.form.getlist("no") if k.strip()]
    try:
        candidates = json.loads(hw["scope_candidates"] or "[]")
    except (TypeError, ValueError):
        candidates = []
    valid_keys = {str(item.get("key") or item.get("no"))
                  for item in candidates if isinstance(item, dict)}
    keys = list(dict.fromkeys(key for key in keys if key in valid_keys))
    if not keys:
        flash("请至少勾选一道题再保存；未划定作业范围时 AI 批阅不会执行。")
        return redirect(url_for("lecture_detail", hid=hid))
    selected_json = json.dumps(keys, ensure_ascii=False)
    with _scope_analysis_lock:
        current = db.execute("SELECT scope_selected FROM lectures WHERE id=?", (hid,)).fetchone()
        try:
            old_keys = json.loads(current["scope_selected"] or "[]") if current else []
        except (TypeError, ValueError):
            old_keys = []
        if set(map(str, old_keys)) != set(keys):
            _advance_scope_analysis_generation(hid)
        db.execute("UPDATE lectures SET scope_selected=? WHERE id=?", (selected_json, hid))
        db.commit()
    # 范围补齐后，已提交但尚未批改的作业可以立即进入自动批改队列。
    queued = _grading_queue.auto_enqueue_lecture(hid)
    flash(f"已保存作业范围：{len(keys)} 道题。" +
          (f" 已自动加入批改队列 {queued} 份。" if queued else ""))
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/tags/save", methods=["POST"])
@role_required("teacher")
def lecture_tag_save(hid):
    """按题整体替换知识点标签：新增、改名、删除都在同一张表单里提交。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    try:
        candidates = json.loads(hw["scope_candidates"] or "[]")
    except (TypeError, ValueError):
        candidates = []
    valid = {}
    for item in candidates:
        if isinstance(item, dict):
            key = str(item.get("key") or item.get("no") or "").strip()
            if key:
                valid[key] = str(item.get("no") or "").strip()
    key = (request.form.get("question_key") or "").strip()
    if key not in valid:
        flash("题目不在当前题目清单中，请刷新页面后重试。")
        return redirect(url_for("lecture_detail", hid=hid))
    saved, err = tag_utils.replace_question_tags(
        db, hid, key, valid[key] or request.form.get("question_no"), request.form.get("tags"))
    if err:
        db.rollback()
        flash(f"标签未保存：{err}")
    else:
        db.commit()
        flash(f"第 {valid[key]} 题已保存 {len(saved)} 个知识点标签"
              if saved else f"第 {valid[key]} 题的标签已清空")
    return redirect(url_for("lecture_detail", hid=hid) + "#scope")


@_routes.route("/lecture/<oid:hid>/tags/generate", methods=["POST"])
@role_required("teacher")
def lecture_tags_generate(hid):
    """重新为当前作业范围生成知识点标签，用于失败重试或更换答案页后刷新。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    try:
        keys = json.loads(hw["scope_selected"] or "[]")
    except (TypeError, ValueError):
        keys = []
    if not keys:
        flash("请先在「作业范围」勾选并保存本讲题目，再生成知识点标签。")
        return redirect(url_for("lecture_detail", hid=hid))
    count, err = _generate_question_tags(hw, keys)
    if err:
        flash(f"知识点标签未生成：{err}")
    elif count is None:
        flash("知识点标签已加入后台队列，可离开当前页面。")
    else:
        flash(f"已重新生成 {count} 个知识点标签")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/tag-analysis", methods=["POST"])
@role_required("teacher")
def lecture_tag_analysis(hid):
    """横向分析：把本讲各知识点标签与全班逐题判定结合，找共性问题。异步执行。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    course = own_course(hw["course_id"])
    stats = tag_utils.lecture_stats(db, hid)
    if not stats:
        flash("本讲还没有知识点标签；请先在「作业范围」保存一次，系统会自动调用 AI 标注。")
        return redirect(url_for("lecture_detail", hid=hid))
    question_stats = tag_utils.lecture_question_stats(db, hid)
    if not question_stats:
        flash("本讲还没有批改完成的作业，暂时无法做知识点分析。")
        return redirect(url_for("lecture_detail", hid=hid))
    total_students = db.execute(
        "SELECT COUNT(DISTINCT q.submission_id) FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "WHERE s.lecture_id=? AND q.verdict<>''", (hid,)).fetchone()[0]
    weak_tag_names = [s["tag"] for s in stats if s["n_wrong"] or s["n_partial"]]
    kg_chains = _kg_weakness_chains(db, hw["course_id"], weak_tag_names)
    context = {
        "subject_and_course": _course_label(course),
        "lecture_name": hw["title"],
        "total_students": total_students,
        "aggregated_question_stats": question_stats,
        "kg_weakness_chains": kg_chains or "本课程未挂载知识图谱，或薄弱标签未能匹配图谱节点。",
    }
    prompt = _prompt_content("tag_horizontal", course["tag_horizontal_prompt_id"])
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        flash(f"知识点分析未生成：{err}")
        return redirect(url_for("lecture_detail", hid=hid))
    max_tokens = _get_max_tokens(g.user["id"], "max_tokens_horizontal")
    teacher_id = g.user["id"]
    course_id = hw["course_id"]
    question_stats_json = json.dumps(question_stats, ensure_ascii=False)

    def runner(database, operation):
        try:
            result = ai_horizontal_analysis(context, api_url, api_key, model,
                                            prompt=prompt,
                                            usage_hook=operation["usage_hook"],
                                            max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            _finish_ai_operation(operation, "failed", str(e))
            log.exception("横向知识点分析异常 lecture=%s", hid)
            return
        error = "" if result else "AI 未返回可用的分析结果，请稍后重试"
        if not result:
            _finish_ai_operation(operation, "failed", error)
            return
        md = tag_utils.render_horizontal_md(result)
        database.execute("INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,kind,"
                         "content_md,stats_json) VALUES(?,?,?,'horizontal',?,?)",
                         (teacher_id, course_id, hid, md, question_stats_json))
        database.commit()
        _finish_ai_operation(operation, "success")
        log.info("横向知识点分析完成 lecture=%s", hid)

    operation, queue_err = _ai_task_queue.submit(
        g.user["id"], "tag_horizontal", model=model, runner=runner,
        course_id=course_id, lecture_id=hid,
        dedup_key=f"tag_horizontal|{hid}")
    if queue_err:
        flash(queue_err)
    else:
        flash("全班知识点分析已加入后台队列，可离开当前页面。")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/summary/generate", methods=["POST"])
@role_required("teacher")
def lecture_summary_generate(hid):
    """AI 聚合全班逐题判定与评语，生成讲次 summary（每题要点 + 整体总结）。异步执行。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    qcheck = db.execute("""
        SELECT COUNT(*) FROM questions q
        JOIN submissions s ON s.id=q.submission_id
        WHERE s.lecture_id=? AND s.status='已批改' AND q.verdict != ''""", (hid,)).fetchone()[0]
    if qcheck == 0:
        flash("还没有批改完成的作业，无法生成讲次汇总")
        return redirect(url_for("lecture_detail", hid=hid))
    qstats = db.execute("""
        SELECT q.no,
          SUM(CASE WHEN q.verdict='对' THEN 1 ELSE 0 END) AS n_right,
          SUM(CASE WHEN q.verdict='半对' THEN 1 ELSE 0 END) AS n_partial,
          SUM(CASE WHEN q.verdict='错' THEN 1 ELSE 0 END) AS n_wrong,
          SUM(CASE WHEN q.verdict='无法判定' THEN 1 ELSE 0 END) AS n_unknown,
          GROUP_CONCAT(CASE WHEN q.ai_comment THEN q.ai_comment ELSE NULL END, '||SPLIT||') AS all_comments
        FROM questions q
        JOIN submissions s ON s.id=q.submission_id
        WHERE s.lecture_id=?
        GROUP BY q.no""", (hid,)).fetchall()
    qstats.sort(key=lambda row: _question_no_key(row["no"]))
    # 序列化成普通 dict 列表，供后台线程使用
    qstats_plain = [dict(r) for r in qstats]

    if get_setting(g.user["id"], "ai_demo") == "1":
        flash("演示模式不会调用 AI，无法生成讲次汇总。")
        return redirect(url_for("lecture_detail", hid=hid))
    block = _offpeak_block_if_enabled(g.user["id"])
    if block:
        flash(block)
        return redirect(url_for("lecture_detail", hid=hid))

    api_url = get_setting(g.user["id"], "ai_api_url")
    api_key = get_setting(g.user["id"], "ai_api_key")
    model = get_setting(g.user["id"], "ai_model")
    max_tokens = _get_max_tokens(g.user["id"], "max_tokens_summary")
    lec_stats = tag_utils.lecture_stats(db, hid)
    weak_tag_names = [s["tag"] for s in lec_stats if s["n_wrong"] or s["n_partial"]]
    kg_chains = _kg_weakness_chains(db, hw["course_id"], weak_tag_names)
    course_id = hw["course_id"]

    def runner(database, operation):
        try:
            result = ai_lecture_summary(qstats_plain, api_url, api_key, model,
                                        max_tokens=max_tokens, kg_chains=kg_chains,
                                        usage_hook=operation["usage_hook"])
        except Exception as e:  # noqa: BLE001
            _finish_ai_operation(operation, "failed", f"生成异常：{e}")
            log.exception("讲次汇总异常 lecture=%s", hid)
            return
        ai_error = result.get("error", "") if result else ""
        if ai_error:
            error_type = result.get("error_type", "")
            hint = ""
            if error_type == "truncated":
                hint = "请到 AI 设置 → 「讲次汇总」max_tokens 调大（如 8192 或 16384）后重试"
            elif error_type == "network":
                hint = "请检查 AI 设置中的 API 地址和密钥是否正确，网络是否通畅"
            else:
                hint = "请重试，或检查 AI 设置"
            _finish_ai_operation(operation, "failed", f"{ai_error}。{hint}")
            return
        if not result or not result.get("overall"):
            _finish_ai_operation(operation, "failed", "AI 返回内容为空")
            return
        per_q = result.get("per_question") or {}
        lines = ["## 每题要点\n"]
        for no in sorted(per_q.keys(), key=lambda x: (0 if str(x).replace(".", "").isdigit() else 1, str(x))):
            lines.append(f"- **第 {no} 题**：{per_q[no]}")
        lines.append("\n## 整体总结\n")
        lines.append(result.get("overall", ""))
        md = "\n".join(lines)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        database.execute("UPDATE lectures SET summary_md=?, summary_at=? WHERE id=?",
                         (md, now, hid))
        database.commit()
        _finish_ai_operation(operation, "success")
        log.info("讲次 summary 生成成功 lecture=%s", hid)

    operation, queue_err = _ai_task_queue.submit(
        g.user["id"], "lecture_summary", model=model, runner=runner,
        course_id=course_id, lecture_id=hid,
        dedup_key=f"lecture_summary|{hid}")
    if queue_err:
        if request.headers.get("Accept") == "application/json":
            return jsonify(ok=False, error=queue_err)
        flash(queue_err)
    else:
        if request.headers.get("Accept") == "application/json":
            return jsonify(ok=True, queued=True)
        flash("讲次汇总已加入后台队列，可离开当前页面。")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/delete", methods=["POST"])
@role_required("teacher")
def lecture_delete(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    deletion.delete_lecture(db, hid)
    db.commit()
    return redirect(url_for("course_detail", cid=hw["course_id"]))


# ---------- 老师：单个学生在课程内的进度 ----------
def own_student(cid, sid):
    """确认学生在本课程名单内，返回学生记录。"""
    own_course(cid)
    row = get_db().execute("""
        SELECT u.* FROM enrollments e JOIN users u ON u.id=e.student_id
        WHERE e.course_id=? AND e.student_id=?""", (cid, sid)).fetchone()
    if not row:
        abort(404)
    return row


@_routes.route("/course/<oid:cid>/student/<oid:sid>")
@role_required("teacher")
def student_detail(cid, sid):
    db = get_db()
    student = own_student(cid, sid)
    course = own_course(cid)
    lectures = db.execute("""
        SELECT h.id, h.title, h.deadline,
          (SELECT s.id FROM submissions s WHERE s.lecture_id=h.id AND s.student_id=?
            ORDER BY s.id DESC LIMIT 1) AS last_sub_id,
          (SELECT COUNT(*) FROM submissions s WHERE s.lecture_id=h.id AND s.student_id=?) AS n_subs,
          (SELECT s.status FROM submissions s WHERE s.lecture_id=h.id AND s.student_id=?
            ORDER BY s.id DESC LIMIT 1) AS my_status
        FROM lectures h WHERE h.course_id=?
        ORDER BY h.title COLLATE NOCASE, h.id""",
        (sid, sid, sid, cid)).fetchall()
    exams = db.execute("""
        SELECT x.id, x.title, x.exam_date, x.full_score, s.score, s.comment,
               COALESCE(s.status,'pending') AS score_status
        FROM exams x LEFT JOIN exam_scores s ON s.exam_id=x.id AND s.student_id=?
        WHERE x.course_id=? ORDER BY x.id DESC""", (sid, cid)).fetchall()
    acc_by = _accuracy_by_submission(db, [h["last_sub_id"] for h in lectures])
    weak_stats = tag_utils.student_stats(db, cid, sid)
    vertical_kg_chains = _kg_weakness_chains(db, cid, [r["tag"] for r in weak_stats])
    return render_template(
        "student_detail.html", course=course, student=student,
        lectures=lectures, exams=exams, acc_by=acc_by,
        weak_stats=weak_stats,
        vertical_kg_chains=vertical_kg_chains,
        tag_analysis=tag_utils.latest_analysis(db, "vertical", student_id=sid, course_id=cid))


@_routes.route("/course/<oid:cid>/student/<oid:sid>/tag-analysis", methods=["POST"])
@role_required("teacher")
def student_tag_analysis(cid, sid):
    """纵向分析：把该生跨讲次的错题按知识点标签聚合，找反复出错的知识点。异步执行。"""
    db = get_db()
    student = own_student(cid, sid)
    course = own_course(cid)
    mastery = tag_utils.student_mastery_stats(db, cid, sid)
    weak = [row for row in mastery if row["n_wrong"] or row["n_partial"]]
    if not weak:
        flash("暂时没有可分析的数据：需要先给讲次生成知识点标签，并且该生有批改结果。")
        return redirect(url_for("student_detail", cid=cid, sid=sid))
    trend = tag_utils.student_trend(db, cid, sid)
    titles = [row["lecture"] for row in trend]
    lecture_range = (f"{titles[0]} ~ {titles[-1]}（共 {len(titles)} 讲）"
                     if len(titles) > 1 else (titles[0] if titles else ""))
    kg_chains = _kg_weakness_chains(db, cid, [row["tag"] for row in weak])
    if not kg_chains:
        kg_chains = ("本课程未挂载知识图谱，或薄弱标签未能匹配图谱节点；"
                     "请仅依据同一或相近标签在不同讲次的重复出错推断前置基础，"
                     "不要编造数据中不存在的依赖关系。")
    habit_stats = tag_utils.habit_stats(db, cid, sid)
    context = {
        "lecture_count": len(trend),
        "subject_and_course": _course_label(course),
        "student_name": student["display_name"],
        "lecture_range": lecture_range,
        "module_mastery_stats": mastery,
        "kg_weakness_chains": kg_chains,
        "error_habit_stats": habit_stats,
        "trend_data": trend,
    }
    prompt = _prompt_content("tag_vertical", course["tag_vertical_prompt_id"])
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        flash(f"纵向分析未生成：{err}")
        return redirect(url_for("student_detail", cid=cid, sid=sid))
    max_tokens = _get_max_tokens(g.user["id"], "max_tokens_vertical")
    teacher_id = g.user["id"]
    stats_json = json.dumps({"mastery": mastery, "trend": trend, "habits": habit_stats},
                            ensure_ascii=False)

    def runner(database, operation):
        try:
            result = ai_vertical_analysis(context, api_url, api_key, model,
                                          prompt=prompt,
                                          usage_hook=operation["usage_hook"],
                                          max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            _finish_ai_operation(operation, "failed", str(e))
            log.exception("纵向知识点分析异常 course=%s student=%s", cid, sid)
            return
        error = "" if result else "AI 未返回可用的分析结果，请稍后重试"
        if not result:
            _finish_ai_operation(operation, "failed", error)
            return
        md = tag_utils.render_vertical_md(result)
        database.execute("INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,student_id,"
                         "kind,content_md,stats_json) VALUES(?,?,0,?,'vertical',?,?)",
                         (teacher_id, cid, sid, md, stats_json))
        database.commit()
        _finish_ai_operation(operation, "success")
        log.info("纵向知识点分析完成 course=%s student=%s", cid, sid)

    operation, queue_err = _ai_task_queue.submit(
        g.user["id"], "tag_vertical", model=model, runner=runner,
        course_id=cid, dedup_key=f"tag_vertical|{cid}|{sid}")
    if queue_err:
        flash(queue_err)
    else:
        flash("纵向知识点分析已加入后台队列，可离开当前页面。")
    return redirect(url_for("student_detail", cid=cid, sid=sid))


# ---------- 老师：考试（只录分数，不做 AI 批阅） ----------
def own_exam(eid):
    exam = get_db().execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
    if not exam:
        abort(404)
    own_course(exam["course_id"])
    return exam


@_routes.route("/course/<oid:cid>/exam/create", methods=["POST"])
@role_required("teacher")
def exam_create(cid):
    own_course(cid)
    title = request.form.get("title", "").strip()
    if title:
        try:
            full_score = float(request.form.get("full_score") or 100)
        except ValueError:
            full_score = 100.0
        db = get_db()
        db.execute("INSERT INTO exams(course_id,title,description,exam_date,full_score) "
                   "VALUES(?,?,?,?,?)",
                   (cid, title, request.form.get("description", "").strip(),
                    request.form.get("exam_date", ""), full_score))
        db.commit()
    return redirect(url_for("course_detail", cid=cid))


@_routes.route("/exam/<int:eid>")
@role_required("teacher")
def exam_detail(eid):
    db = get_db()
    exam = own_exam(eid)
    course = own_course(exam["course_id"])
    rows = db.execute("""
        SELECT u.id, u.username, u.display_name, u.student_no,
           s.score, s.comment, COALESCE(s.status,'pending') AS score_status
        FROM enrollments e
        JOIN users u ON u.id=e.student_id
        LEFT JOIN exam_scores s ON s.exam_id=? AND s.student_id=u.id
        WHERE e.course_id=? AND u.active=1 ORDER BY u.display_name""", (eid, exam["course_id"])).fetchall()
    return render_template("exam.html", exam=exam, course=course, rows=rows)


@_routes.route("/exam/<int:eid>/scores", methods=["POST"])
@role_required("teacher")
def exam_scores_save(eid):
    db = get_db()
    exam = own_exam(eid)
    roster_ids = {str(row[0]) for row in db.execute(
        "SELECT u.id FROM enrollments e JOIN users u ON u.id=e.student_id "
        "WHERE e.course_id=? AND u.role='student' AND u.active=1",
        (exam["course_id"],)).fetchall()}
    for key in request.form:
        if not key.startswith("score_"):
            continue
        sid = key[len("score_"):]
        if sid not in roster_ids:
            continue
        score = request.form.get(key, "").strip()
        comment = request.form.get(f"comment_{sid}", "").strip()
        requested = request.form.get(f"status_{sid}", "")
        if requested not in {"pending", "absent", "recorded"}:
            # 兼容旧页面/客户端：有分数或评语视为即时录入；空值仍是待录入。
            requested = "recorded" if score or comment else "pending"
        elif requested == "pending" and (score or comment):
            # 输入内容本身意味着已录入，避免因漏改状态而把成绩误存为待处理。
            requested = "recorded"
        if requested == "recorded" and not score and not comment:
            requested = "pending"
        if requested == "pending":
            db.execute("DELETE FROM exam_scores WHERE exam_id=? AND student_id=?", (eid, sid))
            continue
        if requested == "absent":
            # 缺考不是零分；不保留之前的分数，以免切回已录入时旧成绩意外重现。
            score = ""
        db.execute(
            "INSERT INTO exam_scores(exam_id,student_id,score,comment,status,updated_at) "
            "VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(exam_id,student_id) DO UPDATE SET score=excluded.score, "
            "comment=excluded.comment,status=excluded.status,updated_at=CURRENT_TIMESTAMP",
            (eid, sid, score, comment, requested))
    db.commit()
    flash("考试状态已保存；已录入成绩/评语立即对对应学生可见，缺考不会按零分计入。")
    return redirect(url_for("exam_detail", eid=eid))


@_routes.route("/exam/<int:eid>/delete", methods=["POST"])
@role_required("teacher")
def exam_delete(eid):
    db = get_db()
    exam = own_exam(eid)
    db.execute("DELETE FROM exam_scores WHERE exam_id=?", (eid,))
    db.execute("DELETE FROM exams WHERE id=?", (eid,))
    db.commit()
    return redirect(url_for("course_detail", cid=exam["course_id"]))


# ---------- 批改：学生作业与标准答案一起交给 AI，老师逐题核对 ----------
# 队列调度（去重、线程池、定时扫描）在 grading_service.py，这里只保留批阅业务
def _save_grading_result(db, sid, gen, answer_pages=None):
    """把模型返回的逐题结果写入数据库；演示模式和后台任务共用。

    answer_pages 为发送给模型的标准答案页码列表（1 起）；为空表示整份发送。
    用于把模型返回的 answer_page（在发送页中的序号）映射回 PDF 实际页码。
    """
    db.execute("DELETE FROM student_question_reviews WHERE question_id IN "
               "(SELECT id FROM questions WHERE submission_id=?)", (sid,))
    db.execute("DELETE FROM questions WHERE submission_id=?", (sid,))
    ap_list = list(answer_pages or [])
    for i, q in enumerate(gen["questions"], 1):
        comment = (q.get("comment") or "").strip()
        sp = int(q.get("student_page") or 1)
        ap_idx = int(q.get("answer_page") or 1)
        # 模型给的 answer_page 是「发送页中的序号」，映射回 PDF 实际页码
        if ap_list:
            apage = ap_list[ap_idx - 1] if 1 <= ap_idx <= len(ap_list) else 0
        else:
            apage = ap_idx if ap_idx >= 1 else 0
        db.execute(
            "INSERT INTO questions(submission_id,no,page_no,image_path,ocr_text,"
            "standard_answer,note,verdict,ai_comment,answer_page,student_box,answer_box) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, q.get("no") or str(i), sp if sp >= 1 else 1, "",
             q.get("ocr", ""), q.get("standard_answer", ""), comment,
             q.get("verdict", ""), comment, apage,
             q.get("student_box", ""), q.get("answer_box", "")))
    summary = gen.get("summary") or {}
    db.execute("UPDATE submissions SET status='待批改', report_published=0, "
               "report_md='',report_detail='',feedback='',grader='',score='',ai_summary=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(summary, ensure_ascii=False) if summary else "", sid))
    db.commit()


def _run_grading_job(teacher_id, sid, operation):
    """后台批阅：不依赖任何浏览器请求，完成后直接把结果写入数据库。

    去重状态由 GradingQueue 在任务结束后统一清理。
    """
    with _service_app_context():
        db = None
        try:
            db = get_db()
            db.execute("UPDATE ai_operations SET status='running', started_at=? "
                       "WHERE id=? AND status='queued'",
                       (datetime.now().isoformat(sep=" ", timespec="milliseconds"),
                        operation["id"]))
            db.commit()
            sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            if not sub:
                _finish_ai_operation(operation, "failed", "作业不存在")
                return
            hw = db.execute("SELECT * FROM lectures WHERE id=?",
                            (sub["lecture_id"],)).fetchone()
            course = db.execute("SELECT * FROM courses WHERE id=?",
                                (hw["course_id"],)).fetchone()
            if not hw["answer_pdf"]:
                _finish_ai_operation(operation, "failed", "教师版标准答案已缺失，请重新上传")
                return
            gen = run_grading(
                teacher_id, file_list(sub["file_path"]), hw["answer_pdf"],
                scope=_scope_instruction(hw),
                answer_pages=_page_list(hw["answer_pages"]),
                prompt=_prompt_content("grade", course["grade_prompt_id"]),
                usage_hook=operation["usage_hook"])
            if gen["error"]:
                _finish_ai_operation(operation, "failed", gen["error"])
                grading_log.warning("后台批阅失败 submission=%s 原因=%s", sid, gen["error"])
                return
            _save_grading_result(db, sid, gen, answer_pages=_page_list(hw["answer_pages"]))
            _finish_ai_operation(operation, "success")
            grading_log.info("后台批阅完成 submission=%s 题数=%s", sid, len(gen["questions"]))
        except Exception as e:
            grading_log.exception("后台批阅异常 submission=%s", sid)
            if db is not None:
                db.rollback()
            _finish_ai_operation(operation, "failed", str(e))


def _settings_for_service(teacher_id, key, default=""):
    """延迟解析模块全局 get_setting，测试中替换 app.get_setting 仍然生效。"""
    return get_setting(teacher_id, key, default)


def _offpeak_for_service(teacher_id):
    return _offpeak_block_if_enabled(teacher_id)


def _service_app_context():
    application = current_app._get_current_object() if has_app_context() else app
    return application.app_context()


def _instance_service(name):
    application = current_app._get_current_object() if has_app_context() else app
    return application.extensions[name]


_grading_queue = LocalProxy(lambda: _instance_service("grading_queue"))
_ai_task_queue = LocalProxy(lambda: _instance_service("ai_task_queue"))


def _configure_queues(application):
    def run_job(*args):
        with application.app_context():
            return _run_grading_job(*args)
    application.extensions["kg_state"] = {
        "build_lock": threading.Lock(), "schedule_lock": threading.Lock(), "enqueued": set()}
    application.extensions["grading_queue"] = GradingQueue(
        get_db=get_db, get_setting=_settings_for_service,
        start_operation=_start_ai_operation, finish_operation=_finish_ai_operation,
        run_job=run_job, app_context=application.app_context, offpeak_block=_offpeak_for_service)
    application.extensions["ai_task_queue"] = ai_task_queue.AiTaskQueue(
        get_db=get_db, close_db=close_db, start_operation=_start_ai_operation,
        finish_operation=_finish_ai_operation, app_context=application.app_context)


def _enqueue_grading(teacher_id, sid, course_id, lecture_id):
    """统一入队入口：手动点击和定时扫描共用，返回 (operation, 错误提示)。"""
    return _grading_queue.enqueue(teacher_id, sid, course_id, lecture_id)


def _auto_enqueue_submission(sid):
    """上传后立即尝试自动批改，返回是否已进入队列。"""
    queued, reason = _grading_queue.auto_enqueue(sid)
    if queued:
        grading_log.info("上传后自动批阅入队 submission=%s", sid)
    elif reason:
        grading_log.info("上传后未自动批阅 submission=%s：%s", sid, reason)
    return queued


def _submission_grade_active(db, sid):
    """这份作业最近的批阅任务是否仍在排队/执行中。"""
    row = latest_operation(db, sid)
    return bool(row and row["status"] in ("queued", "running"))


@_routes.route("/submission/<oid:sid>/grade", methods=["POST"])
@role_required("teacher")
def submission_grade(sid):
    """提交批阅任务：真实批阅在后台队列执行，提交后即可离开页面。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (sub["lecture_id"],)).fetchone()
    course = own_course(hw["course_id"])
    back = "review" if request.form.get("back") == "review" else "lecture"

    def _back_to_page():
        if back == "review":
            return redirect(url_for("submission_review", sid=sid))
        return redirect(url_for("lecture_detail", hid=hw["id"]))

    if not hw["answer_pdf"]:
        flash("请先上传教师版标准答案 PDF，再开始批阅。")
        return _back_to_page()
    if not (hw["scope_candidates"] or "").strip() or not (hw["scope_selected"] or "").strip():
        flash("请先在「作业范围」选择教师版页码并保存，生成题目清单并勾选本讲题目，再开始批阅。")
        return _back_to_page()

    block = _offpeak_block_if_enabled(g.user["id"])
    if block:
        flash(block)
        return _back_to_page()

    if get_setting(g.user["id"], "ai_demo") == "1":
        # 演示模式是本地占位数据，不需要排队，直接生成
        try:
            gen = run_grading(g.user["id"], file_list(sub["file_path"]), hw["answer_pdf"],
                              scope=_scope_instruction(hw),
                              answer_pages=_page_list(hw["answer_pages"]),
                              prompt=_prompt_content("grade", course["grade_prompt_id"]))
        except Exception as e:
            log.exception("演示批阅失败 submission=%s", sid)
            flash(str(e))
            return _back_to_page()
        _save_grading_result(db, sid, gen, answer_pages=_page_list(hw["answer_pages"]))
        flash(f"演示模式：未调用 AI，已生成 {len(gen['questions'])} 道题，请逐题复核后手动生成报告")
        return _back_to_page()

    _, err = _enqueue_grading(g.user["id"], sid, hw["course_id"], hw["id"])
    if err:
        flash(err)
        return _back_to_page()

    grading_log.info("已加入批阅队列 submission=%s lecture=%s file=%s answer=%s pages=%s",
             sid, hw["id"], sub["file_path"], hw["answer_pdf"], hw["answer_pages"])
    flash("已加入批阅队列，可以离开本页；完成后回到讲次页或核对页查看结果。")
    return _back_to_page()


@_routes.route("/lecture/<oid:hid>/grading-status")
@role_required("teacher")
def lecture_grading_status(hid):
    """讲次页轮询：返回本讲每份作业最近的批阅状态。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    rows = db.execute(
        "SELECT o.submission_id,o.status,o.error FROM ai_operations o "
        "JOIN (SELECT submission_id,MAX(id) AS max_id FROM ai_operations "
        "WHERE operation_type='grade' GROUP BY submission_id) m ON m.max_id=o.id "
        "JOIN submissions s ON s.id=o.submission_id WHERE s.lecture_id=?", (hid,)).fetchall()
    jobs = {str(r["submission_id"]): {"status": r["status"], "error": r["error"] or ""}
            for r in rows}
    return jsonify({
        "jobs": jobs,
        "active": any(j["status"] in ("queued", "running") for j in jobs.values()),
    })


@_routes.route("/submission/<oid:sid>/grading-status")
@role_required("teacher")
def submission_grading_status(sid):
    """核对页轮询：返回这一份作业最近的批阅状态。"""
    db = get_db()
    sub = db.execute("SELECT lecture_id FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT course_id FROM lectures WHERE id=?",
                    (sub["lecture_id"],)).fetchone()
    own_course(hw["course_id"])
    row = latest_operation(db, sid)
    status = row["status"] if row else ""
    return jsonify({
        "status": status,
        "error": (row["error"] if row else "") or "",
        "active": status in ("queued", "running"),
    })


@_routes.route("/ai-queue")
@_routes.route("/grading-queue")
@role_required("teacher")
def grading_queue():
    """教师导航栏的后台任务队列：返回自己名下所有排队中/进行中的 AI 任务，
    最近 60 秒内完成的任务（供前端弹出完成提示），以及最近若干条已完成任务
    （供弹窗展示历史）。"""
    db = get_db()
    rows = db.execute(
        "SELECT o.id,o.operation_type,o.status,o.started_at,o.finished_at,o.error,"
        "o.course_id,COALESCE(o.lecture_id, s.lecture_id) AS lecture_id,o.submission_id,o.material_id,"
        "COALESCE(u.display_name,'未知学生') AS student_name,"
        "COALESCE(l.title,'未知讲次') AS lecture_title,"
        "COALESCE(c.name,'') AS course_name,"
        "COALESCE(g.name,'') AS graph_name "
        "FROM ai_operations o "
        "LEFT JOIN submissions s ON s.id=o.submission_id "
        "LEFT JOIN users u ON u.id=s.student_id "
        "LEFT JOIN lectures l ON l.id=COALESCE(o.lecture_id, s.lecture_id) "
        "LEFT JOIN courses c ON c.id=COALESCE(o.course_id, l.course_id) "
        "LEFT JOIN kg_materials m ON m.id=o.material_id "
        "LEFT JOIN knowledge_graphs g ON g.id=m.graph_id "
        "WHERE o.teacher_id=? AND o.status IN ('queued','running') "
        "ORDER BY o.id", (g.user["id"],)).fetchall()

    jobs = [_queue_job(r) for r in rows]

    _FINISHED_SQL = (
        "SELECT o.id,o.operation_type,o.status,o.error,o.finished_at,o.course_id,"
        "COALESCE(o.lecture_id, s.lecture_id) AS lecture_id,"
        "o.submission_id,o.material_id,"
        "COALESCE(u.display_name,'未知学生') AS student_name,"
        "COALESCE(l.title,'未知讲次') AS lecture_title,"
        "COALESCE(c.name,'') AS course_name,"
        "COALESCE(g.name,'') AS graph_name "
        "FROM ai_operations o "
        "LEFT JOIN submissions s ON s.id=o.submission_id "
        "LEFT JOIN users u ON u.id=s.student_id "
        "LEFT JOIN lectures l ON l.id=COALESCE(o.lecture_id, s.lecture_id) "
        "LEFT JOIN courses c ON c.id=COALESCE(o.course_id, l.course_id) "
        "LEFT JOIN kg_materials m ON m.id=o.material_id "
        "LEFT JOIN knowledge_graphs g ON g.id=m.graph_id "
        "WHERE o.teacher_id=? AND o.status IN ('success','failed') "
        "AND o.finished_at IS NOT NULL ")

    # 最近 60 秒内完成的任务，用于前端 toast 提示
    finished = db.execute(
        _FINISHED_SQL
        + "AND julianday('now') - julianday(o.finished_at) < 60/86400.0 "
        + "ORDER BY o.id DESC LIMIT 20", (g.user["id"],)).fetchall()
    finished_jobs = [_queue_job(r, finished=True) for r in finished]

    # 最近 8 条已完成任务（不限时间），用于弹窗展示历史与时间
    recent = db.execute(
        _FINISHED_SQL
        + "ORDER BY o.id DESC LIMIT 8", (g.user["id"],)).fetchall()
    recent_jobs = [_queue_job(r, finished=True) for r in recent]

    return jsonify({
        "jobs": jobs,
        "finished": finished_jobs,
        "recent": recent_jobs,
        "active": bool(jobs),
        "queued": sum(1 for j in jobs if j["status"] == "queued"),
        "running": sum(1 for j in jobs if j["status"] == "running"),
    })


def _queue_job(r, finished=False):
    """把 ai_operations 行（含关联字段）转成队列项字典。"""
    op_type = r["operation_type"]
    label = AI_OPERATION_LABEL.get(op_type, op_type)
    title_parts = []
    if r["lecture_title"]:
        title_parts.append(r["lecture_title"])
    if r["student_name"] and op_type in ("grade", "report_generate", "report_comment"):
        title_parts.append(r["student_name"])
    if r["graph_name"]:
        title_parts.append(r["graph_name"])
    if r["course_name"] and not r["lecture_title"]:
        title_parts.append(r["course_name"])
    title = " · ".join(title_parts) or label

    url = ""
    if r["lecture_id"]:
        url = url_for("lecture_detail", hid=r["lecture_id"])
    elif op_type == "tag_vertical" and r["course_id"]:
        url = url_for("course_detail", cid=r["course_id"])
    elif r["submission_id"] and op_type in ("report_generate", "report_comment"):
        url = url_for("submission_report", sid=r["submission_id"])
    elif r["material_id"]:
        # kg_build：跳到图谱列表，具体图谱由前端 toast 文字说明
        url = url_for("knowledge_graphs")

    item = {
        "id": r["id"],
        "type": op_type,
        "label": label,
        "status": r["status"],
        "title": title,
        "url": url,
    }
    if finished:
        item["error"] = r["error"] or ""
        item["finished_at"] = (r["finished_at"] or "")[:19]
    return item


def _auto_grade_scan():
    """扫描并自动入队待批阅作业，返回本次新入队数量。"""
    return _grading_queue.scan()


def start_auto_grade():
    """启动自动批阅线程；多进程部署时只让抢到文件锁的进程跑。"""
    _grading_queue.start(enabled=cfg.get("AUTO_GRADE"), interval_hours=cfg.get("AUTO_GRADE_HOURS"),
                         lock_dir=cfg.get("DATA_DIR"))


def _resolve_student_page(files, global_page):
    """把学生作业的全局页序号（发给 AI 的连续编号）映射回 (文件相对路径, 文件内页码)。

    files 为 file_list(sub.file_path) 的结果；global_page 为 1 起序号。
    找不到时返回 (None, 0)。
    """
    if global_page < 1:
        return None, 0
    acc = 0
    for fp in files:
        ext = os.path.splitext(fp)[1].lower()
        if ext == ".pdf":
            full = os.path.join(_upload_dir(), fp)
            try:
                count = pdf_page_count(full)
            except Exception:
                count = 1
        else:
            count = 1
        if acc + count >= global_page:
            return fp, global_page - acc
        acc += count
    return None, 0


@_routes.route("/submission/<oid:sid>/review")
@role_required("teacher")
def submission_review(sid):
    db = get_db()
    sub = db.execute("""
        SELECT s.*, u.display_name, u.username FROM submissions s
        JOIN users u ON u.id=s.student_id WHERE s.id=?""", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (sub["lecture_id"],)).fetchone()
    course = own_course(hw["course_id"])
    questions = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (sid,)).fetchall()
    # 为每题预生成学生答案/标准答案的图片 URL（含裁剪框），供复核页内嵌展示
    sfiles = file_list(sub["file_path"])
    answer_pdf = hw["answer_pdf"] or ""
    q_media = []
    for q in questions:
        sp_file, sp_page = _resolve_student_page(sfiles, int(q["page_no"] or 1))
        sbox = q["student_box"] or ""
        abox = q["answer_box"] or ""
        def _url(fp, page, box):
            if not fp or page < 1:
                return ""
            u = url_for("uploaded_file", filename=fp, page=page)
            if box:
                # box 已存为 JSON 字符串 "[x,y,w,h]"
                u += "&box=" + box.strip("[]").replace(" ", "")
            return u
        q_media.append({
            "student_url": _url(sp_file, sp_page, sbox),
            "student_full_url": _url(sp_file, sp_page, "") if sp_file else "",
            "answer_url": _url(answer_pdf, int(q["answer_page"] or 0), abox),
            "answer_full_url": _url(answer_pdf, int(q["answer_page"] or 0), "") if answer_pdf else "",
        })
    return render_template("review.html", sub=sub, hw=hw, course=course, questions=questions,
                           q_media=q_media,
                           tag_by_key=tag_utils.lecture_tags_by_key(db, hw["id"]),
                           grade_job=latest_operation(db, sid),
                           ai_operations=_ai_operation_history(submission_id=sid, limit=5))


@_routes.route("/submission/<oid:sid>/review/save", methods=["POST"])
@role_required("teacher")
def submission_review_save(sid):
    """保存逐题的 OCR 文本、判定、评语，以及总评；作业不再单独赋分。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (sub["lecture_id"],)).fetchone()
    own_course(hw["course_id"])
    for q in db.execute("SELECT id FROM questions WHERE submission_id=?", (sid,)).fetchall():
        qid = q["id"]
        fields = {"ocr_text": request.form.get(f"ocr_{qid}", "").strip(),
                  "standard_answer": request.form.get(f"standard_{qid}", "").strip(),
                  "note": request.form.get(f"note_{qid}", "").strip()}
        # 判定可人工修正；旧页面没有该字段时保持原值不动
        if f"verdict_{qid}" in request.form:
            v = request.form.get(f"verdict_{qid}", "").strip()
            fields["verdict"] = v if v in ("", "对", "半对", "错", "无法判定") else ""
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE questions SET {sets} WHERE id=?", (*fields.values(), qid))
    # 作业只统计正确率，不再保存总分；清空历史遗留的分数串
    pending = db.execute("SELECT COUNT(*) FROM questions WHERE submission_id=? AND reviewed=0",
                         (sid,)).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM questions WHERE submission_id=?", (sid,)).fetchone()[0]
    job = latest_operation(db, sid)
    reviewed = bool(total) and not pending and not (job and job["status"] in ("queued", "running"))
    # 草稿保存和报告预览不隐式公开；旧客户端省略 action 时兼容原保存行为。
    draft = request.form.get("action") == "draft" or (
        request.form.get("then") == "report" and sub["status"] != "已批改")
    reviewed = reviewed and not draft
    db.execute("UPDATE submissions SET score='', feedback=?, status=?, "
               "report_published=CASE WHEN ? THEN report_published ELSE 0 END,grader=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (request.form.get("overall", "").strip(),
                "已批改" if reviewed else "待批改", reviewed,
                f"{g.user['display_name']} 逐题批改", sid))
    db.commit()
    # 更新学生个人薄弱点图谱（失败不影响批改保存）
    try:
        if reviewed:
            _update_student_weak_points(db, sub)
    except Exception:
        log.exception("更新学生薄弱点失败 submission=%s student=%s", sid, sub["student_id"])
    if (request.form.get("then") or "") == "report":
        if job and job["status"] in ("queued", "running"):
            flash("批阅任务尚未完成，请稍后再复核并生成报告。")
            return redirect(url_for("submission_review", sid=sid))
        pending = db.execute("SELECT COUNT(*) c FROM questions WHERE submission_id=? AND reviewed=0",
                             (sid,)).fetchone()["c"]
        if pending:
            flash(f"生成批阅报告前需要人工审核每一道题，还有 {pending} 题未审核")
            return redirect(url_for("submission_review", sid=sid))
    if (request.form.get("then") or "") == "report":
        current = db.execute("SELECT report_md FROM submissions WHERE id=?", (sid,)).fetchone()
        if current and current["report_md"]:
            flash("批改已保存，已打开现有报告；如需同步本次修改，请在报告页重新生成。")
            return redirect(url_for("submission_report", sid=sid))
        try:
            generate_report(db, sid, g.user["id"], reuse=True)
        except Exception:  # 报告生成失败不影响批改保存，可在报告页重试
            log.exception("生成批改报告失败 submission=%s", sid)
            flash("批改已保存，但报告生成失败；可稍后在报告页重试。")
            return redirect(url_for("submission_report", sid=sid))
        generated = db.execute("SELECT report_md FROM submissions WHERE id=?", (sid,)).fetchone()
        if not generated or not generated["report_md"]:
            flash("批改已保存，但报告评价生成失败；可稍后在报告页重试。")
            return redirect(url_for("submission_report", sid=sid))
        flash("批改已保存，报告已生成，可继续修改后发布。")
        return redirect(url_for("submission_report", sid=sid))
    flash("批改已保存，学生可以查看" if reviewed else "草稿已保存，全部题目复核后再保存即可公开。")
    return redirect(url_for("submission_review", sid=sid))


@_routes.route("/question/<int:qid>/reviewed", methods=["POST"])
@role_required("teacher")
def question_reviewed(qid):
    """单题的「已复核」标记，随点随存；同时保存该题的判定/OCR/标准答案/评语，
    避免老师改完判定点「已复核」却只存了审核状态、修改被丢弃。"""
    db = get_db()
    q = db.execute("""
        SELECT q.id, s.lecture_id FROM questions q
        JOIN submissions s ON s.id=q.submission_id WHERE q.id=?""", (qid,)).fetchone()
    if not q:
        abort(404)
    hw = db.execute("SELECT course_id FROM lectures WHERE id=?",
                    (q["lecture_id"],)).fetchone()
    own_course(hw["course_id"])
    payload = request.get_json(silent=True) or {}
    reviewed = 1 if payload.get("reviewed") else 0
    # 同时保存该题当前表单值（前端会把最新值一起带上来）
    verdict = str(payload.get("verdict", "")).strip()
    if verdict not in ("", "对", "半对", "错", "无法判定"):
        verdict = ""
    db.execute(
        "UPDATE questions SET reviewed=?, verdict=?, ocr_text=?, standard_answer=?, note=? "
        "WHERE id=?",
        (reviewed, verdict,
         str(payload.get("ocr_text", "")).strip(),
         str(payload.get("standard_answer", "")).strip(),
         str(payload.get("note", "")).strip(), qid))
    db.commit()
    log.info("复核保存 question=%s reviewed=%s verdict=%s", qid, reviewed, verdict)
    return {"ok": True, "reviewed": reviewed, "verdict": verdict}


# ---------- 教案 ----------
@_routes.route("/course/<oid:cid>/plan/create", methods=["POST"])
@role_required("teacher")
def plan_create(cid):
    own_course(cid)
    title = request.form.get("title", "").strip()
    if title:
        db = get_db()
        db.execute("INSERT INTO lesson_plans(course_id,title) VALUES(?,?)", (cid, title))
        db.commit()
    return redirect(url_for("course_detail", cid=cid))


@_routes.route("/plan/<int:pid>/edit", methods=["GET", "POST"])
@role_required("teacher")
def plan_edit(pid):
    db = get_db()
    plan = db.execute("SELECT * FROM lesson_plans WHERE id=?", (pid,)).fetchone()
    if not plan:
        abort(404)
    own_course(plan["course_id"])
    if request.method == "POST":
        db.execute("UPDATE lesson_plans SET title=?, content=?, updated_at=CURRENT_TIMESTAMP "
                   "WHERE id=?", (request.form.get("title", plan["title"]),
                                  request.form.get("content", ""), pid))
        db.commit()
        return redirect(url_for("course_detail", cid=plan["course_id"]))
    return render_template("plan_edit.html", plan=plan)


@_routes.route("/plan/<int:pid>/delete", methods=["POST"])
@role_required("teacher")
def plan_delete(pid):
    db = get_db()
    plan = db.execute("SELECT * FROM lesson_plans WHERE id=?", (pid,)).fetchone()
    if not plan:
        abort(404)
    own_course(plan["course_id"])
    db.execute("DELETE FROM lesson_plans WHERE id=?", (pid,))
    db.commit()
    return redirect(url_for("course_detail", cid=plan["course_id"]))


# ---------- 老师：我的提示词 ----------
@_routes.route("/prompts")
@role_required("teacher")
def prompts_page():
    db = get_db()
    defaults = db.execute("SELECT * FROM prompts WHERE is_default=1 ORDER BY kind").fetchall()
    mine = db.execute("SELECT * FROM prompts WHERE teacher_id=? ORDER BY kind, id DESC",
                      (g.user["id"],)).fetchall()
    usage = {}
    for course in db.execute("SELECT c.* FROM courses c JOIN course_teachers ct "
                             "ON ct.course_id=c.id WHERE ct.teacher_id=?", (g.user["id"],)):
        for column in ("grade_prompt_id", "report_prompt_id", "tag_horizontal_prompt_id", "tag_vertical_prompt_id"):
            if course[column]:
                usage.setdefault(course[column], []).append({"id": course["id"], "name": course["name"]})
    return render_template("prompts.html", defaults=defaults, mine=mine,
                           usage=usage,
                           kind_label=PROMPT_KIND_LABEL,
                           placeholder_hints=PROMPT_PLACEHOLDER_HINTS)


@_routes.route("/prompts/create", methods=["POST"])
@role_required("teacher")
def prompt_create():
    kind = request.form.get("kind", "")
    name = request.form.get("name", "").strip()
    content = request.form.get("content", "").strip()
    if kind not in PROMPT_KIND_LABEL:
        flash("提示词类型不合法")
    elif not name or not content:
        flash("名称和内容都不能为空")
    else:
        db = get_db()
        db.execute("INSERT INTO prompts(teacher_id,kind,name,content) VALUES(?,?,?,?)",
                   (g.user["id"], kind, name[:60], content))
        db.commit()
        log.info("新建提示词 user=%s#%s kind=%s name=%s",
                 g.user["username"], g.user["id"], kind, name)
        flash(f"已新建{PROMPT_KIND_LABEL[kind]}提示词「{name}」")
    return redirect(url_for("prompts_page"))


def _editable_prompt(pid):
    """系统内置提示词不可改；别人的提示词不可改。"""
    row = get_db().execute("SELECT * FROM prompts WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    if row["is_default"]:
        flash("系统内置提示词不可修改，请先复制一份再改")
        return None
    if row["teacher_id"] != g.user["id"]:
        abort(403)
    return row


@_routes.route("/prompts/<oid:pid>/save", methods=["POST"])
@role_required("teacher")
def prompt_save(pid):
    row = _editable_prompt(pid)
    if row is None:
        return redirect(url_for("prompts_page"))
    name = request.form.get("name", "").strip()[:60] or row["name"]
    content = request.form.get("content", "").strip()
    if not content:
        flash("提示词内容不能为空")
        return redirect(url_for("prompts_page"))
    db = get_db()
    db.execute("UPDATE prompts SET name=?, content=?, updated_at=CURRENT_TIMESTAMP "
               "WHERE id=?", (name, content, pid))
    db.commit()
    flash(f"提示词「{name}」已保存")
    return redirect(url_for("prompts_page") + f"#prompt-{pid}")


@_routes.route("/prompts/<oid:pid>/copy", methods=["POST"])
@role_required("teacher")
def prompt_copy(pid):
    """复制成自己的副本，便于在副本上随意修改、测试。"""
    db = get_db()
    row = db.execute("SELECT * FROM prompts WHERE id=?", (pid,)).fetchone()
    if not row:
        abort(404)
    if not row["is_default"] and row["teacher_id"] != g.user["id"]:
        abort(403)
    name = f"{row['name']}（副本）"[:60]
    cur = db.execute("INSERT INTO prompts(teacher_id,kind,name,content) VALUES(?,?,?,?)",
                     (g.user["id"], row["kind"], name, row["content"]))
    db.commit()
    flash(f"已复制为「{name}」，可以自由修改")
    return redirect(url_for("prompts_page") + f"#prompt-{cur.lastrowid}")


@_routes.route("/prompts/<oid:pid>/delete", methods=["POST"])
@role_required("teacher")
def prompt_delete(pid):
    row = _editable_prompt(pid)
    if row is None:
        return redirect(url_for("prompts_page"))
    db = get_db()
    # 引用了这条提示词的课程回落到系统默认提示词
    for col in ("grade_prompt_id", "report_prompt_id",
                "tag_horizontal_prompt_id", "tag_vertical_prompt_id"):
        db.execute(f"UPDATE courses SET {col}=0 WHERE {col}=?", (pid,))
    db.execute("DELETE FROM prompts WHERE id=?", (pid,))
    db.commit()
    flash(f"提示词「{row['name']}」已删除")
    return redirect(url_for("prompts_page"))


@_routes.route("/course/<oid:cid>/prompts", methods=["POST"])
@role_required("teacher")
def course_prompts_save(cid):
    """为整门课程选定批改 / 报告 / 知识点分析提示词。"""
    course = own_course(cid)
    db = get_db()
    picked = {}
    fields = (("grade_prompt_id", "grade"), ("report_prompt_id", "report"),
              ("tag_horizontal_prompt_id", "tag_horizontal"),
              ("tag_vertical_prompt_id", "tag_vertical"))
    for field, kind in fields:
        pid = request.form.get(field, type=int) or 0
        allowed = {r["id"] for r in _prompt_options(cid, kind)}
        if pid and pid not in allowed:
            flash("所选提示词不在本课程可用范围内，已保留原设置")
            pid = course[field] or 0
        picked[field] = pid
    db.execute("UPDATE courses SET grade_prompt_id=?, report_prompt_id=?,"
               " tag_horizontal_prompt_id=?, tag_vertical_prompt_id=? WHERE id=?",
               (picked["grade_prompt_id"], picked["report_prompt_id"],
                picked["tag_horizontal_prompt_id"],
                picked["tag_vertical_prompt_id"], cid))
    db.commit()
    flash("本课程的提示词配置已更新")
    return redirect(url_for("course_detail", cid=cid))


@_routes.route("/course/<oid:cid>/knowledge-graphs", methods=["POST"])
@role_required("teacher", "admin")
def course_knowledge_graphs_save(cid):
    """为课程关联一个或多个学科知识图谱；提交空选择即解除全部关联。"""
    own_course(cid)
    db = get_db()
    valid = _kg_accessible_ids(db, g.user)
    chosen = set()
    for raw in request.form.getlist("graph_ids"):
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        if gid in valid:
            chosen.add(gid)
    db.execute("DELETE FROM course_knowledge_graphs WHERE course_id=?", (cid,))
    db.executemany("INSERT INTO course_knowledge_graphs(course_id, graph_id) VALUES(?,?)",
                   [(cid, gid) for gid in sorted(chosen)])
    db.commit()
    kg_log.info("课程绑定知识图谱 user=%s#%s course=%s graphs=%s",
                g.user["username"], g.user["id"], cid, sorted(chosen))
    flash(f"已关联 {len(chosen)} 张知识图谱" if chosen else "已解除全部知识图谱关联")
    return redirect(url_for("course_detail", cid=cid))


def _course_graph_ids(db, cid):
    return [r["graph_id"] for r in db.execute(
        "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=?", (cid,))]


def _course_kg_nodes(db, cid):
    """本课程关联图谱下的全部节点（含定义），用于标签绑定与匹配展示。"""
    graph_ids = _course_graph_ids(db, cid)
    if not graph_ids:
        return []
    ph = ",".join("?" * len(graph_ids))
    return db.execute(
        f"SELECT id,graph_id,name,definition FROM kg_nodes WHERE graph_id IN ({ph})"
        f" ORDER BY name", graph_ids).fetchall()


def _course_tags(db, cid):
    return db.execute(
        "SELECT qt.tag, COUNT(*) AS n_questions, COUNT(DISTINCT l.id) AS n_lectures"
        " FROM question_tags qt JOIN lectures l ON l.id=qt.lecture_id"
        " WHERE l.course_id=? GROUP BY qt.tag"
        " ORDER BY n_questions DESC, qt.tag", (cid,)).fetchall()


def _sync_tag_link_to_weak_points(db, cid, tag, node):
    """老师绑定后立即刷新已有薄弱点记录，避免历史数据等下一轮复核才生效。"""
    db.execute(
        "UPDATE student_weak_points SET graph_id=?, node_id=?, node_name=?, definition=?"
        " WHERE course_id=? AND tag=?",
        (node["graph_id"], node["id"], node["name"], node["definition"] or "", cid, tag))


@_routes.route("/course/<oid:cid>/tag-links")
@role_required("teacher", "admin")
def course_tag_links(cid):
    """教师端：把课程里出现过的知识点标签手工绑定到图谱节点。"""
    course = own_course(cid)
    db = get_db()
    graphs = db.execute(
        "SELECT id,name,subject FROM knowledge_graphs WHERE id IN"
        " (SELECT graph_id FROM course_knowledge_graphs WHERE course_id=?)"
        " ORDER BY subject, name COLLATE NOCASE", (cid,)).fetchall()
    nodes = _course_kg_nodes(db, cid)
    name_to_node = {n["name"]: n for n in nodes}
    node_by_id = {n["id"]: n for n in nodes}
    links = _course_tag_links(db, cid)
    rows = []
    for r in _course_tags(db, cid):
        auto_node, auto_type = _kg_match_node(nodes, name_to_node, r["tag"])
        link = links.get(r["tag"])
        bound = node_by_id.get(link["node_id"]) if link else None
        rows.append({
            "tag": r["tag"],
            "n_questions": r["n_questions"],
            "n_lectures": r["n_lectures"],
            "auto_node": auto_node,
            "auto_type": auto_type,
            "bound_node_id": bound["id"] if bound else 0,
            "effective_node": bound or auto_node,
            "effective_is_manual": bool(bound),
        })
    nodes_by_graph = {}
    for n in nodes:
        nodes_by_graph.setdefault(n["graph_id"], []).append(n)
    return render_template("course_tag_links.html", course=course, graphs=graphs,
                           nodes_by_graph=nodes_by_graph, rows=rows,
                           unmatched=sum(1 for r in rows if not r["effective_node"]))


@_routes.route("/course/<oid:cid>/tag-links/save", methods=["POST"])
@role_required("teacher", "admin")
def course_tag_links_save(cid):
    own_course(cid)
    db = get_db()
    node_by_id = {n["id"]: n for n in _course_kg_nodes(db, cid)}
    valid_tags = {r["tag"] for r in _course_tags(db, cid)}
    saved = removed = skipped = 0
    for tag, raw in zip(request.form.getlist("tag"), request.form.getlist("node")):
        tag = (tag or "").strip()
        if tag not in valid_tags:
            continue
        try:
            nid = int(raw)
        except (TypeError, ValueError):
            nid = 0
        if not nid:
            removed += db.execute(
                "DELETE FROM tag_node_links WHERE course_id=? AND tag=?",
                (cid, tag)).rowcount
            continue
        node = node_by_id.get(nid)
        if not node:
            skipped += 1
            continue
        db.execute(
            "INSERT INTO tag_node_links(course_id,tag,graph_id,node_id,created_by)"
            " VALUES(?,?,?,?,?) ON CONFLICT(course_id,tag) DO UPDATE SET"
            " graph_id=excluded.graph_id, node_id=excluded.node_id,"
            " created_by=excluded.created_by",
            (cid, tag, node["graph_id"], node["id"], g.user["id"]))
        _sync_tag_link_to_weak_points(db, cid, tag, node)
        saved += 1
    db.commit()
    kg_log.info("课程标签绑定 user=%s#%s course=%s saved=%s removed=%s skipped=%s",
                g.user["username"], g.user["id"], cid, saved, removed, skipped)
    parts = []
    if saved:
        parts.append(f"已绑定 {saved} 个")
    if removed:
        parts.append(f"已解除 {removed} 个")
    flash("、".join(parts) + "知识点标签" if parts else "标签绑定未发生变化")
    return redirect(url_for("course_tag_links", cid=cid))


# ---------- 老师：学科知识图谱 ----------
KG_ROLE_LABEL = {"manage": "管理", "edit": "编辑", "use": "使用"}
KG_ROLE_RANK = {"use": 1, "edit": 2, "manage": 3}


def _kg_role(graph):
    """当前用户在该图谱上的最高协作角色；创建者与管理员始终是管理。"""
    if g.user["role"] == "admin" or graph["created_by"] == g.user["id"]:
        return "manage"
    row = get_db().execute(
        "SELECT role FROM kg_members WHERE graph_id=? AND user_id=?",
        (graph["id"], g.user["id"])).fetchone()
    return row["role"] if row else ""


def _kg_accessible_ids(db, user):
    """用户可引入到自己课程里的图谱集合：自己创建或被授予任意协作角色。"""
    if user["role"] == "admin":
        return {row["id"] for row in db.execute("SELECT id FROM knowledge_graphs")}
    return {row["id"] for row in db.execute(
        "SELECT id FROM knowledge_graphs WHERE created_by=? UNION "
        "SELECT graph_id FROM kg_members WHERE user_id=?",
        (user["id"], user["id"]))}


def _kg_graph(gid, edit=False, manage=False, use=False):
    """图谱按学科共享可查看；修改需要编辑权限，改名/删除需要管理权限。"""
    row = get_db().execute("SELECT * FROM knowledge_graphs WHERE id=?",
                           (gid,)).fetchone()
    if not row:
        abort(404)
    if not (edit or manage or use):
        return row
    role = _kg_role(row)
    if role == "manage":
        return row
    if manage or (edit and KG_ROLE_RANK.get(role, 0) < KG_ROLE_RANK["edit"]):
        abort(403)
    if use and KG_ROLE_RANK.get(role, 0) < KG_ROLE_RANK["use"]:
        abort(403)
    return row


def _kg_size_text(size):
    size = max(0, int(size or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


# 图谱相关表及其行内容长度估算：表名仅用于展示，SQL 按图谱过滤
KG_DB_TABLES = (
    ("图谱记录", "knowledge_graphs",
     "SELECT 1 AS n, LENGTH(subject)+LENGTH(name)+LENGTH(description)"
     "+LENGTH(grade_level)+48 AS b FROM knowledge_graphs WHERE id=?"),
    ("语料记录", "kg_materials",
     "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(filename)+LENGTH(stored_name)"
     "+LENGTH(mime)+LENGTH(sha256)+LENGTH(status)+LENGTH(error)"
     "+LENGTH(COALESCE(started_at,''))+LENGTH(COALESCE(finished_at,''))"
     "+LENGTH(created_at)+72),0) AS b FROM kg_materials WHERE graph_id=?"),
    ("文本块", "kg_chunks",
     "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(content)+LENGTH(status)"
     "+LENGTH(error)+LENGTH(extraction_json)+LENGTH(COALESCE(started_at,''))"
     "+LENGTH(COALESCE(finished_at,''))+48),0) AS b"
     " FROM kg_chunks WHERE graph_id=?"),
    ("概念节点", "kg_nodes",
     "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(name)+LENGTH(normalized)"
     "+LENGTH(category)+LENGTH(definition)+LENGTH(aliases)"
     "+LENGTH(source_materials)+LENGTH(created_at)+LENGTH(updated_at)"
     "+48),0) AS b FROM kg_nodes WHERE graph_id=?"),
    ("关系边", "kg_edges",
     "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(relation)+LENGTH(evidence)"
     "+LENGTH(source_materials)+LENGTH(created_at)+40),0) AS b"
     " FROM kg_edges WHERE graph_id=?"),
    ("体检记录", "kg_audits",
     "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(status)+LENGTH(model)"
     "+LENGTH(error)+LENGTH(suggestions)+LENGTH(rejected)+LENGTH(snapshot)"
     "+LENGTH(created_at)+LENGTH(COALESCE(applied_at,''))+56),0) AS b"
     " FROM kg_audits WHERE graph_id=?"),
    ("协作成员", "kg_members",
     "SELECT COUNT(*) AS n, COUNT(*)*40 AS b FROM kg_members WHERE graph_id=?"),
)


def _kg_db_usage(db, graph_id):
    """按数据表估算该图谱的数据库占用；行内容之外计入约 15% 行头与索引开销。"""
    items, total = [], 0
    for label, table, sql in KG_DB_TABLES:
        row = db.execute(sql, (graph_id,)).fetchone()
        count = row["n"] or 0
        size = int(round((row["b"] or 0) * 1.15)) if count else 0
        items.append({"label": label, "table": table, "count": count,
                      "bytes": size, "text": _kg_size_text(size)})
        total += size
    return items, total


def _kg_metrics(db, graph_id):
    """图谱空间占用、累计建图耗时、token 与涉及过的模型。"""
    usage_sql = (
        "o.material_id IN (SELECT id FROM kg_materials WHERE graph_id=?)"
        " OR o.id IN (SELECT operation_id FROM kg_audits"
        " WHERE graph_id=? AND operation_id IS NOT NULL)"
        " OR o.id IN (SELECT operation_id FROM kg_category_maps"
        " WHERE graph_id=? AND operation_id IS NOT NULL)")
    material = db.execute(
        "SELECT COALESCE(SUM(size),0) AS bytes,"
        " COALESCE(SUM(CASE WHEN started_at IS NOT NULL AND finished_at IS NOT NULL"
        " THEN MAX(0,(julianday(finished_at)-julianday(started_at))*86400)"
        " ELSE 0 END),0) AS seconds"
        " FROM kg_materials WHERE graph_id=?", (graph_id,)).fetchone()
    usage = db.execute(
        "SELECT SUM(e.total_tokens) AS tokens FROM ai_usage_events e"
        " JOIN ai_operations o ON o.id=e.operation_id WHERE " + usage_sql,
        (graph_id, graph_id, graph_id)).fetchone()
    models = [row["model"] for row in db.execute(
        "SELECT DISTINCT o.model FROM ai_operations o WHERE o.model<>'' AND ("
        + usage_sql + ") ORDER BY o.model", (graph_id, graph_id, graph_id))]
    tokens = usage["tokens"] if usage else None
    db_items, db_bytes = _kg_db_usage(db, graph_id)
    file_bytes = material["bytes"] or 0
    return {
        "size_text": _kg_size_text(file_bytes + db_bytes),
        "file_text": _kg_size_text(file_bytes),
        "db_text": _kg_size_text(db_bytes),
        "db_rows": db_items,
        "duration_text": (_kg_duration_text(material["seconds"])
                          if material["seconds"] else "—"),
        "tokens_text": f"{int(tokens):,}" if tokens is not None else "—",
        "models_text": "、".join(models) if models else "—",
    }


# 多份资料集中上传时串行建图，避免同时打满模型限流；集合防止重复入队
_kg_build_lock = LocalProxy(lambda: _instance_service("kg_state")["build_lock"])
_kg_schedule_lock = LocalProxy(lambda: _instance_service("kg_state")["schedule_lock"])
_kg_enqueued = LocalProxy(lambda: _instance_service("kg_state")["enqueued"])


def _kg_start_build(graph_id, material_id, teacher_id):
    """后台构建图谱：请求返回后继续跑，资料状态可刷新查看。"""
    with _kg_schedule_lock:
        if material_id in _kg_enqueued:
            return False
        _kg_enqueued.add(material_id)
    try:
        threading.Thread(target=_kg_build_worker,
                         args=(graph_id, material_id, teacher_id),
                         kwargs={"application": current_app._get_current_object() if has_app_context() else app},
                         daemon=True).start()
    except Exception:
        with _kg_schedule_lock:
            _kg_enqueued.discard(material_id)
        raise
    return True


def _kg_build_worker(graph_id, material_id, teacher_id, application=None):
    application = application or (current_app._get_current_object() if has_app_context() else app)
    with application.app_context():
        return _kg_build_worker_in_context(graph_id, material_id, teacher_id)


def _kg_build_worker_in_context(graph_id, material_id, teacher_id):
    try:
        with _kg_build_lock:
            with _service_app_context():
                db = get_db()
                try:
                    material = db.execute(
                        "SELECT status FROM kg_materials WHERE id=? AND graph_id=?",
                        (material_id, graph_id)).fetchone()
                    if not material or material["status"] not in (
                            kg_utils.STATUS_PENDING, kg_utils.STATUS_BUILDING):
                        return
                    api_url, api_key, model, err = _ai_endpoint(teacher_id)
                    if err:
                        db.execute("UPDATE kg_materials SET status=?, error=?,"
                                   "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (kg_utils.STATUS_FAILED, err, material_id))
                        db.commit()
                        return
                    operation = _start_ai_operation(
                        teacher_id, "kg_build", model, material_id=material_id)
                    try:
                        result = kg_utils.build_material(
                            db, material_id, api_url, api_key, model,
                            usage_hook=operation["usage_hook"],
                            max_tokens=_get_max_tokens(teacher_id, "max_tokens_kg_build"))
                    except Exception as e:  # noqa: BLE001 后台任务失败写入资料状态
                        _finish_ai_operation(operation, "failed", str(e))
                        db.execute("UPDATE kg_materials SET status=?, error=?,"
                                   "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                                   (kg_utils.STATUS_FAILED, str(e)[:500], material_id))
                        db.commit()
                        kg_log.exception("知识图谱构建失败 material=%s", material_id)
                        return
                    if result:
                        _finish_ai_operation(operation, "success", "")
                    else:
                        row = db.execute("SELECT error FROM kg_materials WHERE id=?",
                                         (material_id,)).fetchone()
                        _finish_ai_operation(operation, "failed", row["error"] if row else "")
                finally:
                    close_db()
    finally:
        with _kg_schedule_lock:
            _kg_enqueued.discard(material_id)


def start_kg_recovery():
    """服务启动后按上传顺序恢复中断及等待中的资料，成功块由建图逻辑直接复用。"""
    with _service_app_context():
        try:
            rows = get_db().execute(
                "SELECT id,graph_id,uploaded_by FROM kg_materials "
                "WHERE status IN (?,?) ORDER BY id",
                (kg_utils.STATUS_PENDING, kg_utils.STATUS_BUILDING)).fetchall()
        finally:
            close_db()
    for row in rows:
        _kg_start_build(row["graph_id"], row["id"], row["uploaded_by"])
    if rows:
        kg_log.info("已恢复 %s 份知识图谱资料的建图队列", len(rows))
    return len(rows)


@_routes.route("/knowledge-graphs")
@role_required("teacher", "admin")
def knowledge_graphs():
    rows = get_db().execute("""
        SELECT g.*,
          (SELECT COUNT(*) FROM kg_materials m WHERE m.graph_id=g.id) AS n_materials,
          (SELECT COUNT(*) FROM kg_nodes n WHERE n.graph_id=g.id) AS n_nodes,
          (SELECT COUNT(*) FROM kg_edges e WHERE e.graph_id=g.id) AS n_edges
        FROM knowledge_graphs g ORDER BY g.subject, g.name""").fetchall()
    return render_template("knowledge_graphs.html", graphs=rows,
                           max_import_mb=cfg.get("MAX_RESTORE_MB"))


@_routes.route("/knowledge-graphs/create", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_create():
    subject = request.form.get("subject", "").strip()[:40]
    name = request.form.get("name", "").strip()[:80]
    if not subject or not name:
        flash("学科与图谱名称都不能为空")
        return redirect(url_for("knowledge_graphs"))
    db = get_db()
    cur = db.execute(
        "INSERT INTO knowledge_graphs(subject,name,description,grade_level,created_by)"
        " VALUES(?,?,?,?,?)",
        (subject, name, request.form.get("description", "").strip()[:300],
         request.form.get("grade_level", "").strip()[:40], g.user["id"]))
    db.commit()
    kg_log.info("新建知识图谱 user=%s#%s subject=%s name=%s",
             g.user["username"], g.user["id"], subject, name)
    flash(f"已创建知识图谱「{name}」，接下来上传教材、考纲或讲义来构建它")
    return redirect(url_for("knowledge_graph_detail", gid=cur.lastrowid))


@_routes.route("/knowledge-graphs/<oid:gid>/export")
@role_required("teacher", "admin")
def knowledge_graph_export(gid):
    """导出迁移包；scope=graph 时仅导出图谱结构，不含原始资料文件。"""
    graph = _kg_graph(gid, manage=True)
    scope = "graph" if request.args.get("scope") == "graph" else "full"
    os.makedirs(cfg.get("EXPORT_DIR"), exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=f"kg_{gid}_", suffix=".zip", dir=cfg.get("EXPORT_DIR"))
    os.close(fd)
    try:
        kg_transfer.export_graph(get_db(), gid, _upload_dir(), path, scope=scope)
    except Exception:  # noqa: BLE001 半截导出文件不能留在服务器
        kg_log.exception("知识图谱迁移包导出失败 user=%s#%s graph=%s scope=%s",
                         g.user["username"], g.user["id"], gid, scope)
        try:
            os.remove(path)
        except OSError:
            pass
        flash("知识图谱导出失败，请查看知识图谱日志")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    kg_log.info("导出知识图谱迁移包 user=%s#%s graph=%s scope=%s package=%s",
                g.user["username"], g.user["id"], gid, scope, os.path.basename(path))
    suffix = "_知识图谱.zip" if scope == "graph" else "_完整迁移包.zip"
    download = (f"{course_export.safe_component(graph['subject'], '知识图谱')}"
                f"{suffix}")
    response = send_file(path, as_attachment=True, download_name=download)
    response.call_on_close(lambda: os.remove(path) if os.path.exists(path) else None)
    return response


@_routes.route("/knowledge-graphs/import", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_import():
    """导入完整迁移包；始终新建图谱，不覆盖同名的既有图谱。"""
    name = request.form.get("name", "").strip()[:80]
    if not name:
        flash("请为新导入的知识图谱命名")
        return redirect(url_for("knowledge_graphs"))
    package = request.files.get("package")
    if not package or not package.filename:
        flash("请选择知识图谱迁移包")
        return redirect(url_for("knowledge_graphs"))
    if not package.filename.lower().endswith(".zip"):
        flash("迁移包必须是 ZIP 文件")
        return redirect(url_for("knowledge_graphs"))
    tmp = tempfile.NamedTemporaryFile(
        prefix=".kg_import_", suffix=".zip", dir=cfg.get("DATA_DIR"), delete=False)
    package_path = tmp.name
    try:
        size = 0
        with tmp:
            while True:
                chunk = package.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > cfg.get("MAX_RESTORE_MB") * 1024 * 1024:
                    raise ValueError(f"迁移包超过 {cfg.get("MAX_RESTORE_MB")}MB 限制")
                tmp.write(chunk)
        result = kg_transfer.import_graph(
            get_db(), package_path, _upload_dir(), g.user["id"], name=name)
    except (ValueError, zipfile.BadZipFile) as e:
        kg_log.warning("知识图谱迁移包导入被拒绝 user=%s#%s file=%s error=%s",
                       g.user["username"], g.user["id"], package.filename, e)
        flash(f"知识图谱导入失败：{e}")
        return redirect(url_for("knowledge_graphs"))
    except Exception:  # noqa: BLE001 页面不暴露内部路径，详细信息留在 KG 日志
        kg_log.exception("知识图谱迁移包导入异常 user=%s#%s file=%s",
                         g.user["username"], g.user["id"], package.filename)
        flash("知识图谱导入失败，请检查迁移包或查看知识图谱日志")
        return redirect(url_for("knowledge_graphs"))
    finally:
        try:
            os.remove(package_path)
        except OSError:
            pass
    graph = _kg_graph(result["graph_id"])
    kg_log.info("导入知识图谱迁移包 user=%s#%s new_graph=%s file=%s",
                g.user["username"], g.user["id"], graph["id"], package.filename)
    flash(f"已导入知识图谱「{graph['name']}」，未自动调用 AI")
    return redirect(url_for("knowledge_graph_detail", gid=graph["id"]))


def _kg_status_view(status):
    """资料状态的展示文案与样式，页面首屏与轮询接口共用。"""
    if status == kg_utils.STATUS_DONE:
        return {"label": "已完成", "style": "done", "icon": "✓"}
    if status == kg_utils.STATUS_BUILDING:
        return {"label": "处理中", "style": "building", "icon": ""}
    if status == kg_utils.STATUS_FAILED:
        return {"label": "处理失败", "style": "failed", "icon": "✕"}
    return {"label": "待处理", "style": "pending", "icon": "○"}


def _kg_status_counts(materials):
    counts = {"done": 0, "building": 0, "pending": 0, "failed": 0}
    for item in materials:
        counts[_kg_status_view(item["status"])["style"]] += 1
    return counts


def _kg_duration_text(seconds):
    if seconds is None:
        return ""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒" if secs else f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def _kg_material_view(db, item):
    """资料状态、本地时间、chunk 进度与基于实际块耗时的剩余时间粗估。"""
    view = dict(item)
    view.update(_kg_status_view(item["status"]))
    for key in ("created_at", "started_at", "finished_at"):
        view[key + "_local"] = _localtime(item[key])
    timing = db.execute(
        "SELECT ROUND((julianday(COALESCE(finished_at,CURRENT_TIMESTAMP))-"
        "julianday(started_at))*86400,1) AS elapsed FROM kg_materials "
        "WHERE id=? AND started_at IS NOT NULL", (item["id"],)).fetchone()
    view["elapsed_s"] = timing["elapsed"] if timing else None
    view["elapsed_text"] = _kg_duration_text(view["elapsed_s"])
    chunks = {kg_utils.CHUNK_PENDING: 0, kg_utils.CHUNK_RUNNING: 0,
              kg_utils.CHUNK_SUCCESS: 0, kg_utils.CHUNK_FAILED: 0}
    for row in db.execute("SELECT status,COUNT(*) AS n FROM kg_chunks "
                          "WHERE material_id=? GROUP BY status", (item["id"],)):
        chunks[row["status"]] = row["n"]
    view["chunks"] = {"total": sum(chunks.values()), "pending": chunks["pending"],
                      "running": chunks["running"], "success": chunks["success"],
                      "failed": chunks["failed"]}
    durations = sorted(row[0] for row in db.execute(
        "SELECT (julianday(finished_at)-julianday(started_at))*86400 FROM kg_chunks "
        "WHERE material_id=? AND status='success' "
        "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC,id DESC LIMIT 5", (item["id"],)))
    eta = None
    if durations and item["status"] in (kg_utils.STATUS_PENDING, kg_utils.STATUS_BUILDING):
        middle = len(durations) // 2
        typical = (durations[middle] if len(durations) % 2 else
                   (durations[middle - 1] + durations[middle]) / 2)
        eta = chunks[kg_utils.CHUNK_PENDING] * typical
        running = db.execute(
            "SELECT (julianday(CURRENT_TIMESTAMP)-julianday(started_at))*86400 AS elapsed "
            "FROM kg_chunks WHERE material_id=? AND status='running' "
            "ORDER BY chunk_no LIMIT 1", (item["id"],)).fetchone()
        if running:
            eta += max(0, typical - (running["elapsed"] or 0))
    view["eta_s"] = max(0, round(eta)) if eta is not None else None
    view["eta_text"] = _kg_duration_text(view["eta_s"])
    return view


def _kg_material_requests(db, material_id):
    """展开单份资料时返回逐次请求，包含重启前的历史操作与 HTTP 重试。"""
    rows = db.execute("""
        SELECT e.id,e.operation_id,e.attempt,e.status,e.latency_ms,e.total_tokens,
          e.prompt_tokens,e.completion_tokens,e.finish_reason,e.error,e.created_at,
          c.chunk_no
        FROM ai_usage_events e
        JOIN ai_operations o ON o.id=e.operation_id
        LEFT JOIN kg_chunks c ON c.id=e.chunk_id
        WHERE o.material_id=? ORDER BY e.id""", (material_id,)).fetchall()
    result = []
    for row in rows:
        elapsed = row["latency_ms"] / 1000 if row["latency_ms"] is not None else None
        if elapsed is None and row["status"] == "running":
            elapsed = db.execute(
                "SELECT (julianday(CURRENT_TIMESTAMP)-julianday(?))*86400",
                (row["created_at"],)).fetchone()[0]
        result.append({
            "id": row["id"], "operation_id": row["operation_id"],
            "chunk_no": row["chunk_no"] + 1 if row["chunk_no"] is not None else None,
            "attempt": row["attempt"], "status": row["status"],
            "status_label": {"running": "进行中", "success": "成功"}.get(
                row["status"], "失败"),
            "started_at": _localtime(row["created_at"]),
            "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
            "elapsed_text": _kg_duration_text(elapsed),
            "prompt_tokens": row["prompt_tokens"], "completion_tokens": row["completion_tokens"],
            "total_tokens": row["total_tokens"], "finish_reason": row["finish_reason"] or "",
            "error": row["error"] or "",
        })
    return result


def _kg_audit_view(row):
    """把一条体检记录转成模板可直接渲染的结构。"""
    try:
        items = json.loads(row["suggestions"] or "[]")
    except (TypeError, ValueError):
        items = []
    try:
        rejected = json.loads(row["rejected"] or "[]")
    except (TypeError, ValueError):
        rejected = []
    for item in items:
        item["action_label"] = kg_utils.AUDIT_ACTION_LABEL.get(
            item.get("action"), item.get("action") or "")
    return {
        "id": row["id"], "status": row["status"],
        "model": row["model"] or "", "error": row["error"] or "",
        "created_at": _localtime(row["created_at"]),
        "applied_at": _localtime(row["applied_at"]) if row["applied_at"] else "",
        "items": items, "rejected": rejected,
        "has_snapshot": bool(row["snapshot"]),
    }


def _kg_category_map_view(row):
    """把一条分类重构记录转成模板可直接渲染的结构。"""
    try:
        items = json.loads(row["mappings"] or "[]")
    except (TypeError, ValueError):
        items = []
    try:
        rejected = json.loads(row["rejected"] or "[]")
    except (TypeError, ValueError):
        rejected = []
    return {
        "id": row["id"], "status": row["status"],
        "model": row["model"] or "", "error": row["error"] or "",
        "created_at": _localtime(row["created_at"]),
        "applied_at": _localtime(row["applied_at"]) if row["applied_at"] else "",
        "items": items, "rejected": rejected,
        "has_snapshot": bool(row["snapshot"]),
        "count": sum(int(item.get("count") or 0) for item in items),
    }


@_routes.route("/knowledge-graphs/<oid:gid>")
@role_required("teacher", "admin")
def knowledge_graph_detail(gid):
    graph = _kg_graph(gid)
    db = get_db()
    rows = db.execute("SELECT * FROM kg_materials WHERE graph_id=? "
                      "ORDER BY id DESC", (gid,)).fetchall()
    materials = [_kg_material_view(db, row) for row in rows]
    building = any(m["status"] in (kg_utils.STATUS_PENDING, kg_utils.STATUS_BUILDING)
                   for m in materials)
    audits = [_kg_audit_view(row) for row in db.execute(
        "SELECT * FROM kg_audits WHERE graph_id=? ORDER BY id DESC LIMIT 3",
        (gid,)).fetchall()]
    cat_maps = [_kg_category_map_view(row) for row in db.execute(
        "SELECT * FROM kg_category_maps WHERE graph_id=? ORDER BY id DESC LIMIT 10",
        (gid,)).fetchall()]
    role = _kg_role(graph)
    members = db.execute(
        "SELECT m.user_id, m.role, u.username, u.display_name FROM kg_members m"
        " JOIN users u ON u.id=m.user_id WHERE m.graph_id=? ORDER BY m.role, u.display_name",
        (gid,)).fetchall()
    member_ids = {row["user_id"] for row in members}
    teacher_candidates = [row for row in db.execute(
        "SELECT id, username, display_name FROM users WHERE role='teacher'"
        " AND active=1 AND id<>? ORDER BY display_name",
        (graph["created_by"],)) if row["id"] not in member_ids]
    return render_template(
        "knowledge_graph.html", graph=graph, materials=materials,
        counts=kg_utils.graph_counts(db, gid),
        status_counts=_kg_status_counts(materials),
        nodes=kg_utils.list_nodes(db, gid), edges=kg_utils.list_edges(db, gid),
        building=building, relation_label=kg_utils.RELATION_LABEL,
        layered=kg_utils.layered_graph(db, gid),
        cycles=kg_utils.prerequisite_cycles(db, gid),
        audits=audits,
        cat_maps=cat_maps,
        metrics=_kg_metrics(db, gid),
        members=members, teacher_candidates=teacher_candidates,
        role=role, role_label=KG_ROLE_LABEL.get(role, "只读"),
        role_labels=KG_ROLE_LABEL,
        max_upload_mb=cfg.get("KG_MAX_UPLOAD_MB"),
        can_edit=role in ("manage", "edit"),
        can_manage=role == "manage")


@_routes.route("/knowledge-graphs/<oid:gid>/graph.json")
@role_required("teacher", "admin")
def knowledge_graph_graph_data(gid):
    """知识星系数据：一次性下发全量节点、关系、分类与连接度。"""
    _kg_graph(gid)
    payload = kg_utils.graph_payload(get_db(), gid)
    for node in payload["nodes"]:
        node["url"] = url_for("knowledge_graph_concept", gid=gid, nid=node["id"])
    return jsonify(payload)


@_routes.route("/knowledge-graphs/<oid:gid>/neighbors.json")
@role_required("teacher", "admin")
def knowledge_graph_neighbors_data(gid):
    """知识点详情：复用 concept_neighbors 并补齐可跳转链接。"""
    _kg_graph(gid)
    node_id = request.args.get("node_id", type=int)
    if not node_id:
        abort(400)
    detail = kg_utils.concept_neighbors(get_db(), gid, node_id)
    if detail is None:
        abort(404)
    node = detail["node"]

    def with_url(items):
        return [dict(item, url=url_for("knowledge_graph_concept", gid=gid,
                                       nid=item["id"])) for item in items]

    return jsonify({
        "node": {
            "id": node["id"], "name": node["name"],
            "category": node["category"] or "未分类",
            "definition": node["definition"] or "",
            "confidence": node["confidence"] or 0,
            "url": url_for("knowledge_graph_concept", gid=gid, nid=node["id"]),
        },
        "prerequisites": with_url(detail["prerequisites"]),
        "dependents": with_url(detail["dependents"]),
        "others": with_url(detail["others"]),
    })


@_routes.route("/knowledge-graphs/<oid:gid>/scope.json")
@role_required("teacher", "admin")
def knowledge_graph_scope_data(gid):
    """当前下钻层级的节点与关联边，用于让下方概念/关系表跟随星图联动。"""
    graph = _kg_graph(gid)
    db = get_db()
    ids = []
    for part in (request.args.get("ids") or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    ids = ids[:200]
    if not ids:
        return jsonify({"nodes": [], "edges": [], "can_edit": False,
                        "csrf": "", "node_total": 0, "edge_total": 0})
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT * FROM kg_nodes WHERE graph_id=? AND id IN ({marks})",
        (gid, *ids)).fetchall()
    order = {node_id: index for index, node_id in enumerate(ids)}
    rows = sorted(rows, key=lambda row: order.get(row["id"], len(ids)))
    node_payload = []
    for row in rows:
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except (TypeError, ValueError):
            aliases = []
        node_payload.append({
            "name": row["name"],
            "url": url_for("knowledge_graph_concept", gid=gid, nid=row["id"]),
            "category": row["category"] or "",
            "definition": row["definition"] or "",
            "aliases": "、".join(str(alias) for alias in aliases),
            "confidence": round((row["confidence"] or 0) * 100),
        })
    edge_rows = db.execute(
        f"SELECT e.*, s.name AS src_name, d.name AS dst_name FROM kg_edges e"
        f" JOIN kg_nodes s ON s.id=e.src_id JOIN kg_nodes d ON d.id=e.dst_id"
        f" WHERE e.graph_id=? AND e.src_id IN ({marks}) AND e.dst_id IN ({marks})"
        f" ORDER BY e.confidence DESC, e.id LIMIT 400",
        (gid, *ids, *ids)).fetchall()
    edge_total = db.execute(
        f"SELECT COUNT(*) FROM kg_edges WHERE graph_id=? AND src_id IN ({marks})"
        f" AND dst_id IN ({marks})", (gid, *ids, *ids)).fetchone()[0]
    can_edit = _kg_role(graph) in ("manage", "edit")
    edge_payload = [{
        "relation": row["relation"],
        "relation_label": kg_utils.RELATION_LABEL.get(row["relation"], row["relation"]),
        "src_name": row["src_name"], "dst_name": row["dst_name"],
        "evidence": row["evidence"] or "",
        "confidence": round((row["confidence"] or 0) * 100),
        "reverse_url": url_for("knowledge_graph_edge_reverse", gid=gid,
                               eid=row["id"]) if row["relation"] == "prerequisite" else "",
        "delete_url": url_for("knowledge_graph_edge_delete", gid=gid, eid=row["id"]),
    } for row in edge_rows]
    return jsonify({
        "nodes": node_payload, "edges": edge_payload, "can_edit": can_edit,
        "csrf": csrf_token(),
        "node_total": len(node_payload), "edge_total": edge_total,
    })


@_routes.route("/knowledge-graphs/<oid:gid>/concepts/<oid:nid>")
@role_required("teacher", "admin")
def knowledge_graph_concept(gid, nid):
    """单个概念的上下游邻域，服务讲评时的根因追问。"""
    graph = _kg_graph(gid)
    db = get_db()
    detail = kg_utils.concept_neighbors(db, gid, nid)
    if not detail:
        abort(404)
    return render_template(
        "knowledge_graph_concept.html", graph=graph, detail=detail,
        relation_label=kg_utils.RELATION_LABEL)


@_routes.route("/knowledge-graphs/<oid:gid>/status.json")
@role_required("teacher", "admin")
def knowledge_graph_status(gid):
    """供页面轮询，避免建图期间整页刷新打断正在选择的新资料。"""
    _kg_graph(gid)
    db = get_db()
    rows = db.execute("SELECT * FROM kg_materials WHERE graph_id=? "
                      "ORDER BY id DESC", (gid,)).fetchall()
    materials = [_kg_material_view(db, row) for row in rows]
    payload = {
        "counts": kg_utils.graph_counts(db, gid),
        "status_counts": _kg_status_counts(materials),
        "building": any(v["style"] in ("pending", "building") for v in materials),
        "materials": [{
            "id": item["id"], "label": item["label"], "style": item["style"],
            "icon": item["icon"], "error": item["error"] or "",
            "page_count": item["page_count"] or 0, "char_count": item["char_count"] or 0,
            "created_at_local": item["created_at_local"],
            "started_at_local": item["started_at_local"],
            "finished_at_local": item["finished_at_local"],
            "elapsed_text": item["elapsed_text"], "chunks": item["chunks"],
            "eta_s": item["eta_s"], "eta_text": item["eta_text"],
        } for item in materials],
    }
    material_id = request.args.get("material_id", type=int)
    if material_id is not None:
        if not any(item["id"] == material_id for item in materials):
            abort(404)
        payload["request_material_id"] = material_id
        payload["requests"] = _kg_material_requests(db, material_id)
    return jsonify(payload)


@_routes.route("/knowledge-graphs/<oid:gid>/materials", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_upload(gid):
    _kg_graph(gid, edit=True)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("请选择要上传的资料文件")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    db = get_db()
    abs_dir = os.path.join(_upload_dir(), "kg", str(gid))
    os.makedirs(abs_dir, exist_ok=True)
    queued = 0
    for item in files:
        filename = item.filename
        ext = os.path.splitext(filename)[1].lower()
        if ext not in kg_text.KG_EXTENSIONS:
            flash(f"「{filename}」格式不支持，只接受 PDF / Markdown / TXT")
            continue
        stored = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(4)}{ext}"
        abs_path = os.path.join(abs_dir, stored)
        digest = hashlib.sha256()
        size = 0
        too_big = False
        with open(abs_path, "wb") as fh:
            while True:
                chunk = item.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > cfg.get("KG_MAX_UPLOAD_MB") * 1024 * 1024:
                    too_big = True
                    break
                digest.update(chunk)
                fh.write(chunk)
        if too_big:
            os.remove(abs_path)
            flash(f"「{filename}」超过 {cfg.get("KG_MAX_UPLOAD_MB")}MB，已跳过")
            continue
        sha = digest.hexdigest()
        duplicate = db.execute(
            "SELECT id FROM kg_materials WHERE graph_id=? AND sha256=?",
            (gid, sha)).fetchone()
        if duplicate:
            os.remove(abs_path)
            flash(f"「{filename}」与已上传资料内容相同，未重复导入")
            continue
        cur = db.execute(
            "INSERT INTO kg_materials(graph_id,filename,stored_name,mime,size,sha256,"
            "status,uploaded_by) VALUES(?,?,?,?,?,?,?,?)",
            (gid, filename[:200], f"kg/{gid}/{stored}", item.mimetype or "",
             size, sha, kg_utils.STATUS_PENDING, g.user["id"]))
        db.commit()
        material = db.execute("SELECT * FROM kg_materials WHERE id=?",
                              (cur.lastrowid,)).fetchone()
        chunks, _truncated = kg_utils.extract_material(db, material, abs_path)
        if not chunks:
            failed = db.execute("SELECT error FROM kg_materials WHERE id=?",
                                (material["id"],)).fetchone()
            flash(f"「{filename}」解析失败：{failed['error'] if failed else '未知原因'}")
            continue
        _kg_start_build(gid, material["id"], g.user["id"])
        queued += 1
        kg_log.info("上传知识图谱资料 user=%s#%s graph=%s material=%s file=%s size=%s chunks=%s",
                    g.user["username"], g.user["id"], gid, material["id"], filename, size, chunks)
        flash(f"「{filename}」已解析 {chunks} 个文本块，已加入建图队列")
    if queued > 1:
        flash(f"共 {queued} 份资料已排队，将逐个建图，可在下方查看每份状态")
    if queued:
        db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (gid,))
        db.commit()
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/materials/<oid:mid>/retry", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_material_retry(mid):
    """复用成功文本块的抽取结果，只把未完成块重新加入建图队列。"""
    db = get_db()
    material = db.execute("SELECT * FROM kg_materials WHERE id=?", (mid,)).fetchone()
    if not material:
        abort(404)
    _kg_graph(material["graph_id"], edit=True)
    if material["status"] != kg_utils.STATUS_FAILED:
        flash("只有处理失败的资料可以重试")
        return redirect(url_for("knowledge_graph_detail", gid=material["graph_id"]))
    db.execute("UPDATE kg_materials SET status=?,error='',finished_at=NULL WHERE id=?",
               (kg_utils.STATUS_PENDING, mid))
    db.execute("UPDATE kg_chunks SET status=?,error='',started_at=NULL,finished_at=NULL "
               "WHERE material_id=? AND status IN (?,?)",
               (kg_utils.CHUNK_PENDING, mid, kg_utils.CHUNK_RUNNING, kg_utils.CHUNK_FAILED))
    db.commit()
    queued = _kg_start_build(material["graph_id"], mid, g.user["id"])
    kg_log.info("重试知识图谱资料建图 user=%s#%s graph=%s material=%s queued=%s",
                g.user["username"], g.user["id"], material["graph_id"], mid, queued)
    flash("已加入重试队列，将复用已成功的文本块" if queued else "该资料已在建图队列中")
    return redirect(url_for("knowledge_graph_detail", gid=material["graph_id"]))


@_routes.route("/knowledge-graphs/materials/<oid:mid>/delete", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_material_delete(mid):
    db = get_db()
    material = db.execute("SELECT * FROM kg_materials WHERE id=?", (mid,)).fetchone()
    if not material:
        abort(404)
    _kg_graph(material["graph_id"], edit=True)
    try:
        os.remove(os.path.join(_upload_dir(), material["stored_name"]))
    except OSError:
        pass
    kg_utils.delete_material(db, material)
    kg_log.info("删除知识图谱资料 user=%s#%s graph=%s material=%s file=%s",
                g.user["username"], g.user["id"], material["graph_id"], mid,
                material["filename"])
    flash(f"已删除资料「{material['filename']}」及其独有节点")
    return redirect(url_for("knowledge_graph_detail", gid=material["graph_id"]))


@_routes.route("/knowledge-graphs/<oid:gid>/edit", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_edit(gid):
    """修改图谱名称与说明；学科与资料范围保持不变。"""
    _kg_graph(gid, manage=True)
    name = request.form.get("name", "").strip()[:80]
    if not name:
        flash("图谱名称不能为空")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    description = request.form.get("description", "").strip()[:300]
    db = get_db()
    db.execute("UPDATE knowledge_graphs SET name=?, description=?,"
               " updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (name, description, gid))
    db.commit()
    kg_log.info("编辑知识图谱 user=%s#%s id=%s name=%s",
             g.user["username"], g.user["id"], gid, name)
    flash("已更新图谱信息")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/edges/<oid:eid>/delete", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_edge_delete(gid, eid):
    _kg_graph(gid, edit=True)
    db = get_db()
    edge = db.execute(
        "SELECT e.*, s.name AS src_name, d.name AS dst_name FROM kg_edges e "
        "JOIN kg_nodes s ON s.id=e.src_id JOIN kg_nodes d ON d.id=e.dst_id "
        "WHERE e.id=? AND e.graph_id=?", (eid, gid)).fetchone()
    if not edge:
        abort(404)
    db.execute("DELETE FROM kg_edges WHERE id=?", (eid,))
    db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (gid,))
    db.commit()
    kg_log.info("删除知识图谱关系 user=%s#%s graph=%s edge=%s",
             g.user["username"], g.user["id"], gid, eid)
    flash(f"已删除关系「{edge['src_name']} → {edge['dst_name']}」")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/edges/<oid:eid>/reverse", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_edge_reverse(gid, eid):
    """反向前置关系；先排除原边判断是否会制造新环，有重复边时直接去重。"""
    _kg_graph(gid, edit=True)
    db = get_db()
    edge = db.execute(
        "SELECT e.*, s.name AS src_name, d.name AS dst_name FROM kg_edges e "
        "JOIN kg_nodes s ON s.id=e.src_id JOIN kg_nodes d ON d.id=e.dst_id "
        "WHERE e.id=? AND e.graph_id=?", (eid, gid)).fetchone()
    if not edge:
        abort(404)
    if edge["relation"] != "prerequisite":
        flash("只有前置关系可以反向")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    mirror = db.execute(
        "SELECT id FROM kg_edges WHERE graph_id=? AND src_id=? AND dst_id=? "
        "AND relation='prerequisite' AND id<>?",
        (gid, edge["dst_id"], edge["src_id"], eid)).fetchone()
    if not mirror and kg_utils.prerequisite_reachable(
            db, gid, edge["src_id"], edge["dst_id"], exclude_edge_id=eid):
        flash("反向会与现有前置关系形成环，已取消")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    kg_utils.reverse_edge(db, gid, eid)
    db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (gid,))
    db.commit()
    kg_log.info("反向知识图谱关系 user=%s#%s graph=%s edge=%s mirror=%s",
             g.user["username"], g.user["id"], gid, eid, bool(mirror))
    if mirror:
        flash(f"反向边已存在，已合并去重为「{edge['dst_name']} → {edge['src_name']}」")
    else:
        flash(f"已反转为「{edge['dst_name']} → {edge['src_name']}」")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/audit", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_audit_run(gid):
    """让模型给出结构化治理建议：异步执行，结果保存在 kg_audits。"""
    _kg_graph(gid, edit=True)
    db = get_db()
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        db.execute(
            "INSERT INTO kg_audits(graph_id,status,model,error,created_by)"
            " VALUES(?,?,?,?,?)",
            (gid, kg_utils.AUDIT_FAILED, "", err, g.user["id"]))
        db.commit()
        flash(f"图谱体检未执行：{err}")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    max_tokens = _get_max_tokens(g.user["id"], "max_tokens_kg_audit")
    created_by = g.user["id"]

    def runner(database, operation):
        try:
            suggestions, rejected, run_err = kg_utils.analyze_graph(
                database, gid, api_url, api_key, model,
                usage_hook=operation["usage_hook"], max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            run_err = str(e)
            kg_log.exception("知识图谱体检失败 graph=%s", gid)
            suggestions, rejected = [], []
        if run_err:
            status = kg_utils.AUDIT_FAILED
        elif suggestions:
            status = kg_utils.AUDIT_PENDING
        elif rejected:
            status = kg_utils.AUDIT_CLEAN
        else:
            status = kg_utils.AUDIT_CLEAN
        database.execute(
            "INSERT INTO kg_audits(graph_id,status,model,error,suggestions,"
            "rejected,operation_id,created_by) VALUES(?,?,?,?,?,?,?,?)",
            (gid, status, model, (run_err or "")[:500],
             json.dumps(suggestions, ensure_ascii=False),
             json.dumps(rejected, ensure_ascii=False), operation["id"],
             created_by))
        database.commit()
        kg_log.info("知识图谱体检 user=#%s graph=%s status=%s 建议=%s 拦截=%s",
                    created_by, gid, status, len(suggestions), len(rejected))
        _finish_ai_operation(operation, "failed" if run_err else "success", run_err)

    operation, queue_err = _ai_task_queue.submit(
        g.user["id"], "kg_audit", model=model, runner=runner,
        material_id=None, dedup_key=f"kg_audit|{gid}")
    if queue_err:
        flash(queue_err)
    else:
        flash("图谱体检已加入后台队列，可离开当前页面。")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


def _kg_audit(gid, aid):
    row = get_db().execute("SELECT * FROM kg_audits WHERE id=? AND graph_id=?",
                           (aid, gid)).fetchone()
    if not row:
        abort(404)
    return row


@_routes.route("/knowledge-graphs/<oid:gid>/audit/<oid:aid>/apply", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_audit_apply(gid, aid):
    _kg_graph(gid, edit=True)
    db = get_db()
    audit = _kg_audit(gid, aid)
    if audit["status"] != kg_utils.AUDIT_PENDING:
        flash("该体检结果已处理，不能重复应用")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    try:
        items = json.loads(audit["suggestions"] or "[]")
    except (TypeError, ValueError):
        items = []
    if not items:
        flash("没有可应用的建议")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    snapshot = kg_utils.apply_audit(db, gid, items, commit=False)
    db.execute("UPDATE kg_audits SET status=?, snapshot=?, applied_at=CURRENT_TIMESTAMP"
               " WHERE id=?",
               (kg_utils.AUDIT_APPLIED,
                json.dumps(snapshot, ensure_ascii=False), aid))
    db.commit()
    kg_log.info("应用知识图谱体检 user=%s#%s graph=%s audit=%s 建议=%s",
             g.user["username"], g.user["id"], gid, aid, len(items))
    flash(f"已应用 {len(items)} 条体检建议；后续没有建图或编辑时可回滚。")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/audit/<oid:aid>/rollback", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_audit_rollback(gid, aid):
    _kg_graph(gid, edit=True)
    db = get_db()
    audit = _kg_audit(gid, aid)
    if audit["status"] != kg_utils.AUDIT_APPLIED or not audit["snapshot"]:
        flash("这条体检结果没有可回滚的快照")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    later = db.execute(
        "SELECT id FROM kg_audits WHERE graph_id=? AND id>? AND status=? LIMIT 1",
        (gid, aid, kg_utils.AUDIT_APPLIED)).fetchone()
    if later:
        flash("之后已有新的体检结果被应用，请先回滚最新的那条")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    try:
        snapshot = json.loads(audit["snapshot"])
    except (TypeError, ValueError):
        snapshot = {}
    try:
        kg_utils.restore_snapshot(db, gid, snapshot, commit=False)
    except ValueError as exc:
        db.rollback()
        flash(str(exc))
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    db.execute("UPDATE kg_audits SET status=? WHERE id=?",
               (kg_utils.AUDIT_ROLLED_BACK, aid))
    db.commit()
    kg_log.info("回滚知识图谱体检 user=%s#%s graph=%s audit=%s",
             g.user["username"], g.user["id"], gid, aid)
    flash("已回滚到体检应用前的图谱状态")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/audit/<oid:aid>/discard", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_audit_discard(gid, aid):
    _kg_graph(gid, edit=True)
    db = get_db()
    audit = _kg_audit(gid, aid)
    if audit["status"] != kg_utils.AUDIT_PENDING:
        flash("该体检结果已处理")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    db.execute("UPDATE kg_audits SET status=? WHERE id=?",
               (kg_utils.AUDIT_DISCARDED, aid))
    db.commit()
    kg_log.info("忽略知识图谱体检 user=%s#%s graph=%s audit=%s",
                g.user["username"], g.user["id"], gid, aid)
    flash("已忽略这次体检建议")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/categories/analyze", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_category_run(gid):
    """让模型把自由分类重映射为分层学科树：异步执行，先预览后应用。"""
    _kg_graph(gid, edit=True)
    db = get_db()
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        db.execute(
            "INSERT INTO kg_category_maps(graph_id,status,model,error,created_by)"
            " VALUES(?,?,?,?,?)",
            (gid, kg_utils.CATEGORY_FAILED, "", err, g.user["id"]))
        db.commit()
        flash(f"分类重构未执行：{err}")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    max_tokens = _get_max_tokens(g.user["id"], "max_tokens_kg_category")
    created_by = g.user["id"]

    def runner(database, operation):
        try:
            mappings, rejected, run_err = kg_utils.analyze_categories(
                database, gid, api_url, api_key, model,
                usage_hook=operation["usage_hook"], max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            run_err = str(e)
            kg_log.exception("知识图谱分类重构失败 graph=%s", gid)
            mappings, rejected = [], []
        if run_err:
            status = kg_utils.CATEGORY_FAILED
        elif mappings:
            status = kg_utils.CATEGORY_PENDING
        else:
            status = kg_utils.AUDIT_CLEAN
        database.execute(
            "INSERT INTO kg_category_maps(graph_id,status,model,error,mappings,"
            "rejected,operation_id,created_by) VALUES(?,?,?,?,?,?,?,?)",
            (gid, status, model, (run_err or "")[:500],
             json.dumps(mappings, ensure_ascii=False),
             json.dumps(rejected, ensure_ascii=False), operation["id"],
             created_by))
        database.commit()
        kg_log.info("知识图谱分类重构 user=#%s graph=%s status=%s 映射=%s 拦截=%s",
                    created_by, gid, status, len(mappings), len(rejected))
        _finish_ai_operation(operation, "failed" if run_err else "success", run_err)

    operation, queue_err = _ai_task_queue.submit(
        g.user["id"], "kg_category", model=model, runner=runner,
        material_id=None, dedup_key=f"kg_category|{gid}")
    if queue_err:
        flash(queue_err)
    else:
        flash("分类重构已加入后台队列，可离开当前页面。")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


def _kg_category_map(gid, mid):
    row = get_db().execute(
        "SELECT * FROM kg_category_maps WHERE id=? AND graph_id=?", (mid, gid)).fetchone()
    if not row:
        abort(404)
    return row


@_routes.route("/knowledge-graphs/<oid:gid>/categories/<oid:mid>/apply", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_category_apply(gid, mid):
    _kg_graph(gid, edit=True)
    db = get_db()
    record = _kg_category_map(gid, mid)
    if record["status"] != kg_utils.CATEGORY_PENDING:
        flash("该分类重构结果已处理，不能重复应用")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    try:
        items = json.loads(record["mappings"] or "[]")
    except (TypeError, ValueError):
        items = []
    if not items:
        flash("没有可应用的分类映射")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    picked = []
    for raw in request.form.getlist("selected"):
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(items) and index not in picked:
            picked.append(index)
    if not picked:
        flash("请先勾选要应用的映射")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    chosen = [items[index] for index in picked]
    skipped = [{"text": items[index].get("label") or items[index].get("from") or "",
                "reason": "本次未勾选，未应用"}
               for index in range(len(items)) if index not in picked]
    try:
        rejected = json.loads(record["rejected"] or "[]")
    except (TypeError, ValueError):
        rejected = []
    snapshot = kg_utils.apply_category_map(db, gid, chosen, commit=False)
    db.execute(
        "UPDATE kg_category_maps SET status=?, snapshot=?, mappings=?, rejected=?, "
        "applied_at=CURRENT_TIMESTAMP WHERE id=?",
        (kg_utils.CATEGORY_APPLIED, json.dumps(snapshot, ensure_ascii=False),
         json.dumps(chosen, ensure_ascii=False),
         json.dumps(rejected + skipped, ensure_ascii=False), mid))
    db.commit()
    kg_log.info("应用知识图谱分类重构 user=%s#%s graph=%s map=%s 选中=%s 跳过=%s",
                g.user["username"], g.user["id"], gid, mid, len(chosen), len(skipped))
    flash(f"已应用选中的 {len(chosen)} 条分类映射"
          + (f"，{len(skipped)} 条未勾选保留原分类" if skipped else "")
          + "，可随时回滚")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/categories/<oid:mid>/rollback",
               methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_category_rollback(gid, mid):
    _kg_graph(gid, edit=True)
    db = get_db()
    record = _kg_category_map(gid, mid)
    if record["status"] != kg_utils.CATEGORY_APPLIED or not record["snapshot"]:
        flash("这条分类重构没有可回滚的快照")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    later = db.execute(
        "SELECT id FROM kg_category_maps WHERE graph_id=? AND id>? AND status=? LIMIT 1",
        (gid, mid, kg_utils.CATEGORY_APPLIED)).fetchone()
    if later:
        flash("之后已有新的分类重构被应用，请先回滚最新的那条")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    try:
        snapshot = json.loads(record["snapshot"])
    except (TypeError, ValueError):
        snapshot = {}
    try:
        kg_utils.restore_snapshot(db, gid, snapshot, commit=False)
    except ValueError as exc:
        db.rollback()
        flash(str(exc))
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    db.execute("UPDATE kg_category_maps SET status=? WHERE id=?",
               (kg_utils.CATEGORY_ROLLED_BACK, mid))
    db.commit()
    kg_log.info("回滚知识图谱分类重构 user=%s#%s graph=%s map=%s",
                g.user["username"], g.user["id"], gid, mid)
    flash("已回滚到分类重构前的图谱状态")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/categories/<oid:mid>/discard",
               methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_category_discard(gid, mid):
    _kg_graph(gid, edit=True)
    db = get_db()
    record = _kg_category_map(gid, mid)
    if record["status"] != kg_utils.CATEGORY_PENDING:
        flash("该分类重构结果已处理")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    db.execute("UPDATE kg_category_maps SET status=? WHERE id=?",
               (kg_utils.CATEGORY_DISCARDED, mid))
    db.commit()
    kg_log.info("忽略知识图谱分类重构 user=%s#%s graph=%s map=%s",
                g.user["username"], g.user["id"], gid, mid)
    flash("已忽略这次分类重构建议")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/delete", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_delete(gid):
    graph = _kg_graph(gid, manage=True)
    db = get_db()
    db.execute("DELETE FROM kg_audits WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_category_maps WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_members WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_chunks WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_edges WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_nodes WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_materials WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM course_knowledge_graphs WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM knowledge_graphs WHERE id=?", (gid,))
    db.commit()
    shutil.rmtree(os.path.join(_upload_dir(), "kg", str(gid)), ignore_errors=True)
    kg_log.info("删除知识图谱 user=%s#%s id=%s name=%s",
             g.user["username"], g.user["id"], gid, graph["name"])
    flash(f"已删除知识图谱「{graph['name']}」")
    return redirect(url_for("knowledge_graphs"))


@_routes.route("/knowledge-graphs/<oid:gid>/members", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_member_save(gid):
    """新增或调整协作老师；同一老师在同一图谱只保留一条角色记录。"""
    graph = _kg_graph(gid, manage=True)
    db = get_db()
    try:
        user_id = int(request.form.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    role = request.form.get("role") or "use"
    if role not in KG_ROLE_LABEL:
        role = "use"
    user = db.execute(
        "SELECT * FROM users WHERE id=? AND role='teacher' AND active=1",
        (user_id,)).fetchone()
    if not user:
        flash("请选择启用状态的老师账号")
    elif user_id == graph["created_by"]:
        flash("创建者已拥有管理权限")
    else:
        db.execute(
            "INSERT INTO kg_members(graph_id,user_id,role) VALUES(?,?,?)"
            " ON CONFLICT(graph_id,user_id) DO UPDATE SET role=excluded.role",
            (gid, user_id, role))
        db.commit()
        kg_log.info("设置知识图谱协作 user=%s#%s graph=%s member=%s role=%s",
                 g.user["username"], g.user["id"], gid, user_id, role)
        flash(f"已把「{user['display_name']}」设为{KG_ROLE_LABEL[role]}")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/members/<oid:uid>/remove", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_member_remove(gid, uid):
    _kg_graph(gid, manage=True)
    db = get_db()
    db.execute("DELETE FROM kg_members WHERE graph_id=? AND user_id=?",
               (gid, uid))
    db.commit()
    kg_log.info("移除知识图谱协作 user=%s#%s graph=%s member=%s",
             g.user["username"], g.user["id"], gid, uid)
    flash("已移除该老师的协作权限")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/assistant/chat", methods=["POST"])
@role_required("admin", "teacher", "student")
def assistant_chat():
    """右下角数字助手：带工具调用的多轮对话，按角色只读查询本人可见的数据。"""
    data = request.get_json(silent=True) or {}
    msgs = []
    for m in (data.get("messages") or [])[-12:]:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content[:4000]})
    if not msgs or msgs[-1]["role"] != "user":
        return jsonify({"error": "请先输入问题"}), 400
    # 学生与管理员没有自己的 AI 配置，统一用平台已配置的接口（取第一个可用的老师配置或环境变量）
    role = g.user["role"]
    cfg = llm.resolve_config(g.user["id"] if role == "teacher" else None)
    # 助手是交互式的，单次请求很小，不受「仅闲时批阅」限制
    reply, trace, err = assistant.reply(msgs, cfg, g.user["id"], role=role)
    if err:
        return jsonify({"error": err, "trace": trace}), 502
    return jsonify({"reply": reply, "trace": trace})


def _probe_ai_config(api_url, api_key, model):
    """用一个最小请求验证某套配置，返回可直接 flash 的结果文本。"""
    api_url = (api_url or "").rstrip("/")
    if not api_url or not api_key or not model:
        return "请先填写接口地址、API Key 和模型"
    body = json.dumps({"model": model, "max_tokens": 128,
                       "messages": [{"role": "user", "content": "只回复两个字：正常"}]}).encode()
    req = urllib.request.Request(
        f"{api_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read())
        msg = data["choices"][0]["message"]
        reply = (msg.get("content") or "").strip()
        if reply:
            return f"连接成功，模型 {model} 回复：{reply[:40]}"
        return f"连接成功，模型 {model} 已响应（该模型为思考模型，本次未输出正文，属正常现象）"
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        hint = "（免费模型有并发/频率限制，等几秒再试）" if e.code == 429 else ""
        return f"连接失败：HTTP {e.code}{hint} {detail}"
    except Exception as e:
        return f"连接失败：{e}"


# ---------- 学生端 ----------
def student_lecture(hid):
    db = get_db()
    hw = db.execute("""
        SELECT h.*, c.name AS course_name, c.teacher_id AS teacher_id FROM lectures h
        JOIN courses c ON c.id=h.course_id WHERE h.id=?""", (hid,)).fetchone()
    if not hw:
        abort(404)
    ok = db.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?",
                    (hw["course_id"], g.user["id"])).fetchone()
    if not ok:
        abort(403)
    return hw


STUDENT_DEADLINE_WINDOW_DAYS = 7


def _student_deadline_tasks(lectures, now=None):
    """Build a cross-course due list using real instants and the user's display timezone."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    horizon = now + timedelta(days=STUDENT_DEADLINE_WINDOW_DAYS)
    upcoming, overdue = [], []
    for lecture in lectures:
        raw_deadline = lecture["deadline"]
        parsed = _parse_display_time(raw_deadline, "legacy")
        if parsed is None:
            continue
        due = parsed.astimezone(timezone.utc)
        submitted = bool(lecture["my_subs"])
        if due < now:
            # 已经提交的迟交作业仍按最新提交状态展示在课程表中，不列作待交逾期任务。
            if submitted:
                continue
            group = overdue
            state = "overdue"
        elif due <= horizon:
            group = upcoming
            state = "upcoming"
        else:
            continue

        row = dict(lecture)
        row["deadline_display"] = _format_display_time(raw_deadline, "legacy")
        row["deadline_iso"] = due.isoformat()
        row["deadline_state"] = state
        row["task_status"] = (lecture["my_status"] or "已提交") if submitted else "未提交"
        row["task_action"] = ("去提交" if not submitted else
                              "查看批改" if lecture["my_status"] == "已批改" else "查看进度")
        row["_deadline_sort"] = due.timestamp()
        group.append(row)

    # 逾期区先显示最近到期的未交任务；近期区按下一截止时间升序。
    overdue.sort(key=lambda item: item["_deadline_sort"], reverse=True)
    upcoming.sort(key=lambda item: item["_deadline_sort"])
    for item in upcoming + overdue:
        item.pop("_deadline_sort", None)
    return upcoming, overdue


@_routes.route("/student")
@role_required("student")
def student_home():
    db = get_db()
    courses = db.execute("""
        SELECT c.id, c.name, c.created_at
        FROM courses c
        JOIN enrollments e ON e.course_id=c.id AND e.student_id=?
        ORDER BY c.id DESC""", (g.user["id"],)).fetchall()
    lectures = db.execute("""
        SELECT h.*, c.name AS course_name,
          (SELECT COUNT(*) FROM submissions s
            WHERE s.lecture_id=h.id AND s.student_id=?) AS my_subs,
          (SELECT s.id FROM submissions s
            WHERE s.lecture_id=h.id AND s.student_id=?
            ORDER BY s.id DESC LIMIT 1) AS last_sub_id,
          (SELECT s.status FROM submissions s
            WHERE s.lecture_id=h.id AND s.student_id=?
            ORDER BY s.id DESC LIMIT 1) AS my_status
        FROM lectures h
        JOIN courses c ON c.id=h.course_id
        JOIN enrollments e ON e.course_id=c.id AND e.student_id=?
        ORDER BY h.title COLLATE NOCASE, h.id""",
        (g.user["id"],) * 4).fetchall()
    acc_by = _accuracy_by_submission(db, [h["last_sub_id"] for h in lectures])
    grading_active_ids = {
        r["submission_id"] for r in db.execute(
            "SELECT o.submission_id FROM ai_operations o "
            "JOIN submissions s ON s.id=o.submission_id "
            "WHERE s.student_id=? AND o.operation_type='grade' "
            "AND o.status IN ('queued','running') "
            "AND o.id=(SELECT MAX(x.id) FROM ai_operations x "
            " WHERE x.submission_id=o.submission_id AND x.operation_type='grade')",
            (g.user["id"],))
    }
    by_course = {}
    for h in lectures:
        by_course.setdefault(h["course_id"], []).append(h)
    # 每个课程一个折叠块：作业未提交数 = 还没有任何一次提交的讲次数
    course_groups = [{
        "course": c,
        "lectures": by_course.get(c["id"], []),
        "pending": sum(1 for h in by_course.get(c["id"], []) if not h["my_subs"]),
    } for c in courses]
    due_soon_tasks, overdue_tasks = _student_deadline_tasks(lectures)
    exams = db.execute("""
        SELECT x.*, c.name AS course_name, s.score, s.comment,
               COALESCE(s.status,'pending') AS score_status
        FROM exams x
        JOIN courses c ON c.id=x.course_id
        JOIN enrollments e ON e.course_id=c.id AND e.student_id=?
        LEFT JOIN exam_scores s ON s.exam_id=x.id AND s.student_id=?
        ORDER BY x.id DESC""", (g.user["id"], g.user["id"])).fetchall()
    return render_template("student_home.html", course_groups=course_groups,
                           exams=exams, acc_by=acc_by,
                           grading_active_ids=grading_active_ids,
                           due_soon_tasks=due_soon_tasks, overdue_tasks=overdue_tasks,
                           deadline_window_days=STUDENT_DEADLINE_WINDOW_DAYS)


@_routes.route("/student/lecture/<oid:hid>", methods=["GET", "POST"])
@role_required("student")
def student_lecture_page(hid):
    hw = student_lecture(hid)
    db = get_db()
    if request.method == "POST":
        files = [f for f in (request.files.getlist("files") or request.files.getlist("file"))
                 if f and f.filename]
        if not files:
            flash("请选择要提交的作业图片或 PDF")
        elif _submission_grade_active(db, _latest_submission_id(db, hid, g.user["id"])):
            flash("这份作业正在批改中，已暂时锁定；请稍后回来再提交新的版本。")
        elif len(files) > 20:
            flash("一次最多提交 20 个文件，请合并或分次提交")
        else:
            # 先统一校验，避免部分文件已落盘后才发现格式不对
            try:
                prepared = [(f, _validate_upload(f)) for f in files]
            except ValueError as e:
                flash(str(e))
                return redirect(url_for("student_lecture_page", hid=hid))
            names = [save_upload(f, hw["course_id"], hid, g.user["id"], filename=fname)
                     for f, fname in prepared]
            stored = names[0] if len(names) == 1 else json.dumps(names, ensure_ascii=False)
            cur = db.execute("INSERT INTO submissions(lecture_id,student_id,file_path) "
                             "VALUES(?,?,?)", (hid, g.user["id"], stored))
            db.commit()
            sid = cur.lastrowid
            if _auto_enqueue_submission(sid):
                flash(f"提交成功（{len(names)} 个文件），已自动进入批改队列；"
                      "批改期间作业暂时锁定，完成后可回来修改提交。")
            else:
                flash(f"提交成功（{len(names)} 个文件），等待老师批改")
        return redirect(url_for("student_lecture_page", hid=hid))
    subs = db.execute("SELECT * FROM submissions WHERE lecture_id=? AND student_id=? "
                      "ORDER BY id DESC", (hid, g.user["id"])).fetchall()
    # 老师复核完成（status=已批改）或已发布批改报告后，向学生公开逐题明细。
    # 明细按最新一次提交取，且不再依赖 score（默认定性批改时 score 为空）。
    latest = subs[0] if subs else None
    revealed = bool(latest) and (latest["status"] == "已批改" or latest["report_published"])
    items = []
    if revealed:
        items = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (latest["id"],)).fetchall()
    locked = bool(latest) and _submission_grade_active(db, latest["id"])
    return render_template("student_lecture.html", hw=hw, subs=subs, items=items,
                           revealed=revealed, locked=locked,
                           auto_grade_limit=_grading_queue.attempt_limit(hw["teacher_id"]))


def _latest_submission_id(db, lecture_id, student_id):
    row = db.execute("SELECT id FROM submissions WHERE lecture_id=? AND student_id=? "
                     "ORDER BY id DESC LIMIT 1", (lecture_id, student_id)).fetchone()
    return row["id"] if row else None


# ---------- 工具 ----------

def demo_questions(count=6):
    """演示模式：生成占位批阅结果，用于没有 AI 接口时体验流程。"""
    return [{"no": str(i), "ocr": f"（演示模式）第 {i} 题 OCR 占位文本，请手动修改成学生实际作答。",
             "standard_answer": f"（演示模式）请在此填写第 {i} 题的标准答案。",
             "verdict": "半对",
             "comment": "（演示模式）请手动核对并修改。"}
            for i in range(1, count + 1)]


def _build_summary(questions, weak_points, evaluation, errors):
    """判定分布与正确率一律由后端按题汇总，不依赖模型跨批统计。"""
    right = [q["no"] for q in questions if q.get("verdict") == "对"]
    partial = [q["no"] for q in questions if q.get("verdict") == "半对"]
    wrong = [q["no"] for q in questions if q.get("verdict") == "错"]
    unknown = [q["no"] for q in questions if q.get("verdict") == "无法判定"]
    return {
        "accuracy": accuracy_text(len(right), len(partial), len(wrong)),
        "right": right,
        "partial": partial,
        "wrong": wrong,
        "unknown": unknown,
        "weak_points": weak_points,
        "evaluation": evaluation,
        "errors": errors,
    }


def run_grading(teacher_id, file_name, answer_pdf="", scope="", answer_pages=None,
                prompt="", usage_hook=None):
    """批阅入口：学生作业与标准答案整份交给支持 PDF 的模型，一次返回逐题结果。

    prompt 为本课程选定的批阅提示词正文；为空时用系统内置默认提示词。
    """
    if get_setting(teacher_id, "ai_demo") == "1":
        questions = demo_questions()
        return {"questions": questions,
                "summary": _build_summary(questions, [], "演示模式：未调用 AI。", []),
                "errors": [], "error": "", "demo": True}

    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    model = get_setting(teacher_id, "ai_model")
    if not api_url or not api_key or not model:
        return {"questions": [], "summary": {}, "errors": [],
                "error": "尚未配置 AI 接口。可以先勾选「演示模式」体验完整流程，或到「AI 设置」填写接口地址、密钥与模型。"}

    extra = scope or ""
    student_paths = [os.path.join(_upload_dir(), p) for p in
                     (file_name if isinstance(file_name, (list, tuple)) else [file_name])]
    gen = grade_pdf_submission(
        student_paths,
        os.path.join(_upload_dir(), answer_pdf) if answer_pdf else "",
        api_url, api_key, model,
        extra=extra, answer_pages=answer_pages, prompt=prompt, usage_hook=usage_hook,
        max_tokens=_get_max_tokens(teacher_id, "max_tokens_grading"))
    gen["summary"] = _build_summary(gen["questions"], gen.get("weak_points") or [],
                                    gen.get("evaluation", ""), gen.get("errors") or [])
    return gen


def _scope_instruction(hw):
    """把老师勾选的作业范围翻译成给模型的约束；未设置范围时返回空串（调用方已拦截）。"""
    try:
        keys = json.loads(hw["scope_selected"] or "[]")
        cands = json.loads(hw["scope_candidates"] or "[]")
    except (TypeError, ValueError):
        return ""
    by_key = {str(c.get("key")): c for c in cands if isinstance(c, dict)}
    picked = [by_key[str(k)] for k in keys if str(k) in by_key]
    if not picked:
        return ""
    counts = {}
    for c in picked:
        counts[c.get("no")] = counts.get(c.get("no"), 0) + 1
    labels = []
    for c in picked:
        no = str(c.get("no", "")).strip()
        # 同一题号在答案里出现多次时，加简要说明以便模型区分
        labels.append(f"{no}（{c.get('hint')}）" if counts[no] > 1 and c.get("hint") else no)
    return ("本讲次作业范围只包含以下题目：「" + "、".join(labels) + "」。"
            "请只批阅这些题目（题目含小问时，该题的全部小问都要批阅），"
            "范围之外的题目一律不要出现在输出里；"
            "范围内某题学生没作答时，仍按未作答处理。")


# ---------- 批改报告（Markdown） ----------

def generate_report(db, sub_id, teacher_id, reuse=False, operation=None):
    """按当前逐题结果生成/刷新一份 Markdown 批改报告，返回报告文本。

    reuse=True 时沿用上次由 AI 生成的评价段落，只重算表格与统计（保存批改时不额外调模型）。
    operation 由调用方的任务队列传入时，复用其 usage_hook 且不再自行创建 ai_operation。
    """
    sub = db.execute("""
        SELECT s.*, u.display_name, l.title AS lecture_title, l.course_id
        FROM submissions s JOIN users u ON u.id=s.student_id
        JOIN lectures l ON l.id=s.lecture_id WHERE s.id=?""", (sub_id,)).fetchone()
    if not sub:
        return ""
    questions = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (sub_id,)).fetchall()
    completion_error = None
    summary = _json_or_none(sub["ai_summary"]) or {}
    detail = _json_or_none(sub["report_detail"]) or {} if reuse else {}
    if not detail and questions and get_setting(teacher_id, "ai_demo") != "1":
        # 自动生成报告评语也遵守闲时设置（手动点击"生成新的总评"也会经过这里）
        block = _offpeak_block_if_enabled(teacher_id)
        if not block:
            api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
            api_key = get_setting(teacher_id, "ai_api_key")
            model = get_setting(teacher_id, "ai_model")
            course = db.execute("SELECT c.report_prompt_id FROM courses c "
                                "JOIN lectures l ON l.course_id=c.id WHERE l.id=?",
                                (sub["lecture_id"],)).fetchone()
            if api_url and api_key and model:
                if operation is None:
                    operation = _start_ai_operation(
                        teacher_id, "report_generate", model, course_id=sub["course_id"],
                        lecture_id=sub["lecture_id"], submission_id=sub_id)
                try:
                    kg_diag = _kg_diagnosis(db, sub)
                    kg_context = _kg_diagnosis_text(kg_diag) if kg_diag else ""
                    detail = ai_report_comment(
                        questions, summary.get("weak_points") or [],
                        summary.get("evaluation", ""), api_url, api_key, model,
                        prompt=_prompt_content("report", course["report_prompt_id"] if course else 0),
                        usage_hook=operation["usage_hook"],
                        max_tokens=_get_max_tokens(teacher_id, "max_tokens_report"),
                        kg_context=kg_context)
                except Exception as e:
                    completion_error = str(e)
                    log.exception("生成报告评语异常 submission=%s by=%s", sub_id, teacher_id)
                    # 不中断报告生成：AI 评语失败时，报告仍包含表格与统计
                    detail = {}
                completion_error = completion_error or ("" if detail else "AI 未返回可用的报告评价")
    try:
        stamp = datetime.strptime(str(sub["created_at"])[:10], "%Y-%m-%d")
        date_label = f"{stamp.month}月{stamp.day}日"
    except (TypeError, ValueError):
        date_label = ""
    md = build_report_md(sub["display_name"], sub["lecture_title"], date_label,
                         questions, summary, detail,
                         kg_diagnosis=_kg_diagnosis(db, sub))
    if completion_error is not None:
        # AI 评价请求失败时保留旧报告；没有旧报告时保持空值，供界面显示“生成”。
        _finish_ai_operation(operation, "failed" if completion_error else "success", completion_error)
    else:
        db.execute("UPDATE submissions SET report_md=?, report_detail=? WHERE id=?",
                   (md, json.dumps(detail, ensure_ascii=False) if detail else "", sub_id))
        db.commit()
    return md


_TAG_NOISE_RE = re.compile(r"[\s\u3000（）()【】\[\]{}·、,，.。:：;；\"'“”‘’\-—_/]+")


def _tag_key(text):
    """标签/节点名归一化：忽略空白、括号、常见标点与全半角差异。"""
    return _TAG_NOISE_RE.sub("", str(text or "")).lower()


def _kg_match_node(nodes, name_to_node, tag):
    """知识点标签 -> 图谱节点。

    顺序：原名精确 -> 归一化精确 -> 包含关系（取覆盖最长且明显更优的候选）
    -> 字符相似度兜底。名称来自不同 AI 流程，常出现空格、括号、俗称差异。
    返回 (node, match_type)；match_type 用于日志与教师端排查。
    """
    node = name_to_node.get(tag)
    if node:
        return node, "exact"
    key = _tag_key(tag)
    if not key or len(key) < 2:
        return None, "no_match"
    candidates = []
    for n in nodes:
        nkey = _tag_key(n["name"])
        if not nkey or len(nkey) < 2:
            continue
        if nkey == key:
            return n, "normalized"
        if nkey in key:
            candidates.append((n, "tag_contains_node", len(nkey) / len(key)))
        elif key in nkey:
            candidates.append((n, "node_contains_tag", len(key) / len(nkey)))
    if candidates:
        candidates.sort(key=lambda c: c[2], reverse=True)
        best, kind, score = candidates[0]
        # 多个候选接近时宁可不匹配，也不要把学生挂到错误的节点上
        if len(candidates) == 1 or score - candidates[1][2] >= 0.12:
            return best, kind
    best_node, best_ratio = None, 0.0
    for n in nodes:
        nkey = _tag_key(n["name"])
        if not nkey:
            continue
        ratio = difflib.SequenceMatcher(None, key, nkey).ratio()
        if ratio > best_ratio:
            best_node, best_ratio = n, ratio
    if best_node is not None and best_ratio >= 0.86:
        return best_node, "similar"
    return None, "no_match"


def _course_tag_links(db, course_id):
    """老师手动绑定的 标签 -> 节点，优先于自动匹配。"""
    return {r["tag"]: {"graph_id": r["graph_id"], "node_id": r["node_id"]}
            for r in db.execute(
                "SELECT graph_id,node_id,tag FROM tag_node_links WHERE course_id=?",
                (course_id,))}


def _resolve_tag_node(db, course_id, nodes, name_to_node, tag, links=None,
                      node_by_id=None):
    """优先使用老师绑定，其次自动匹配；返回 (node, match_type)。"""
    if links is None:
        links = _course_tag_links(db, course_id)
    link = links.get(tag)
    if link:
        node = (node_by_id or {}).get(link["node_id"])
        if node is not None:
            return node, "manual"
    return _kg_match_node(nodes, name_to_node, tag)


WEAK_AUTO_REVIEW_STREAK = 2


def _weak_latest_attempts(db, student_id, course_id=None):
    """每个 (讲次, 题号) 取学生最近一次提交的判定，并带上该讲该题的全部标签。

    同一题重复提交只算一次，避免重批把累计计数放大。
    """
    sql = ("SELECT s.lecture_id, l.course_id, qt.tag, q.no, q.verdict "
           "FROM submissions s JOIN lectures l ON l.id=s.lecture_id "
           "JOIN question_tags qt ON qt.lecture_id=l.id "
           "JOIN questions q ON q.submission_id=s.id AND q.no=qt.question_no "
           "WHERE s.student_id=? AND q.verdict<>''")
    params = [student_id]
    if course_id is not None:
        sql += " AND l.course_id=?"
        params.append(course_id)
    latest = {}
    for h in db.execute(sql + " ORDER BY s.id", params):
        item = latest.setdefault(
            (h["lecture_id"], h["no"]),
            {"course_id": h["course_id"], "lecture_id": h["lecture_id"],
             "verdict": h["verdict"], "tags": set()})
        item["verdict"] = h["verdict"]
        item["tags"].add(h["tag"])
    return list(latest.values())


def _weak_tag_totals(attempts):
    """按标签累计最近判定，返回 {tag: [对, 半对, 错, 总数]}。"""
    totals = {}
    for attempt in attempts:
        for tag in attempt["tags"]:
            row = totals.setdefault(tag, [0, 0, 0, 0])
            row[3] += 1
            if attempt["verdict"] == "对":
                row[0] += 1
            elif attempt["verdict"] == "半对":
                row[1] += 1
            elif attempt["verdict"] == "错":
                row[2] += 1
    return totals


def _update_student_weak_points(db, submission):
    """人工复核保存后，基于逐题判定与知识点标签更新学生薄弱点记录。

    - 只有真正出现错/半对的知识点才新建记录；全对不产生薄弱点
    - 计数写入与展示同一口径：跨讲次累计、每题最近一次判定
    - 本次全对时累计 right_streak；连续 WEAK_AUTO_REVIEW_STREAK 讲全对
      自动从 weak 收敛为 reviewed（mastered 仍需学生自己确认）
    - 同一讲重复保存保留学生设定的状态；后续讲次再错才回退为 weak
    """
    if not submission:
        return
    student_id = submission["student_id"]
    lecture_id = submission["lecture_id"]
    sub_id = submission["id"]
    hw = db.execute("SELECT course_id FROM lectures WHERE id=?", (lecture_id,)).fetchone()
    if not hw:
        return
    course_id = hw["course_id"]

    questions = db.execute(
        "SELECT no, verdict FROM questions WHERE submission_id=? ORDER BY id",
        (sub_id,)).fetchall()
    if not questions:
        return
    no_to_verdict = {}
    for q in questions:
        v = (q["verdict"] or "").strip()
        if v:
            no_to_verdict[q["no"]] = v

    tags_rows = db.execute(
        "SELECT question_no, tag FROM question_tags WHERE lecture_id=? ORDER BY id",
        (lecture_id,)).fetchall()
    if not tags_rows:
        return
    tag_to_qnos = {}
    for r in tags_rows:
        tag_to_qnos.setdefault(r["tag"], []).append(r["question_no"])

    gids = [r["graph_id"] for r in db.execute(
        "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=?",
        (course_id,))]
    nodes = []
    name_to_node = {}
    if gids:
        ph = ",".join("?" * len(gids))
        nodes = db.execute(
            f"SELECT * FROM kg_nodes WHERE graph_id IN ({ph})", gids).fetchall()
        name_to_node = {n["name"]: n for n in nodes}
    node_by_id = {n["id"]: n for n in nodes}
    links = _course_tag_links(db, course_id)

    now = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    totals = _weak_tag_totals(_weak_latest_attempts(db, student_id, course_id))
    for tag, qnos in tag_to_qnos.items():
        wrong = partial = right = total = 0
        for qno in qnos:
            v = no_to_verdict.get(qno, "")
            if not v:
                continue
            total += 1
            if v == "错":
                wrong += 1
            elif v == "半对":
                partial += 1
            elif v == "对":
                right += 1
        if total == 0:
            continue
        # 计数字段与展示口径一致：跨讲累计、每题最近一次判定
        right, partial_c, wrong_c, total_c = totals.get(tag, [right, partial, wrong, total])
        node = None
        if nodes:
            node, _ = _resolve_tag_node(db, course_id, nodes, name_to_node, tag,
                                        links, node_by_id)
        node_name = node["name"] if node else ""
        definition = (node["definition"] or "") if node else ""
        graph_id = node["graph_id"] if node else 0
        node_id = node["id"] if node else 0

        existing = db.execute(
            "SELECT * FROM student_weak_points WHERE student_id=? AND course_id=? AND tag=?",
            (student_id, course_id, tag)).fetchone()
        if existing:
            new_status = existing["status"]
            streak = existing["right_streak"] or 0
            if wrong or partial:
                # 同一讲反复重批保留学生设定的状态；后续讲次再次做错则回退为薄弱
                if new_status in ("reviewed", "mastered", "removed") \
                        and (existing["status_lecture_id"] or 0) != lecture_id:
                    new_status = "weak"
                streak = 0
            else:
                streak += 1
                if new_status == "weak" and streak >= WEAK_AUTO_REVIEW_STREAK:
                    new_status = "reviewed"
            anchor = existing["status_lecture_id"] or 0
            if new_status == "weak" or new_status != existing["status"]:
                anchor = lecture_id
            db.execute(
                "UPDATE student_weak_points SET graph_id=?, node_id=?, node_name=?, "
                "definition=?, source_submission_id=?, status=?, wrong_count=?, "
                "partial_count=?, right_count=?, total_count=?, right_streak=?, "
                "last_seen_at=?, status_lecture_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (graph_id, node_id, node_name, definition, sub_id, new_status,
                 wrong_c, partial_c, right, total_c, streak, now, anchor,
                 existing["id"]))
        else:
            # 全对不产生薄弱点：只有真实出现过错/半对才建档
            if not (wrong_c or partial_c):
                continue
            db.execute(
                "INSERT INTO student_weak_points(student_id,course_id,graph_id,node_id,"
                "tag,node_name,definition,source_submission_id,status,wrong_count,"
                "partial_count,right_count,total_count,right_streak,last_seen_at,"
                "status_lecture_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (student_id, course_id, graph_id, node_id, tag, node_name, definition,
                 sub_id, "weak", wrong_c, partial_c, right, total_c, 0, now, lecture_id))
    db.commit()


def _kg_weakness_chains(db, course_id, weak_tag_names):
    """基于课程挂载的 KG，为一组薄弱知识点标签生成「前置 -> 薄弱」知识链文本。

    供横向/纵向知识点分析的 prompt 注入，让 AI 能引用真实的图谱依赖关系做归因。
    返回多行文本；无 KG、无匹配或无前置关系时返回空字符串。
    """
    if not weak_tag_names:
        return ""
    graph_rows = db.execute(
        "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=?",
        (course_id,)).fetchall()
    if not graph_rows:
        return ""
    graph_ids = [g["graph_id"] for g in graph_rows]
    ph = ",".join("?" * len(graph_ids))
    nodes = db.execute(
        f"SELECT id,graph_id,name,definition FROM kg_nodes WHERE graph_id IN ({ph})",
        graph_ids).fetchall()
    if not nodes:
        return ""
    name_to_node = {n["name"]: n for n in nodes}
    node_by_id = {n["id"]: n for n in nodes}
    links = _course_tag_links(db, course_id)
    lines = []
    for tag in sorted(set(weak_tag_names)):
        node, _ = _resolve_tag_node(db, course_id, nodes, name_to_node, tag,
                                    links, node_by_id)
        if not node:
            continue
        neighbors = kg_utils.concept_neighbors(db, node["graph_id"], node["id"])
        prereqs = (neighbors or {}).get("prerequisites", [])
        if prereqs:
            pre_names = "、".join(p["name"] for p in prereqs[:5])
            lines.append(f"- {tag}（图谱节点：{node['name']}）：前置依赖 {pre_names}")
        else:
            lines.append(f"- {tag}（图谱节点：{node['name']}）：图谱中无前置依赖，属基础概念")
    return "\n".join(lines)


def _kg_diagnosis(db, sub):
    """基于课程挂载的知识图谱，为批阅报告生成知识点诊断。

    对每道错题/半对题关联的知识点：
    1. 通过 knowledge_tags 匹配 KG 节点（按名称）
    2. 查询该节点的前置知识（prerequisite 边）
    3. 交叉比对学生在前置知识所属题目上的判定，找出「知识断层」
    返回结构化列表，供报告页展示；无 KG 或无错题时返回 None。

    注意：submissions 表没有 course_id 列，需通过 lecture_id 关联 lectures 表获取。
    """
    lecture = db.execute("SELECT course_id FROM lectures WHERE id=?",
                         (sub["lecture_id"],)).fetchone()
    if not lecture:
        return None
    graph_rows = db.execute(
        "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=?",
        (lecture["course_id"],)).fetchall()
    if not graph_rows:
        return None
    graph_ids = [g["graph_id"] for g in graph_rows]

    questions = db.execute(
        "SELECT id,no,verdict FROM questions WHERE submission_id=? ORDER BY id",
        (sub["id"],)).fetchall()
    if not questions:
        return None

    # 本讲次所有 AI 知识标签，建立 qkey→tags 与 tag→qkeys 双向索引
    tag_rows = db.execute(
        "SELECT question_no AS qkey,tag FROM question_tags WHERE lecture_id=?",
        (sub["lecture_id"],)).fetchall()
    if not tag_rows:
        return None
    qkey_to_tags, tag_to_qkeys = {}, {}
    for t in tag_rows:
        qkey_to_tags.setdefault(t["qkey"], []).append(t["tag"])
        tag_to_qkeys.setdefault(t["tag"], set()).add(t["qkey"])

    # 学生在每个知识点上的判定分布
    tag_perf = {}
    for q in questions:
        for tag in qkey_to_tags.get(q["no"], []):
            p = tag_perf.setdefault(tag, {"right": 0, "partial": 0, "wrong": 0, "total": 0})
            p["total"] += 1
            if q["verdict"] == "对":
                p["right"] += 1
            elif q["verdict"] == "半对":
                p["partial"] += 1
            else:
                p["wrong"] += 1

    # 薄弱知识点（有错题或半对题）
    weak_tags = {t for t, p in tag_perf.items() if p["wrong"] + p["partial"] > 0}
    if not weak_tags:
        return None

    # 按名称匹配 KG 节点
    ph = ",".join("?" * len(graph_ids))
    nodes = db.execute(
        f"SELECT id,graph_id,name,definition FROM kg_nodes WHERE graph_id IN ({ph})",
        graph_ids).fetchall()
    name_to_node = {n["name"]: n for n in nodes}
    node_by_id = {n["id"]: n for n in nodes}
    links = _course_tag_links(db, lecture["course_id"])

    matched_tags, unmatched_tags = [], []
    diagnoses = []
    for tag in sorted(weak_tags):
        node, match_type = _resolve_tag_node(
            db, lecture["course_id"], nodes, name_to_node, tag, links, node_by_id)
        if not node:
            unmatched_tags.append(tag)
            continue
        matched_tags.append(f"{tag}→{node['name']}({match_type})")
        neighbors = kg_utils.concept_neighbors(db, node["graph_id"], node["id"])
        prereqs = (neighbors or {}).get("prerequisites", [])
        weak_pre, untested_pre = [], []
        for p in prereqs:
            # 前置知识名称是 KG 节点名，tag_perf 的 key 是知识点标签，需模糊匹配
            p_perf = tag_perf.get(p["name"])
            if not p_perf:
                for tag_name, perf in tag_perf.items():
                    if p["name"] in tag_name or tag_name in p["name"]:
                        p_perf = perf
                        break
            item = {"name": p["name"], "evidence": p.get("evidence", "")}
            if p_perf and (p_perf["wrong"] + p_perf["partial"]) > 0:
                item["perf"] = p_perf
                weak_pre.append(item)
            elif not p_perf:
                untested_pre.append(item)
        diagnoses.append({
            "tag": tag,
            "definition": node["definition"] or "",
            "perf": tag_perf[tag],
            "weak_prereqs": weak_pre,
            "untested_prereqs": untested_pre,
        })
    kg_log.info("KG 诊断匹配结果 submission=%s: 薄弱知识点=%d, 匹配成功=%d [%s], 未匹配=%d [%s]",
             sub["id"], len(weak_tags), len(matched_tags), "; ".join(matched_tags),
             len(unmatched_tags), ", ".join(unmatched_tags))
    return diagnoses or None


def _kg_diagnosis_text(diagnoses):
    """把 KG 诊断结果转成简洁文本，注入 AI 报告 prompt。"""
    if not diagnoses:
        return ""
    lines = []
    for d in diagnoses:
        p = d["perf"]
        line = f"- {d['tag']}（对{p['right']}/半对{p['partial']}/错{p['wrong']}）"
        if d["definition"]:
            line += f"：{d['definition']}"
        if d["weak_prereqs"]:
            pre_names = "、".join(
                f"{x['name']}（对{x['perf']['right']}/半对{x['perf']['partial']}/错{x['perf']['wrong']}）"
                for x in d["weak_prereqs"])
            line += f"\n  前置知识断层：{pre_names}"
        if d["untested_prereqs"]:
            line += f"\n  本次未考的前置知识：{'、'.join(x['name'] for x in d['untested_prereqs'])}"
        lines.append(line)
    return "\n".join(lines)


def own_submission(sid):
    """取一份提交并校验归属，返回 (sub, lecture, course)。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (sub["lecture_id"],)).fetchone()
    return sub, hw, own_course(hw["course_id"])


@_routes.route("/submission/<oid:sid>/report")
@role_required("teacher")
def submission_report(sid):
    sub, hw, course = own_submission(sid)
    db = get_db()
    md = sub["report_md"]
    stu = db.execute("SELECT display_name FROM users WHERE id=?", (sub["student_id"],)).fetchone()
    kg_diag = _kg_diagnosis(db, sub)
    return render_template("report.html", sub=sub, hw=hw, course=course, md=md,
                           body=render_markdown(md), student_name=stu["display_name"],
                           is_teacher=True, kg_diagnosis=kg_diag,
                           ai_operations=_ai_operation_history(submission_id=sid, limit=5))


@_routes.route("/submission/<oid:sid>/report/build", methods=["POST"])
@role_required("teacher")
def submission_report_build(sid):
    sub, hw, course = own_submission(sid)
    teacher_id = g.user["id"]
    db = get_db()
    rows = db.execute(
        "SELECT COUNT(*) AS total, SUM(reviewed=0) AS pending "
        "FROM questions WHERE submission_id=?", (sid,)).fetchone()
    active = latest_operation(db, sid)
    if not rows["total"] or rows["pending"]:
        flash("请先完成全部题目的逐题复核，再生成或重新生成报告。")
        return redirect(url_for("submission_review", sid=sid))
    if active and active["status"] in ("queued", "running"):
        flash("批阅任务尚未完成，请稍后再复核并生成报告。")
        return redirect(url_for("submission_review", sid=sid))
    model = get_setting(teacher_id, "ai_model") or "report"

    def runner(database, operation):
        try:
            generate_report(database, sid, teacher_id, operation=operation)
        except Exception as e:  # noqa: BLE001
            log.exception("生成批改报告失败 submission=%s", sid)
            try:
                _finish_ai_operation(operation, "failed", str(e))
            except Exception:
                pass
            return
        # generate_report 只在真正调用 AI 时自行 finish；若被闲时/演示拦截，
        # 队列已把状态置为 running，这里兜底标记 success。
        cur = database.execute(
            "SELECT status FROM ai_operations WHERE id=?", (operation["id"],)).fetchone()
        if cur and cur["status"] in ("queued", "running"):
            _finish_ai_operation(operation, "success")

    operation, queue_err = _ai_task_queue.submit(
        teacher_id, "report_generate", model=model, runner=runner,
        course_id=course["id"], lecture_id=hw["id"], submission_id=sid,
        dedup_key=f"report_generate|{sid}")
    if queue_err:
        flash(queue_err)
    else:
        flash("批改报告生成已加入后台队列，可离开当前页面。")
    return redirect(url_for("submission_report", sid=sid))


@_routes.route("/submission/<oid:sid>/report/publish", methods=["POST"])
@role_required("teacher")
def submission_report_publish(sid):
    sub, hw, _ = own_submission(sid)
    db = get_db()
    if not sub["report_published"]:
        rows = db.execute("SELECT COUNT(*),SUM(reviewed=0) FROM questions WHERE submission_id=?",
                          (sid,)).fetchone()
        if not rows[0] or rows[1]:
            flash("全部题目复核后才能发布报告。")
            return redirect(url_for("submission_review", sid=sid))
        if latest_operation(db, sid) and latest_operation(db, sid)["status"] in ("queued", "running"):
            flash("批阅任务尚未完成，请稍后复核新结果。")
            return redirect(url_for("submission_review", sid=sid))
    if not sub["report_md"]:
        flash("请先生成批改报告，再发布给学生。")
        return redirect(url_for("submission_report", sid=sid))
    publish = 0 if sub["report_published"] else 1
    db.execute("UPDATE submissions SET report_published=? WHERE id=?", (publish, sid))
    db.commit()
    flash("报告已发布，学生可在「我的学习」里查看。" if publish else "报告已撤回，学生不再可见。")
    return redirect(url_for("submission_report", sid=sid))


@_routes.route("/submission/<oid:sid>/report/comment", methods=["POST"])
@role_required("teacher")
def submission_report_comment(sid):
    """全部题目复核完成后，用 AI 重写一份总评（异步），结果写入 report_detail。"""
    db = get_db()
    sub, hw, course = own_submission(sid)
    questions = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (sid,)).fetchall()
    if not questions:
        return {"ok": False, "error": "还没有批阅结果，无法生成总评"}, 400
    pending = [q for q in questions if not q["reviewed"]]
    if pending:
        return {"ok": False, "error": f"还有 {len(pending)} 道题未复核，全部复核后才能生成总评"}, 400
    api_url = get_setting(g.user["id"], "ai_api_url").rstrip("/")
    api_key = get_setting(g.user["id"], "ai_api_key")
    model = get_setting(g.user["id"], "ai_model")
    if not (api_url and api_key and model):
        return {"ok": False, "error": "尚未配置 AI 接口，无法生成总评"}, 400
    if get_setting(g.user["id"], "ai_demo") == "1":
        return {"ok": False, "error": "演示模式不会调用 AI，无法生成总评"}, 400
    summary = _json_or_none(sub["ai_summary"]) or {}
    teacher_id = g.user["id"]
    course_id = course["id"]
    lecture_id = hw["id"]
    max_tokens = _get_max_tokens(teacher_id, "max_tokens_report")
    prompt = _prompt_content("report", course["report_prompt_id"])
    # 在线程序列化 questions（sqlite3.Row 不可跨线程）
    questions_plain = [dict(q) for q in questions]
    kg_diag = _kg_diagnosis(db, sub)
    kg_context = _kg_diagnosis_text(kg_diag) if kg_diag else ""

    def runner(database, operation):
        try:
            detail = ai_report_comment(
                questions_plain, summary.get("weak_points") or [],
                summary.get("evaluation", ""), api_url, api_key, model,
                prompt=prompt, usage_hook=operation["usage_hook"],
                max_tokens=max_tokens, kg_context=kg_context)
        except Exception as e:  # noqa: BLE001
            _finish_ai_operation(operation, "failed", str(e))
            log.exception("生成总评异常 submission=%s", sid)
            return
        error = "" if detail else "AI 生成总评失败，请稍后重试"
        if not detail:
            _finish_ai_operation(operation, "failed", error)
            return
        if detail:
            database.execute("UPDATE submissions SET report_detail=? WHERE id=?",
                             (json.dumps(detail, ensure_ascii=False), sid))
            _finish_ai_operation(operation, "success")
            log.info("总评生成成功 submission=%s", sid)

    operation, queue_err = _ai_task_queue.submit(
        teacher_id, "report_comment", model=model, runner=runner,
        course_id=course_id, lecture_id=lecture_id, submission_id=sid,
        dedup_key=f"report_comment|{sid}")
    if queue_err:
        return {"ok": False, "error": queue_err}, 409
    return {"ok": True, "queued": True, "operation_id": operation["id"]}


@_routes.route("/submission/<oid:sid>/report/comment/status")
@role_required("teacher")
def submission_report_comment_status(sid):
    """前端轮询异步总评生成状态；完成时返回可回填的总评文本。"""
    own_submission(sid)
    op_id = request.args.get("operation_id", type=int)
    if not op_id:
        return {"ok": False, "error": "缺少 operation_id"}, 400
    op = get_db().execute(
        "SELECT status,error FROM ai_operations WHERE id=? AND teacher_id=?",
        (op_id, g.user["id"])).fetchone()
    if not op:
        return {"ok": False, "error": "任务不存在"}, 404
    if op["status"] in ("queued", "running"):
        return {"ok": True, "status": op["status"]}
    if op["status"] == "failed":
        return {"ok": True, "status": "failed", "error": op["error"] or "生成失败"}
    sub = get_db().execute("SELECT ai_summary,report_detail FROM submissions WHERE id=?",
                           (sid,)).fetchone()
    summary = _json_or_none(sub["ai_summary"]) or {}
    detail = _json_or_none(sub["report_detail"]) or {}
    return {"ok": True, "status": "success",
            "text": _overall_text(detail, summary.get("evaluation", ""))}


@_routes.route("/submission/<oid:sid>/report/save", methods=["POST"])
@role_required("teacher")
def submission_report_save(sid):
    """保存老师在报告页对 Markdown 的手工修改。"""
    sub, hw, _ = own_submission(sid)
    md = (request.form.get("report_md") or "").strip()
    db = get_db()
    db.execute("UPDATE submissions SET report_md=? WHERE id=?", (md, sid))
    db.commit()
    flash("报告修改已保存" + ("，学生端同步可见。" if sub["report_published"] else "。"))
    return redirect(url_for("submission_report", sid=sid))


@_routes.route("/submission/<oid:sid>/report/download")
@role_required("teacher")
def submission_report_download(sid):
    sub, hw, _ = own_submission(sid)
    if not sub["report_md"]:
        abort(404)
    name = f"{hw['title']}_批改报告.md".replace("/", "_")
    import io as _io
    return send_file(_io.BytesIO(sub["report_md"].encode("utf-8")),
                     mimetype="text/markdown", as_attachment=True, download_name=name)


@_routes.route("/student/submission/<oid:sid>/report")
@role_required("student")
def student_report(sid):
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=? AND student_id=?",
                     (sid, g.user["id"])).fetchone()
    if not sub or not sub["report_published"] or not sub["report_md"]:
        abort(404)
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (sub["lecture_id"],)).fetchone()
    course = db.execute("SELECT * FROM courses WHERE id=?", (hw["course_id"],)).fetchone()
    kg_diag = _kg_diagnosis(db, sub)
    return render_template("report.html", sub=sub, hw=hw, course=course, md=sub["report_md"],
                           body=render_markdown(sub["report_md"]),
                           student_name=g.user["display_name"], is_teacher=False,
                           kg_diagnosis=kg_diag)


WEAK_STATUS_RANK = {"weak": 0, "reviewed": 1, "mastered": 2}
# 星图只保留每个薄弱点的有限邻居，并优先展示前置关系
MAX_CONTEXT_PER_WEAK = 6
_KG_RELATION_ORDER = {"prerequisite": 0, "contains": 1, "related": 2}


def _weak_group_key(row):
    """同一 KG 节点合并展示；没匹配到节点的按标签单独成组。"""
    if row["node_id"]:
        return f"n:{row['graph_id']}:{row['node_id']}"
    return f"t:{row['tag']}"


def _weak_point_groups(db, student_id):
    """按 KG 节点聚合薄弱点：多个标签命中同一节点只算一组，累计统计各讲最近一次判定。"""
    rows = db.execute(
        "SELECT w.*, c.name AS course_name FROM student_weak_points w "
        "JOIN courses c ON c.id=w.course_id "
        "JOIN enrollments e ON e.course_id=w.course_id AND e.student_id=w.student_id "
        "WHERE w.student_id=? AND w.status<>'removed' ORDER BY c.name, w.tag",
        (student_id,)).fetchall()
    groups = {}
    tag_meta = {}
    for r in rows:
        key = _weak_group_key(r)
        item = groups.get(key)
        if item is None:
            item = groups[key] = {
                "key": key, "course_id": r["course_id"],
                "course_name": r["course_name"],
                "title": r["node_name"] or r["tag"],
                "definition": r["definition"] or "",
                "status": r["status"], "graph_id": r["graph_id"],
                "node_id": r["node_id"], "tags": [], "note": "", "note_at": "",
                "last_seen_at": r["last_seen_at"] or "",
                "right_count": 0, "partial_count": 0, "wrong_count": 0,
                "total_count": 0, "stored": (0, 0, 0, 0), "prerequisites": [],
                "verdicts": [],
            }
        item["tags"].append(r["tag"])
        if (r["last_seen_at"] or "") > item["last_seen_at"]:
            item["last_seen_at"] = r["last_seen_at"]
        if WEAK_STATUS_RANK.get(r["status"], 0) < WEAK_STATUS_RANK.get(item["status"], 0):
            item["status"] = r["status"]
        if r["student_note"] and (r["updated_at"] or "") >= item["note_at"]:
            item["note"] = r["student_note"]
            item["note_at"] = r["updated_at"] or ""
        stored = (r["right_count"], r["partial_count"],
                  r["wrong_count"], r["total_count"])
        if stored[3] > item["stored"][3]:
            item["stored"] = stored
        tag_meta[(r["course_id"], r["tag"])] = key

    # 累计判定：同一讲同一题只取最近一次提交，避免重复提交放大计数
    attempts = _weak_latest_attempts(db, student_id)
    attempts.sort(key=lambda a: (a["course_id"], a["lecture_id"]))
    for attempt in attempts:
        keys = {tag_meta.get((attempt["course_id"], tag))
                for tag in attempt["tags"]}
        for key in keys:
            item = groups.get(key) if key else None
            if item is None:
                continue
            item["total_count"] += 1
            if attempt["verdict"] == "对":
                item["right_count"] += 1
            elif attempt["verdict"] == "半对":
                item["partial_count"] += 1
            elif attempt["verdict"] == "错":
                item["wrong_count"] += 1
            if attempt["verdict"] in ("对", "半对", "错"):
                item["verdicts"].append(attempt["verdict"])

    for item in groups.values():
        if not item["total_count"]:
            (item["right_count"], item["partial_count"],
             item["wrong_count"], item["total_count"]) = item["stored"]
        item["verdicts"] = item["verdicts"][-5:]
    return groups


@_routes.route("/student/weak-points")
@role_required("student")
def student_weak_points():
    """学生查看薄弱点：按 KG 节点聚合去重，统计各讲最近一次判定。"""
    db = get_db()
    groups = _weak_point_groups(db, g.user["id"])
    by_course = {}
    counts = {"all": 0, "weak": 0, "reviewed": 0, "mastered": 0}
    for item in groups.values():
        if item["node_id"]:
            nb = kg_utils.concept_neighbors(db, item["graph_id"], item["node_id"])
            if nb:
                item["prerequisites"] = nb.get("prerequisites", [])
        by_course.setdefault(item["course_id"], {
            "name": item["course_name"], "weak_points": []})
        by_course[item["course_id"]]["weak_points"].append(item)
        counts["all"] += 1
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    for group in by_course.values():
        group["weak_points"].sort(key=lambda w: (
            WEAK_STATUS_RANK.get(w["status"], 0), -w["wrong_count"],
            -w["partial_count"], -w["total_count"], w["title"]))
    return render_template("student_weak_points.html", by_course=by_course,
                           counts=counts)


@_routes.route("/student/weak-points/galaxy.json")
@role_required("student")
def student_weak_points_galaxy():
    """薄弱点星图：下发薄弱知识点及其有限的一跳邻居，前端据此点亮。

    每个薄弱点最多保留 MAX_CONTEXT_PER_WEAK 条关系边（按前置/包含/相关排
    序），并补全可见背景知识之间的边，避免散成孤点；节点与边均批量查询。
    """
    db = get_db()
    groups = _weak_point_groups(db, g.user["id"])
    courses = {}
    for item in groups.values():
        course = courses.setdefault(item["course_id"], {
            "id": item["course_id"], "name": item["course_name"],
            "weak_total": 0, "unmatched": 0, "context_total": 0,
            "nodes": [], "edges": [],
        })
        course["weak_total"] += 1
        if not item["node_id"]:
            course["unmatched"] += 1
    for course_id, course in courses.items():
        weak_items = [it for it in groups.values()
                      if it["course_id"] == course_id and it["node_id"]]
        by_graph = {}
        for item in weak_items:
            by_graph.setdefault(item["graph_id"], []).append(item)
        node_cache, weak_map, edges = {}, {}, {}
        for graph_id, items in by_graph.items():
            ids = sorted({it["node_id"] for it in items})
            ph = ",".join("?" * len(ids))
            found = {n["id"]: n for n in db.execute(
                f"SELECT * FROM kg_nodes WHERE graph_id=? AND id IN ({ph})",
                (graph_id, *ids))}
            for item in items:
                node = found.get(item["node_id"])
                if node is None:
                    course["unmatched"] += 1
                    continue
                node_cache[node["id"]] = node
                weak_map[node["id"]] = item
            if not found:
                continue
            per_node = {}
            for edge in db.execute(
                    f"SELECT * FROM kg_edges WHERE graph_id=? AND"
                    f" (src_id IN ({ph}) OR dst_id IN ({ph}))",
                    (graph_id, *ids, *ids)):
                if edge["src_id"] in found:
                    per_node.setdefault(edge["src_id"], []).append(edge)
                if edge["dst_id"] in found:
                    per_node.setdefault(edge["dst_id"], []).append(edge)
            context_ids = set()
            for nid, node_edges in per_node.items():
                node_edges.sort(key=lambda e: (
                    _KG_RELATION_ORDER.get(e["relation"], 9), e["id"]))
                for edge in node_edges[:MAX_CONTEXT_PER_WEAK]:
                    edges[edge["id"]] = edge
                    other_id = (edge["dst_id"] if edge["src_id"] == nid
                                else edge["src_id"])
                    if other_id not in found:
                        context_ids.add(other_id)
            if context_ids:
                cph = ",".join("?" * len(context_ids))
                for node in db.execute(
                        f"SELECT * FROM kg_nodes WHERE graph_id=? AND id IN ({cph})",
                        (graph_id, *sorted(context_ids))):
                    node_cache.setdefault(node["id"], node)
            # 背景知识之间也可能有前置/相关关系，补全可见节点内部的边
            visible = set(found) | context_ids
            vph = ",".join("?" * len(visible))
            for edge in db.execute(
                    f"SELECT * FROM kg_edges WHERE graph_id=? AND"
                    f" src_id IN ({vph}) AND dst_id IN ({vph})",
                    (graph_id, *visible, *visible)):
                edges[edge["id"]] = edge
        # 图谱数据不完整时可能引用不存在的节点，直接丢弃这类边
        edges = {eid: e for eid, e in edges.items()
                 if e["src_id"] in node_cache and e["dst_id"] in node_cache}
        degree = {}
        for edge in edges.values():
            degree[edge["src_id"]] = degree.get(edge["src_id"], 0) + 1
            degree[edge["dst_id"]] = degree.get(edge["dst_id"], 0) + 1
        course["context_total"] = len(node_cache) - len(weak_map)
        course["nodes"] = []
        for node in node_cache.values():
            entry = {
                "id": node["id"], "name": node["name"],
                "category": node["category"] or "",
                "definition": node["definition"] or "",
                "degree": degree.get(node["id"], 0),
                "weak": node["id"] in weak_map,
            }
            item = weak_map.get(node["id"])
            if item is not None:
                total = item["total_count"]
                entry.update({
                    "key": item["key"],
                    "status": item["status"],
                    "right_count": item["right_count"],
                    "partial_count": item["partial_count"],
                    "wrong_count": item["wrong_count"],
                    "total_count": total,
                    "error_rate": (round((item["wrong_count"]
                                          + item["partial_count"] * 0.5)
                                         / total * 100) if total else 0),
                    "trend": item["verdicts"],
                    "tags": item["tags"],
                })
            course["nodes"].append(entry)
        course["edges"] = [{
            "src": edge["src_id"], "dst": edge["dst_id"],
            "relation": edge["relation"],
        } for edge in edges.values()]
    # 整门课都没匹配到图谱时也下发，前端需要提示「未关联图谱」而不是整门课消失
    payload = list(courses.values())
    payload.sort(key=lambda course: (-course["weak_total"], course["name"]))
    return jsonify({"courses": payload})


def _weak_group_filter(group):
    """解析页面传来的组标识（节点组 n:图id:节点id / 标签组 t:标签）。"""
    if group.startswith("n:") and group.count(":") == 2:
        _, graph_id, node_id = group.split(":")
        try:
            return "graph_id=? AND node_id=?", (int(graph_id), int(node_id))
        except ValueError:
            abort(404)
    if group.startswith("t:") and len(group) > 2:
        return "tag=?", (group[2:],)
    abort(404)


def _weak_group_ids(db, student_id, course_id, group):
    """取一组未移除的薄弱点记录 id；组不存在或已全部移除时 404。"""
    condition, params = _weak_group_filter(group)
    ids = [r["id"] for r in db.execute(
        f"SELECT id FROM student_weak_points WHERE student_id=? AND course_id=?"
        f" AND status<>'removed' AND {condition}",
        (student_id, course_id, *params))]
    if not ids:
        abort(404)
    return ids


@_routes.route("/student/weak-points/status", methods=["POST"])
@role_required("student")
def student_weak_point_status():
    """学生标记一组薄弱点（同一 KG 节点）的状态与备注。"""
    status = (request.form.get("status") or "").strip()
    if status not in WEAK_STATUS_RANK:
        flash("状态不合法")
        return redirect(url_for("student_weak_points"))
    note = (request.form.get("note") or "").strip()
    course_id = request.form.get("course_id", type=int) or 0
    group = (request.form.get("group") or "").strip()
    db = get_db()
    ids = _weak_group_ids(db, g.user["id"], course_id, group)
    db.execute(
        "UPDATE student_weak_points SET status=?, student_note=?, "
        "status_lecture_id=COALESCE((SELECT lecture_id FROM submissions "
        "WHERE id=student_weak_points.source_submission_id), 0), "
        f"updated_at=CURRENT_TIMESTAMP WHERE id IN ({','.join('?' * len(ids))})",
        (status, note, *ids))
    db.commit()
    flash("薄弱点状态已更新")
    return redirect(url_for("student_weak_points"))


@_routes.route("/student/weak-points/remove", methods=["POST"])
@role_required("student")
def student_weak_point_remove():
    """学生移除一组薄弱点；同一讲重批不会恢复，后续讲次再次做错会自动回到薄弱。"""
    course_id = request.form.get("course_id", type=int) or 0
    group = (request.form.get("group") or "").strip()
    db = get_db()
    ids = _weak_group_ids(db, g.user["id"], course_id, group)
    db.execute(
        f"UPDATE student_weak_points SET status='removed', "
        "status_lecture_id=COALESCE((SELECT lecture_id FROM submissions "
        "WHERE id=student_weak_points.source_submission_id), 0), "
        f"updated_at=CURRENT_TIMESTAMP WHERE id IN ({','.join('?' * len(ids))})",
        (*ids,))
    db.commit()
    flash("已移除该薄弱点")
    return redirect(url_for("student_weak_points"))


def _tags_for_question(tag_map, no):
    """按题号取标签：兼容「3.」「第3题」「3(1)」等写法。"""
    key = str(no or "").strip()
    tags = tag_map.get(key)
    if tags:
        return tags
    m = re.match(r"\s*(?:第\s*)?(\d+)", key)
    if not m:
        return []
    for tag_no, values in tag_map.items():
        other = re.match(r"\s*(?:第\s*)?(\d+)", str(tag_no or ""))
        if other and other.group(1) == m.group(1):
            return values
    return []


@_routes.route("/student/wrong-questions")
@role_required("student")
def student_wrong_questions():
    """学生错题本：收录老师已公开批改结果中的错题与半对题。"""
    db = get_db()
    course_filter = request.args.get("course", type=int) or 0
    tag_filter = [t.strip() for t in (request.args.get("tags") or "").split(",")
                  if t.strip()]
    verdict_filter = (request.args.get("verdict") or "").strip()
    review_filter = (request.args.get("review") or "").strip()
    rows = db.execute(
        "SELECT q.*, s.created_at AS submitted_at, s.lecture_id, "
        "l.title AS lecture_title, l.course_id, c.name AS course_name, "
        "CASE WHEN r.status='reviewed' THEN 1 ELSE 0 END AS student_reviewed "
        "FROM questions q "
        "JOIN submissions s ON s.id=q.submission_id "
        "JOIN lectures l ON l.id=s.lecture_id "
        "JOIN courses c ON c.id=l.course_id "
        "LEFT JOIN student_question_reviews r "
        "  ON r.question_id=q.id AND r.student_id=s.student_id "
        "WHERE s.student_id=? AND q.verdict IN ('错','半对') "
        "AND s.id=(SELECT MAX(s2.id) FROM submissions s2 "
        "  WHERE s2.lecture_id=s.lecture_id AND s2.student_id=s.student_id) "
        "AND (s.status='已批改' OR s.report_published=1) "
        "ORDER BY c.name, l.id DESC, q.id",
        (g.user["id"],)).fetchall()
    # 先按知识点筛选，再按课程汇总可选范围
    tag_maps = {}
    tagged = []
    for row in rows:
        tag_map = tag_maps.get(row["lecture_id"])
        if tag_map is None:
            tag_map = tag_utils.lecture_tags_by_key(db, row["lecture_id"])
            tag_maps[row["lecture_id"]] = tag_map
        tags = _tags_for_question(tag_map, row["no"])
        if tag_filter and not set(tags) & set(tag_filter):
            continue
        tagged.append((row, tags))
    courses = {}
    for row, _ in tagged:
        course = courses.setdefault(row["course_id"], {
            "id": row["course_id"], "name": row["course_name"], "count": 0})
        course["count"] += 1
    counts = {"all": 0, "wrong": 0, "partial": 0, "open": 0, "reviewed": 0}
    by_course = {}
    for row, tags in tagged:
        if course_filter and row["course_id"] != course_filter:
            continue
        counts["all"] += 1
        counts["wrong" if row["verdict"] == "错" else "partial"] += 1
        counts["reviewed" if row["student_reviewed"] else "open"] += 1
        if verdict_filter and row["verdict"] != verdict_filter:
            continue
        if review_filter == "open" and row["student_reviewed"]:
            continue
        if review_filter == "reviewed" and not row["student_reviewed"]:
            continue
        course = by_course.setdefault(row["course_id"], {
            "id": row["course_id"], "name": row["course_name"],
            "lectures": {}, "count": 0})
        lecture = course["lectures"].setdefault(row["lecture_id"], {
            "id": row["lecture_id"], "title": row["lecture_title"],
            "submitted_at": row["submitted_at"], "questions": []})
        item = {key: row[key] for key in row.keys()}
        item["tags"] = tags
        item["reviewed"] = bool(row["student_reviewed"])
        lecture["questions"].append(item)
        course["count"] += 1
    for course in by_course.values():
        course["lectures"] = list(course["lectures"].values())
    filters = {key: value for key, value in (
        ("course", course_filter or ""), ("tags", ",".join(tag_filter)),
        ("verdict", verdict_filter), ("review", review_filter)) if value}
    return render_template(
        "student_wrong_questions.html", by_course=list(by_course.values()),
        courses=list(courses.values()), counts=counts, filters=filters,
        tag_filter=tag_filter, verdict_filter=verdict_filter,
        review_filter=review_filter)


@_routes.route("/student/wrong-questions/review", methods=["POST"])
@role_required("student")
def student_question_review():
    """学生标记单道错题是否已复习。"""
    status = (request.form.get("status") or "").strip()
    question_id = request.form.get("question_id", type=int) or 0
    if status not in ("open", "reviewed"):
        flash("复习状态不合法")
        return redirect(url_for("student_wrong_questions"))
    db = get_db()
    owned = db.execute(
        "SELECT q.id FROM questions q JOIN submissions s ON s.id=q.submission_id "
        "WHERE q.id=? AND s.student_id=?",
        (question_id, g.user["id"])).fetchone()
    if not owned:
        abort(404)
    db.execute(
        "INSERT INTO student_question_reviews(student_id,question_id,status,updated_at) "
        "VALUES(?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(student_id,question_id) DO UPDATE SET "
        "status=excluded.status, updated_at=CURRENT_TIMESTAMP",
        (g.user["id"], question_id, status))
    db.commit()
    flash("已标记为已复习" if status == "reviewed" else "已取消复习标记")
    args = {key: request.form.get(key) for key in
            ("course", "tags", "verdict", "review") if request.form.get(key)}
    return redirect(url_for("student_wrong_questions", **args))


def _consistency_issues():
    """扫描数据库记录与磁盘文件的偏差。fixable=True 的项可在页面上安全自动修复。"""
    db = get_db()
    issues = []

    def add(code, level, msg, rows, fixable=False, detail=""):
        if rows:
            issues.append({"code": code, "level": level, "msg": msg, "count": len(rows),
                           "sample": ", ".join(str(r[0]) for r in rows[:8]) or detail,
                           "fixable": fixable})

    def missing(rows, col):
        return [r for r in rows
                if r[col] and not os.path.exists(os.path.join(_upload_dir(), r[col]))]

    add("orphan_questions", "error", "题目记录指向已删除的提交",
        [(r["id"],) for r in db.execute(
            "SELECT q.id FROM questions q LEFT JOIN submissions s ON s.id=q.submission_id "
            "WHERE s.id IS NULL")], fixable=True, detail="可安全删除")

    add("missing_submission_file", "error", "提交记录对应的学生文件已丢失",
        [(r["id"],) for r in db.execute("SELECT id, file_path FROM submissions").fetchall()
         if file_list(r["file_path"])
         and not all(os.path.exists(os.path.join(_upload_dir(), p))
                     for p in file_list(r["file_path"]))],
        detail="原文件不可下载，批改结果仍在")

    add("missing_answer_pdf", "warn", "讲次的教师版 PDF 丢失",
        [(r["id"],) for r in missing(
            db.execute("SELECT id, answer_pdf FROM lectures WHERE answer_pdf != ''").fetchall(),
            "answer_pdf")], detail="缺少标准答案 PDF 时无法进行 AI 批阅")
    return issues


@_routes.route("/admin/consistency")
@role_required("admin")
def consistency_page():
    stats = {t: get_db().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("users", "courses", "lectures", "exams", "submissions", "questions")}
    backups = []
    if os.path.isdir(_backup_dir()):
        for name in sorted(os.listdir(_backup_dir()), reverse=True):
            path = os.path.join(_backup_dir(), name)
            if BACKUP_RE.fullmatch(name):
                backups.append({
                    "name": name,
                    "size": storage.human_size(os.path.getsize(path)),
                    "mtime": datetime.fromtimestamp(os.path.getmtime(path))
                    .strftime("%Y-%m-%d %H:%M")})
    return render_template("consistency.html", issues=_consistency_issues(), stats=stats,
                           data_dir=cfg.get("DATA_DIR"), db_path=_db_path(), backups=backups,
                           auto_backup=cfg.get("AUTO_BACKUP"), backup_keep=cfg.get("BACKUP_KEEP"))


@_routes.route("/admin/consistency/fix", methods=["POST"])
@role_required("admin")
def consistency_fix():
    db = get_db()
    n1 = db.execute("DELETE FROM questions WHERE submission_id NOT IN "
                    "(SELECT id FROM submissions)").rowcount
    db.commit()
    log.info("一致性修复：孤儿题目 %s 条", n1)
    flash(f"已修复：清理孤儿题目 {n1} 条")
    return redirect(url_for("consistency_page"))


def _build_backup_zip(dest):
    """把数据库在线快照 + 全部附件 + manifest 打包到 dest，返回 manifest。"""
    snap = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    snap.close()
    src = sqlite3.connect(_db_path())
    dst = sqlite3.connect(snap.name)
    try:
        with dst:
            src.backup(dst)  # 在线备份，避免 WAL 下拷到半截数据
    finally:
        src.close()
        dst.close()

    con = sqlite3.connect(_db_path())
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("users", "courses", "lectures", "exams", "submissions",
                            "questions", "knowledge_graphs", "kg_materials",
                            "kg_nodes", "kg_edges", "course_knowledge_graphs")}
    finally:
        con.close()
    manifest = {"app": "GradeLogy", "version": cfg.APP_VERSION,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "counts": counts}
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap.name, "data.sqlite")
            for root, _, files in os.walk(_upload_dir()):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.join("uploads", os.path.relpath(full, _upload_dir())))
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        os.remove(snap.name)
    return manifest


def _prune_backups():
    """只保留最近 BACKUP_KEEP 份自动/手动备份（不含恢复前的 pre_restore 快照）。"""
    os.makedirs(_backup_dir(), exist_ok=True)
    items = sorted((os.path.join(_backup_dir(), n) for n in os.listdir(_backup_dir())
                    if BACKUP_RE.fullmatch(n)), key=os.path.getmtime, reverse=True)
    for path in items[cfg.get("BACKUP_KEEP"):]:
        try:
            os.remove(path)
        except OSError:
            pass
    return items[:cfg.get("BACKUP_KEEP")]


def _run_auto_backup():
    """当天还没有备份就生成一份；命名为 gradelogy_YYYYMMDD_HHMMSS.zip。"""
    os.makedirs(_backup_dir(), exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    if any(n.startswith(f"gradelogy_{today}_") for n in os.listdir(_backup_dir())):
        return None
    path = os.path.join(_backup_dir(), datetime.now().strftime("gradelogy_%Y%m%d_%H%M%S.zip"))
    _build_backup_zip(path)
    _prune_backups()
    log.info("自动备份完成：%s", path)
    return path


_backup_lock = None
_backup_started = False


def _backup_loop():
    while True:
        try:
            _run_auto_backup()
        except Exception:
            log.exception("自动备份失败")
        time.sleep(1800)


def start_auto_backup():
    """启动每日自动备份线程；多进程部署时用文件锁保证只有一个进程执行。"""
    global _backup_lock, _backup_started
    if _backup_started or not cfg.get("AUTO_BACKUP"):
        return
    _backup_started = True
    try:
        os.makedirs(_backup_dir(), exist_ok=True)
        if fcntl is not None:
            _backup_lock = open(os.path.join(_backup_dir(), ".auto.lock"), "w")
            fcntl.flock(_backup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("自动备份未启动：另一个进程已在执行")
        return
    application = current_app._get_current_object() if has_app_context() else app
    def run():
        with application.app_context():
            _backup_loop()
    threading.Thread(target=run, daemon=True, name="auto-backup").start()
    log.info("自动备份已启用：每 30 分钟检查一次，保留最近 %s 份", cfg.get("BACKUP_KEEP"))


@_routes.route("/admin/export")
@role_required("admin")
def admin_export():
    """整体导出数据库快照 + 全部附件 + manifest，用于备份或迁移到新机器。"""
    out = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    out.close()
    _build_backup_zip(out.name)
    log.info("导出系统数据：%s", out.name)
    return send_file(out.name, as_attachment=True,
                     download_name=datetime.now().strftime("gradelogy_%Y%m%d_%H%M%S.zip"))


@_routes.route("/admin/backup/now", methods=["POST"])
@role_required("admin")
def admin_backup_now():
    """立即生成一份备份，存到数据目录的 backups/ 下。"""
    path = os.path.join(_backup_dir(), datetime.now().strftime("gradelogy_%Y%m%d_%H%M%S.zip"))
    try:
        _build_backup_zip(path)
    except Exception:
        log.exception("手动备份失败")
        flash("备份失败，请查看日志")
        return redirect(url_for("consistency_page"))
    _prune_backups()
    log.info("手动备份完成：%s", path)
    flash(f"已生成备份 {os.path.basename(path)}")
    return redirect(url_for("consistency_page"))


@_routes.route("/admin/backup/<name>")
@role_required("admin")
def admin_backup_download(name):
    if not BACKUP_RE.fullmatch(name or ""):
        abort(404)
    path = os.path.join(_backup_dir(), name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


def _extract_backup_zip(zpath, extract_root):
    """校验 zip 路径安全 / 数据库结构 / 完整性后解压，返回 (src_db, 错误提示)。"""
    if not zipfile.is_zipfile(zpath):
        return None, "不是有效的 zip 备份包"
    os.makedirs(extract_root, exist_ok=True)
    limit = cfg.get("MAX_RESTORE_MB") * 1024 * 1024
    total = 0
    with zipfile.ZipFile(zpath) as z:
        members = z.infolist()
        if len(members) > 50000:
            return None, "备份包文件数量异常，已拒绝导入"
        for info in members:
            target = os.path.abspath(os.path.join(extract_root, info.filename))
            if target != extract_root and not target.startswith(extract_root + os.sep):
                security_log.warning("备份包含越界路径：%s", info.filename)
                return None, "备份包包含非法路径，已拒绝导入"
            total += info.file_size
            if total > limit:
                return None, f"备份包解压后超过 {cfg.get("MAX_RESTORE_MB")}MB 上限，已拒绝导入"
        z.extractall(extract_root)

    src_db = os.path.join(extract_root, "data.sqlite")
    if not os.path.isfile(src_db):
        return None, "备份包缺少 data.sqlite，不是本系统导出的备份"
    con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # 旧备份里作业表叫 homeworks，恢复后由 init_db 迁移为 lectures
        if not ({"users", "submissions"} <= tables and ({"lectures", "homeworks"} & tables)):
            return None, "备份包里的数据库结构不符合本系统，已拒绝导入"
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return None, "备份包里的数据库未通过完整性检查，已拒绝导入"
    except sqlite3.DatabaseError:
        log.exception("备份数据库不可用")
        return None, "备份包里的数据库文件损坏，无法恢复"
    finally:
        con.close()
    return src_db, None


def _apply_restore(extract_root):
    """用已解压的备份覆盖当前数据库与附件，返回 (账号数, 提交数, 旧数据备份目录)。"""
    src_db = os.path.join(extract_root, "data.sqlite")
    # 破坏性操作前，先把当前数据完整备份一份，失败可人工回退
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(cfg.get("DATA_DIR"), "backups")
    os.makedirs(bdir, exist_ok=True)
    cur = sqlite3.connect(_db_path(), timeout=10)
    bk = sqlite3.connect(os.path.join(bdir, f"pre_restore_{ts}.sqlite"))
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        cur.backup(bk)
        # 先撤销所有旧会话，避免数据库编号重用后 Cookie 变成另一个账号。
        revoke_sessions(cfg.get("DATA_DIR"))
        src.backup(cur)  # 用 backup API 覆盖，避免替换文件句柄影响正在处理的请求
    finally:
        src.close()
        bk.close()
        cur.close()

    # 附件整体替换：旧目录先移入备份，再把包内的 uploads 放回原位
    if os.path.isdir(_upload_dir()):
        shutil.move(_upload_dir(), os.path.join(bdir, f"uploads_{ts}"))
    new_uploads = os.path.join(extract_root, "uploads")
    if os.path.isdir(new_uploads):
        shutil.move(new_uploads, _upload_dir())
    cfg.ensure_dirs()
    # 旧备份可能缺后续版本新增的列/表，恢复后立刻跑一次迁移
    init_db()

    db = get_db()
    n_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    n_subs = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    log.info("从备份恢复：%s 个账号、%s 份提交，旧数据备份于 %s", n_users, n_subs, bdir)
    session.clear()
    return n_users, n_subs, bdir


@_routes.route("/admin/restore", methods=["POST"])
@role_required("admin")
def admin_restore():
    """从导出的 zip 恢复：先校验包结构与路径安全，备份当前数据，再替换附件与数据库。"""
    up = request.files.get("backup")
    if not up or not up.filename:
        flash("请选择此前导出的 zip 备份文件")
        return redirect(url_for("consistency_page"))

    tmpdir = tempfile.mkdtemp(prefix=".restore_", dir=cfg.get("DATA_DIR"))
    try:
        zpath = os.path.join(tmpdir, "backup.zip")
        up.save(zpath)
        extract_root = os.path.join(tmpdir, "data")
        _, err = _extract_backup_zip(zpath, extract_root)
        if err:
            flash(err)
            return redirect(url_for("consistency_page"))
        n_users, n_subs, bdir = _apply_restore(extract_root)
        flash(f"恢复完成：{n_users} 个账号、{n_subs} 份提交。旧数据已备份到 {bdir}，请重新登录。")
        return redirect(url_for("login"))
    except sqlite3.DatabaseError:
        log.exception("备份数据库不可用")
        flash("备份包里的数据库文件损坏，无法恢复，当前数据未改动")
        return redirect(url_for("consistency_page"))
    except OSError:
        log.exception("恢复失败（磁盘/权限）")
        flash(f"恢复失败，当前数据已备份到 {os.path.join(cfg.get("DATA_DIR"), 'backups')}，请检查磁盘与权限")
        return redirect(url_for("consistency_page"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 初始化导入向导（仅全新空系统可用） ----------
SETUP_TMP_ROOT = os.path.join(cfg.get("DATA_DIR"), "tmp_setup")


def _is_pristine():
    """系统是否全新全空：没有老师 / 学生账号，也没有任何课程、讲次、考试与提交。"""
    row = get_db().execute("""
        SELECT (SELECT COUNT(*) FROM users WHERE role!='admin') AS users,
               (SELECT COUNT(*) FROM courses) AS courses,
               (SELECT COUNT(*) FROM lectures) AS lectures,
               (SELECT COUNT(*) FROM exams) AS exams,
               (SELECT COUNT(*) FROM submissions) AS submissions,
               (SELECT COUNT(*) FROM enrollments) AS enrollments,
               (SELECT COUNT(*) FROM course_teachers) AS course_teachers
        """).fetchone()
    return all(row[k] == 0 for k in row.keys())


def _purge_setup_stash():
    """清掉上一轮向导上传的临时目录，避免占用磁盘。"""
    stash = session.pop("setup_import", None)
    if not stash:
        return
    path = os.path.abspath(stash.get("dir") or "")
    root = os.path.abspath(_setup_tmp_root())
    if path.startswith(root + os.sep):
        shutil.rmtree(path, ignore_errors=True)


def _backup_summary(extract_root):
    """统计包内数据库与附件，供确认页展示。"""
    con = sqlite3.connect(f"file:{os.path.join(extract_root, 'data.sqlite')}?mode=ro", uri=True)
    try:
        counts = {}
        for table in ("users", "courses", "lectures", "exams", "submissions", "questions"):
            try:
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = 0
        try:
            teachers = con.execute("SELECT COUNT(*) FROM users WHERE role='teacher'").fetchone()[0]
            students = con.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]
        except sqlite3.Error:
            teachers = students = 0
    finally:
        con.close()

    manifest = {}
    mpath = os.path.join(extract_root, "manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
    n_files = total = 0
    for root, _, files in os.walk(os.path.join(extract_root, "uploads")):
        for name in files:
            n_files += 1
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return {"counts": counts, "teachers": teachers, "students": students,
            "files": n_files, "bytes": total, "manifest": manifest}


@_routes.route("/setup")
@role_required("admin")
def setup_import():
    if not _is_pristine():
        flash("系统里已有数据，初始化导入向导仅限全新空系统使用；如需恢复旧备份，请到「系统维护」页")
        return redirect(url_for("index"))
    return render_template("setup_import.html", step="upload")


@_routes.route("/setup/upload", methods=["POST"])
@role_required("admin")
def setup_upload():
    if not _is_pristine():
        flash("系统里已有数据，初始化导入向导仅限全新空系统使用")
        return redirect(url_for("index"))
    up = request.files.get("backup")
    if not up or not up.filename:
        flash("请选择备份 zip 文件")
        return redirect(url_for("setup_import"))
    if not up.filename.lower().endswith(".zip"):
        flash("请上传 .zip 备份包")
        return redirect(url_for("setup_import"))

    _purge_setup_stash()
    os.makedirs(_setup_tmp_root(), exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="import_", dir=_setup_tmp_root())
    try:
        zpath = os.path.join(tmpdir, "backup.zip")
        up.save(zpath)
        extract_root = os.path.join(tmpdir, "data")
        _, err = _extract_backup_zip(zpath, extract_root)
        if err:
            shutil.rmtree(tmpdir, ignore_errors=True)
            flash(err)
            return redirect(url_for("setup_import"))
        session["setup_import"] = {"dir": tmpdir, "summary": _backup_summary(extract_root)}
    except OSError:
        log.exception("初始化导入：保存上传文件失败")
        shutil.rmtree(tmpdir, ignore_errors=True)
        flash("上传失败，请检查磁盘空间与权限")
        return redirect(url_for("setup_import"))
    return redirect(url_for("setup_preview"))


@_routes.route("/setup/preview")
@role_required("admin")
def setup_preview():
    stash = session.get("setup_import")
    if not stash or not os.path.isdir(stash.get("dir", "")):
        flash("请先选择备份包")
        return redirect(url_for("setup_import"))
    if not _is_pristine():
        _purge_setup_stash()
        flash("系统里已有数据，初始化导入已取消")
        return redirect(url_for("index"))
    return render_template("setup_import.html", step="preview", summary=stash["summary"])


@_routes.route("/setup/confirm", methods=["POST"])
@role_required("admin")
def setup_confirm():
    stash = session.get("setup_import")
    if not stash or not os.path.isdir(stash.get("dir", "")):
        flash("上传的备份已失效，请重新选择")
        return redirect(url_for("setup_import"))
    tmpdir = stash["dir"]
    if not os.path.abspath(tmpdir).startswith(os.path.abspath(_setup_tmp_root()) + os.sep):
        _purge_setup_stash()
        abort(400)
    if not _is_pristine():
        _purge_setup_stash()
        flash("系统里已有数据，初始化导入已取消")
        return redirect(url_for("index"))

    try:
        n_users, n_subs, _ = _apply_restore(os.path.join(tmpdir, "data"))
    except sqlite3.DatabaseError:
        log.exception("初始化导入失败：数据库不可用")
        flash("备份包里的数据库无法使用，系统未改动")
        return redirect(url_for("setup_import"))
    except OSError:
        log.exception("初始化导入失败（磁盘/权限）")
        flash("导入失败，请检查磁盘空间与权限")
        return redirect(url_for("setup_import"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    log.info("初始化导入完成：%s 个账号、%s 份提交", n_users, n_subs)
    flash(f"初始化导入完成：{n_users} 个账号、{n_subs} 份提交。请用原系统的账号登录。")
    return redirect(url_for("login"))


@_routes.route("/setup/cancel", methods=["POST"])
@role_required("admin")
def setup_cancel():
    _purge_setup_stash()
    flash("已取消，上传的备份包已删除")
    return redirect(url_for("setup_import"))


@_routes.route("/files/<path:filename>")
@login_required
def uploaded_file(filename):
    if not can_read_file(get_db(), g.user, filename):
        security_log.warning("越权访问文件 user=%s path=%s", g.user["id"], filename)
        abort(403)
    full = os.path.abspath(os.path.join(_upload_dir(), filename))
    if not full.startswith(os.path.abspath(_upload_dir()) + os.sep):
        abort(404)
    # 手机端 PDF 预览：iframe 在 iOS 上只能显示第一页，改由服务端逐页渲染成图片
    if request.args.get("meta"):
        return jsonify(pages=pdf_page_count(full))
    page = request.args.get("page", type=int)
    if page:
        # box=x,y,w,h（0~1 比例），用于逐题复核时裁剪学生答案/标准答案区域
        box = None
        box_str = request.args.get("box", "")
        if box_str:
            try:
                parts = [float(v) for v in box_str.split(",")]
                if len(parts) == 4:
                    box = parts
            except ValueError:
                box = None
        data = render_page_jpeg(full, page, scale=1.8, quality=80, box=box)
        if not data:
            abort(404)
        return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=3600)
    return send_from_directory(_upload_dir(), filename)


@_routes.route("/healthz")
def healthz():
    """容器健康检查：不要求登录，只报告数据库与存储是否可用。"""
    checks, ok = {}, True
    try:
        get_db().execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001 健康检查需要兜住一切异常
        checks["db"] = f"error: {e}"
        ok = False
    checks["storage"] = "ok" if os.access(_upload_dir(), os.W_OK) else "readonly"
    ok = ok and checks["storage"] == "ok"
    return {"status": "ok" if ok else "degraded",
            "version": cfg.APP_VERSION, "checks": checks}, (200 if ok else 503)


def _lan_ip():
    """取本机在局域网中的 IP（不实际发包，只是让系统选路由）。取不到返回 None。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def sync_models_for_teacher(teacher_id, force=False):
    """同步入口：把本模块的 fetch_* 注入给实现，保留测试对 app 层的 patch 点。"""
    return _sync_models_for_teacher(teacher_id, force=force,
                                    fetch_models=fetch_available_models,
                                    fetch_prices=fetch_tokenhub_prices)


def _ai_route_deps():
    """交给 ai_routes 的依赖。

    可调用项统一包成惰性代理，调用时才从本模块全局取值，
    这样测试里 patch app.get_setting / app._probe_ai_config 仍然作用于这些路由。
    """
    def lazy(name):
        return lambda *args, **kwargs: globals()[name](*args, **kwargs)

    return {
        "role_required": role_required,
        "get_db": lazy("get_db"),
        "get_setting": lazy("get_setting"),
        "save_setting": lazy("save_setting"),
        "now_text": lazy("_now_text"),
        "api_key_rows": lazy("_api_key_rows"),
        "activate_api_key": lazy("_activate_api_key"),
        "build_model_rows": lazy("build_model_rows"),
        "sync_models_for_teacher": lazy("sync_models_for_teacher"),
        "start_ai_operation": lazy("_start_ai_operation"),
        "finish_ai_operation": lazy("_finish_ai_operation"),
        "probe_ai_config": lazy("_probe_ai_config"),
        "ai_dashboard_data": lazy("_ai_dashboard_data"),
        "model_catalog": MODEL_CATALOG,
        "est_tokens_input": EST_TOKENS_INPUT,
        "est_tokens_output": EST_TOKENS_OUTPUT,
        "env_ai_settings": ENV_AI_SETTINGS,
        "ai_operation_label": AI_OPERATION_LABEL,
        "ai_status_label": AI_STATUS_LABEL,
    }


def create_app(config_overrides=None):
    """应用工厂：集中创建 Flask 实例，并挂载路由、请求钩子与模板扩展。"""
    app = Flask(__name__)
    overrides = dict(config_overrides or {})
    if "DATA_DIR" in overrides:
        root = os.path.abspath(overrides["DATA_DIR"])
        overrides["DATA_DIR"] = root
        for key, name in (("DB_PATH", "data.sqlite"), ("STORAGE_DIR", "uploads"),
                          ("BACKUP_DIR", "backups"), ("EXPORT_DIR", "exports"),
                          ("LOG_DIR", "logs"), ("SECRET_KEY_FILE", ".secret_key")):
            overrides.setdefault(key, os.path.join(root, name))
    app.config["GRADELOGY_SETTINGS"] = overrides
    app.config.update(overrides)
    with app.app_context():
        cfg.ensure_dirs()
        app.config["SECRET_KEY"] = overrides.get("SECRET_KEY") or cfg.SECRET_KEY or load_secret_key()
        cfg.setup_logging()
        app.config["MAX_CONTENT_LENGTH"] = max(
            cfg.get("MAX_UPLOAD_MB"), cfg.get("MAX_RESTORE_MB"), cfg.get("KG_MAX_UPLOAD_MB")) * 1024 * 1024
    # Cookie 加固：禁止 JS 读取；SameSite 阻断跨站携带；HTTPS 部署时用 COOKIE_SECURE=1 打开 Secure
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = cfg.COOKIE_SECURE
    app.config.update(overrides)
    app.config.setdefault("SESSION_IDLE_TIMEOUT_SECONDS", cfg.SESSION_IDLE_TIMEOUT_SECONDS)
    app.config.setdefault("SESSION_IDLE_WARNING_SECONDS", cfg.SESSION_IDLE_WARNING_SECONDS)
    idle_timeout = int(app.config["SESSION_IDLE_TIMEOUT_SECONDS"])
    idle_warning = int(app.config["SESSION_IDLE_WARNING_SECONDS"])
    if idle_timeout <= 0 or idle_warning < 0 or idle_warning >= idle_timeout:
        raise ValueError(
            "SESSION_IDLE_TIMEOUT_SECONDS must be positive and "
            "SESSION_IDLE_WARNING_SECONDS must be smaller")
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        seconds=idle_timeout)
    # Cookie 只在已验证的活动请求上滚动，后台轮询不能延长浏览器会话。
    app.config["SESSION_REFRESH_EACH_REQUEST"] = False
    app.teardown_appcontext(close_db)

    app.url_map.gradelogy_signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="gradelogy-url-id")
    app.url_map.converters["oid"] = SignedIdConverter

    routes = AppRoutes()
    routes._items = list(_routes._items)
    ai_routes.register(routes, _ai_route_deps())
    routes.apply(app)
    _configure_queues(app)

    app.jinja_env.filters["fromjson"] = _json_or_none
    app.jinja_env.filters["file_list"] = file_list
    app.jinja_env.filters["markdown_safe"] = _md_safe
    app.jinja_env.globals["accuracy"] = accuracy_text
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["ai_operation_label"] = AI_OPERATION_LABEL.get
    app.jinja_env.globals["ai_status_label"] = AI_STATUS_LABEL.get
    app.jinja_env.globals["ai_usage_text"] = _ai_usage_text
    app.jinja_env.globals["compact_number"] = _compact_number
    return app


def initialize_app(application, start_workers=True):
    """显式运行迁移与后台启动；读取 app 模块不迁移数据库或启动任务。"""
    with application.app_context():
        init_db(recover_interrupted=start_workers)
        if start_workers:
            start_kg_recovery()
            start_auto_backup()
            start_model_sync()
            start_auto_grade()
    return application


# 保留 app 对象供测试和工具使用；生产启动由 wsgi.py 负责。
app = create_app()


if __name__ == "__main__":
    initialize_app(app)
    host = cfg.HOST
    port = cfg.PORT
    ip = _lan_ip()
    print("\n  本机访问：http://127.0.0.1:%d" % port)
    if ip:
        print("  手机/其他电脑（同一 WiFi）：http://%s:%d\n" % (ip, port))
    # debug=False 避免 stat watcher 触发代码重载把长耗时批改请求掐掉
    app.run(host=host, port=port, debug=False)
