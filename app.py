# -*- coding: utf-8 -*-
"""教学管理平台
结构：课程 → 讲次（作业 + AI 批阅）/ 考试（只录分数）
角色：admin（用户管理）/ teacher（课程、讲次、考试、批改、AI 设置）/ student（提交与查看批改）
"""
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
from werkzeug.utils import secure_filename

import config as cfg
from utilities import assistant, course_export, llm, logview, roster, storage
from utilities.pdfgrade import (DEFAULT_GRADE_PROMPT, DEFAULT_REPORT_PROMPT,
                                ai_lecture_summary, ai_report_comment,
                                analyze_answer_pdf, grade_pdf_submission,
                                pdf_page_count, render_page_jpeg)

cfg.ensure_dirs()
cfg.setup_logging()

BASE_DIR = cfg.BASE_DIR
UPLOAD_DIR = cfg.STORAGE_DIR
DB_PATH = cfg.DB_PATH
BACKUP_DIR = cfg.BACKUP_DIR
BACKUP_RE = re.compile(r"gradelogy_\d{8}_\d{6}\.zip")
# 课程备份包：course_<课程ID>_<生成时间>[_序号].zip，保留 24 小时
COURSE_EXPORT_RE = re.compile(r"course_(\d+)_(\d{8}_\d{6})(?:_(\d+))?\.zip")
COURSE_EXPORT_KEEP_HOURS = 24
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底，避免 HTTPS 调用 AI 时报 SSL 错误
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

ROLE_LABEL = {"admin": "管理员", "teacher": "老师", "student": "学生"}

# 腾讯云 TokenHub 常用的、支持图片理解的模型（价格为 2025.11 公开价目，单位：元/百万 token；
# 高峰时段（通常 9:00-12:00 / 14:00-18:00）大约翻倍，仅供参考）
MODEL_PRESETS = [
    {
        "model": "deepseek-v4.1-flash",
        "vendor": "DeepSeek",
        "tags": ["图片理解", "1M 上下文", "性价比高"],
        "price_in": 1.0,
        "price_out": 4.0,
        "note": "vision-exp 的继任者，与旧版同价，能力全面升级",
    },
    {
        "model": "glm-5.3-flash",
        "vendor": "智谱",
        "tags": ["图片理解", "最便宜", "128k 上下文"],
        "price_in": 0.8,
        "price_out": 2.8,
        "note": "TokenHub 上价格最低的多模态模型",
    },
    {
        "model": "minimax-m3.1-flash",
        "vendor": "MiniMax",
        "tags": ["图片理解", "1M 上下文"],
        "price_in": 1.0,
        "price_out": 4.0,
        "note": "与 DeepSeek-V4.1-Flash 同价，1M 上下文",
    },
    {
        "model": "deepseek-v4-pro",
        "vendor": "DeepSeek",
        "tags": ["图片理解", "旗舰", "1M 上下文"],
        "price_in": 4.5,
        "price_out": 13.5,
        "note": "贵 4.5 倍，推理能力更强，但批阅速度相近",
    },
    {
        "model": "kimi-k3.1-pro",
        "vendor": "Kimi (月之暗面)",
        "tags": ["图片+视频", "旗舰", "1M 上下文"],
        "price_in": 13.0,
        "price_out": 68.0,
        "note": "最贵，支持视频输入，一般作业批阅用不上",
    },
]

# 一份典型作业（约 26 页图片 + 答案）的估算 token 量
# 144dpi 的 A4 页经视觉模型切块后每页约 2000-2500 token
EST_TOKENS_INPUT = 60000   # 输入：学生卷 + 答案约 46 页 × 约 1300 token/页
EST_TOKENS_OUTPUT = 4000   # 输出：逐题 JSON + 评语

DEFAULT_PASSWORDS = ("admin123", "123456")


# ---------- 闲时批阅：TokenHub 高峰时段价格翻倍 ----------
# 高峰定义（参考腾讯云公开价目说明）：工作日 9:00-12:00, 14:00-18:00
# 闲时窗口：非高峰（包括所有夜间、早高峰前、午间午休、以及周六周日全天）
# 如果当前不在闲时且老师开启了「仅闲时批阅」，则拦截 AI 调用
PEAK_RANGES = [  # (start_hour, start_minute, end_hour, end_minute)，24 小时制
    (9, 0, 12, 0),
    (14, 0, 18, 0),
]


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


app = Flask(__name__)
app.config["SECRET_KEY"] = cfg.SECRET_KEY or load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = max(cfg.MAX_UPLOAD_MB, cfg.MAX_RESTORE_MB) * 1024 * 1024
# Cookie 加固：禁止 JS 读取；SameSite 阻断跨站携带；HTTPS 部署时用 COOKIE_SECURE=1 打开 Secure
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = cfg.COOKIE_SECURE
# 会话空闲 8 小时过期：每次请求自动续期，长时间不操作需重新登录
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
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


_id_signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="gradelogy-url-id")
app.url_map.converters["oid"] = SignedIdConverter

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


@app.context_processor
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


app.jinja_env.filters["fromjson"] = _json_or_none


def file_list(value):
    """submissions.file_path 兼容单文件与 JSON 数组（一次提交多张图片/多页）。"""
    if not value:
        return []
    text = str(value)
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return [text]
        return [str(p) for p in data if p] if isinstance(data, list) else [text]
    return [text]


app.jinja_env.filters["file_list"] = file_list


def accuracy_text(n_right, n_partial, n_wrong):
    """作业正确率 = 完全答对题数 / 已判定题数；半对、无法判定不计为正确。"""
    judged = (n_right or 0) + (n_partial or 0) + (n_wrong or 0)
    if not judged:
        return ""
    return f"{round(n_right * 100 / judged)}%"


app.jinja_env.globals["accuracy"] = accuracy_text


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


app.jinja_env.filters["markdown_safe"] = _md_safe


@app.template_filter("localtime")
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


app.jinja_env.globals["csrf_token"] = csrf_token

