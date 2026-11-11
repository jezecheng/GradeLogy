# -*- coding: utf-8 -*-
"""化学竞赛作业管理平台
角色：admin（用户管理）/ teacher（课程、作业、批改、AI 设置）/ student（提交与查看批改）
"""
import base64
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import workflow as wf
from utilities import storage
from utilities.prepare import prepare_answer_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "data.sqlite")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "pdf"}
IMG_EXT = {"png", "jpg", "jpeg", "webp"}

# macOS 自带 Python 往往没有 CA 证书链，用 certifi 兜底，避免 HTTPS 调用 AI 时报 SSL 错误
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

ROLE_LABEL = {"admin": "管理员", "teacher": "老师", "student": "学生"}

def load_secret_key():
    """密钥持久化，避免每次重启把所有人都登出。"""
    path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _json_or_none(value):
    """模板里把 ai_summary 的 JSON 字符串转成 dict；空值或坏数据一律返回 None。"""
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


app.jinja_env.filters["fromjson"] = _json_or_none

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
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
CREATE TABLE IF NOT EXISTS pipelines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  teacher_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  graph_json TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS homework_questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  homework_id INTEGER NOT NULL REFERENCES homeworks(id),
  no TEXT DEFAULT '',
  page_no INTEGER DEFAULT 1,
  image_path TEXT DEFAULT '',
  answer_text TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(homework_id, no));
