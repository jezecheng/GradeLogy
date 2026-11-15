# -*- coding: utf-8 -*-
"""教学管理平台
结构：课程 → 讲次（作业 + AI 批阅）/ 考试（只录分数）
角色：admin（用户管理）/ teacher（课程、讲次、考试、批改、AI 设置）/ student（提交与查看批改）
"""
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

try:
    import fcntl  # 仅类 Unix 可用，用于保证自动备份只在一个进程里跑
except ImportError:  # pragma: no cover
    fcntl = None

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.routing import BaseConverter, ValidationError
from werkzeug.security import check_password_hash, generate_password_hash

import ai_routes
import config as cfg
import db as database
from ai_config import AI_SETTING_KEYS, ENV_AI_SETTINGS, get_ai_setting
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
from utilities import assistant, course_export, llm, logview, roster, storage
from utilities import knowledge_graph as kg_utils
from utilities import kg_text
from utilities import tags as tag_utils
from utilities.report import _overall_text, build_report_md, render_markdown
from utilities.uploads import (_student_files, _validate_upload, allowed_file, file_list,
                               save_upload)
from utilities.pdfgrade import (DEFAULT_GRADE_PROMPT, DEFAULT_REPORT_PROMPT,
                                DEFAULT_TAG_HORIZONTAL_PROMPT,
                                DEFAULT_TAG_VERTICAL_PROMPT,
                                ai_horizontal_analysis, ai_lecture_summary,
                                ai_report_comment, ai_vertical_analysis,
                                analyze_answer_pdf, analyze_question_tags,
                                grade_pdf_submission, pdf_page_count,
                                render_page_jpeg)

cfg.ensure_dirs()
cfg.setup_logging()

BASE_DIR = cfg.BASE_DIR
UPLOAD_DIR = cfg.STORAGE_DIR
BACKUP_DIR = cfg.BACKUP_DIR
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
    path = cfg.SECRET_KEY_FILE
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
os.makedirs(UPLOAD_DIR, exist_ok=True)


class SignedIdConverter(BaseConverter):
    """把内部自增 ID 签名为不可读的 URL 片段，路由内仍拿到 int。

    只做「不暴露可枚举 ID + 防篡改」，鉴权仍由各路由的归属校验负责；
    伪造或过期的 URL 直接 404。
    """

    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = r"[A-Za-z0-9_\-\.]+"

    def to_python(self, value):
        try:
            return int(_id_signer.loads(value))
        except BadSignature:
            raise ValidationError()

    def to_url(self, value):
        return _id_signer.dumps(int(value))


_id_signer = None   # 由 create_app() 按应用密钥初始化

log = logging.getLogger("gradelogy")

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
        "app_version_updated": APP_GIT_DATE,
        "app_python": platform.python_version(),
        "app_started": APP_STARTED_AT.strftime("%Y-%m-%d %H:%M"),
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
    """模板里直接用 {{ hw.summary_md|markdown_safe }} 渲染 Markdown 片段。"""
    if not md:
        return ""
    return render_markdown(md)


@_routes.template_filter("localtime")
def _localtime(value):
    """SQLite CURRENT_TIMESTAMP 存 UTC，展示时转成本机时区。"""
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def csrf_token():
    """为当前会话生成并复用 CSRF token；模板里用 csrf_token() 写入表单隐藏域。"""
    token = session.get("_csrf")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf"] = token
    return token


# 无需 CSRF 校验的端点：健康检查（只读）、静态资源
CSRF_EXEMPT = {"healthz", "static"}

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
        src = os.path.join(UPLOAD_DIR, rel)
        if not os.path.isfile(src):
            return name
        parts = [f"course_{course_id}", f"lecture_{lecture_id}"]
        if student_id:
            parts.append(f"student_{student_id}")
        target_dir = os.path.join(UPLOAD_DIR, *parts)
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


def init_db():
    """兼容入口：建表与迁移已收敛到 db.py，附件目录与提示词仍由本层提供。"""
    database.init_db(
        upload_migrator=_migrate_upload_layout,
        default_prompts=(("grade", "默认批改提示词", DEFAULT_GRADE_PROMPT),
                         ("report", "默认报告提示词", DEFAULT_REPORT_PROMPT),
                         ("tag_horizontal", "默认横向知识点分析提示词",
                          DEFAULT_TAG_HORIZONTAL_PROMPT),
                         ("tag_vertical", "默认纵向知识点分析提示词",
                          DEFAULT_TAG_VERTICAL_PROMPT)),
        default_passwords=DEFAULT_PASSWORDS)





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
    if request.endpoint in ("static", "healthz", "admin_logs_tail"):
        return resp
    started = getattr(g, "request_started", None)
    cost_ms = (time.perf_counter() - started) * 1000 if started else 0.0
    user = g.get("user")
    who = f"{user['username']}#{user['id']}" if user else "-"
    log.info("HTTP %s %s %s %.0fms user=%s ip=%s", request.method,
             request.path, resp.status_code, cost_ms, who, request.remote_addr or "-")
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
        "script-src 'self' 'unsafe-inline'; frame-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


# ---------- 登录与权限 ----------
@_routes.before_request
def load_user():
    uid = session.get("uid")
    g.user = None
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None or not g.user["active"]:
            # 账号被删除或停用后，已登录的会话立即失效
            session.clear()
            g.user = None


@_routes.before_request
def limit_body_size():
    """按端点限制请求体大小：恢复备份与知识图谱资料走大上限，其余用 MAX_UPLOAD_MB。

    必须排在 CSRF 校验之前：CSRF 会解析表单，超大请求应在解析前就拒绝。
    """
    if request.endpoint == "admin_restore":
        cap = cfg.MAX_RESTORE_MB
    elif request.endpoint == "knowledge_graph_upload":
        cap = cfg.KG_MAX_UPLOAD_MB
    else:
        cap = cfg.MAX_UPLOAD_MB
    if (request.content_length or 0) > cap * 1024 * 1024:
        abort(413, description=f"上传内容超过 {cap}MB 限制。")


@_routes.before_request
def csrf_protect():
    """所有写操作必须带本次会话的 CSRF token（表单隐藏域 _csrf 或请求头 X-CSRF-Token）。"""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.endpoint in CSRF_EXEMPT:
        return None
    expect = session.get("_csrf") or ""
    got = request.headers.get("X-CSRF-Token") or request.form.get("_csrf") or ""
    if not expect or not got or not secrets.compare_digest(expect, got):
        log.warning("CSRF 校验失败 method=%s path=%s ip=%s",
                    request.method, request.path, request.remote_addr)
        abort(400, description="页面已过期或请求来源不可信，请刷新页面后重试。")
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
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("login"))
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
    msg = getattr(e, "description", "") or "上传内容过大。"
    return render_template("error.html", code=413, msg=msg), 413


@_routes.errorhandler(403)
def forbidden(_):
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
            session["uid"] = user["id"]
            session.permanent = True  # 启用 8 小时空闲过期
            db = get_db()
            db.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (user["id"],))
            db.commit()
            log.info("登录成功 user=%s role=%s ip=%s",
                     username, user["role"], request.remote_addr or "-")
            nxt = _safe_next(request.args.get("next"))
            return redirect(nxt or url_for("index"))
        log.warning("登录失败 user=%s ip=%s", username or "-", request.remote_addr or "-")
        flash("用户名或密码错误")
    return render_template("login.html")