# 无需 CSRF 校验的端点：健康检查（只读）、静态资源
CSRF_EXEMPT = {"healthz", "static"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
  student_no TEXT DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  last_login_at TEXT DEFAULT '',
  must_change_password INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  teacher_id INTEGER NOT NULL REFERENCES users(id),
  grade_prompt_id INTEGER DEFAULT 0,
  report_prompt_id INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS enrollments(
  course_id INTEGER NOT NULL REFERENCES courses(id),
  student_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(course_id, student_id));
CREATE TABLE IF NOT EXISTS course_teachers(
  course_id INTEGER NOT NULL REFERENCES courses(id),
  teacher_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(course_id, teacher_id));
CREATE TABLE IF NOT EXISTS lectures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  deadline TEXT DEFAULT '',
  answer_pdf TEXT DEFAULT '',
  answer_pages TEXT DEFAULT '',
  student_pdf TEXT DEFAULT '',
  scope_candidates TEXT DEFAULT '',
  scope_selected TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS exams(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  exam_date TEXT DEFAULT '',
  full_score REAL DEFAULT 100,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS exam_scores(
  exam_id INTEGER NOT NULL REFERENCES exams(id),
  student_id INTEGER NOT NULL REFERENCES users(id),
  score TEXT DEFAULT '',
  comment TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(exam_id, student_id));
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lecture_id INTEGER NOT NULL REFERENCES lectures(id),
  student_id INTEGER NOT NULL REFERENCES users(id),
  file_path TEXT NOT NULL,
  status TEXT DEFAULT '已提交',
  score TEXT DEFAULT '',
  feedback TEXT DEFAULT '',
  grader TEXT DEFAULT '',
  ai_summary TEXT DEFAULT '',
  report_md TEXT DEFAULT '',
  report_detail TEXT DEFAULT '',
  report_published INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER NOT NULL REFERENCES submissions(id),
  no TEXT DEFAULT '',
  page_no INTEGER DEFAULT 1,
  image_path TEXT DEFAULT '',
  ocr_text TEXT DEFAULT '',
  standard_answer TEXT DEFAULT '',
  score TEXT DEFAULT '',
  note TEXT DEFAULT '',
  max_score REAL DEFAULT 0,
  verdict TEXT DEFAULT '',
  ai_comment TEXT DEFAULT '',
  reviewed INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lesson_plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  title TEXT NOT NULL,
  content TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS prompts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  teacher_id INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL CHECK(kind IN ('grade','report')),
  name TEXT NOT NULL,
  content TEXT DEFAULT '',
  is_default INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(
  teacher_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT DEFAULT '',
  PRIMARY KEY(teacher_id, key));
CREATE TABLE IF NOT EXISTS ai_operations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  teacher_id INTEGER NOT NULL REFERENCES users(id),
  operation_type TEXT NOT NULL,
  course_id INTEGER,
  lecture_id INTEGER,
  submission_id INTEGER,
  model TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS ai_usage_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id INTEGER NOT NULL REFERENCES ai_operations(id),
  attempt INTEGER NOT NULL,
  provider_request_id TEXT,
  model TEXT DEFAULT '',
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  total_tokens INTEGER,
  cached_tokens INTEGER,
  reasoning_tokens INTEGER,
  latency_ms INTEGER,
  finish_reason TEXT DEFAULT '',
  estimated_cost REAL,
  pricing_json TEXT DEFAULT '',
  usage_json TEXT DEFAULT '',
  status TEXT NOT NULL,
  error TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_ai_operations_started ON ai_operations(started_at, id);
CREATE INDEX IF NOT EXISTS idx_ai_operations_teacher ON ai_operations(teacher_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_operations_lecture ON ai_operations(lecture_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_operations_submission ON ai_operations(submission_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_operation ON ai_usage_events(operation_id, attempt);
"""

# ---------- 数据库 ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


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
    # 旧版（无账号体系）数据库与本版不兼容，备份后重建；旧库当时仅有测试数据
    con = sqlite3.connect(DB_PATH)
    # WAL 允许读写并发（长批改占线程时页面仍可访问），busy_timeout 避免瞬时锁直接报错
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    cols = {r[1] for r in con.execute("PRAGMA table_info(homeworks)")}
    if "token" in cols:
        con.close()
        os.replace(DB_PATH, DB_PATH + ".v1.bak")
        con = sqlite3.connect(DB_PATH)
    # 课程下的「作业」更名为「讲次」：一次作业即一讲，旧数据直接改名沿用
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "homeworks" in tables and "lectures" not in tables:
        con.execute("ALTER TABLE homeworks RENAME TO lectures")
    if "submissions" in tables:
        scols_now = {r[1] for r in con.execute("PRAGMA table_info(submissions)")}
        if "homework_id" in scols_now and "lecture_id" not in scols_now:
            con.execute("ALTER TABLE submissions RENAME COLUMN homework_id TO lecture_id")
    con.executescript(SCHEMA)
    # 轻量迁移：早期版本没有 page_no 列
    qcols = {r[1] for r in con.execute("PRAGMA table_info(questions)")}
    if "page_no" not in qcols:
        con.execute("ALTER TABLE questions ADD COLUMN page_no INTEGER DEFAULT 1")
    # 轻量迁移：接入逐题 AI 批改后新增的字段
    for col, ddl in (("max_score", "REAL DEFAULT 0"),
                     ("verdict", "TEXT DEFAULT ''"),
                     ("ai_comment", "TEXT DEFAULT ''"),
                     ("reviewed", "INTEGER DEFAULT 0")):
        if col not in qcols:
            con.execute(f"ALTER TABLE questions ADD COLUMN {col} {ddl}")
    scols = {r[1] for r in con.execute("PRAGMA table_info(submissions)")}
    if "ai_summary" not in scols:
        con.execute("ALTER TABLE submissions ADD COLUMN ai_summary TEXT DEFAULT ''")
    # 轻量迁移：批改报告（Markdown）与发布状态
    if "report_md" not in scols:
        con.execute("ALTER TABLE submissions ADD COLUMN report_md TEXT DEFAULT ''")
    if "report_detail" not in scols:
        con.execute("ALTER TABLE submissions ADD COLUMN report_detail TEXT DEFAULT ''")
    if "report_published" not in scols:
        con.execute("ALTER TABLE submissions ADD COLUMN report_published INTEGER DEFAULT 0")
    # 轻量迁移：讲次新增「学生版」讲义 PDF，与「教师版」标准答案并存
    lcols = {r[1] for r in con.execute("PRAGMA table_info(lectures)")}
    if "student_pdf" not in lcols:
        con.execute("ALTER TABLE lectures ADD COLUMN student_pdf TEXT DEFAULT ''")
    # 轻量迁移：作业范围（AI 识别的题目清单 + 老师勾选的批阅范围）
    for col in ("scope_candidates", "scope_selected"):
        if col not in lcols:
            con.execute(f"ALTER TABLE lectures ADD COLUMN {col} TEXT DEFAULT ''")
    # 轻量迁移：教师版 PDF 只发送指定页码（逗号分隔的 1 起页码）
    if "answer_pages" not in lcols:
        con.execute("ALTER TABLE lectures ADD COLUMN answer_pages TEXT DEFAULT ''")
    # 轻量迁移：讲次级别的整体统计 summary（每题 AI 评语汇总 + 整体 summary）
    for col in ("summary_md", "summary_at"):
        if col not in lcols:
            con.execute(f"ALTER TABLE lectures ADD COLUMN {col} TEXT DEFAULT ''")
    # 轻量迁移：课程级提示词配置（0 表示使用系统内置默认提示词）
    ccols = {r[1] for r in con.execute("PRAGMA table_info(courses)")}
    for col in ("grade_prompt_id", "report_prompt_id"):
        if col not in ccols:
            con.execute(f"ALTER TABLE courses ADD COLUMN {col} INTEGER DEFAULT 0")
    # 系统内置提示词：全局可用、不可删除，代码升级时内容随之更新
    for kind, name, content in (("grade", "默认批改提示词", DEFAULT_GRADE_PROMPT),
                                ("report", "默认报告提示词", DEFAULT_REPORT_PROMPT)):
        row = con.execute("SELECT id FROM prompts WHERE is_default=1 AND kind=?",
                          (kind,)).fetchone()
        if row:
            con.execute("UPDATE prompts SET name=?, content=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=?", (name, content, row[0]))
        else:
            con.execute("INSERT INTO prompts(teacher_id,kind,name,content,is_default) "
                        "VALUES(0,?,?,?,1)", (kind, name, content))
    # 轻量迁移：仍在使用初始密码的账号，登录后强制先改密码
    ucols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "must_change_password" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")
        for r in con.execute("SELECT id, password_hash FROM users").fetchall():
            if any(check_password_hash(r[1], p) for p in DEFAULT_PASSWORDS):
                con.execute("UPDATE users SET must_change_password=1 WHERE id=?", (r[0],))
    # 轻量迁移：学籍号（与登录用户名分离）与末次登录时间
    if "student_no" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN student_no TEXT DEFAULT ''")
    if "last_login_at" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT DEFAULT ''")
    # 轻量迁移：账号启用状态（停用后不能登录，也不会被加入新课程）
    if "active" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    # 课程可多名老师共同管理：把每门课的原创建者补进 course_teachers
    con.execute("INSERT OR IGNORE INTO course_teachers(course_id, teacher_id) "
                "SELECT id, teacher_id FROM courses")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute("INSERT INTO users(username,password_hash,display_name,role,must_change_password) "
                    "VALUES(?,?,?,?,1)",
                    ("admin", generate_password_hash("admin123"), "系统管理员", "admin"))
    # 附件目录结构：把旧版平铺文件迁入 course_<id>/lecture_<id>[/student_<id>]/
    try:
        _migrate_upload_layout(con)
    except OSError:
        log.exception("附件目录迁移失败，旧路径仍可用")
    con.commit()
    con.close()
    try:
        os.chmod(DB_PATH, 0o600)  # 数据库含账号哈希与 API Key，仅当前用户可读写
    except OSError:
        pass


# 环境变量优先于数据库：容器部署时把 Key 放在环境里，库里就不必留明文
ENV_AI_SETTINGS = {
    "ai_api_url": cfg.AI_API_URL,
    "ai_api_key": cfg.AI_API_KEY,
    "ai_model": cfg.AI_MODEL,
}


def get_setting(teacher_id, key, default=""):
    if key in ENV_AI_SETTINGS and ENV_AI_SETTINGS[key]:
        return ENV_AI_SETTINGS[key]
    row = get_db().execute("SELECT value FROM settings WHERE teacher_id=? AND key=?",
                           (teacher_id, key)).fetchone()
    return row["value"] if row else default


def save_setting(teacher_id, key, value):
    get_db().execute(
        "INSERT INTO settings(teacher_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(teacher_id,key) DO UPDATE SET value=excluded.value",
        (teacher_id, key, value))


# ---------- AI 操作与逐次调用用量 ----------
AI_OPERATION_LABEL = {
    "scope_analyze": "作业范围分析",
    "grade": "作业批阅",
    "lecture_summary": "讲次汇总",
    "report_generate": "批改报告评价",
    "report_comment": "重新生成总评",
}
AI_STATUS_LABEL = {"running": "进行中", "success": "成功", "failed": "失败"}


def _start_ai_operation(teacher_id, operation_type, model, course_id=None,
                        lecture_id=None, submission_id=None):
    """创建一次用户操作；每次点击/重试都使用新的 operation_id。"""
    preset = next((m for m in MODEL_PRESETS if m["model"] == model), None)
    peak = _now_in_peak()[0]
    pricing = {
        "price_in": preset["price_in"] if preset else None,
        "price_out": preset["price_out"] if preset else None,
        "peak_multiplier": 2 if peak else 1,
        "currency": "CNY",
        "unit": "元/百万 token",
        "source": "MODEL_PRESETS",
    }
    started_at = datetime.now().isoformat(sep=" ", timespec="milliseconds")
    db = get_db()
    cur = db.execute(
        "INSERT INTO ai_operations(teacher_id,operation_type,course_id,lecture_id,"
        "submission_id,model,status,started_at) VALUES(?,?,?,?,?,?,'running',?)",
        (teacher_id, operation_type, course_id, lecture_id, submission_id, model, started_at))
    db.commit()
    operation = {"id": cur.lastrowid, "pricing": pricing, "finished": False}
    operation["usage_hook"] = lambda event: _record_ai_usage(operation, event)
    return operation


def _record_ai_usage(operation, event):
    """记录单次模型请求；价格和高峰倍率取操作开始时的快照。"""
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
    db.execute(
        "INSERT INTO ai_usage_events(operation_id,attempt,provider_request_id,model,"
        "prompt_tokens,completion_tokens,total_tokens,cached_tokens,reasoning_tokens,"
        "latency_ms,finish_reason,estimated_cost,pricing_json,usage_json,status,error) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (operation["id"], event.get("attempt"), event.get("provider_request_id"),
         event.get("model") or "", prompt, completion, event.get("total_tokens"),
         event.get("cached_tokens"), event.get("reasoning_tokens"), event.get("latency_ms"),
         event.get("finish_reason") or "", cost,
         json.dumps(pricing, ensure_ascii=False),
         json.dumps(usage, ensure_ascii=False) if usage is not None else "",
         event.get("status") or "error", str(event.get("error") or "")[:1000]))
    db.commit()


def _finish_ai_operation(operation, status, error=""):
    if not operation or operation["finished"]:
        return
    get_db().execute(
        "UPDATE ai_operations SET status=?,finished_at=?,error=? WHERE id=?",
        (status, datetime.now().isoformat(sep=" ", timespec="milliseconds"),
         str(error or "")[:1000], operation["id"]))
    get_db().commit()
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
        cost = f"预估费用 ¥{operation['estimated_cost']:.4f}"
        if operation["unknown_cost_count"]:
            cost += f"，另有 {operation['unknown_cost_count']} 次请求费用未知"
        parts.append(cost)
    elif operation["request_count"]:
        parts.append("预估费用未知（缺少价格或 token 用量）")
    return "本次 AI 消耗：" + "；".join(parts)


app.jinja_env.globals["ai_operation_label"] = AI_OPERATION_LABEL.get
app.jinja_env.globals["ai_status_label"] = AI_STATUS_LABEL.get
app.jinja_env.globals["ai_usage_text"] = _ai_usage_text


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
            "recent": recent, "options": options}


# ---------- 提示词（批改 / 报告）----------
PROMPT_KIND_LABEL = {"grade": "批改", "report": "报告"}


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
@app.before_request
def _mark_request_start():
    g.request_started = time.perf_counter()


@app.after_request
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


@app.after_request
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
@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = None
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None or not g.user["active"]:
            # 账号被删除或停用后，已登录的会话立即失效
            session.clear()
            g.user = None


@app.before_request
def limit_body_size():
    """按端点限制请求体大小：恢复备份走大上限，其余（学生上传等）仍用 MAX_UPLOAD_MB。

    必须排在 CSRF 校验之前：CSRF 会解析表单，超大请求应在解析前就拒绝。
    """
    cap = cfg.MAX_RESTORE_MB if request.endpoint == "admin_restore" else cfg.MAX_UPLOAD_MB
    if (request.content_length or 0) > cap * 1024 * 1024:
        abort(413, description=f"上传内容超过 {cap}MB 限制。")


@app.before_request
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


@app.before_request
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


@app.errorhandler(400)
def bad_request(e):
    msg = getattr(e, "description", "") or "请求无效。"
    return render_template("error.html", code=400, msg=msg), 400


@app.errorhandler(413)
def too_large(e):
    msg = getattr(e, "description", "") or "上传内容过大。"
    return render_template("error.html", code=413, msg=msg), 413


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, msg="你没有访问该页面的权限。"), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, msg="页面或资源不存在。"), 404


def _safe_next(target):
    """只接受站内绝对路径，挡掉 //evil.com 这类协议相对跳转。"""
    return target if target and target.startswith("/") and not target.startswith("//") else ""


@app.route("/login", methods=["GET", "POST"])
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


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
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


@app.route("/")
def index():
    if not g.user:
        return redirect(url_for("login"))
    return redirect(url_for({"admin": "admin_users", "teacher": "courses",
                             "student": "student_home"}[g.user["role"]]))


@app.route("/help/<name>")
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


@app.route("/student_home")
@app.route("/teacher_home")
@app.route("/teacher")
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


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    users, filters = _admin_user_query()
    return render_template("admin_users.html", users=users, filters=filters,
                           pristine=_is_pristine())


@app.route("/admin/ai-usage")
@role_required("admin")
def admin_ai_usage():
    period = request.args.get("period", "30")
    if period not in {"7", "30", "90", "all"}:
        period = "30"

    def optional_id(name):
        try:
            value = int(request.args.get(name, ""))
            return value if value > 0 else ""
        except (TypeError, ValueError):
            return ""

    operation_type = request.args.get("operation_type", "")
    if operation_type not in AI_OPERATION_LABEL:
        operation_type = ""
    status = request.args.get("status", "")
    if status not in AI_STATUS_LABEL:
        status = ""
    filters = {
        "period": period,
        "teacher_id": optional_id("teacher_id"),
        "course_id": optional_id("course_id"),
        "operation_type": operation_type,
        "model": request.args.get("model", "").strip()[:200],
        "status": status,
    }
    return render_template("admin_ai_usage.html", filters=filters,
                           **_ai_dashboard_data(filters))


@app.route("/admin/logs")
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


@app.route("/admin/logs/tail")
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


@app.route("/admin/logs/download/<name>")
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


@app.route("/admin_users")
def admin_users_alias():
    """兼容手输下划线的地址。"""
    return redirect(url_for("admin_users"))


@app.route("/admin/users/create", methods=["POST"])
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


@app.route("/admin/users/import", methods=["POST"])
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



@app.route("/admin/users/<int:uid>/reset", methods=["POST"])
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


@app.route("/admin/users/<int:uid>/edit", methods=["POST"])
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


@app.route("/admin/users/<int:uid>/toggle", methods=["POST"])
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


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
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


@app.route("/courses")
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


@app.route("/course/create", methods=["POST"])
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


@app.route("/course/<oid:cid>/edit", methods=["POST"])
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


@app.route("/course/<oid:cid>")
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
                           exports=_course_exports(cid),
                           can_manage_teachers=is_course_owner(course))


@app.route("/course/<oid:cid>/export", methods=["POST"])
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


@app.route("/course/<oid:cid>/export/<name>")
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


@app.route("/course/<oid:cid>/teachers", methods=["POST"])
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


@app.route("/course/<oid:cid>/teachers/<oid:tid>/delete", methods=["POST"])
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


@app.route("/course/<oid:cid>/delete", methods=["POST"])
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


@app.route("/course/<oid:cid>/enroll", methods=["POST"])
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


@app.route("/course/<oid:cid>/unenroll/<oid:sid>", methods=["POST"])
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
@app.route("/course/<oid:cid>/lecture/create", methods=["POST"])
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


@app.route("/lecture/<oid:hid>")
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
    return render_template("lecture.html", hw=hw, course=course, subs=subs, students=students,
                           qstats=qstats, n_subs=n_subs, n_graded=n_graded, n_reviewed=n_reviewed,
                           answer_total=answer_total, answer_picked=answer_picked,
                           acc_by=acc_by, ai_operations=_ai_operation_history(lecture_id=hid, limit=8))


@app.route("/lecture/<oid:hid>/upload-for", methods=["POST"])
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



@app.route("/lecture/<oid:hid>/edit", methods=["POST"])
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


@app.route("/lecture/<oid:hid>/answer", methods=["POST"])
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


@app.route("/lecture/<oid:hid>/answer/pages", methods=["POST"])
@role_required("teacher")
def lecture_answer_pages(hid):
    """保存教师版 PDF 要发送给模型的页码；全部不选等于发送整份文件。"""
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
    if not picked:
        flash("已清除页码限制：批阅时会发送教师版 PDF 的全部页。")
    elif len(picked) == total:
        flash(f"已保存页码：全部 {total} 页（与不限制相同）。")
    else:
        flash(f"已保存页码：{len(picked)}/{total} 页，批阅时只发送这些页。")
    return redirect(url_for("lecture_detail", hid=hid))


@app.route("/lecture/<oid:hid>/answer/page/<int:no>.jpg")
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


@app.route("/lecture/<oid:hid>/student-pdf", methods=["POST"])
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


@app.route("/lecture/<oid:hid>/scope/analyze", methods=["POST"])
@role_required("teacher")
def lecture_scope_analyze(hid):
    """用 AI 读教师版标准答案，生成可勾选的题目清单。"""
    db = get_db()
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    if not hw["answer_pdf"]:
        flash("请先上传教师版标准答案 PDF，再分析题目清单。")
        return redirect(url_for("lecture_detail", hid=hid))

    teacher_id = g.user["id"]
    if get_setting(teacher_id, "ai_demo") == "1":
        flash("演示模式不会调用 AI，无法分析题目清单；请到「AI 设置」关闭演示模式。")
        return redirect(url_for("lecture_detail", hid=hid))
    block = _offpeak_block_if_enabled(teacher_id)
    if block:
        flash(block)
        return redirect(url_for("lecture_detail", hid=hid))
    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    model = get_setting(teacher_id, "ai_model")
    if not api_url or not api_key or not model:
        flash("尚未配置 AI 接口，无法分析标准答案。")
        return redirect(url_for("lecture_detail", hid=hid))

    answer_pages = _page_list(hw["answer_pages"])
    log.info("分析作业范围 lecture=%s answer_pdf=%s pages=%s",
             hid, hw["answer_pdf"], answer_pages)
    operation = _start_ai_operation(
        teacher_id, "scope_analyze", model, course_id=hw["course_id"], lecture_id=hid)
    try:
        questions, err = analyze_answer_pdf(
            os.path.join(UPLOAD_DIR, hw["answer_pdf"]), api_url, api_key, model,
            pages=answer_pages, usage_hook=operation["usage_hook"])
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    _finish_ai_operation(operation, "failed" if err else "success", err)
    usage_text = _ai_usage_text(_ai_operation_history(operation_id=operation["id"], limit=1)[0])
    if err:
        flash(f"{err}；{usage_text}")
        return redirect(url_for("lecture_detail", hid=hid))
    db.execute("UPDATE lectures SET scope_candidates=?, scope_selected='' WHERE id=?",
               (json.dumps(questions, ensure_ascii=False), hid))
    db.commit()
    flash(f"已识别 {len(questions)} 道题，请勾选本讲需要批阅的题目。{usage_text}")
    return redirect(url_for("lecture_detail", hid=hid))


@app.route("/lecture/<oid:hid>/scope/save", methods=["POST"])
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
    flash(f"已保存作业范围：{len(keys)} 道题")
    return redirect(url_for("lecture_detail", hid=hid))


@app.route("/lecture/<oid:hid>/summary/generate", methods=["POST"])
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


@app.route("/lecture/<oid:hid>/delete", methods=["POST"])
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


@app.route("/course/<oid:cid>/student/<oid:sid>")
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
    return render_template("student_detail.html", course=course, student=student,
                           lectures=lectures, exams=exams, acc_by=acc_by)


# ---------- 老师：考试（只录分数，不做 AI 批阅） ----------
def own_exam(eid):
    exam = get_db().execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
    if not exam:
        abort(404)
    own_course(exam["course_id"])
    return exam


@app.route("/course/<oid:cid>/exam/create", methods=["POST"])
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


@app.route("/exam/<int:eid>")
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


@app.route("/exam/<int:eid>/scores", methods=["POST"])
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


@app.route("/exam/<int:eid>/delete", methods=["POST"])
@role_required("teacher")
def exam_delete(eid):
    db = get_db()
    exam = own_exam(eid)
    db.execute("DELETE FROM exam_scores WHERE exam_id=?", (eid,))
    db.execute("DELETE FROM exams WHERE id=?", (eid,))
    db.commit()
    return redirect(url_for("course_detail", cid=exam["course_id"]))


# ---------- 批改：学生作业与标准答案一起交给 AI，老师逐题核对 ----------
# 同一份作业同时只允许一个批阅任务，避免重复点击重复消耗模型调用
_GRADING = set()
_GRADING_LOCK = threading.Lock()


@app.route("/submission/<oid:sid>/grade", methods=["POST"])
@role_required("teacher")
def submission_grade(sid):
    """把学生作业和教师版标准答案一起交给支持 PDF 的 AI，逐题批阅。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM lectures WHERE id=?", (sub["lecture_id"],)).fetchone()
    course = own_course(hw["course_id"])
    if not hw["answer_pdf"]:
        flash("请先上传教师版标准答案 PDF，再开始批阅。")
        return redirect(url_for("lecture_detail", hid=hw["id"]))
    if not (hw["scope_candidates"] or "").strip() or not (hw["scope_selected"] or "").strip():
        flash("请先在「作业范围」分析标准答案、勾选本讲题目并保存，再开始批阅。")
        return redirect(url_for("lecture_detail", hid=hw["id"]))

    block = _offpeak_block_if_enabled(g.user["id"])
    if block:
        flash(block)
        return redirect(url_for("lecture_detail", hid=hw["id"]))

    with _GRADING_LOCK:
        if sid in _GRADING:
            flash("这份作业正在批阅中，请等待本次完成后再操作。")
            return redirect(url_for("lecture_detail", hid=hw["id"]))
        _GRADING.add(sid)

    log.info("开始批阅 submission=%s lecture=%s file=%s answer=%s pages=%s",
             sid, hw["id"], sub["file_path"], hw["answer_pdf"], hw["answer_pages"])
    operation = None
    usage_text = ""
    try:
        if get_setting(g.user["id"], "ai_demo") != "1":
            model = get_setting(g.user["id"], "ai_model")
            operation = _start_ai_operation(
                g.user["id"], "grade", model, course_id=hw["course_id"],
                lecture_id=hw["id"], submission_id=sid)
        gen = run_grading(g.user["id"], file_list(sub["file_path"]), hw["answer_pdf"],
                          scope=_scope_instruction(hw),
                          answer_pages=_page_list(hw["answer_pages"]),
                          prompt=_prompt_content("grade", course["grade_prompt_id"]),
                          usage_hook=operation["usage_hook"] if operation else None)
    except Exception as e:
        _finish_ai_operation(operation, "failed", str(e))
        raise
    finally:
        with _GRADING_LOCK:
            _GRADING.discard(sid)
    if operation:
        _finish_ai_operation(operation, "failed" if gen["error"] else "success", gen["error"])
        usage_text = _ai_usage_text(
            _ai_operation_history(operation_id=operation["id"], limit=1)[0])
    if gen["error"]:
        log.warning("批阅失败 submission=%s 原因=%s", sid, gen["error"])
        flash(gen["error"] + ("；" + usage_text if usage_text else ""))
        return redirect(url_for("lecture_detail", hid=hw["id"]))

    db.execute("DELETE FROM questions WHERE submission_id=?", (sid,))
    for i, q in enumerate(gen["questions"], 1):
        comment = (q.get("comment") or "").strip()
        db.execute("INSERT INTO questions(submission_id,no,page_no,image_path,ocr_text,"
                   "standard_answer,note,verdict,ai_comment) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (sid, q.get("no") or str(i), 1, "", q.get("ocr", ""),
                    q.get("standard_answer", ""), comment,
                    q.get("verdict", ""), comment))
    summary = gen.get("summary") or {}
    db.execute("UPDATE submissions SET status='待批改', ai_summary=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(summary, ensure_ascii=False) if summary else "", sid))
    db.commit()
    count = len(gen["questions"])
    try:
        generate_report(db, sid, g.user["id"])
    except Exception:  # 报告生成失败不影响批阅结果本身
        log.exception("生成批改报告失败 submission=%s", sid)
    if gen.get("demo"):
        msg = f"演示模式：未调用 AI，已生成 {count} 道题，请逐题修正内容"
    else:
        msg = f"AI 已批阅 {count} 道题，请逐题核对 OCR、判定与评语。{usage_text}"
    flash(msg)
    return redirect(url_for("submission_review", sid=sid))


@app.route("/submission/<oid:sid>/review")
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
                           ai_operations=_ai_operation_history(submission_id=sid, limit=5))


@app.route("/submission/<oid:sid>/review/save", methods=["POST"])
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


@app.route("/question/<int:qid>/reviewed", methods=["POST"])
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
@app.route("/course/<oid:cid>/plan/create", methods=["POST"])
@role_required("teacher")
def plan_create(cid):
    own_course(cid)
    title = request.form.get("title", "").strip()
    if title:
        db = get_db()
        db.execute("INSERT INTO lesson_plans(course_id,title) VALUES(?,?)", (cid, title))
        db.commit()
    return redirect(url_for("course_detail", cid=cid))


@app.route("/plan/<int:pid>/edit", methods=["GET", "POST"])
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


@app.route("/plan/<int:pid>/delete", methods=["POST"])
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
@app.route("/prompts")
@role_required("teacher")
def prompts_page():
    db = get_db()
    defaults = db.execute("SELECT * FROM prompts WHERE is_default=1 ORDER BY kind").fetchall()
    mine = db.execute("SELECT * FROM prompts WHERE teacher_id=? ORDER BY kind, id DESC",
                      (g.user["id"],)).fetchall()
    return render_template("prompts.html", defaults=defaults, mine=mine,
                           kind_label=PROMPT_KIND_LABEL)


@app.route("/prompts/create", methods=["POST"])
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


@app.route("/prompts/<oid:pid>/save", methods=["POST"])
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


@app.route("/prompts/<oid:pid>/copy", methods=["POST"])
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


@app.route("/prompts/<oid:pid>/delete", methods=["POST"])
@role_required("teacher")
def prompt_delete(pid):
    row = _editable_prompt(pid)
    if row is None:
        return redirect(url_for("prompts_page"))
    db = get_db()
    # 引用了这条提示词的课程回落到系统默认提示词
    db.execute("UPDATE courses SET grade_prompt_id=0 WHERE grade_prompt_id=?", (pid,))
    db.execute("UPDATE courses SET report_prompt_id=0 WHERE report_prompt_id=?", (pid,))
    db.execute("DELETE FROM prompts WHERE id=?", (pid,))
    db.commit()
    flash(f"提示词「{row['name']}」已删除")
    return redirect(url_for("prompts_page"))


@app.route("/course/<oid:cid>/prompts", methods=["POST"])
@role_required("teacher")
def course_prompts_save(cid):
    """为整门课程选定批改 / 报告提示词。"""
    course = own_course(cid)
    db = get_db()
    picked = {}
    for field, kind in (("grade_prompt_id", "grade"), ("report_prompt_id", "report")):
        pid = request.form.get(field, type=int) or 0
        allowed = {r["id"] for r in _prompt_options(cid, kind)}
        if pid and pid not in allowed:
            flash("所选提示词不在本课程可用范围内，已保留原设置")
            pid = course[field] or 0
        picked[field] = pid
    db.execute("UPDATE courses SET grade_prompt_id=?, report_prompt_id=? WHERE id=?",
               (picked["grade_prompt_id"], picked["report_prompt_id"], cid))
    db.commit()
    flash("本课程的提示词配置已更新")
    return redirect(url_for("course_detail", cid=cid))


# ---------- 老师：AI 设置 ----------
@app.route("/settings", methods=["GET", "POST"])
@role_required("teacher")
def settings_page():
    if request.method == "POST":
        if request.form.get("demo_only"):
            save_setting(g.user["id"], "ai_demo", "1" if request.form.get("ai_demo") else "")
        else:
            for k in ("ai_api_url", "ai_api_key", "ai_model"):
                if ENV_AI_SETTINGS.get(k):
                    continue  # 已由环境变量注入，页面设置不覆盖，也不写库
                value = request.form.get(k, "").strip()
                if k == "ai_api_key" and not value:
                    continue  # Key 留空表示不修改，避免误清空
                save_setting(g.user["id"], k, value)
            save_setting(g.user["id"], "ai_demo", "1" if request.form.get("ai_demo") else "")
            save_setting(g.user["id"], "ai_offpeak_only", "1" if request.form.get("ai_offpeak_only") else "")
        get_db().commit()
        flash("已保存")
        return redirect(url_for("settings_page"))
    cfg = {k: get_setting(g.user["id"], k)
           for k in ("ai_api_url", "ai_api_key", "ai_model", "ai_demo", "ai_offpeak_only")}
    return render_template("settings.html", cfg=cfg, env_keys=[k for k, v in ENV_AI_SETTINGS.items() if v],
                           model_presets=MODEL_PRESETS,
                           est_tokens_input=EST_TOKENS_INPUT,
                           est_tokens_output=EST_TOKENS_OUTPUT)


@app.route("/assistant/chat", methods=["POST"])
@role_required("teacher")
def assistant_chat():
    """页面右下角教师助手：带工具调用的多轮对话，只读查询本老师的数据。"""
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
    # 助手是交互式的，单次请求很小，不受「仅闲时批阅」限制
    reply, trace, err = assistant.reply(msgs, llm.resolve_config(g.user["id"]), g.user["id"])
    if err:
        return jsonify({"error": err, "trace": trace}), 502
    return jsonify({"reply": reply, "trace": trace})


@app.route("/settings/test", methods=["POST"])
@role_required("teacher")
def settings_test():
    """发一个最小请求，验证接口地址与 Key 是否可用。"""
    api_url = get_setting(g.user["id"], "ai_api_url").rstrip("/")
    api_key = get_setting(g.user["id"], "ai_api_key")
    model = get_setting(g.user["id"], "ai_model")
    if not api_url or not api_key or not model:
        flash("请先填写接口地址、API Key 和模型并保存")
        return redirect(url_for("settings_page"))
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
            flash(f"连接成功，模型 {model} 回复：{reply[:40]}")
        else:
            flash(f"连接成功，模型 {model} 已响应（该模型为思考模型，本次未输出正文，属正常现象）")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        hint = "（免费模型有并发/频率限制，等几秒再试）" if e.code == 429 else ""
        flash(f"连接失败：HTTP {e.code}{hint} {detail}")
    except Exception as e:
        flash(f"连接失败：{e}")
    return redirect(url_for("settings_page"))


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


@app.route("/student")
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


@app.route("/student/lecture/<oid:hid>", methods=["GET", "POST"])
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
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# 文件头魔数：只靠扩展名过滤，改名的 HTML/脚本也能传进来，这里按内容再判一次
MAGIC_HEADS = {
    "pdf": (b"%PDF",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),  # 需再检查 8~12 字节是否为 WEBP
}


def _upload_rel_dir(course_id, lecture_id, student_id=0):
    """附件在 uploads/ 下的相对目录：course/lecture[/student]。"""
    parts = [f"course_{course_id}", f"lecture_{lecture_id}"]
    if student_id:
        parts.append(f"student_{student_id}")
    return "/".join(parts)


def _validate_upload(file_storage):
    """校验扩展名与文件头，返回安全的落盘文件名（此时尚未写入磁盘）。"""
    orig = secure_filename(file_storage.filename or "upload")
    ext = orig.rsplit(".", 1)[1].lower() if "." in orig else "bin"
    if ext not in MAGIC_HEADS:
        raise ValueError("只支持 png / jpg / webp / pdf 文件")
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    if not any(head.startswith(sig) for sig in MAGIC_HEADS[ext]) or \
            (ext == "webp" and head[8:12] != b"WEBP"):
        raise ValueError("文件内容与扩展名不符，请重新导出后再上传")
    size = file_storage.content_length
    if size is None:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
    if size > cfg.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"单个文件不能超过 {cfg.MAX_UPLOAD_MB}MB")
    return f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_{orig or 'file.' + ext}"


def save_upload(file_storage, course_id, lecture_id, student_id=0, filename=None):
    """按 course/lecture[/student] 分层保存附件，返回相对路径（用 / 分隔）。"""
    fname = filename or _validate_upload(file_storage)
    rel_dir = _upload_rel_dir(course_id, lecture_id, student_id)
    abs_dir = os.path.join(UPLOAD_DIR, *rel_dir.split("/"))
    os.makedirs(abs_dir, exist_ok=True)
    file_storage.save(os.path.join(abs_dir, fname))
    return f"{rel_dir}/{fname}"


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
# 判定图标统一用彩色 emoji：⚠ 必须带变体选择符 U+FE0F，否则浏览器会渲染成灰白线框三角
VERDICT_ICON = {"对": "✅", "半对": "⚠️", "错": "❌", "无法判定": "❔"}


def _md_cell(value):
    """表格单元格里的竖线与换行会破坏 Markdown 表格，统一收敛。"""
    return str(value or "").replace("|", "｜").replace("\n", " ").strip()


def build_report_md(student_name, lecture_title, date_label, questions, summary, detail):
    """生成学生的 Markdown 批改报告：逐题明细 + 失分归因 + 评价。"""
    qs = [dict(q) for q in questions]
    n_total = len(qs)
    right = [q for q in qs if (q.get("verdict") or "") == "对"]
    partial = [q for q in qs if (q.get("verdict") or "") == "半对"]
    wrong = [q for q in qs if (q.get("verdict") or "") == "错"]
    unknown = [q for q in qs if (q.get("verdict") or "") == "无法判定"]
    skipped = [q for q in qs if "未作答" in (q.get("ai_comment") or "")]

    out = [f"# {student_name} · {date_label}作业《{lecture_title}》批改结果", "",
           f"## 一、逐题明细（{n_total} 题）", "",
           "| # | 题目 | 判定 | 批改说明 |", "| ---: | --- | :---: | --- |"]
    for i, q in enumerate(qs, 1):
        icon = VERDICT_ICON.get(q.get("verdict") or "", q.get("verdict") or "—")
        out.append(f"| {i} | {_md_cell(q.get('no'))} | {icon} | {_md_cell(q.get('ai_comment'))} |")

    def _nos(items, limit=6):
        names = [str(q.get("no")) for q in items]
        text = "、".join(f"第 {n} 题" for n in names[:limit])
        return text + ("等" if len(names) > limit else "")

    stat = [f"统计：{VERDICT_ICON['对']} {len(right)}"]
    stat.append(f"{VERDICT_ICON['错']} {len(wrong)}" + (f"（{_nos(wrong)}）" if wrong else ""))
    stat.append(f"{VERDICT_ICON['半对']} {len(partial)}" + (f"（{_nos(partial)}）" if partial else ""))
    if unknown:
        stat.append(f"{VERDICT_ICON['无法判定']} {len(unknown)}（{_nos(unknown)}）")
    stat.append(f"未作答 {len(skipped)}")
    out += ["", " | ".join(stat), ""]

    weak = (summary or {}).get("weak_points") or []
    out += ["## 二、失分点归因", ""]
    if weak:
        out += ["| 题 | 类型 | 症结 |", "| --- | --- | --- |"]
        for w in weak:
            out.append(f"| {_md_cell(w.get('no'))} | {_md_cell(w.get('type'))} | "
                       f"{_md_cell(w.get('detail'))} |")
    else:
        out.append("本次没有失分点。" if not (wrong or partial) else "AI 未给出失分归类，请参考逐题说明。")

    out += ["", "## 三、评价", ""]
    good = (detail or {}).get("good") or (summary or {}).get("evaluation", "")
    if good:
        out += [good, ""]
    improve = (detail or {}).get("improve") or []
    if improve:
        out += ["需要改的几处：", ""]
        out += [f"{i}. {item}" for i, item in enumerate(improve, 1)]
        out.append("")
    nxt = (detail or {}).get("next") or ""
    if nxt:
        out += [f"**下一步建议：** {nxt}", ""]
    return "\n".join(out).strip() + "\n"


def _overall_text(detail, fallback=""):
    """把 AI 返回的三段式评价拼成纯文本总评，供逐题核对页的文本框直接编辑。"""
    if not detail:
        return fallback or ""
    parts = []
    good = (detail.get("good") or "").strip()
    if good:
        parts.append(good)
    improve = [str(x).strip() for x in (detail.get("improve") or []) if str(x).strip()]
    if improve:
        parts.append("需要改的几处：\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(improve, 1)))
    nxt = (detail.get("next") or "").strip()
    if nxt:
        parts.append("下一步建议：" + nxt)
    return "\n\n".join(parts) or (fallback or "")


def render_markdown(md):
    """把报告用的极简 Markdown（标题/表格/列表/粗体）渲染成 HTML，避免引入额外依赖。"""
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def inline(text):
        s = esc(text)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    lines = (md or "").splitlines()
    html, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # 跳过结束标记
            html.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
            continue
        if line.startswith(">"):
            quotes = []
            while i < len(lines) and lines[i].startswith(">"):
                quotes.append(lines[i].lstrip(">").strip())
                i += 1
            html.append("<blockquote>"
                        + "".join(f"<p>{inline(x)}</p>" for x in quotes if x)
                        + "</blockquote>")
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", line.strip()):
            html.append("<hr>")
            i += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            html.append(f"<h{min(level, 4)}>{inline(line[level:].strip())}</h{min(level, 4)}>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html.append("<table class='report-table'>")
            for r, cells in enumerate(rows):
                tag = "th" if r == 0 else "td"
                html.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            html.append("</table>")
            continue
        if re.match(r"^\d+\.\s", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
                items.append(re.sub(r"^\d+\.\s", "", lines[i]))
                i += 1
            html.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:])
                i += 1
            html.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue
        html.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(html)


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


@app.route("/submission/<oid:sid>/report")
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


@app.route("/submission/<oid:sid>/report/build", methods=["POST"])
@role_required("teacher")
def submission_report_build(sid):
    sub, hw, _ = own_submission(sid)
    generate_report(get_db(), sid, g.user["id"])
    flash("批改报告已生成，确认无误后发布给学生。")
    return redirect(url_for("submission_report", sid=sid))


@app.route("/submission/<oid:sid>/report/publish", methods=["POST"])
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


@app.route("/submission/<oid:sid>/report/comment", methods=["POST"])
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


@app.route("/submission/<oid:sid>/report/save", methods=["POST"])
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


@app.route("/submission/<oid:sid>/report/download")
@role_required("teacher")
def submission_report_download(sid):
    sub, hw, _ = own_submission(sid)
    if not sub["report_md"]:
        abort(404)
    name = f"{hw['title']}_批改报告.md".replace("/", "_")
    import io as _io
    return send_file(_io.BytesIO(sub["report_md"].encode("utf-8")),
                     mimetype="text/markdown", as_attachment=True, download_name=name)


@app.route("/student/submission/<oid:sid>/report")
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


@app.route("/admin/consistency")
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


@app.route("/admin/consistency/fix", methods=["POST"])
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
                  for t in ("users", "courses", "lectures", "exams", "submissions", "questions")}
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


@app.route("/admin/export")
@role_required("admin")
def admin_export():
    """整体导出数据库快照 + 全部附件 + manifest，用于备份或迁移到新机器。"""
    out = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    out.close()
    _build_backup_zip(out.name)
    log.info("导出系统数据：%s", out.name)
    return send_file(out.name, as_attachment=True,
                     download_name=datetime.now().strftime("gradelogy_%Y%m%d_%H%M%S.zip"))


@app.route("/admin/backup/now", methods=["POST"])
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


@app.route("/admin/backup/<name>")
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


@app.route("/admin/restore", methods=["POST"])
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


@app.route("/setup")
@role_required("admin")
def setup_import():
    if not _is_pristine():
        flash("系统里已有数据，初始化导入向导仅限全新空系统使用；如需恢复旧备份，请到「系统维护」页")
        return redirect(url_for("index"))
    return render_template("setup_import.html", step="upload")


@app.route("/setup/upload", methods=["POST"])
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


@app.route("/setup/preview")
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


@app.route("/setup/confirm", methods=["POST"])
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


@app.route("/setup/cancel", methods=["POST"])
@role_required("admin")
def setup_cancel():
    _purge_setup_stash()
    flash("已取消，上传的备份包已删除")
    return redirect(url_for("setup_import"))



def _student_files(uid):
    """学生可访问的文件白名单：自己的提交原件 + 自己提交下的题目裁剪图 + 所选课程的讲次学生版讲义。

    教师版答案、未选课程的材料、他人作业一律不在集合内，避免知道路径就能下载。
    """
    db = get_db()
    paths = set()
    for r in db.execute("SELECT file_path FROM submissions WHERE student_id=?", (uid,)):
        for p in file_list(r["file_path"]):
            paths.add(os.path.normpath(p).replace("\\", "/"))
    for r in db.execute("SELECT q.image_path FROM questions q "
                        "JOIN submissions s ON s.id=q.submission_id "
                        "WHERE s.student_id=? AND q.image_path != ''", (uid,)):
        paths.add(os.path.normpath(r["image_path"]).replace("\\", "/"))
    for r in db.execute("""
            SELECT h.student_pdf FROM lectures h
            JOIN enrollments e ON e.course_id=h.course_id AND e.student_id=?
            WHERE h.student_pdf != ''""", (uid,)):
        paths.add(os.path.normpath(r["student_pdf"]).replace("\\", "/"))
    return paths


@app.route("/files/<path:filename>")
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


@app.route("/healthz")
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


# 模块导入时即完成建表与迁移，gunicorn 直接加载 app:app 也能拿到完整 schema
init_db()
start_auto_backup()


if __name__ == "__main__":
    host = cfg.HOST
    port = cfg.PORT
    ip = _lan_ip()
    print("\n  本机访问：http://127.0.0.1:%d" % port)
    if ip:
        print("  手机/其他电脑（同一 WiFi）：http://%s:%d\n" % (ip, port))
    # debug=False 避免 stat watcher 触发代码重载把长耗时批改请求掐掉
    app.run(host=host, port=port, debug=False)