"""

# 阶段一：只识别题目位置（题号 + 坐标），输出极小，不会被截断
DETECT_PROMPT = (
    "你是试卷切题助手。输入是学生手写作答的扫描页（可能多页）。\n"
    "请识别每页中每道题的作答区域，只输出 JSON，不要输出解释或代码块标记：\n"
    '{"questions":[{"no":"3.1","page":1,"bbox":[x1,y1,x2,y2]}]}\n'
    "要求：\n"
    "1. bbox 用 0-1000 归一化坐标（以该页左上角为原点），紧贴该题作答区，禁止跨题合并；\n"
    "2. no 用题面标注的题号，保留原样（如 3.1、例1.1、4(1)、选3）；纯字母子选项如 (a)(b)(c) 忽略，不算独立题；\n"
    "3. page 必须填页面标签中给出的真实页码，禁止从 1 重新编号；\n"
    "4. 某页没有学生作答（正文页/空白页）时，questions 里就不要放该页；只输出真正有作答题目的页。\n"
    "5. 只输出题目位置，不要写答案、不要批改。")

# 阶段三：单题批改——只看一道题的裁剪图 + 该题标准答案，输出小而稳
GRADE_PROMPT = (
    "你是化学竞赛阅卷老师。输入是一道学生手写作答的裁剪图，以及该题的标准答案。\n"
    "请批改本题，只输出 JSON，不要输出解释或代码块标记：\n"
    '{"ocr":"学生写出的完整内容","standard_answer":"标准答案要点",'
    '"verdict":"对|半对|错","max_score":2,"score":2,'
    '"comment":"给分/扣分说明，指出具体失分点"}\n'
    "判分规则：\n"
    "1. 化学方程式/离子方程式必须查配平、条件、状态符号、可逆号；数值题查有效数字与单位。\n"
    "2. 只按学生实际写出的内容判，不得推测未写内容；OCR 严格忠实，看不清写〔不清〕。\n"
    "3. 一题若有多个空，分别对照并在 comment 说明哪一空失分。\n"
    "4. 不给印象分；未作答的题 score=0，comment 写\"未作答\"。\n"
    "5. comment 不超过 60 个汉字，standard_answer 不超过 60 个汉字；只写关键结论，不写完整推导。")

DETECT_BATCH_PAGES = 1  # 单页最稳：混入空页不会拖累整批


def _request_json(api_url, api_key, model, content, max_tokens):
    """通用 OpenAI 兼容调用，返回 (reply, finish_reason, error)。失败会自动重试（含 429 退避）。"""
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        f"{api_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            choice = data["choices"][0]
            return (choice["message"].get("content") or ""),\
                    (choice.get("finish_reason") or "").lower(), ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code == 429 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return "", "", f"HTTP {e.code} {detail}"
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            return "", "", str(e)
    return "", "", "请求多次失败"


def match_answer(no, text, window=400):
    """在教师版答案文本中按题号定位，返回附近的答案片段；找不到返回空串。"""
    if not no or not text:
        return ""
    pat = re.escape(str(no))
    m = re.search(r"(?<![A-Za-z0-9.])" + pat + r"(?![0-9])", text)
    if not m:
        return ""
    start = max(0, m.start() - 15)
    end = min(len(text), m.start() + window)
    return text[start:end].strip()



# ---------- 数据库 ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
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
    # 轻量迁移：作业可绑定自定义批改流程（0 = 默认流程）
    hcols = {r[1] for r in con.execute("PRAGMA table_info(homeworks)")}
    if "pipeline_id" not in hcols:
        con.execute("ALTER TABLE homeworks ADD COLUMN pipeline_id INTEGER DEFAULT 0")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
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


# ---------- 登录与权限 ----------
@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = None
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if g.user is None:
            session.clear()


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
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("index"))
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
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("密码已更新")
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
    role = request.form.get("role", "student")
    password = request.form.get("password", "") or "123456"
    if not username:
        flash("用户名不能为空")
    elif role not in ("teacher", "student", "admin"):
        flash("角色不合法")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
                       (username, generate_password_hash(password), name, role))
            db.commit()
            flash(f"已创建{ROLE_LABEL[role]}账号 {username}，初始密码 {password}")
        except sqlite3.IntegrityError:
            flash(f"用户名 {username} 已存在")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/reset", methods=["POST"])
@role_required("admin")
def admin_user_reset(uid):
    password = request.form.get("password", "").strip() or "123456"
    db = get_db()
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), uid))
    db.commit()
    flash(f"密码已重置为 {password}")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@role_required("admin")
def admin_user_delete(uid):
    if uid == g.user["id"]:
        flash("不能删除当前登录的管理员账号")
        return redirect(url_for("admin_users"))
    db = get_db()
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


@app.route("/course/<int:cid>")
@role_required("teacher", "admin")
def course_detail(cid):
    db = get_db()
    course = own_course(cid)
    students = db.execute("""
        SELECT u.id, u.username, u.display_name FROM enrollments e
        JOIN users u ON u.id=e.student_id WHERE e.course_id=? ORDER BY u.display_name""",
        (cid,)).fetchall()
    candidates = db.execute("""
        SELECT u.id, u.username, u.display_name FROM users u
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
    pipelines = db.execute("SELECT id, name FROM pipelines WHERE teacher_id=? ORDER BY id DESC",
                           (g.user["id"],)).fetchall()
    answer_count = db.execute("SELECT COUNT(*) AS c FROM homework_questions WHERE homework_id=?",
                              (hid,)).fetchone()["c"]
    return render_template("homework.html", hw=hw, course=course, subs=subs,
                           pipelines=pipelines, answer_count=answer_count)


