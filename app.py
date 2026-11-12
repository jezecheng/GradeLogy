# -*- coding: utf-8 -*-
"""化学竞赛作业管理平台
角色：admin（用户管理）/ teacher（课程、作业、批改、AI 设置）/ student（提交与查看批改）
"""
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from functools import wraps

try:
    import fcntl  # 仅类 Unix 可用，用于保证自动备份只在一个进程里跑
except ImportError:  # pragma: no cover
    fcntl = None

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config as cfg
from utilities import roster, storage
from utilities.pdfgrade import grade_pdf_submission

cfg.ensure_dirs()
cfg.setup_logging()

BASE_DIR = cfg.BASE_DIR
UPLOAD_DIR = cfg.STORAGE_DIR
DB_PATH = cfg.DB_PATH
BACKUP_DIR = cfg.BACKUP_DIR
BACKUP_RE = re.compile(r"gradelogy_\d{8}_\d{6}\.zip")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底，避免 HTTPS 调用 AI 时报 SSL 错误
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

ROLE_LABEL = {"admin": "管理员", "teacher": "老师", "student": "学生"}
# 系统初始密码：使用这些密码的账号会被要求登录后立即修改
DEFAULT_PASSWORDS = ("admin123", "123456")

def load_secret_key():
    """密钥持久化，避免每次重启把所有人都登出（容器里通过 SECRET_KEY 环境变量注入）。"""
    path = cfg.SECRET_KEY_FILE
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = cfg.SECRET_KEY or load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = max(cfg.MAX_UPLOAD_MB, cfg.MAX_RESTORE_MB) * 1024 * 1024
os.makedirs(UPLOAD_DIR, exist_ok=True)

log = logging.getLogger("gradelogy")


def _json_or_none(value):
    """模板里把 ai_summary 的 JSON 字符串转成 dict；空值或坏数据一律返回 None。"""
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


app.jinja_env.filters["fromjson"] = _json_or_none


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
  real_name TEXT DEFAULT '',
  student_no TEXT DEFAULT '',
  must_change_password INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  teacher_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS enrollments(
  course_id INTEGER NOT NULL REFERENCES courses(id),
  student_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(course_id, student_id));
CREATE TABLE IF NOT EXISTS homeworks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  deadline TEXT DEFAULT '',
  answer_pdf TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  homework_id INTEGER NOT NULL REFERENCES homeworks(id),
  student_id INTEGER NOT NULL REFERENCES users(id),
  file_path TEXT NOT NULL,
  status TEXT DEFAULT '已提交',
  score TEXT DEFAULT '',
  feedback TEXT DEFAULT '',
  grader TEXT DEFAULT '',
  ai_summary TEXT DEFAULT '',
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
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lesson_plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  title TEXT NOT NULL,
  content TEXT DEFAULT '',
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(
  teacher_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT DEFAULT '',
  PRIMARY KEY(teacher_id, key));
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
    con.executescript(SCHEMA)
    # 轻量迁移：早期版本没有 page_no 列
    qcols = {r[1] for r in con.execute("PRAGMA table_info(questions)")}
    if "page_no" not in qcols:
        con.execute("ALTER TABLE questions ADD COLUMN page_no INTEGER DEFAULT 1")
    # 轻量迁移：接入逐题 AI 批改后新增的字段
    for col, ddl in (("max_score", "REAL DEFAULT 0"),
                     ("verdict", "TEXT DEFAULT ''"),
                     ("ai_comment", "TEXT DEFAULT ''")):
        if col not in qcols:
            con.execute(f"ALTER TABLE questions ADD COLUMN {col} {ddl}")
    scols = {r[1] for r in con.execute("PRAGMA table_info(submissions)")}
    if "ai_summary" not in scols:
        con.execute("ALTER TABLE submissions ADD COLUMN ai_summary TEXT DEFAULT ''")
    # 轻量迁移：仍在使用初始密码的账号，登录后强制先改密码
    ucols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    if "must_change_password" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")
        for r in con.execute("SELECT id, password_hash FROM users").fetchall():
            if any(check_password_hash(r[1], p) for p in DEFAULT_PASSWORDS):
                con.execute("UPDATE users SET must_change_password=1 WHERE id=?", (r[0],))
    # 轻量迁移：学生真实姓名与学籍号（与登录用户名分离）
    for col in ("real_name", "student_no"):
        if col not in ucols:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute("INSERT INTO users(username,password_hash,display_name,role,must_change_password) "
                    "VALUES(?,?,?,?,1)",
                    ("admin", generate_password_hash("admin123"), "系统管理员", "admin"))
    con.commit()
    con.close()


def get_setting(teacher_id, key, default=""):
    row = get_db().execute("SELECT value FROM settings WHERE teacher_id=? AND key=?",
                           (teacher_id, key)).fetchone()
    return row["value"] if row else default