@_routes.route("/logout")
def logout():
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
            log.info("修改密码 user=%s#%s ip=%s", g.user["username"], g.user["id"],
                     request.remote_addr or "-")
            flash("密码已更新")
            return redirect(url_for("index"))
    return render_template("password.html")


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
    for item in logview.list_logs(cfg.LOG_DIR):
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
        result = logview.read_chunk(cfg.LOG_DIR, name, cursor)
    except FileNotFoundError:
        abort(404)
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@_routes.route("/admin/logs/download/<name>")
@role_required("admin")
def admin_logs_download(name):
    try:
        path = logview.resolve_log(cfg.LOG_DIR, name)
    except FileNotFoundError:
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
    db.execute("DELETE FROM enrollments WHERE student_id=?", (uid,))
    db.execute("DELETE FROM submissions WHERE student_id=?", (uid,))
    # 只解除共同管理关系；仍担任创建者的课程要转交，没人接手才整门删除
    db.execute("DELETE FROM course_teachers WHERE teacher_id=?", (uid,))
    for c in db.execute("SELECT id FROM courses WHERE teacher_id=?", (uid,)).fetchall():
        heir = db.execute("SELECT teacher_id FROM course_teachers WHERE course_id=? LIMIT 1",
                          (c["id"],)).fetchone()
        if heir:
            db.execute("UPDATE courses SET teacher_id=? WHERE id=?", (heir["teacher_id"], c["id"]))
            continue
        db.execute("DELETE FROM submissions WHERE lecture_id IN "
                   "(SELECT id FROM lectures WHERE course_id=?)", (c["id"],))
        db.execute("DELETE FROM lectures WHERE course_id=?", (c["id"],))
        db.execute("DELETE FROM exam_scores WHERE exam_id IN "
                   "(SELECT id FROM exams WHERE course_id=?)", (c["id"],))
        db.execute("DELETE FROM exams WHERE course_id=?", (c["id"],))
        db.execute("DELETE FROM enrollments WHERE course_id=?", (c["id"],))
        db.execute("DELETE FROM lesson_plans WHERE course_id=?", (c["id"],))
        db.execute("DELETE FROM courses WHERE id=?", (c["id"],))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
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
    return render_template("courses.html", courses=rows)


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
    if not os.path.isdir(cfg.EXPORT_DIR):
        return
    cutoff = time.time() - COURSE_EXPORT_KEEP_HOURS * 3600
    for name in os.listdir(cfg.EXPORT_DIR):
        path = os.path.join(cfg.EXPORT_DIR, name)
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
    if not os.path.isdir(cfg.EXPORT_DIR):
        return items
    for name in sorted(os.listdir(cfg.EXPORT_DIR), reverse=True):
        match = COURSE_EXPORT_RE.fullmatch(name)
        path = os.path.join(cfg.EXPORT_DIR, name)
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
    return render_template("course.html", course=course, students=students,
                           candidates=candidates, lectures=lectures, exams=exams,
                           plans=plans, teachers=teachers,
                           teacher_candidates=teacher_candidates,
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
    os.makedirs(cfg.EXPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name, n = f"course_{cid}_{stamp}.zip", 2
    path = os.path.join(cfg.EXPORT_DIR, name)
    while os.path.exists(path):
        name = f"course_{cid}_{stamp}_{n}.zip"
        path = os.path.join(cfg.EXPORT_DIR, name)
        n += 1
    try:
        written, missing = course_export.build_course_zip(
            path, course["name"], _course_export_lectures(cid), UPLOAD_DIR)
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
    path = os.path.join(cfg.EXPORT_DIR, name)
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
    db.execute("DELETE FROM submissions WHERE lecture_id IN "
               "(SELECT id FROM lectures WHERE course_id=?)", (cid,))
    for t in ("lectures", "enrollments", "lesson_plans", "course_teachers"):
        db.execute(f"DELETE FROM {t} WHERE course_id=?", (cid,))
    db.execute("DELETE FROM exam_scores WHERE exam_id IN (SELECT id FROM exams WHERE course_id=?)",
               (cid,))
    db.execute("DELETE FROM exams WHERE course_id=?", (cid,))
    db.execute("DELETE FROM courses WHERE id=?", (cid,))
    db.commit()
    return redirect(url_for("courses"))


@_routes.route("/course/<oid:cid>/enroll", methods=["POST"])
@role_required("teacher")
def course_enroll(cid):
    own_course(cid)
    sid = request.form.get("student_id")
    if sid:
        db = get_db()
        target = db.execute(
            "SELECT display_name FROM users WHERE id=? AND role='student' AND active=1",
            (sid,)).fetchone()
        if target:
            db.execute("INSERT OR IGNORE INTO enrollments(course_id,student_id) VALUES(?,?)",
                       (cid, sid))
            db.commit()
            flash(f"已把 {target['display_name']} 加入课程")
        else:
            flash("只能加入启用中的学生账号；找不到该学生或账号已停用")
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
    cur = db.execute("""
        DELETE FROM questions WHERE submission_id IN (
          SELECT s.id FROM submissions s JOIN lectures h ON h.id=s.lecture_id
          WHERE h.course_id=? AND s.student_id=?)""", (cid, sid))
    n_questions = cur.rowcount
    cur = db.execute("""
        DELETE FROM submissions WHERE student_id=? AND lecture_id IN
          (SELECT id FROM lectures WHERE course_id=?)""", (sid, cid))
    n_subs = cur.rowcount
    cur = db.execute("""
        DELETE FROM exam_scores WHERE student_id=? AND exam_id IN
          (SELECT id FROM exams WHERE course_id=?)""", (sid, cid))
    n_scores = cur.rowcount
    db.execute("DELETE FROM enrollments WHERE course_id=? AND student_id=?", (cid, sid))
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
                      request.form.get("deadline", ""))).lastrowid
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
    answer_total = pdf_page_count(os.path.join(UPLOAD_DIR, hw["answer_pdf"])) if hw["answer_pdf"] else 0
    answer_picked = _page_list(hw["answer_pages"])

    acc_by = _accuracy_by_submission(db, [s["id"] for s in subs])
    grade_jobs = {r["submission_id"]: r for r in db.execute(
        "SELECT o.submission_id,o.status,o.error FROM ai_operations o "
        "JOIN (SELECT submission_id,MAX(id) AS max_id FROM ai_operations "
        "WHERE operation_type='grade' GROUP BY submission_id) m ON m.max_id=o.id "
        "JOIN submissions s ON s.id=o.submission_id WHERE s.lecture_id=?", (hid,))}
    tag_by_key = tag_utils.lecture_tags_by_key(db, hid)
    tag_stats = tag_utils.lecture_stats(db, hid)
    return render_template("lecture.html", hw=hw, course=course, subs=subs, students=students,
                           qstats=qstats, n_subs=n_subs, n_graded=n_graded, n_reviewed=n_reviewed,
                           answer_total=answer_total, answer_picked=answer_picked,
                           grade_jobs=grade_jobs,
                           grading_active=any(j["status"] in ("queued", "running")
                                              for j in grade_jobs.values()),
                           acc_by=acc_by, tag_by_key=tag_by_key, tag_stats=tag_stats,
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
    db.execute("INSERT INTO submissions(lecture_id,student_id,file_path) VALUES(?,?,?)",
               (hid, student_id, stored))
    db.commit()
    student = db.execute("SELECT display_name FROM users WHERE id=?", (student_id,)).fetchone()
    log.info("老师代传作业 lecture=%s student=%s files=%s operator=%s",
             hid, student_id, len(names), g.user["username"])
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
                    request.form.get("deadline", ""), hid))
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
        db.execute("UPDATE lectures SET answer_pdf=?, answer_pages='' WHERE id=?", (name, hid))
        db.commit()
    return redirect(url_for("lecture_detail", hid=hid))