@app.route("/homework/<int:hid>/extract", methods=["POST"])
@role_required("teacher")
def homework_extract(hid):
    """教师版讲义一次性预处理：切题 + 裁题目图 + 提取标准答案，缓存到 homework_questions。"""
    db = get_db()
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    course = own_course(hw["course_id"])
    if not hw["answer_pdf"]:
        flash("请先上传教师版答案 PDF")
        return redirect(url_for("homework_detail", hid=hid))

    api_url = get_setting(g.user["id"], "ai_api_url").rstrip("/")
    api_key = get_setting(g.user["id"], "ai_api_key")
    if not api_url or not api_key:
        flash("尚未配置 AI 接口，请先到「AI 设置」填写接口地址与密钥。")
        return redirect(url_for("homework_detail", hid=hid))

    attach_dir = os.path.join(storage.attach_dir(course["name"], hw["title"]), "answer")
    os.makedirs(os.path.join(UPLOAD_DIR, attach_dir), exist_ok=True)
    items, errors = prepare_answer_key(
        hw["answer_pdf"], attach_dir, api_url, api_key,
        get_setting(g.user["id"], "ai_model") or "gpt-4o")

    if not items:
        flash("未从教师版讲义中提取到题目" + (f"：{errors[0]}" if errors else ""))
        return redirect(url_for("homework_detail", hid=hid))

    db.execute("DELETE FROM homework_questions WHERE homework_id=?", (hid,))
    for it in items:
        db.execute("INSERT INTO homework_questions(homework_id,no,page_no,image_path,answer_text) "
                   "VALUES(?,?,?,?,?)",
                   (hid, it["no"], it["page"], it["image_path"], it["answer"]))
    db.commit()
    msg = f"已提取 {len(items)} 道题的标准答案（重跑会覆盖旧结果）"
    if errors:
        msg += f"；{len(errors)} 个批次未完整处理：{errors[0]}"
    flash(msg)
    return redirect(url_for("homework_detail", hid=hid))


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


# ---------- 批改：AI 按题切分 + 老师逐题核对 ----------
@app.route("/submission/<int:sid>/segment", methods=["POST"])
@role_required("teacher")
def submission_segment(sid):
    """调用 AI 把扫描件按题切分成小图，并生成 OCR 文本与标准答案。"""
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    if not sub:
        abort(404)
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (sub["homework_id"],)).fetchone()
    course = own_course(hw["course_id"])
    student = db.execute("SELECT display_name, username FROM users WHERE id=?",
                         (sub["student_id"],)).fetchone()
    student_name = (student["display_name"] or student["username"]) if student else str(sub["student_id"])
    # 附件按 课程/讲次/ 组织，命名 {学生}_p{页码} / {学生}_q_{题号}，重跑覆盖不堆碎片
    attach = {"dir": storage.attach_dir(course["name"], hw["title"]),
              "student": student_name}
    answer_key = {r["no"]: r["answer_text"] for r in db.execute(
        "SELECT no, answer_text FROM homework_questions WHERE homework_id=? AND answer_text!=''",
        (hw["id"],)).fetchall()}

    gen = run_grading(g.user["id"], sub["file_path"], hw["answer_pdf"], hw["pipeline_id"],
                      attach, answer_key)
    if gen["error"]:
        flash(gen["error"])
        return redirect(url_for("homework_detail", hid=sub["homework_id"]))

    db.execute("DELETE FROM questions WHERE submission_id=?", (sid,))
    pages = gen["pages"] or [sub["file_path"]]
    for i, q in enumerate(gen["questions"], 1):
        pno = _as_int(q.get("page"), 1)
        pno = pno if 1 <= pno <= len(pages) else 1
        # call_ai_segment 已按 bbox 裁好每题小图；只有缺失时才兜底重裁
        stem = storage.question_name(student_name, q.get("no") or i)[:-4]
        img = q.get("image_path") or crop_question_image(pages[pno - 1], q.get("bbox"),
                                                         stem, subdir=attach["dir"])
        score = q.get("score")
        score = "" if score is None else str(score)
        comment = (q.get("comment") or "").strip()
        db.execute("INSERT INTO questions(submission_id,no,page_no,image_path,ocr_text,"
                   "standard_answer,score,note,max_score,verdict,ai_comment) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, q.get("no") or str(i), pno, img, q.get("ocr", ""),
                    q.get("standard_answer", ""), score, comment,
                    _as_num(q.get("max_score")), q.get("verdict", ""), comment))
    summary = gen.get("summary") or {}
    db.execute("UPDATE submissions SET status='待批改', ai_summary=?, "
               "updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(summary, ensure_ascii=False) if summary else "", sid))
    db.commit()
    count = len(gen["questions"])
    if gen.get("demo"):
        msg = f"演示模式：未调用 AI，已按等分生成 {count} 道题，请逐题修正内容"
    else:
        msg = f"AI 已批改 {count} 道题，请逐题核对 OCR、AI 建议分数与评语"
        if gen.get("errors"):
            msg += f"（{len(gen['errors'])} 个批次未完整处理：{gen['errors'][0]}）"
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
        db.execute("UPDATE questions SET ocr_text=?, standard_answer=?, score=?, note=? WHERE id=?",
                   (request.form.get(f"ocr_{qid}", "").strip(),
                    request.form.get(f"standard_{qid}", "").strip(),
                    request.form.get(f"score_{qid}", "").strip(),
                    request.form.get(f"note_{qid}", "").strip(), qid))
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
    items = db.execute("""
        SELECT q.* FROM questions q JOIN submissions s ON s.id=q.submission_id
        WHERE s.homework_id=? AND s.student_id=? AND q.score != ''
        ORDER BY q.id""", (hid, g.user["id"])).fetchall()
    return render_template("student_homework.html", hw=hw, subs=subs, items=items)


# ---------- 工具 ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def save_upload(file_storage):
    orig = secure_filename(file_storage.filename or "upload")
    ext = orig.rsplit(".", 1)[1].lower() if "." in orig else "bin"
    fname = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}_{orig or 'file.' + ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, fname))
    return fname