def save_setting(teacher_id, key, value):
    get_db().execute(
        "INSERT INTO settings(teacher_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(teacher_id,key) DO UPDATE SET value=excluded.value",
        (teacher_id, key, value))


# ---------- 访问日志 ----------
@app.before_request
def _mark_request_start():
    g.request_started = time.perf_counter()


@app.after_request
def _access_log(resp):
    """每个请求写入一条访问日志；静态资源与健康检查跳过，避免轮询刷屏。"""
    if request.endpoint in ("static", "healthz"):
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


# ---------- 登录与权限 ----------
@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = None
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None:
            session.clear()


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


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["uid"] = user["id"]
            log.info("登录成功 user=%s role=%s ip=%s",
                     username, user["role"], request.remote_addr or "-")
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("index"))
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


@app.route("/student_home")
@app.route("/teacher_home")
@app.route("/teacher")
def home_alias():
    """兼容手输的首页地址，统一按角色跳转到各自首页。"""
    return redirect(url_for("index"))


# ---------- 管理员：用户管理 ----------
@app.route("/admin/users")
@role_required("admin")
def admin_users():
    users = get_db().execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM courses c WHERE c.teacher_id=u.id) AS n_courses,
               (SELECT COUNT(*) FROM enrollments e WHERE e.student_id=u.id) AS n_enrolled
        FROM users u ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'teacher' THEN 1 ELSE 2 END, u.id
        """).fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin_users")
def admin_users_alias():
    """兼容手输下划线的地址。"""
    return redirect(url_for("admin_users"))


@app.route("/admin/users/create", methods=["POST"])
@role_required("admin")
def admin_user_create():
    username = request.form.get("username", "").strip()
    name = request.form.get("display_name", "").strip() or username
    real_name = request.form.get("real_name", "").strip() or name
    student_no = request.form.get("student_no", "").strip()
    role = request.form.get("role", "student")
    password = request.form.get("password", "") or "123456"
    if not username:
        flash("用户名不能为空")
    elif role not in ("teacher", "student", "admin"):
        flash("角色不合法")
    elif student_no and not roster.STUDENT_NO_RE.match(student_no):
        flash("学籍号不合法（1-32 位字母、数字或 _ . -）")
    elif len(real_name) > 32:
        flash("真实姓名最多 32 字")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO users(username,password_hash,display_name,role,"
                       "real_name,student_no,must_change_password) VALUES(?,?,?,?,?,?,?)",
                       (username, generate_password_hash(password), name, role,
                        real_name, student_no, 1 if password in DEFAULT_PASSWORDS else 0))
            db.commit()
            log.info("创建账号 operator=%s#%s user=%s role=%s",
                     g.user["username"], g.user["id"], username, role)
            flash(f"已创建{ROLE_LABEL[role]}账号 {username}，初始密码 {password}")
        except sqlite3.IntegrityError:
            flash(f"用户名 {username} 已存在")
    return redirect(url_for("admin_users"))


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
    for c in db.execute("SELECT id FROM courses WHERE teacher_id=?", (uid,)).fetchall():
        db.execute("DELETE FROM submissions WHERE homework_id IN "
                   "(SELECT id FROM homeworks WHERE course_id=?)", (c["id"],))
        db.execute("DELETE FROM homeworks WHERE course_id=?", (c["id"],))
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
    course = get_db().execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    if not course:
        abort(404)
    if g.user["role"] != "admin" and course["teacher_id"] != g.user["id"]:
        abort(403)
    return course


@app.route("/courses")
@role_required("teacher")
def courses():
    rows = get_db().execute("""
        SELECT c.*,
          (SELECT COUNT(*) FROM enrollments e WHERE e.course_id=c.id) AS n_students,
          (SELECT COUNT(*) FROM homeworks h WHERE h.course_id=c.id) AS n_homeworks
        FROM courses c WHERE c.teacher_id=? ORDER BY c.id DESC""", (g.user["id"],)).fetchall()
    return render_template("courses.html", courses=rows)


@app.route("/course/create", methods=["POST"])
@role_required("teacher")
def course_create():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT INTO courses(name,description,teacher_id) VALUES(?,?,?)",
                   (name, request.form.get("description", "").strip(), g.user["id"]))
        db.commit()
    return redirect(url_for("courses"))


@app.route("/courses/import", methods=["POST"])
@role_required("teacher")
def courses_import():
    """批量建课：CSV 中每门课程一行学生，先预览再确认。"""
    raw, error = _read_import_file()
    if error:
        flash(error)
        return redirect(url_for("courses"))
    try:
        rows, errors = roster.parse_course_csv(raw)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("courses"))
    if not rows:
        flash("没有可导入的数据：" + ("；".join(errors[:3]) if errors else "文件为空"))
        return redirect(url_for("courses"))
    groups = _course_groups(g.user["id"], rows)
    total = {"course_new": sum(1 for g in groups if g["id"] is None),
             "course_exists": sum(1 for g in groups if g["id"] is not None)}
    for key in ("create", "join", "skip", "error"):
        total[key] = sum(g["counts"][key] for g in groups)
    token = _stash_import(raw)
    return render_template("course_import_preview.html", groups=groups, errors=errors,
                           total=total, token=token)


@app.route("/courses/import/confirm", methods=["POST"])
@role_required("teacher")
def courses_import_confirm():
    raw = _take_import(request.form.get("token", ""))
    if raw is None:
        flash("导入内容已过期，请重新上传")
        return redirect(url_for("courses"))
    try:
        rows, errors = roster.parse_course_csv(raw)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("courses"))
    total = _apply_course_groups(g.user["id"], _course_groups(g.user["id"], rows))
    log.info("批量建课 operator=%s#%s 新课程=%s 已有课程=%s 新建学生=%s 加入=%s 已在课程=%s",
             g.user["username"], g.user["id"], total["course_new"], total["course_exists"],
             total["create"], total["join"], total["skip"])
    msg = (f"导入完成：新课程 {total['course_new']} 门、已有课程 {total['course_exists']} 门；"
           f"新建学生 {total['create']} 人，加入 {total['join']} 人，已在课程 {total['skip']} 人")
    if errors:
        msg += f"；{len(errors)} 行未处理：" + "；".join(errors[:5])
        if len(errors) > 5:
            msg += f" 等共 {len(errors)} 条"
    flash(msg)
    return redirect(url_for("courses"))


@app.route("/course/<int:cid>")
@role_required("teacher", "admin")
def course_detail(cid):
    db = get_db()
    course = own_course(cid)
    students = db.execute("""
        SELECT u.id, u.username, u.display_name, u.real_name, u.student_no FROM enrollments e
        JOIN users u ON u.id=e.student_id WHERE e.course_id=? ORDER BY u.display_name""",
        (cid,)).fetchall()
    candidates = db.execute("""
        SELECT u.id, u.username, u.display_name, u.real_name, u.student_no FROM users u
        WHERE u.role='student' AND u.id NOT IN
              (SELECT student_id FROM enrollments WHERE course_id=?)
        ORDER BY u.display_name""", (cid,)).fetchall()
    homeworks = db.execute("""
        SELECT h.*,
          (SELECT COUNT(*) FROM submissions s WHERE s.homework_id=h.id) AS n_subs,
          (SELECT COUNT(*) FROM enrollments e WHERE e.course_id=h.course_id) AS n_students
        FROM homeworks h WHERE h.course_id=? ORDER BY h.id DESC""", (cid,)).fetchall()
    plans = db.execute("SELECT * FROM lesson_plans WHERE course_id=? ORDER BY updated_at DESC",
                       (cid,)).fetchall()
    return render_template("course.html", course=course, students=students,
                           candidates=candidates, homeworks=homeworks, plans=plans)


@app.route("/course/<int:cid>/delete", methods=["POST"])
@role_required("teacher")
def course_delete(cid):
    own_course(cid)
    db = get_db()
    db.execute("DELETE FROM submissions WHERE homework_id IN "
               "(SELECT id FROM homeworks WHERE course_id=?)", (cid,))
    for t in ("homeworks", "enrollments", "lesson_plans"):
        db.execute(f"DELETE FROM {t} WHERE course_id=?", (cid,))
    db.execute("DELETE FROM courses WHERE id=?", (cid,))
    db.commit()
    return redirect(url_for("courses"))


@app.route("/course/<int:cid>/enroll", methods=["POST"])
@role_required("teacher")
def course_enroll(cid):
    own_course(cid)
    sid = request.form.get("student_id")
    if sid:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO enrollments(course_id,student_id) VALUES(?,?)", (cid, sid))
        db.commit()
    return redirect(url_for("course_detail", cid=cid))


IMPORT_TMP_DIR = os.path.join(cfg.DATA_DIR, "tmp_imports")


def _read_import_file():
    """读取上传的 CSV（≤1MB），返回 (raw, error)。"""
    f = request.files.get("file")
    if not f or not f.filename:
        return None, "请选择 CSV 文件"
    if not f.filename.lower().endswith((".csv", ".txt")):
        return None, "只支持 .csv 文件（Excel 可另存为 CSV）"
    raw = f.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        return None, "文件超过 1MB，请拆分后再导入"
    return raw, ""


def _stash_import(raw):
    """暂存上传内容供预览确认，返回一次性 token；顺手清理 1 小时前的残留文件。"""
    os.makedirs(IMPORT_TMP_DIR, exist_ok=True)
    cutoff = time.time() - 3600
    for name in os.listdir(IMPORT_TMP_DIR):
        path = os.path.join(IMPORT_TMP_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass
    token = secrets.token_hex(8)
    with open(os.path.join(IMPORT_TMP_DIR, f"{g.user['id']}_{token}.csv"), "wb") as f:
        f.write(raw)
    return token


def _take_import(token):
    """按 token 取回本人暂存的导入内容；token 仅允许十六进制，避免路径穿越。"""
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        abort(400)
    path = os.path.join(IMPORT_TMP_DIR, f"{g.user['id']}_{token}.csv")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        raw = f.read()
    os.remove(path)
    return raw


def _student_plan(cid, rows):
    """把解析结果映射成待执行动作：create / join / skip / error。cid 为空表示课程尚未创建。"""
    db = get_db()
    plan = []
    for r in rows:
        user = db.execute("SELECT id, role FROM users WHERE username=?",
                          (r["username"],)).fetchone()
        if user is None:
            plan.append({"row": r, "action": "create", "detail": "新建学生账号并加入课程"})
        elif user["role"] != "student":
            plan.append({"row": r, "action": "error", "detail": "用户名已存在且不是学生账号"})
        elif cid and db.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?",
                                (cid, user["id"])).fetchone():
            plan.append({"row": r, "action": "skip", "detail": "已在课程中"})
        else:
            plan.append({"row": r, "action": "join", "detail": "已有学生账号，加入课程"})
    return plan


def _plan_counts(plan):
    counts = {"create": 0, "join": 0, "skip": 0, "error": 0}
    for item in plan:
        counts[item["action"]] += 1
    return counts


def _apply_student_plan(cid, plan):
    """执行 _student_plan 的结果；返回实际计数（并发下可能与预览略有差异）。"""
    db = get_db()
    counts = {"create": 0, "join": 0, "skip": 0, "error": 0}
    for item in plan:
        action, r = item["action"], item["row"]
        if action == "error":
            counts["error"] += 1
            continue
        if action == "create":
            sid = db.execute(
                "INSERT INTO users(username,password_hash,display_name,role,"
                "real_name,student_no,must_change_password) VALUES(?,?,?,?,?,?,?)",
                (r["username"], generate_password_hash(r["password"]), r["name"], "student",
                 r["real_name"], r["student_no"],
                 1 if r["password"] == roster.DEFAULT_PASSWORD else 0)).lastrowid
            counts["create"] += 1
        else:
            found = db.execute("SELECT id FROM users WHERE username=?",
                               (r["username"],)).fetchone()
            if found is None:  # 预览与确认之间账号被删除
                counts["error"] += 1
                continue
            sid = found["id"]
        cur = db.execute("INSERT OR IGNORE INTO enrollments(course_id,student_id) VALUES(?,?)",
                         (cid, sid))
        if action == "create":
            continue
        counts["join" if cur.rowcount else "skip"] += 1
    db.commit()
    return counts


def _course_groups(teacher_id, rows):
    """按课程名分组（保持文件顺序），并为每组生成学生动作。"""
    db = get_db()
    groups, order = {}, []
    for r in rows:
        name = r["course"]
        if name not in groups:
            existing = db.execute("SELECT id FROM courses WHERE name=? AND teacher_id=?",
                                  (name, teacher_id)).fetchone()
            groups[name] = {"name": name, "id": existing["id"] if existing else None,
                            "rows": []}
            order.append(name)
        groups[name]["rows"].append(r)
    result = []
    for name in order:
        g = groups[name]
        g["plan"] = _student_plan(g["id"], g["rows"])
        g["counts"] = _plan_counts(g["plan"])
        result.append(g)
    return result


def _apply_course_groups(teacher_id, groups):
    """先建缺失的课程，再逐课执行学生动作。"""
    db = get_db()
    total = {"course_new": 0, "course_exists": 0,
             "create": 0, "join": 0, "skip": 0, "error": 0}
    for g in groups:
        cid = g["id"]
        if cid is None:
            cid = db.execute("INSERT INTO courses(name,description,teacher_id) VALUES(?,?,?)",
                             (g["name"], "", teacher_id)).lastrowid
            total["course_new"] += 1
            plan = _student_plan(None, g["rows"])
        else:
            total["course_exists"] += 1
            plan = _student_plan(cid, g["rows"])
        counts = _apply_student_plan(cid, plan)
        for key in ("create", "join", "skip", "error"):
            total[key] += counts[key]
    return total


@app.route("/course/<int:cid>/students/import", methods=["POST"])
@role_required("teacher")
def course_students_import(cid):
    """上传名单后先预览动作，确认前不写库。"""
    own_course(cid)
    raw, error = _read_import_file()
    if error:
        flash(error)
        return redirect(url_for("course_detail", cid=cid))
    try:
        rows, errors = roster.parse_csv(raw)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("course_detail", cid=cid))
    if not rows:
        flash("没有可导入的数据：" + ("；".join(errors[:3]) if errors else "文件为空"))
        return redirect(url_for("course_detail", cid=cid))
    plan = _student_plan(cid, rows)
    token = _stash_import(raw)
    course = get_db().execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    return render_template("roster_preview.html", course=course, plan=plan,
                           errors=errors, counts=_plan_counts(plan), token=token)


@app.route("/course/<int:cid>/students/import/confirm", methods=["POST"])
@role_required("teacher")
def course_students_import_confirm(cid):
    own_course(cid)
    raw = _take_import(request.form.get("token", ""))
    if raw is None:
        flash("导入内容已过期，请重新上传")
        return redirect(url_for("course_detail", cid=cid))
    try:
        rows, errors = roster.parse_csv(raw)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("course_detail", cid=cid))
    counts = _apply_student_plan(cid, _student_plan(cid, rows))
    log.info("导入学生名单 course=%s operator=%s#%s 新建=%s 加入=%s 已在课程=%s 无法处理=%s",
             cid, g.user["username"], g.user["id"], counts["create"], counts["join"],
             counts["skip"], counts["error"])
    msg = (f"导入完成：新建 {counts['create']} 人，已有学生加入 {counts['join']} 人，"
           f"已在课程 {counts['skip']} 人")
    if errors:
        msg += f"；{len(errors)} 行未处理：" + "；".join(errors[:5])
        if len(errors) > 5:
            msg += f" 等共 {len(errors)} 条"
    flash(msg)
    return redirect(url_for("course_detail", cid=cid))


@app.route("/course/<int:cid>/unenroll/<int:sid>", methods=["POST"])
@role_required("teacher")
def course_unenroll(cid, sid):
    own_course(cid)
    db = get_db()
    db.execute("DELETE FROM enrollments WHERE course_id=? AND student_id=?", (cid, sid))
    db.commit()
    return redirect(url_for("course_detail", cid=cid))


# ---------- 老师：作业 ----------
@app.route("/course/<int:cid>/homework/create", methods=["POST"])
@role_required("teacher")
def homework_create(cid):
    own_course(cid)
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("course_detail", cid=cid))
    answer_pdf = ""
    f = request.files.get("answer_pdf")
    if f and f.filename and allowed_file(f.filename):
        answer_pdf = save_upload(f)
    db = get_db()
    db.execute("INSERT INTO homeworks(course_id,title,description,deadline,answer_pdf) "
               "VALUES(?,?,?,?,?)",
               (cid, title, request.form.get("description", "").strip(),
                request.form.get("deadline", ""), answer_pdf))
    db.commit()
    return redirect(url_for("course_detail", cid=cid))


@app.route("/homework/<int:hid>")
@role_required("teacher")
def homework_detail(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    course = own_course(hw["course_id"])
    subs = db.execute("""
        SELECT s.*, u.display_name, u.username FROM submissions s
        JOIN users u ON u.id=s.student_id
        WHERE s.homework_id=? ORDER BY s.created_at DESC""", (hid,)).fetchall()
    return render_template("homework.html", hw=hw, course=course, subs=subs)



@app.route("/homework/<int:hid>/answer", methods=["POST"])
@role_required("teacher")
def homework_answer(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    f = request.files.get("answer_pdf")
    if f and f.filename and allowed_file(f.filename):
        db.execute("UPDATE homeworks SET answer_pdf=? WHERE id=?", (save_upload(f), hid))
        db.commit()
    return redirect(url_for("homework_detail", hid=hid))


@app.route("/homework/<int:hid>/delete", methods=["POST"])
@role_required("teacher")
def homework_delete(hid):
    db = get_db()
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    db.execute("DELETE FROM submissions WHERE homework_id=?", (hid,))
    db.execute("DELETE FROM homeworks WHERE id=?", (hid,))
    db.commit()
    return redirect(url_for("course_detail", cid=hw["course_id"]))


# ---------- 批改：学生作业与标准答案一起交给 AI，老师逐题核对 ----------
@app.route("/submission/<int:sid>/grade", methods=["POST"])
@role_required("teacher")
def submission_grade(sid):
    """把学生作业和教师版标准答案一起交给支持 PDF 的 AI，逐题批阅。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    own_course(hw["course_id"])
    if not hw["answer_pdf"]:
        flash("请先上传教师版标准答案 PDF，再开始批阅。")
        return redirect(url_for("homework_detail", hid=hw["id"]))

    log.info("开始批阅 submission=%s homework=%s file=%s answer=%s",
             sid, hw["id"], sub["file_path"], hw["answer_pdf"])
    gen = run_grading(g.user["id"], sub["file_path"], hw["answer_pdf"])
    if gen["error"]:
        log.warning("批阅失败 submission=%s 原因=%s", sid, gen["error"])
        flash(gen["error"])
        return redirect(url_for("homework_detail", hid=hw["id"]))

    db.execute("DELETE FROM questions WHERE submission_id=?", (sid,))
    for i, q in enumerate(gen["questions"], 1):
        comment = (q.get("comment") or "").strip()
        score = q.get("score")
        db.execute("INSERT INTO questions(submission_id,no,page_no,image_path,ocr_text,"
                   "standard_answer,score,note,max_score,verdict,ai_comment) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, q.get("no") or str(i), 1, "", q.get("ocr", ""),
                    q.get("standard_answer", ""), "" if score is None else str(score),
                    comment, _as_num(q.get("max_score")),
                    q.get("verdict", ""), comment))
    summary = gen.get("summary") or {}
    db.execute("UPDATE submissions SET status='待批改', ai_summary=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(summary, ensure_ascii=False) if summary else "", sid))
    db.commit()
    count = len(gen["questions"])
    if gen.get("demo"):
        msg = f"演示模式：未调用 AI，已生成 {count} 道题，请逐题修正内容"
    else:
        msg = f"AI 已批阅 {count} 道题，请逐题核对 OCR、判定与评语"
    flash(msg)
    return redirect(url_for("submission_review", sid=sid))


@app.route("/submission/<int:sid>/review")
@role_required("teacher")
def submission_review(sid):
    db = get_db()
    sub = db.execute("""
        SELECT s.*, u.display_name, u.username FROM submissions s
        JOIN users u ON u.id=s.student_id WHERE s.id=?""", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    course = own_course(hw["course_id"])
    questions = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (sid,)).fetchall()
    return render_template("review.html", sub=sub, hw=hw, course=course, questions=questions)


@app.route("/submission/<int:sid>/review/save", methods=["POST"])
@role_required("teacher")
def submission_review_save(sid):
    """保存逐题的 OCR 文本、得分、评语，以及总评。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    own_course(hw["course_id"])
    for q in db.execute("SELECT id FROM questions WHERE submission_id=?", (sid,)).fetchall():
        qid = q["id"]
        fields = {"ocr_text": request.form.get(f"ocr_{qid}", "").strip(),
                  "standard_answer": request.form.get(f"standard_{qid}", "").strip(),
                  "score": request.form.get(f"score_{qid}", "").strip(),
                  "note": request.form.get(f"note_{qid}", "").strip()}
        # 判定可人工修正；旧页面没有该字段时保持原值不动
        if f"verdict_{qid}" in request.form:
            v = request.form.get(f"verdict_{qid}", "").strip()
            fields["verdict"] = v if v in ("", "对", "半对", "错", "无法判定") else ""
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE questions SET {sets} WHERE id=?", (*fields.values(), qid))
    db.execute("UPDATE submissions SET score=?, feedback=?, status='已批改', grader=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (request.form.get("total_score", "").strip(),
                request.form.get("overall", "").strip(),
                f"{g.user['display_name']} 逐题批改", sid))
    db.commit()
    flash("批改已保存，学生可以查看")
    return redirect(url_for("submission_review", sid=sid))


@app.route("/submission/<int:sid>/correct", methods=["POST"])
@role_required("teacher")
def submission_correct(sid):
    """快速手动更正总分与总评。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    own_course(hw["course_id"])
    db.execute("UPDATE submissions SET score=?, feedback=?, status='已批改', grader=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (request.form.get("score", "").strip(),
                request.form.get("feedback", "").strip(),
                f"{g.user['display_name']} 手动更正", sid))
    db.commit()
    flash("批改结果已更新")
    return redirect(url_for("homework_detail", hid=sub["homework_id"]))