def _answer_pages_note(picked, total):
    """页码保存结果的提示文案，成功生成题目清单后会拼在前面。"""
    if not picked:
        return f"已清除页码限制：将发送教师版 PDF 的全部 {total} 页。"
    if len(picked) == total:
        return f"已保存页码：全部 {total} 页（与不限制相同）。"
    return f"已保存页码：{len(picked)}/{total} 页，只发送这些页。"


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
    """给作业范围内勾选的题目生成知识点标签，返回 (标签数, 错误提示)。

    失败只返回原因，不影响已保存的作业范围；调用方决定如何提示。
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
    operation = _start_ai_operation(
        g.user["id"], "question_tagging", model,
        course_id=hw["course_id"], lecture_id=hw["id"])
    try:
        rows, err = analyze_question_tags(
            os.path.join(UPLOAD_DIR, hw["answer_pdf"]), picked,
            api_url, api_key, model, pages=_page_list(hw["answer_pages"]),
            usage_hook=operation["usage_hook"])
    except Exception as e:  # noqa: BLE001 标签失败不影响作业范围本身
        _finish_ai_operation(operation, "failed", str(e))
        log.exception("生成知识点标签失败 lecture=%s", hw["id"])
        return 0, f"生成失败：{e}"
    _finish_ai_operation(operation, "failed" if err else "success", err)
    if err:
        return 0, err
    db.execute("DELETE FROM question_tags WHERE lecture_id=? AND source='ai'", (hw["id"],))
    count = 0
    for row in rows:
        for tag in row["tags"]:
            db.execute(
                "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,"
                "category,source) VALUES(?,?,?,?,?,'ai')",
                (hw["id"], row["key"], row["no"], tag, row["category"]))
            count += 1
    db.commit()
    log.info("生成知识点标签 lecture=%s 题数=%s 标签数=%s", hw["id"], len(rows), count)
    return count, ""


def _analyze_scope(hw, pages):
    """调用 AI 读取教师版答案并识别题目清单，返回 (题目, 错误, 用量文本)。"""
    teacher_id = g.user["id"]
    if get_setting(teacher_id, "ai_demo") == "1":
        return [], "演示模式不会调用 AI，无法生成题目清单；请到「AI 设置」关闭演示模式。", ""
    block = _offpeak_block_if_enabled(teacher_id)
    if block:
        return [], block, ""
    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    model = get_setting(teacher_id, "ai_model")
    if not api_url or not api_key or not model:
        return [], "尚未配置 AI 接口，无法生成题目清单。", ""

    log.info("分析作业范围 lecture=%s answer_pdf=%s pages=%s",
             hw["id"], hw["answer_pdf"], pages)
    operation = _start_ai_operation(
        teacher_id, "scope_analyze", model, course_id=hw["course_id"], lecture_id=hw["id"])
    try:
        questions, err = analyze_answer_pdf(
            os.path.join(UPLOAD_DIR, hw["answer_pdf"]), api_url, api_key, model,
            pages=pages, usage_hook=operation["usage_hook"])
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    _finish_ai_operation(operation, "failed" if err else "success", err)
    usage_text = _ai_usage_text(_ai_operation_history(operation_id=operation["id"], limit=1)[0])
    return questions, err, usage_text


@_routes.route("/lecture/<oid:hid>/answer/pages", methods=["POST"])
@role_required("teacher")
def lecture_answer_pages(hid):
    """保存教师要发送给模型的页码，并立即用这些页生成题目清单。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    total = pdf_page_count(os.path.join(UPLOAD_DIR, hw["answer_pdf"])) if hw["answer_pdf"] else 0
    if not total:
        flash("请先上传教师版标准答案 PDF，再选择页码。")
        return redirect(url_for("lecture_detail", hid=hid))
    picked = sorted({int(v) for v in request.form.getlist("page")
                     if v.isdigit() and 1 <= int(v) <= total})
    db.execute("UPDATE lectures SET answer_pages=? WHERE id=?",
               (",".join(str(p) for p in picked), hid))
    db.commit()
    note = _answer_pages_note(picked, total)
    try:
        questions, err, usage_text = _analyze_scope(hw, picked)
    except Exception as e:  # noqa: BLE001 页码已保存，分析失败只提示不回滚
        log.exception("生成题目清单失败 lecture=%s", hid)
        flash(f"{note} 生成题目清单失败：{e}")
        return redirect(url_for("lecture_detail", hid=hid))
    if err:
        flash(f"{note} {err}{'；' + usage_text if usage_text else ''}")
        return redirect(url_for("lecture_detail", hid=hid))
    db.execute("UPDATE lectures SET scope_candidates=?, scope_selected='' WHERE id=?",
               (json.dumps(questions, ensure_ascii=False), hid))
    db.commit()
    flash(f"{note} 已识别 {len(questions)} 道题，请勾选本讲需要批阅的题目。{usage_text}")
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
    data = render_page_jpeg(os.path.join(UPLOAD_DIR, hw["answer_pdf"]), no)
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
    if not keys:
        flash("请至少勾选一道题再保存；未划定作业范围时 AI 批阅不会执行。")
        return redirect(url_for("lecture_detail", hid=hid))
    db.execute("UPDATE lectures SET scope_selected=? WHERE id=?",
               (json.dumps(keys, ensure_ascii=False), hid))
    db.commit()
    # 保存范围时顺带为勾选的题目生成知识点标签，供后续横向/纵向薄弱点分析
    count, err = _generate_question_tags(hw, keys)
    if err:
        flash(f"已保存作业范围：{len(keys)} 道题；知识点标签未生成：{err}")
    else:
        flash(f"已保存作业范围：{len(keys)} 道题，已生成 {count} 个知识点标签")
    return redirect(url_for("lecture_detail", hid=hid))


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
    else:
        flash(f"已重新生成 {count} 个知识点标签")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/tag-analysis", methods=["POST"])
@role_required("teacher")
def lecture_tag_analysis(hid):
    """横向分析：把本讲各知识点标签与全班逐题判定结合，找共性问题。"""
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
    context = {
        "subject_and_course": _course_label(course),
        "lecture_name": hw["title"],
        "total_students": total_students,
        "aggregated_question_stats": question_stats,
    }
    prompt = _prompt_content("tag_horizontal", course["tag_horizontal_prompt_id"])
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        flash(f"知识点分析未生成：{err}")
        return redirect(url_for("lecture_detail", hid=hid))
    operation = _start_ai_operation(
        g.user["id"], "tag_horizontal", model,
        course_id=hw["course_id"], lecture_id=hid)
    try:
        result = ai_horizontal_analysis(context, api_url, api_key, model,
                                        prompt=prompt,
                                        usage_hook=operation["usage_hook"])
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    error = "" if result else "AI 未返回可用的分析结果，请稍后重试"
    _finish_ai_operation(operation, "success" if result else "failed", error)
    if not result:
        flash(error)
        return redirect(url_for("lecture_detail", hid=hid))
    md = tag_utils.render_horizontal_md(result)
    db.execute("INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,kind,"
               "content_md,stats_json) VALUES(?,?,?,'horizontal',?,?)",
               (g.user["id"], hw["course_id"], hid, md,
                json.dumps(question_stats, ensure_ascii=False)))
    db.commit()
    log.info("横向知识点分析完成 lecture=%s by=%s", hid, g.user["username"])
    flash("全班知识点分析已生成")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/summary/generate", methods=["POST"])