def pdf_text(path):
    try:
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)[:8000]
    except ImportError:
        pass
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(path)
        parts = [doc[i].get_textpage().get_text_range() for i in range(min(len(doc), 5))]
        return "\n".join(parts)[:8000]
    except Exception:
        return None


def parse_json_block(text):
    """从模型回复里抠出 JSON（容忍 Markdown 代码块和前后废话）。"""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def crop_question_image(file_name, bbox, stem, subdir=""):
    """按 bbox 裁剪题目小图，返回相对 uploads 的文件名；失败返回空串（前端退化为显示整张扫描件）。

    subdir: 输出子目录（相对 uploads），需已存在；stem 为确定性文件名，重跑覆盖。
    """
    if not bbox or len(bbox) != 4:
        return ""
    src = os.path.join(UPLOAD_DIR, file_name)
    if file_name.rsplit(".", 1)[-1].lower() not in IMG_EXT:
        return ""
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB")
        w, h = img.size
        nums = [float(v) for v in bbox]
        scale = 1000.0 if max(nums) <= 1000 else 1.0
        x1, y1, x2, y2 = (nums[0] / scale * w, nums[1] / scale * h,
                          nums[2] / scale * w, nums[3] / scale * h)
        pad = 8
        box = (max(0, int(min(x1, x2)) - pad), max(0, int(min(y1, y2)) - pad),
               min(w, int(max(x1, x2)) + pad), min(h, int(max(y1, y2)) + pad))
        if box[2] - box[0] < 20 or box[3] - box[1] < 20:
            return ""
        out = f"{stem}.jpg"
        rel = os.path.join(subdir, out) if subdir else out
        img.crop(box).save(os.path.join(UPLOAD_DIR, rel), quality=88)
        return rel
    except Exception:
        return ""


def pdf_to_images(file_name, max_pages=12, subdir="", stem=""):
    """把 PDF 逐页渲染成图片，返回相对 uploads 的文件名列表；失败返回空列表。

    subdir/stem: 指定后输出到子目录且命名为 {stem}_p{页码}.jpg（确定性，重跑覆盖）。
    """
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(os.path.join(UPLOAD_DIR, file_name))
        out = []
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            img = page.render(scale=2.0).to_pil()
            name = f"{stem}_p{i + 1}.jpg" if stem else f"page_{secrets.token_hex(5)}_p{i + 1}.jpg"
            rel = os.path.join(subdir, name) if subdir else name
            img.convert("RGB").save(os.path.join(UPLOAD_DIR, rel), quality=90)
            out.append(rel)
        return out
    except Exception:
        return []