# ---------- 教案 ----------
@app.route("/course/<int:cid>/plan/create", methods=["POST"])
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


# ---------- 老师：AI 设置 ----------
@app.route("/settings", methods=["GET", "POST"])
@role_required("teacher")
def settings_page():
    if request.method == "POST":
        if request.form.get("demo_only"):
            save_setting(g.user["id"], "ai_demo", "1" if request.form.get("ai_demo") else "")
        else:
            for k in ("ai_api_url", "ai_api_key", "ai_model", "ai_prompt"):
                value = request.form.get(k, "").strip()
                if k == "ai_api_key" and not value:
                    continue  # Key 留空表示不修改，避免误清空
                save_setting(g.user["id"], k, value)
            save_setting(g.user["id"], "ai_demo", "1" if request.form.get("ai_demo") else "")
        get_db().commit()
        flash("已保存")
        return redirect(url_for("settings_page"))
    cfg = {k: get_setting(g.user["id"], k)
           for k in ("ai_api_url", "ai_api_key", "ai_model", "ai_prompt", "ai_demo")}
    return render_template("settings.html", cfg=cfg)


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
def student_homework(hid):
    db = get_db()
    hw = db.execute("""
        SELECT h.*, c.name AS course_name FROM homeworks h
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
    homeworks = db.execute("""
        SELECT h.*, c.name AS course_name,
          (SELECT COUNT(*) FROM submissions s
            WHERE s.homework_id=h.id AND s.student_id=?) AS my_subs,
          (SELECT s.status FROM submissions s
            WHERE s.homework_id=h.id AND s.student_id=?
            ORDER BY s.id DESC LIMIT 1) AS my_status,
          (SELECT s.score FROM submissions s
            WHERE s.homework_id=h.id AND s.student_id=?
            ORDER BY s.id DESC LIMIT 1) AS my_score
        FROM homeworks h
        JOIN courses c ON c.id=h.course_id
        JOIN enrollments e ON e.course_id=c.id AND e.student_id=?
        ORDER BY h.id DESC""",
        (g.user["id"],) * 4).fetchall()
    return render_template("student_home.html", homeworks=homeworks)


@app.route("/student/homework/<int:hid>", methods=["GET", "POST"])
@role_required("student")
def student_homework_page(hid):
    hw = student_homework(hid)
    db = get_db()
    if request.method == "POST":
        f = request.files.get("file")
        if not (f and f.filename and allowed_file(f.filename)):
            flash("请上传 png / jpg / webp / pdf 文件")
        else:
            db.execute("INSERT INTO submissions(homework_id,student_id,file_path) VALUES(?,?,?)",
                       (hid, g.user["id"], save_upload(f)))
            db.commit()
            flash("提交成功，等待老师批改")
        return redirect(url_for("student_homework_page", hid=hid))
    subs = db.execute("SELECT * FROM submissions WHERE homework_id=? AND student_id=? "
                      "ORDER BY id DESC", (hid, g.user["id"])).fetchall()
    # 只有老师复核完成（status=已批改）才向学生公开逐题明细；AI 原始结果不直接透出。
    # 明细按最新一次提交取，且不再依赖 score（默认定性批改时 score 为空）。
    latest = subs[0] if subs else None
    revealed = bool(latest) and latest["status"] == "已批改"
    items = []
    if revealed:
        items = db.execute("SELECT * FROM questions WHERE submission_id=? ORDER BY id",
                           (latest["id"],)).fetchall()
    return render_template("student_homework.html", hw=hw, subs=subs, items=items,
                           revealed=revealed)


# ---------- 工具 ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def save_upload(file_storage):
    orig = secure_filename(file_storage.filename or "upload")
    ext = orig.rsplit(".", 1)[1].lower() if "." in orig else "bin"
    fname = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_{orig or 'file.' + ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, fname))
    return fname


def demo_questions(count=6):
    """演示模式：生成占位批阅结果，用于没有 AI 接口时体验流程。"""
    return [{"no": str(i), "ocr": f"（演示模式）第 {i} 题 OCR 占位文本，请手动修改成学生实际作答。",
             "standard_answer": f"（演示模式）请在此填写第 {i} 题的标准答案。",
             "verdict": "半对", "score": 0.0, "max_score": 0.0,
             "comment": "（演示模式）请手动核对并修改。"}
            for i in range(1, count + 1)]


def _as_num(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _build_summary(questions, weak_points, evaluation, errors):
    """总分/满分/判定分布一律由后端按题汇总，不依赖模型跨批统计。"""
    return {
        "total": sum(_as_num(q.get("score")) for q in questions),
        "max_total": sum(_as_num(q.get("max_score")) for q in questions),
        "right": [q["no"] for q in questions if q.get("verdict") == "对"],
        "partial": [q["no"] for q in questions if q.get("verdict") == "半对"],
        "wrong": [q["no"] for q in questions if q.get("verdict") == "错"],
        "unknown": [q["no"] for q in questions if q.get("verdict") == "无法判定"],
        "weak_points": weak_points,
        "evaluation": evaluation,
        "errors": errors,
    }


def run_grading(teacher_id, file_name, answer_pdf=""):
    """批阅入口：学生作业与标准答案整份交给支持 PDF 的模型，一次返回逐题结果。"""
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

    gen = grade_pdf_submission(
        os.path.join(UPLOAD_DIR, file_name),
        os.path.join(UPLOAD_DIR, answer_pdf) if answer_pdf else "",
        api_url, api_key, model,
        extra=get_setting(teacher_id, "ai_prompt").strip())
    gen["summary"] = _build_summary(gen["questions"], gen.get("weak_points") or [],
                                    gen.get("evaluation", ""), gen.get("errors") or [])
    return gen


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

    add("missing_submission_file", "error", "提交记录对应的作业文件已丢失",
        [(r["id"],) for r in missing(
            db.execute("SELECT id, file_path FROM submissions").fetchall(), "file_path")],
        detail="原文件不可下载，批改结果仍在")

    add("missing_answer_pdf", "warn", "作业的教师版 PDF 丢失",
        [(r["id"],) for r in missing(
            db.execute("SELECT id, answer_pdf FROM homeworks WHERE answer_pdf != ''").fetchall(),
            "answer_pdf")], detail="缺少标准答案 PDF 时无法进行 AI 批阅")
    return issues


@app.route("/admin/consistency")
@role_required("admin")
def consistency_page():
    stats = {t: get_db().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("users", "courses", "homeworks", "submissions", "questions")}
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
                  for t in ("users", "courses", "homeworks", "submissions", "questions")}
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


@app.route("/admin/restore", methods=["POST"])
@role_required("admin")
def admin_restore():
    """从导出的 zip 恢复：先校验包结构与路径安全，备份当前数据，再替换附件与数据库。"""
    up = request.files.get("backup")
    if not up or not up.filename:
        flash("请选择此前导出的 zip 备份文件")
        return redirect(url_for("consistency_page"))

    tmpdir = tempfile.mkdtemp(prefix=".restore_", dir=cfg.DATA_DIR)
    src = None
    try:
        zpath = os.path.join(tmpdir, "backup.zip")
        up.save(zpath)
        if not zipfile.is_zipfile(zpath):
            flash("不是有效的 zip 备份包")
            return redirect(url_for("consistency_page"))

        extract_root = os.path.join(tmpdir, "data")
        os.makedirs(extract_root)
        limit = cfg.MAX_RESTORE_MB * 1024 * 1024
        total = 0
        with zipfile.ZipFile(zpath) as z:
            members = z.infolist()
            if len(members) > 50000:
                flash("备份包文件数量异常，已拒绝导入")
                return redirect(url_for("consistency_page"))
            for info in members:
                target = os.path.abspath(os.path.join(extract_root, info.filename))
                if target != extract_root and not target.startswith(extract_root + os.sep):
                    log.warning("备份包含越界路径：%s", info.filename)
                    flash("备份包包含非法路径，已拒绝导入")
                    return redirect(url_for("consistency_page"))
                total += info.file_size
                if total > limit:
                    flash(f"备份包解压后超过 {cfg.MAX_RESTORE_MB}MB 上限，已拒绝导入")
                    return redirect(url_for("consistency_page"))
            z.extractall(extract_root)

        src_db = os.path.join(extract_root, "data.sqlite")
        if not os.path.isfile(src_db):
            flash("备份包缺少 data.sqlite，不是本系统导出的备份")
            return redirect(url_for("consistency_page"))
        src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
        tables = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"users", "homeworks", "submissions"} <= tables:
            flash("备份包里的数据库结构不符合本系统，已拒绝导入")
            return redirect(url_for("consistency_page"))
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            flash("备份包里的数据库未通过完整性检查，已拒绝导入")
            return redirect(url_for("consistency_page"))

        # 破坏性操作前，先把当前数据完整备份一份，失败可人工回退
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = os.path.join(cfg.DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        cur = sqlite3.connect(DB_PATH, timeout=10)
        bk = sqlite3.connect(os.path.join(bdir, f"pre_restore_{ts}.sqlite"))
        try:
            cur.backup(bk)
            src.backup(cur)  # 用 backup API 覆盖，避免替换文件句柄影响正在处理的请求
        finally:
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
        if src is not None:
            src.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _student_files(uid):
    """学生可访问的文件白名单：自己的提交原件 + 自己提交下的题目裁剪图。

    教师版答案、他人作业一律不在集合内，避免知道路径就能下载。
    """
    db = get_db()
    paths = set()
    for r in db.execute("SELECT file_path FROM submissions WHERE student_id=?", (uid,)):
        if r["file_path"]:
            paths.add(os.path.normpath(r["file_path"]).replace("\\", "/"))
    for r in db.execute("SELECT q.image_path FROM questions q "
                        "JOIN submissions s ON s.id=q.submission_id "
                        "WHERE s.student_id=? AND q.image_path != ''", (uid,)):
        paths.add(os.path.normpath(r["image_path"]).replace("\\", "/"))
    return paths


@app.route("/files/<path:filename>")
@login_required
def uploaded_file(filename):
    if g.user["role"] == "student":
        wanted = os.path.normpath(filename).replace("\\", "/")
        if wanted not in _student_files(g.user["id"]):
            log.warning("越权访问文件 student=%s path=%s", g.user["id"], wanted)
            abort(403)
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