@role_required("teacher")
def lecture_summary_generate(hid):
    """AI 聚合全班逐题判定与评语，生成讲次 summary（每题要点 + 整体总结）。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    # 必须至少有一份 AI 批阅结果才有意义
    qcheck = db.execute("""
        SELECT COUNT(*) FROM questions q
        JOIN submissions s ON s.id=q.submission_id
        WHERE s.lecture_id=? AND s.status='已批改' AND q.verdict != ''""", (hid,)).fetchone()[0]
    if qcheck == 0:
        flash("还没有批改完成的作业，无法生成讲次汇总")
        return redirect(url_for("lecture_detail", hid=hid))
    # 聚合统计（与 lecture_detail 里一致）
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
    operation = _start_ai_operation(
        g.user["id"], "lecture_summary", model, course_id=hw["course_id"], lecture_id=hid)
    try:
        result = ai_lecture_summary(qstats, api_url, api_key, model, max_tokens=4096,
                                    usage_hook=operation["usage_hook"])
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    error = "" if result else "讲次汇总生成失败，请稍后重试或检查 AI 设置"
    _finish_ai_operation(operation, "success" if result else "failed", error)
    usage_text = _ai_usage_text(_ai_operation_history(operation_id=operation["id"], limit=1)[0])
    if not result:
        flash(f"{error}；{usage_text}")
        return redirect(url_for("lecture_detail", hid=hid))
    # 渲染成 Markdown 存库（per_question 字典按题号展开 + overall）
    per_q = result.get("per_question") or {}
    lines = ["## 每题要点\n"]
    for no in sorted(per_q.keys(), key=lambda x: (0 if x.replace(".", "").isdigit() else 1, str(x))):
        lines.append(f"- **第 {no} 题**：{per_q[no]}")
    lines.append("\n## 整体总结\n")
    lines.append(result.get("overall", ""))
    md = "\n".join(lines)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute("UPDATE lectures SET summary_md=?, summary_at=? WHERE id=?", (md, now, hid))
    db.commit()
    log.info("讲次 summary 生成成功 lecture=%s by=%s", hid, g.user["username"])
    flash(f"讲次汇总已更新。{usage_text}")
    return redirect(url_for("lecture_detail", hid=hid))


@_routes.route("/lecture/<oid:hid>/delete", methods=["POST"])
@role_required("teacher")
def lecture_delete(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    db.execute("DELETE FROM submissions WHERE lecture_id=?", (hid,))
    db.execute("DELETE FROM lectures WHERE id=?", (hid,))
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
        SELECT x.id, x.title, x.exam_date, x.full_score, s.score, s.comment
        FROM exams x LEFT JOIN exam_scores s ON s.exam_id=x.id AND s.student_id=?
        WHERE x.course_id=? ORDER BY x.id DESC""", (sid, cid)).fetchall()
    acc_by = _accuracy_by_submission(db, [h["last_sub_id"] for h in lectures])
    return render_template(
        "student_detail.html", course=course, student=student,
        lectures=lectures, exams=exams, acc_by=acc_by,
        weak_stats=tag_utils.student_stats(db, cid, sid),
        tag_analysis=tag_utils.latest_analysis(db, "vertical", student_id=sid))