def demo_questions(page_count):
    """演示模式：每页纵向等分 4 段生成占位题目，用于没有 AI 接口时体验流程。"""
    out = []
    for p in range(1, page_count + 1):
        for k in range(4):
            no = len(out) + 1
            out.append({"no": str(no), "page": p,
                        "ocr": f"（演示模式）第 {no} 题 OCR 占位文本，请手动修改成学生实际作答。",
                        "standard_answer": f"（演示模式）请在此填写第 {no} 题的标准答案。",
                        "bbox": [40, k * 250 + 15, 960, (k + 1) * 250 - 15]})
    return out


def answer_content(answer_pdf, max_pages=3):
    """标准答案内容：有文字层就用文字，扫描版 PDF 转成图片一起发给模型。"""
    text = pdf_text(os.path.join(UPLOAD_DIR, answer_pdf))
    if text and len(text.strip()) > 20:
        return [{"type": "text", "text": f"标准答案（供对齐，不要照抄题号）：\n{text}"}]
    out = []
    for i, pg in enumerate(pdf_to_images(answer_pdf, max_pages=max_pages), 1):
        b64 = base64.b64encode(open(os.path.join(UPLOAD_DIR, pg), "rb").read()).decode()
        out.append({"type": "text", "text": f"标准答案第 {i} 页（图片，供对齐）："})
        out.append({"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return out


# ---------- 逐题 AI 批改：分批请求 + 页码校正 + 结果合并 ----------
BATCH_PAGES = 3  # 实测 3 页一次约 116 秒，再多会超时/超限


def _as_int(value, default=1):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_num(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bbox_area(bbox):
    """归一化 bbox 的面积，用于同题号跨批重复时挑更完整的一条。"""
    try:
        nums = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return 0.0
    if len(nums) != 4:
        return 0.0
    return abs((nums[2] - nums[0]) * (nums[3] - nums[1]))


def _natural_key(no):
    """「3.1 / 3.2 / 4.1 / 10」这类题号按版本号式排序，纯数字也能正确排。"""
    key = []
    for part in re.split(r"(\d+)", str(no)):
        if part == "":
            continue
        key.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
    return key


def _fix_page(page, offset, size):
    """模型会把每批页码从 1 重编（发第 8-10 页却返回 1,2,3），这里校正成全局真实页码。"""
    if page <= offset:
        page = offset + page
    if not offset < page <= offset + size:
        page = offset + 1
    return page


def _salvage_questions(text):
    """JSON 被截断时，按括号配对逐个抢救 `"questions":[` 里已完整输出的题目对象。"""
    key = text.find('"questions"')
    if key < 0:
        return []
    arr = text.find("[", key)
    if arr < 0:
        return []
    out, depth, obj_at, in_str, esc = [], 0, -1, False, False
    for i in range(arr + 1, len(text)):
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
            if depth == 0:
                obj_at = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_at >= 0:
                    try:
                        obj = json.loads(text[obj_at:i + 1])
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict):
                        out.append(obj)
                    obj_at = -1
        elif ch == "]" and depth == 0:
            break
    return out


def _build_summary(questions, weak_points, evaluation, errors):
    """总分/满分/对错分布一律由后端按题汇总，不依赖模型跨批统计。"""
    return {
        "total": sum(_as_num(q.get("score")) for q in questions),
        "max_total": sum(_as_num(q.get("max_score")) for q in questions),
        "right": [q["no"] for q in questions if q.get("verdict") == "对"],
        "partial": [q["no"] for q in questions if q.get("verdict") == "半对"],
        "wrong": [q["no"] for q in questions if q.get("verdict") == "错"],
        "weak_points": weak_points,
        "evaluation": evaluation,
        "errors": errors,
    }


def call_ai_segment(teacher_id, file_name, answer_pdf="", attach=None):
    """三步流水线：①按页切题 → ②在教师版答案中定位该题 → ③逐题判分。

    attach: {"dir": 附件子目录, "student": 学生名}，有组织地存放页面图与题目裁剪图。
    """
    attach = attach or {}
    adir = attach.get("dir", "")
    student = attach.get("student", "")
    ext = file_name.rsplit(".", 1)[1].lower()
    if ext in IMG_EXT:
        pages = [file_name]
    elif ext == "pdf":
        pages = pdf_to_images(file_name, subdir=adir, stem=student)
        if not pages:
            return {"questions": [], "pages": [], "summary": {}, "errors": [],
                    "error": "PDF 无法转成图片（请确认未加密且是扫描版），或安装 pypdfium2 后重试。"}
    else:
        return {"questions": [], "pages": [], "summary": {}, "errors": [],
                "error": "不支持的文件类型。"}

    if get_setting(teacher_id, "ai_demo") == "1":
        return {"questions": demo_questions(len(pages)), "pages": pages, "summary": {},
                "errors": [], "error": "", "demo": True}

    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    model = get_setting(teacher_id, "ai_model") or "gpt-4o"
    extra = get_setting(teacher_id, "ai_prompt").strip()
    if not api_url or not api_key:
        return {"questions": [], "pages": pages, "summary": {}, "errors": [],
                "error": "尚未配置 AI 接口。可以先勾选「演示模式」体验完整流程，或到「AI 设置」填写接口地址与密钥。"}

    answer_text = pdf_text(os.path.join(UPLOAD_DIR, answer_pdf)) if answer_pdf else ""

    # ---------- 阶段一：逐页（批）识别题目位置 ----------
    detect_prompt = DETECT_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    detected = []
    errors = []
    for start in range(0, len(pages), DETECT_BATCH_PAGES):
        batch = pages[start:start + DETECT_BATCH_PAGES]
        last = start + len(batch)
        content = [{"type": "text", "text": (
            detect_prompt +
            f"\n本次为学生作答第 {start + 1} 页至第 {last} 页，共 {len(batch)} 页；"
            f"page 字段必须填这些真实页码（{start + 1}-{last}），不要从 1 重新编号。")}]
        for i, pg in enumerate(batch):
            content.append({"type": "text", "text": f"学生作答第 {start + i + 1} 页："})
            b64 = base64.b64encode(open(os.path.join(UPLOAD_DIR, pg), "rb").read()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        reply, finish, err = _request_json(api_url, api_key, model, content, max_tokens=3000)
        span = f"第 {start + 1}-{last} 页"
        if err:
            errors.append(f"{span}切题失败：{err}")
            continue
        parsed = parse_json_block(reply)
        raw = parsed.get("questions", []) if parsed else []
        if not raw and finish == "length":
            raw = _salvage_questions(reply)
        if not raw:
            errors.append(f"{span}：未识别到题目")
            continue
        for q in raw:
            if not isinstance(q, dict):
                continue
            detected.append({
                "no": str(q.get("no", "")).strip(),
                "page": _fix_page(_as_int(q.get("page"), start + 1), start, len(batch)),
                "bbox": q.get("bbox"),
            })

    if not detected:
        detail = "；".join(errors) if errors else "AI 未返回任何题目位置。"
        return {"questions": [], "pages": pages, "summary": {}, "errors": errors,
                "error": f"AI 切题失败：{detail}"}

    # 同一页的同一题号去重，保留 bbox 面积更大的那条
    merged = {}
    for q in detected:
        key = (q["page"], q["no"])
        old = merged.get(key)
        if old is None or _bbox_area(q.get("bbox")) > _bbox_area(old.get("bbox")):
            merged[key] = q
    detected = sorted(merged.values(), key=lambda q: (q["page"], _natural_key(q["no"])))

    # ---------- 阶段二 + 三：逐题裁剪 → 匹配答案 → 单题批改 ----------
    grade_prompt = GRADE_PROMPT + (f"\n补充要求：{extra}" if extra else "")
    questions = []
    for i, q in enumerate(detected, 1):
        pno = q["page"]
        pg = pages[pno - 1] if 1 <= pno <= len(pages) else pages[0]
        stem = storage.question_name(student, q["no"])[:-4] if student else f"q{i}"
        img = crop_question_image(pg, q.get("bbox"), stem, subdir=adir)
        if not img:
            img = pg
        ans = match_answer(q["no"], answer_text)

        content = [{"type": "text", "text": grade_prompt}]
        if ans:
            content.append({"type": "text", "text": f"该题标准答案（供对照）：\n{ans}"})
        else:
            content.append({"type": "text", "text": "未在教师版中定位到该题标准答案，按常识与化学规范判分。"})
        b64 = base64.b64encode(open(os.path.join(UPLOAD_DIR, img), "rb").read()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        reply, finish, err = _request_json(api_url, api_key, model, content, max_tokens=1500)
        if err:
            errors.append(f"第 {q['no']} 题批改失败：{err}")
            questions.append({
                "no": q["no"], "page": q["page"], "bbox": q.get("bbox"),
                "image_path": img, "ocr": "", "standard_answer": ans,
                "max_score": 0.0, "score": None, "verdict": "", "comment": ""})
            continue
        parsed = parse_json_block(reply)
        if not parsed:
            parsed = {}
        questions.append({
            "no": q["no"], "page": q["page"], "bbox": q.get("bbox"),
            "image_path": img,
            "ocr": str(parsed.get("ocr", "")).strip(),
            "standard_answer": str(parsed.get("standard_answer", ans)).strip() or ans,
            "max_score": _as_num(parsed.get("max_score"), 0.0),
            "score": parsed.get("score"),
            "verdict": str(parsed.get("verdict", "")).strip(),
            "comment": str(parsed.get("comment", "")).strip(),
        })

    if not questions:
        return {"questions": [], "pages": pages, "summary": {}, "errors": errors,
                "error": "AI 未生成任何题目。"}

    return {"questions": questions, "pages": pages,
            "summary": _build_summary(questions, [], "", errors),
            "errors": errors, "error": ""}


def run_grading(teacher_id, file_name, answer_pdf="", pipeline_id=0, attach=None, answer_key=None):
    """批改入口：走流程执行器（作业绑定的自定义流程或内置 DEFAULT_PIPELINE）。

    attach: {"dir": 附件子目录, "student": 学生名}，由 submission_segment 按课程/讲次/学生生成。
    answer_key: {题号: 标准答案}，来自 homework_questions 缓存；为空则批改时现场匹配。
    """
    attach = attach or {}
    if get_setting(teacher_id, "ai_demo") == "1":
        return call_ai_segment(teacher_id, file_name, answer_pdf, attach)

    ext = file_name.rsplit(".", 1)[1].lower()
    if ext in IMG_EXT:
        pages = [file_name]
    elif ext == "pdf":
        pages = pdf_to_images(file_name, subdir=attach.get("dir", ""),
                              stem=attach.get("student", ""))
        if not pages:
            return {"questions": [], "pages": [], "summary": {}, "errors": [],
                    "error": "PDF 无法转成图片（请确认未加密且是扫描版），或安装 pypdfium2 后重试。"}
    else:
        return {"questions": [], "pages": [], "summary": {}, "errors": [],
                "error": "不支持的文件类型。"}

    api_url = get_setting(teacher_id, "ai_api_url").rstrip("/")
    api_key = get_setting(teacher_id, "ai_api_key")
    if not api_url or not api_key:
        return {"questions": [], "pages": pages, "summary": {}, "errors": [],
                "error": "尚未配置 AI 接口。可以先勾选「演示模式」体验完整流程，或到「AI 设置」填写接口地址与密钥。"}

    graph = wf.DEFAULT_PIPELINE
    if pipeline_id:
        row = get_db().execute(
            "SELECT graph_json FROM pipelines WHERE id=? AND teacher_id=?",
            (pipeline_id, teacher_id)).fetchone()
        if row:
            try:
                graph = json.loads(row["graph_json"])
            except (TypeError, ValueError):
                pass

    state = {"pages": pages,
             "answer_text": pdf_text(os.path.join(UPLOAD_DIR, answer_pdf)) if answer_pdf else "",
             "file_path": file_name,
             "attach_dir": attach.get("dir", ""),
             "student": attach.get("student", ""),
             "answer_key": answer_key or {}}
    cfg = {"api_url": api_url, "api_key": api_key,
           "model": get_setting(teacher_id, "ai_model") or "gpt-4o",
           "extra": get_setting(teacher_id, "ai_prompt").strip()}
    return wf.run_pipeline(graph, state, cfg)


# ---------- 流程设计器 ----------
def _load_manifest():
    with open(os.path.join(BASE_DIR, "utilities", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


@app.route("/workflow/design")
@role_required("teacher")
def workflow_design():
    pipeline = None
    pid = request.args.get("pid", type=int)
    if pid:
        pipeline = get_db().execute(
            "SELECT * FROM pipelines WHERE id=? AND teacher_id=?",
            (pid, g.user["id"])).fetchone()
        if not pipeline:
            abort(404)
    return render_template("workflow_design.html", manifest=_load_manifest(), pipeline=pipeline)


@app.route("/workflow/save", methods=["POST"])
@role_required("teacher")
def workflow_save():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or "未命名流程"
    graph = data.get("graph") or {}
    nodes = graph.get("nodes") or []
    if not nodes:
        return {"ok": False, "error": "流程为空，请先拖入节点"}, 400
    _, err = wf.topo_sort(nodes, graph.get("connections") or [])
    if err:
        return {"ok": False, "error": f"流程不合法：{err}"}, 400
    db = get_db()
    pid = data.get("pid")
    if pid:
        cur = db.execute("UPDATE pipelines SET name=?, graph_json=? WHERE id=? AND teacher_id=?",
                         (name, json.dumps(graph, ensure_ascii=False), pid, g.user["id"]))
        if cur.rowcount == 0:
            abort(404)
    else:
        cur = db.execute("INSERT INTO pipelines(teacher_id,name,graph_json) VALUES(?,?,?)",
                         (g.user["id"], name, json.dumps(graph, ensure_ascii=False)))
        pid = cur.lastrowid
    db.commit()
    return {"ok": True, "id": pid}


@app.route("/workflow/list")
@role_required("teacher")
def workflow_list():
    rows = get_db().execute("SELECT * FROM pipelines WHERE teacher_id=? ORDER BY id DESC",
                            (g.user["id"],)).fetchall()
    return render_template("workflow_list.html", pipelines=rows)


@app.route("/homework/<int:hid>/pipeline", methods=["POST"])
@role_required("teacher")
def homework_pipeline(hid):
    """给作业绑定批改流程（0 = 默认流程）。"""
    db = get_db()
    hw = db.execute("SELECT * FROM homeworks WHERE id=?", (hid,)).fetchone()
    if not hw:
        abort(404)
    own_course(hw["course_id"])
    try:
        pid = int(request.form.get("pipeline_id", "0"))
    except ValueError:
        pid = 0
    db.execute("UPDATE homeworks SET pipeline_id=? WHERE id=?", (pid, hid))
    db.commit()
    flash("批改流程已更新")
    return redirect(url_for("homework_detail", hid=hid))


@app.route("/files/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