@_routes.route("/course/<oid:cid>/student/<oid:sid>/tag-analysis", methods=["POST"])
@role_required("teacher")
def student_tag_analysis(cid, sid):
    """纵向分析：把该生跨讲次的错题按知识点标签聚合，找反复出错的知识点。"""
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
    context = {
        "lecture_count": len(trend),
        "subject_and_course": _course_label(course),
        "student_name": student["display_name"],
        "lecture_range": lecture_range,
        "module_mastery_stats": mastery,
        "kg_weakness_chains": (
            "系统暂未内置学科知识图谱；请仅依据同一或相近标签在不同讲次的重复出错"
            "推断前置基础与衍生知识点，不要编造数据中不存在的依赖关系。"),
        "error_habit_stats": tag_utils.habit_stats(db, cid, sid),
        "trend_data": trend,
    }
    prompt = _prompt_content("tag_vertical", course["tag_vertical_prompt_id"])
    api_url, api_key, model, err = _ai_endpoint(g.user["id"])
    if err:
        flash(f"纵向分析未生成：{err}")
        return redirect(url_for("student_detail", cid=cid, sid=sid))
    operation = _start_ai_operation(
        g.user["id"], "tag_vertical", model, course_id=cid)
    try:
        result = ai_vertical_analysis(context, api_url, api_key, model,
                                      prompt=prompt,
                                      usage_hook=operation["usage_hook"])
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    error = "" if result else "AI 未返回可用的分析结果，请稍后重试"
    _finish_ai_operation(operation, "success" if result else "failed", error)
    if not result:
        flash(error)
        return redirect(url_for("student_detail", cid=cid, sid=sid))
    md = tag_utils.render_vertical_md(result)
    db.execute("INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,student_id,"
               "kind,content_md,stats_json) VALUES(?,?,0,?,'vertical',?,?)",
               (g.user["id"], cid, sid, md,
                json.dumps({"mastery": mastery, "trend": trend,
                            "habits": context["error_habit_stats"]},
                           ensure_ascii=False)))
    db.commit()
    log.info("纵向知识点分析完成 course=%s student=%s by=%s",
             cid, sid, g.user["username"])
    flash("该生的纵向知识点分析已生成")
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
               s.score, s.comment
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
    for key in request.form:
        if not key.startswith("score_"):
            continue
        sid = key[len("score_"):]
        score = request.form.get(key, "").strip()
        comment = request.form.get(f"comment_{sid}", "").strip()
        if not score and not comment:
            db.execute("DELETE FROM exam_scores WHERE exam_id=? AND student_id=?", (eid, sid))
            continue
        db.execute(
            "INSERT INTO exam_scores(exam_id,student_id,score,comment,updated_at) "
            "VALUES(?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(exam_id,student_id) DO UPDATE SET score=excluded.score, "
            "comment=excluded.comment, updated_at=CURRENT_TIMESTAMP",
            (eid, sid, score, comment))
    db.commit()
    flash("成绩已保存，学生端立即可见")
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
def _save_grading_result(db, sid, gen):
    """把模型返回的逐题结果写入数据库；演示模式和后台任务共用。"""
    db.execute("DELETE FROM questions WHERE submission_id=?", (sid,))
    for i, q in enumerate(gen["questions"], 1):
        comment = (q.get("comment") or "").strip()
        db.execute(
            "INSERT INTO questions(submission_id,no,page_no,image_path,ocr_text,"
            "standard_answer,note,verdict,ai_comment) VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, q.get("no") or str(i), 1, "", q.get("ocr", ""),
             q.get("standard_answer", ""), comment, q.get("verdict", ""), comment))
    summary = gen.get("summary") or {}
    db.execute("UPDATE submissions SET status='待批改', ai_summary=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(summary, ensure_ascii=False) if summary else "", sid))
    db.commit()


def _run_grading_job(teacher_id, sid, operation):
    """后台批阅：不依赖任何浏览器请求，完成后直接把结果写入数据库。

    去重状态由 GradingQueue 在任务结束后统一清理。
    """
    with app.app_context():
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
                log.warning("后台批阅失败 submission=%s 原因=%s", sid, gen["error"])
                return
            _save_grading_result(db, sid, gen)
            _finish_ai_operation(operation, "success")
            try:
                generate_report(db, sid, teacher_id)
            except Exception:  # 报告生成失败不影响批阅结果本身
                log.exception("生成批改报告失败 submission=%s", sid)
            log.info("后台批阅完成 submission=%s 题数=%s", sid, len(gen["questions"]))
        except Exception as e:
            log.exception("后台批阅异常 submission=%s", sid)
            _finish_ai_operation(operation, "failed", str(e))


def _settings_for_service(teacher_id, key, default=""):
    """延迟解析模块全局 get_setting，测试中替换 app.get_setting 仍然生效。"""
    return get_setting(teacher_id, key, default)


def _offpeak_for_service(teacher_id):
    return _offpeak_block_if_enabled(teacher_id)


def _service_app_context():
    """延迟取当前 app 实例，便于 create_app() 在模块末尾才创建应用。"""
    return app.app_context()


_grading_queue = GradingQueue(
    get_db=get_db,
    get_setting=_settings_for_service,
    start_operation=_start_ai_operation,
    finish_operation=_finish_ai_operation,
    run_job=_run_grading_job,
    app_context=_service_app_context,
    offpeak_block=_offpeak_for_service,
)


def _enqueue_grading(teacher_id, sid, course_id, lecture_id):
    """统一入队入口：手动点击和定时扫描共用，返回 (operation, 错误提示)。"""
    return _grading_queue.enqueue(teacher_id, sid, course_id, lecture_id)


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
        _save_grading_result(db, sid, gen)
        try:
            generate_report(db, sid, g.user["id"])
        except Exception:
            log.exception("生成批改报告失败 submission=%s", sid)
        flash(f"演示模式：未调用 AI，已生成 {len(gen['questions'])} 道题，请逐题修正内容")
        return _back_to_page()

    _, err = _enqueue_grading(g.user["id"], sid, hw["course_id"], hw["id"])
    if err:
        flash(err)
        return _back_to_page()

    log.info("已加入批阅队列 submission=%s lecture=%s file=%s answer=%s pages=%s",
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


@_routes.route("/grading-queue")
@role_required("teacher")
def grading_queue():
    """教师导航栏的后台批改队列：只返回自己名下排队中/进行中的任务。"""
    rows = get_db().execute(
        "SELECT o.id,o.status,s.id AS submission_id,s.lecture_id,"
        "COALESCE(u.display_name,'未知学生') AS student_name,"
        "COALESCE(l.title,'未知讲次') AS lecture_title,"
        "COALESCE(c.name,'') AS course_name "
        "FROM ai_operations o LEFT JOIN submissions s ON s.id=o.submission_id "
        "LEFT JOIN users u ON u.id=s.student_id "
        "LEFT JOIN lectures l ON l.id=s.lecture_id "
        "LEFT JOIN courses c ON c.id=l.course_id "
        "WHERE o.teacher_id=? AND o.operation_type='grade' "
        "AND o.status IN ('queued','running') ORDER BY o.id", (g.user["id"],)).fetchall()
    jobs = [{
        "id": r["id"],
        "status": r["status"],
        "student": r["student_name"],
        "lecture": r["lecture_title"],
        "course": r["course_name"],
        "url": url_for("lecture_detail", hid=r["lecture_id"]) if r["lecture_id"] else "",
    } for r in rows]
    return jsonify({
        "jobs": jobs,
        "active": bool(jobs),
        "queued": sum(1 for j in jobs if j["status"] == "queued"),
        "running": sum(1 for j in jobs if j["status"] == "running"),
    })


def _auto_grade_scan():
    """扫描并自动入队待批阅作业，返回本次新入队数量。"""
    return _grading_queue.scan()


def start_auto_grade():
    """启动自动批阅线程；多进程部署时只让抢到文件锁的进程跑。"""
    _grading_queue.start(enabled=cfg.AUTO_GRADE, interval_hours=cfg.AUTO_GRADE_HOURS,
                         lock_dir=cfg.DATA_DIR)


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
    return render_template("review.html", sub=sub, hw=hw, course=course, questions=questions,
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
    db.execute("UPDATE submissions SET score='', feedback=?, status='已批改', grader=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (request.form.get("overall", "").strip(),
                f"{g.user['display_name']} 逐题批改", sid))
    db.commit()
    if (request.form.get("then") or "") == "report":
        pending = db.execute("SELECT COUNT(*) c FROM questions WHERE submission_id=? AND reviewed=0",
                             (sid,)).fetchone()["c"]
        if pending:
            flash(f"生成批阅报告前需要人工审核每一道题，还有 {pending} 题未审核")
            return redirect(url_for("submission_review", sid=sid))
    try:
        generate_report(db, sid, g.user["id"], reuse=True)
    except Exception:  # 报告刷新失败不影响批改保存
        log.exception("刷新批改报告失败 submission=%s", sid)
    if (request.form.get("then") or "") == "report":
        flash("批改已保存，报告已按最新结果刷新，可继续修改后发布。")
        return redirect(url_for("submission_report", sid=sid))
    flash("批改已保存，学生可以查看")
    return redirect(url_for("submission_review", sid=sid))


@_routes.route("/question/<int:qid>/reviewed", methods=["POST"])
@role_required("teacher")
def question_reviewed(qid):
    """单题的「已审核」标记，随点随存，供折起的题目显示审核进度。"""
    db = get_db()
    q = db.execute("""
        SELECT q.id, s.lecture_id FROM questions q
        JOIN submissions s ON s.id=q.submission_id WHERE q.id=?""", (qid,)).fetchone()
    if not q:
        abort(404)
    hw = db.execute("SELECT course_id FROM lectures WHERE id=?",
                    (q["lecture_id"],)).fetchone()
    own_course(hw["course_id"])
    reviewed = 1 if (request.get_json(silent=True) or {}).get("reviewed") else 0
    db.execute("UPDATE questions SET reviewed=? WHERE id=?", (reviewed, qid))
    db.commit()
    return {"ok": True, "reviewed": reviewed}


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
    return render_template("prompts.html", defaults=defaults, mine=mine,
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


# ---------- 老师：学科知识图谱 ----------
def _kg_graph(gid, edit=False):
    """图谱按学科共享：所有老师可查看，仅创建者与管理员可修改。"""
    row = get_db().execute("SELECT * FROM knowledge_graphs WHERE id=?",
                           (gid,)).fetchone()
    if not row:
        abort(404)
    if edit and g.user["role"] != "admin" and row["created_by"] != g.user["id"]:
        abort(403)
    return row


# 多份资料集中上传时串行建图，避免同时打满模型限流
_kg_build_lock = threading.Lock()


def _kg_start_build(graph_id, material_id, teacher_id):
    """后台构建图谱：请求返回后继续跑，资料状态可刷新查看。"""
    threading.Thread(target=_kg_build_worker,
                     args=(graph_id, material_id, teacher_id), daemon=True).start()


def _kg_build_worker(graph_id, material_id, teacher_id):
    with _kg_build_lock:
        with app.app_context():
            db = get_db()
            try:
                api_url, api_key, model, err = _ai_endpoint(teacher_id)
                if err:
                    db.execute("UPDATE kg_materials SET status=?, error=? WHERE id=?",
                               (kg_utils.STATUS_FAILED, err, material_id))
                    db.commit()
                    return
                operation = _start_ai_operation(teacher_id, "kg_build", model)
                try:
                    result = kg_utils.build_material(
                        db, material_id, api_url, api_key, model,
                        usage_hook=operation["usage_hook"])
                except Exception as e:  # noqa: BLE001 后台任务失败写入资料状态
                    _finish_ai_operation(operation, "failed", str(e))
                    db.execute("UPDATE kg_materials SET status=?, error=? WHERE id=?",
                               (kg_utils.STATUS_FAILED, str(e)[:500], material_id))
                    db.commit()
                    log.exception("知识图谱构建失败 material=%s", material_id)
                    return
                if result:
                    _finish_ai_operation(operation, "success", "")
                else:
                    row = db.execute("SELECT error FROM kg_materials WHERE id=?",
                                     (material_id,)).fetchone()
                    _finish_ai_operation(operation, "failed", row["error"] if row else "")
            finally:
                close_db()


@_routes.route("/knowledge-graphs")
@role_required("teacher", "admin")
def knowledge_graphs():
    rows = get_db().execute("""
        SELECT g.*,
          (SELECT COUNT(*) FROM kg_materials m WHERE m.graph_id=g.id) AS n_materials,
          (SELECT COUNT(*) FROM kg_nodes n WHERE n.graph_id=g.id) AS n_nodes,
          (SELECT COUNT(*) FROM kg_edges e WHERE e.graph_id=g.id) AS n_edges
        FROM knowledge_graphs g ORDER BY g.subject, g.name""").fetchall()
    return render_template("knowledge_graphs.html", graphs=rows)


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
    log.info("新建知识图谱 user=%s#%s subject=%s name=%s",
             g.user["username"], g.user["id"], subject, name)
    flash(f"已创建知识图谱「{name}」，接下来上传教材、考纲或讲义来构建它")
    return redirect(url_for("knowledge_graph_detail", gid=cur.lastrowid))


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


@_routes.route("/knowledge-graphs/<oid:gid>")
@role_required("teacher", "admin")
def knowledge_graph_detail(gid):
    graph = _kg_graph(gid)
    db = get_db()
    materials = db.execute("SELECT * FROM kg_materials WHERE graph_id=? "
                           "ORDER BY id DESC", (gid,)).fetchall()
    building = any(m["status"] in (kg_utils.STATUS_PENDING, kg_utils.STATUS_BUILDING)
                   for m in materials)
    return render_template(
        "knowledge_graph.html", graph=graph, materials=materials,
        counts=kg_utils.graph_counts(db, gid),
        status_counts=_kg_status_counts(materials),
        nodes=kg_utils.list_nodes(db, gid), edges=kg_utils.list_edges(db, gid),
        building=building, relation_label=kg_utils.RELATION_LABEL,
        layered=kg_utils.layered_graph(db, gid),
        max_upload_mb=cfg.KG_MAX_UPLOAD_MB,
        can_edit=g.user["role"] == "admin" or graph["created_by"] == g.user["id"])


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
    materials = db.execute("SELECT * FROM kg_materials WHERE graph_id=? "
                           "ORDER BY id DESC", (gid,)).fetchall()
    view = [_kg_status_view(item["status"]) for item in materials]
    return jsonify({
        "counts": kg_utils.graph_counts(db, gid),
        "status_counts": _kg_status_counts(materials),
        "building": any(v["style"] in ("pending", "building") for v in view),
        "materials": [{
            "id": item["id"],
            "label": v["label"],
            "style": v["style"],
            "icon": v["icon"],
            "error": item["error"] or "",
            "page_count": item["page_count"] or 0,
            "char_count": item["char_count"] or 0,
        } for item, v in zip(materials, view)],
    })


@_routes.route("/knowledge-graphs/<oid:gid>/materials", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_upload(gid):
    _kg_graph(gid, edit=True)
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("请选择要上传的资料文件")
        return redirect(url_for("knowledge_graph_detail", gid=gid))
    db = get_db()
    abs_dir = os.path.join(UPLOAD_DIR, "kg", str(gid))
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
                if size > cfg.KG_MAX_UPLOAD_MB * 1024 * 1024:
                    too_big = True
                    break
                digest.update(chunk)
                fh.write(chunk)
        if too_big:
            os.remove(abs_path)
            flash(f"「{filename}」超过 {cfg.KG_MAX_UPLOAD_MB}MB，已跳过")
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
        flash(f"「{filename}」已解析 {chunks} 个文本块，已加入建图队列")
    if queued > 1:
        flash(f"共 {queued} 份资料已排队，将逐个建图，可在下方查看每份状态")
    if queued:
        db.execute("UPDATE knowledge_graphs SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (gid,))
        db.commit()
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/materials/<oid:mid>/delete", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_material_delete(mid):
    db = get_db()
    material = db.execute("SELECT * FROM kg_materials WHERE id=?", (mid,)).fetchone()
    if not material:
        abort(404)
    _kg_graph(material["graph_id"], edit=True)
    try:
        os.remove(os.path.join(UPLOAD_DIR, material["stored_name"]))
    except OSError:
        pass
    kg_utils.delete_material(db, material)
    flash(f"已删除资料「{material['filename']}」及其独有节点")
    return redirect(url_for("knowledge_graph_detail", gid=material["graph_id"]))


@_routes.route("/knowledge-graphs/<oid:gid>/edit", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_edit(gid):
    """修改图谱名称与说明；学科与资料范围保持不变。"""
    _kg_graph(gid, edit=True)
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
    log.info("编辑知识图谱 user=%s#%s id=%s name=%s",
             g.user["username"], g.user["id"], gid, name)
    flash("已更新图谱信息")
    return redirect(url_for("knowledge_graph_detail", gid=gid))


@_routes.route("/knowledge-graphs/<oid:gid>/delete", methods=["POST"])
@role_required("teacher", "admin")
def knowledge_graph_delete(gid):
    graph = _kg_graph(gid, edit=True)
    db = get_db()
    db.execute("DELETE FROM kg_chunks WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_edges WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_nodes WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM kg_materials WHERE graph_id=?", (gid,))
    db.execute("DELETE FROM knowledge_graphs WHERE id=?", (gid,))
    db.commit()
    shutil.rmtree(os.path.join(UPLOAD_DIR, "kg", str(gid)), ignore_errors=True)
    log.info("删除知识图谱 user=%s#%s id=%s name=%s",
             g.user["username"], g.user["id"], gid, graph["name"])
    flash(f"已删除知识图谱「{graph['name']}」")
    return redirect(url_for("knowledge_graphs"))


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
        SELECT h.*, c.name AS course_name FROM lectures h
        JOIN courses c ON c.id=h.course_id WHERE h.id=?""", (hid,)).fetchone()
    if not hw:
        abort(404)
    ok = db.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?",
                    (hw["course_id"], g.user["id"])).fetchone()
    if not ok:
        abort(403)
    return hw


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
    by_course = {}
    for h in lectures:
        by_course.setdefault(h["course_id"], []).append(h)
    # 每个课程一个折叠块：作业未提交数 = 还没有任何一次提交的讲次数
    course_groups = [{
        "course": c,
        "lectures": by_course.get(c["id"], []),
        "pending": sum(1 for h in by_course.get(c["id"], []) if not h["my_subs"]),
    } for c in courses]
    exams = db.execute("""
        SELECT x.*, c.name AS course_name, s.score, s.comment
        FROM exams x
        JOIN courses c ON c.id=x.course_id
        JOIN enrollments e ON e.course_id=c.id AND e.student_id=?
        LEFT JOIN exam_scores s ON s.exam_id=x.id AND s.student_id=?
        ORDER BY x.id DESC""", (g.user["id"], g.user["id"])).fetchall()
    return render_template("student_home.html", course_groups=course_groups,
                           exams=exams, acc_by=acc_by)


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
            db.execute("INSERT INTO submissions(lecture_id,student_id,file_path) VALUES(?,?,?)",
                       (hid, g.user["id"], stored))
            db.commit()
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
    return render_template("student_lecture.html", hw=hw, subs=subs, items=items,
                           revealed=revealed)


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
    student_paths = [os.path.join(UPLOAD_DIR, p) for p in
                     (file_name if isinstance(file_name, (list, tuple)) else [file_name])]
    gen = grade_pdf_submission(
        student_paths,
        os.path.join(UPLOAD_DIR, answer_pdf) if answer_pdf else "",
        api_url, api_key, model,
        extra=extra, answer_pages=answer_pages, prompt=prompt, usage_hook=usage_hook)
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

def generate_report(db, sub_id, teacher_id, reuse=False):
    """按当前逐题结果生成/刷新一份 Markdown 批改报告，返回报告文本。

    reuse=True 时沿用上次由 AI 生成的评价段落，只重算表格与统计（保存批改时不额外调模型）。
    """
    sub = db.execute("""
        SELECT s.*, u.display_name, l.title AS lecture_title, l.course_id
        FROM submissions s JOIN users u ON u.id=s.student_id
        JOIN lectures l ON l.id=s.lecture_id WHERE s.id=?""", (sub_id,)).fetchone()
    if not sub:
        return ""
    questions = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (sub_id,)).fetchall()
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
                                "JOIN lectures l ON l.id=c.id WHERE l.id=?",
                                (sub["lecture_id"],)).fetchone()
            if api_url and api_key and model:
                operation = _start_ai_operation(
                    teacher_id, "report_generate", model, course_id=sub["course_id"],
                    lecture_id=sub["lecture_id"], submission_id=sub_id)
                try:
                    detail = ai_report_comment(
                        questions, summary.get("weak_points") or [],
                        summary.get("evaluation", ""), api_url, api_key, model,
                        prompt=_prompt_content("report", course["report_prompt_id"] if course else 0),
                        usage_hook=operation["usage_hook"])
                except Exception as e:
                    _finish_ai_operation(operation, "failed", str(e))
                    raise
                error = "" if detail else "AI 未返回可用的报告评价"
                _finish_ai_operation(operation, "success" if detail else "failed", error)
    try:
        stamp = datetime.strptime(str(sub["created_at"])[:10], "%Y-%m-%d")
        date_label = f"{stamp.month}月{stamp.day}日"
    except (TypeError, ValueError):
        date_label = ""
    md = build_report_md(sub["display_name"], sub["lecture_title"], date_label,
                         questions, summary, detail)
    db.execute("UPDATE submissions SET report_md=?, report_detail=? WHERE id=?",
               (md, json.dumps(detail, ensure_ascii=False) if detail else "", sub_id))
    db.commit()
    return md


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
    return render_template("report.html", sub=sub, hw=hw, course=course, md=md,
                           body=render_markdown(md), student_name=stu["display_name"],
                           is_teacher=True,
                           ai_operations=_ai_operation_history(submission_id=sid, limit=5))


@_routes.route("/submission/<oid:sid>/report/build", methods=["POST"])
@role_required("teacher")
def submission_report_build(sid):
    sub, hw, _ = own_submission(sid)
    generate_report(get_db(), sid, g.user["id"])
    flash("批改报告已生成，确认无误后发布给学生。")
    return redirect(url_for("submission_report", sid=sid))


@_routes.route("/submission/<oid:sid>/report/publish", methods=["POST"])
@role_required("teacher")
def submission_report_publish(sid):
    sub, hw, _ = own_submission(sid)
    db = get_db()
    if not sub["report_md"]:
        generate_report(db, sid, g.user["id"])
    publish = 0 if sub["report_published"] else 1
    db.execute("UPDATE submissions SET report_published=? WHERE id=?", (publish, sid))
    db.commit()
    flash("报告已发布，学生可在「我的学习」里查看。" if publish else "报告已撤回，学生不再可见。")
    return redirect(url_for("submission_report", sid=sid))


@_routes.route("/submission/<oid:sid>/report/comment", methods=["POST"])
@role_required("teacher")
def submission_report_comment(sid):
    """全部题目复核完成后，用 AI 重写一份总评，回填到逐题核对页的文本框继续人工修改。"""
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
    operation = _start_ai_operation(
        g.user["id"], "report_comment", model, course_id=course["id"],
        lecture_id=hw["id"], submission_id=sid)
    try:
        detail = ai_report_comment(questions, summary.get("weak_points") or [],
                                   summary.get("evaluation", ""), api_url, api_key, model,
                                   prompt=_prompt_content("report", course["report_prompt_id"]),
                                   usage_hook=operation["usage_hook"])
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    error = "" if detail else "AI 生成总评失败，请稍后重试"
    _finish_ai_operation(operation, "success" if detail else "failed", error)
    usage_text = _ai_usage_text(_ai_operation_history(operation_id=operation["id"], limit=1)[0])
    if not detail:
        return {"ok": False, "error": error, "usage": usage_text}, 502
    db.execute("UPDATE submissions SET report_detail=? WHERE id=?",
               (json.dumps(detail, ensure_ascii=False), sid))
    db.commit()
    return {"ok": True, "text": _overall_text(detail, summary.get("evaluation", "")),
            "usage": usage_text}


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
    return render_template("report.html", sub=sub, hw=hw, course=course, md=sub["report_md"],
                           body=render_markdown(sub["report_md"]),
                           student_name=g.user["display_name"], is_teacher=False)


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
                if r[col] and not os.path.exists(os.path.join(UPLOAD_DIR, r[col]))]

    add("orphan_questions", "error", "题目记录指向已删除的提交",
        [(r["id"],) for r in db.execute(
            "SELECT q.id FROM questions q LEFT JOIN submissions s ON s.id=q.submission_id "
            "WHERE s.id IS NULL")], fixable=True, detail="可安全删除")

    add("missing_submission_file", "error", "提交记录对应的学生文件已丢失",
        [(r["id"],) for r in db.execute("SELECT id, file_path FROM submissions").fetchall()
         if file_list(r["file_path"])
         and not all(os.path.exists(os.path.join(UPLOAD_DIR, p))
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
    if os.path.isdir(BACKUP_DIR):
        for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
            path = os.path.join(BACKUP_DIR, name)
            if BACKUP_RE.fullmatch(name):
                backups.append({
                    "name": name,
                    "size": storage.human_size(os.path.getsize(path)),
                    "mtime": datetime.fromtimestamp(os.path.getmtime(path))
                    .strftime("%Y-%m-%d %H:%M")})
    return render_template("consistency.html", issues=_consistency_issues(), stats=stats,
                           data_dir=cfg.DATA_DIR, db_path=DB_PATH, backups=backups,
                           auto_backup=cfg.AUTO_BACKUP, backup_keep=cfg.BACKUP_KEEP)


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
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(snap.name)
    try:
        with dst:
            src.backup(dst)  # 在线备份，避免 WAL 下拷到半截数据
    finally:
        src.close()
        dst.close()

    con = sqlite3.connect(DB_PATH)
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("users", "courses", "lectures", "exams", "submissions",
                            "questions", "knowledge_graphs", "kg_materials",
                            "kg_nodes", "kg_edges")}
    finally:
        con.close()
    manifest = {"app": "GradeLogy", "version": cfg.APP_VERSION,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "counts": counts}
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap.name, "data.sqlite")
            for root, _, files in os.walk(UPLOAD_DIR):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.join("uploads", os.path.relpath(full, UPLOAD_DIR)))
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        os.remove(snap.name)
    return manifest


def _prune_backups():
    """只保留最近 BACKUP_KEEP 份自动/手动备份（不含恢复前的 pre_restore 快照）。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    items = sorted((os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR)
                    if BACKUP_RE.fullmatch(n)), key=os.path.getmtime, reverse=True)
    for path in items[cfg.BACKUP_KEEP:]:
        try:
            os.remove(path)
        except OSError:
            pass
    return items[:cfg.BACKUP_KEEP]


def _run_auto_backup():
    """当天还没有备份就生成一份；命名为 gradelogy_YYYYMMDD_HHMMSS.zip。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    if any(n.startswith(f"gradelogy_{today}_") for n in os.listdir(BACKUP_DIR)):
        return None
    path = os.path.join(BACKUP_DIR, datetime.now().strftime("gradelogy_%Y%m%d_%H%M%S.zip"))
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
    if _backup_started or not cfg.AUTO_BACKUP:
        return
    _backup_started = True
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if fcntl is not None:
            _backup_lock = open(os.path.join(BACKUP_DIR, ".auto.lock"), "w")
            fcntl.flock(_backup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("自动备份未启动：另一个进程已在执行")
        return
    threading.Thread(target=_backup_loop, daemon=True, name="auto-backup").start()
    log.info("自动备份已启用：每 30 分钟检查一次，保留最近 %s 份", cfg.BACKUP_KEEP)


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
    path = os.path.join(BACKUP_DIR, datetime.now().strftime("gradelogy_%Y%m%d_%H%M%S.zip"))
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
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


def _extract_backup_zip(zpath, extract_root):
    """校验 zip 路径安全 / 数据库结构 / 完整性后解压，返回 (src_db, 错误提示)。"""
    if not zipfile.is_zipfile(zpath):
        return None, "不是有效的 zip 备份包"
    os.makedirs(extract_root, exist_ok=True)
    limit = cfg.MAX_RESTORE_MB * 1024 * 1024
    total = 0
    with zipfile.ZipFile(zpath) as z:
        members = z.infolist()
        if len(members) > 50000:
            return None, "备份包文件数量异常，已拒绝导入"
        for info in members:
            target = os.path.abspath(os.path.join(extract_root, info.filename))
            if target != extract_root and not target.startswith(extract_root + os.sep):
                log.warning("备份包含越界路径：%s", info.filename)
                return None, "备份包包含非法路径，已拒绝导入"
            total += info.file_size
            if total > limit:
                return None, f"备份包解压后超过 {cfg.MAX_RESTORE_MB}MB 上限，已拒绝导入"
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
    bdir = os.path.join(cfg.DATA_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    cur = sqlite3.connect(DB_PATH, timeout=10)
    bk = sqlite3.connect(os.path.join(bdir, f"pre_restore_{ts}.sqlite"))
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        cur.backup(bk)
        src.backup(cur)  # 用 backup API 覆盖，避免替换文件句柄影响正在处理的请求
    finally:
        src.close()
        bk.close()
        cur.close()

    # 附件整体替换：旧目录先移入备份，再把包内的 uploads 放回原位
    if os.path.isdir(UPLOAD_DIR):
        shutil.move(UPLOAD_DIR, os.path.join(bdir, f"uploads_{ts}"))
    new_uploads = os.path.join(extract_root, "uploads")
    if os.path.isdir(new_uploads):
        shutil.move(new_uploads, UPLOAD_DIR)
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

    tmpdir = tempfile.mkdtemp(prefix=".restore_", dir=cfg.DATA_DIR)
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
        flash(f"恢复失败，当前数据已备份到 {os.path.join(cfg.DATA_DIR, 'backups')}，请检查磁盘与权限")
        return redirect(url_for("consistency_page"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 初始化导入向导（仅全新空系统可用） ----------
SETUP_TMP_ROOT = os.path.join(cfg.DATA_DIR, "tmp_setup")


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
    root = os.path.abspath(SETUP_TMP_ROOT)
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
    os.makedirs(SETUP_TMP_ROOT, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="import_", dir=SETUP_TMP_ROOT)
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
    if not os.path.abspath(tmpdir).startswith(os.path.abspath(SETUP_TMP_ROOT) + os.sep):
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
    if g.user["role"] == "student":
        wanted = os.path.normpath(filename).replace("\\", "/")
        if wanted not in _student_files(g.user["id"]):
            log.warning("越权访问文件 student=%s path=%s", g.user["id"], wanted)
            abort(403)
    full = os.path.abspath(os.path.join(UPLOAD_DIR, filename))
    if not full.startswith(os.path.abspath(UPLOAD_DIR) + os.sep):
        abort(404)
    # 手机端 PDF 预览：iframe 在 iOS 上只能显示第一页，改由服务端逐页渲染成图片
    if request.args.get("meta"):
        return jsonify(pages=pdf_page_count(full))
    page = request.args.get("page", type=int)
    if page:
        data = render_page_jpeg(full, page, scale=1.8, quality=80)
        if not data:
            abort(404)
        return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=3600)
    return send_from_directory(UPLOAD_DIR, filename)


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
    checks["storage"] = "ok" if os.access(UPLOAD_DIR, os.W_OK) else "readonly"
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


def create_app():
    """应用工厂：集中创建 Flask 实例，并挂载路由、请求钩子与模板扩展。"""
    global _id_signer
    app = Flask(__name__)
    app.config["SECRET_KEY"] = cfg.SECRET_KEY or load_secret_key()
    app.config["MAX_CONTENT_LENGTH"] = max(
        cfg.MAX_UPLOAD_MB, cfg.MAX_RESTORE_MB, cfg.KG_MAX_UPLOAD_MB) * 1024 * 1024
    # Cookie 加固：禁止 JS 读取；SameSite 阻断跨站携带；HTTPS 部署时用 COOKIE_SECURE=1 打开 Secure
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = cfg.COOKIE_SECURE
    # 会话空闲 8 小时过期：每次请求自动续期，长时间不操作需重新登录
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    app.teardown_appcontext(close_db)

    _id_signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="gradelogy-url-id")
    app.url_map.converters["oid"] = SignedIdConverter

    ai_routes.register(_routes, _ai_route_deps())
    _routes.apply(app)

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


# 模块导入时即完成建表与迁移，gunicorn 直接加载 app:app 也能拿到完整 schema
app = create_app()
init_db()
start_auto_backup()
start_model_sync()
start_auto_grade()


if __name__ == "__main__":
    host = cfg.HOST
    port = cfg.PORT
    ip = _lan_ip()
    print("\n  本机访问：http://127.0.0.1:%d" % port)
    if ip:
        print("  手机/其他电脑（同一 WiFi）：http://%s:%d\n" % (ip, port))
    # debug=False 避免 stat watcher 触发代码重载把长耗时批改请求掐掉
    app.run(host=host, port=port, debug=False)
