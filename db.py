# -*- coding: utf-8 -*-
"""数据库边界：连接、Schema 与迁移都收敛在这里。

app.py 只通过 get_db() 取连接，不再自己持有建表语句；
后续引入仓储层或切换数据库时，只需改这一个模块。
"""
import logging
import os
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

import config as cfg

DB_PATH = cfg.DB_PATH

_log = logging.getLogger("gradelogy")

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
-- 老师可保存多个 AI API Key，is_active=1 的那条会被写入 settings 的 ai_api_url/ai_api_key，
-- 这样批改、同步模型、测试连接等既有调用链不需要感知多 Key。
CREATE TABLE IF NOT EXISTS ai_api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  teacher_id INTEGER NOT NULL,
  name TEXT DEFAULT '',
  base_url TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL,
  model TEXT DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_ai_api_keys_teacher ON ai_api_keys(teacher_id, is_active);
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
-- 每个老师、每个入口最近一次从 /v1/models 拉到的可用模型
CREATE TABLE IF NOT EXISTS model_sync(
  teacher_id INTEGER NOT NULL,
  base_url TEXT NOT NULL,
  model_id TEXT NOT NULL,
  display_name TEXT DEFAULT '',
  status TEXT DEFAULT '',
  fetched_at TEXT NOT NULL,
  PRIMARY KEY(teacher_id, base_url, model_id));
CREATE TABLE IF NOT EXISTS model_sync_state(
  teacher_id INTEGER NOT NULL,
  base_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  model_count INTEGER DEFAULT 0,
  error TEXT DEFAULT '',
  PRIMARY KEY(teacher_id, base_url));
-- 价目表：静态清单之外的覆盖值（目前由价目截图识别后确认写入），按模型 ID 全局生效
CREATE TABLE IF NOT EXISTS model_prices(
  model TEXT PRIMARY KEY,
  price_in REAL,
  price_out REAL,
  unit TEXT DEFAULT '元/百万 token',
  source TEXT DEFAULT '',
  updated_at TEXT NOT NULL);
"""


def get_db():
    """Flask 请求上下文内的连接；同一请求复用，请求结束由 close_db 关闭。"""
    from flask import g
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_=None):
    from flask import g
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(upload_migrator=None, default_prompts=(), default_passwords=()):
    """建表 + 轻量迁移。

    upload_migrator: 附件目录迁移由调用方注入，避免 db 层依赖上传目录布局。
    default_prompts: 系统内置提示词 (kind, name, content)，同样由调用方提供。
    default_passwords: 旧版初始密码清单，用于标记「登录后必须改密」。
    """
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
    for kind, name, content in default_prompts:
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
            if any(check_password_hash(r[1], p) for p in default_passwords):
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
    # 轻量迁移：旧的单条 AI 配置导入多 Key 列表，页面改为在 ai_api_keys 中管理
    legacy = con.execute(
        "SELECT teacher_id, "
        "MAX(CASE WHEN key='ai_api_url' THEN value END), "
        "MAX(CASE WHEN key='ai_api_key' THEN value END) "
        "FROM settings WHERE key IN ('ai_api_url','ai_api_key') GROUP BY teacher_id").fetchall()
    for teacher_id, base_url, api_key in legacy:
        has_key = con.execute("SELECT 1 FROM ai_api_keys WHERE teacher_id=? LIMIT 1",
                              (teacher_id,)).fetchone()
        if not has_key and (api_key or "").strip():
            model_row = con.execute(
                "SELECT value FROM settings WHERE teacher_id=? AND key='ai_model'",
                (teacher_id,)).fetchone()
            con.execute(
                "INSERT INTO ai_api_keys(teacher_id,name,base_url,api_key,model,is_active) "
                "VALUES(?,?,?,?,?,1)",
                (teacher_id, "默认配置", base_url or "", api_key,
                 model_row[0] if model_row else ""))
    # 轻量迁移：多 Key 增加每组独立的模型字段
    key_cols = {r[1] for r in con.execute("PRAGMA table_info(ai_api_keys)")}
    if "model" not in key_cols:
        con.execute("ALTER TABLE ai_api_keys ADD COLUMN model TEXT DEFAULT ''")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        con.execute("INSERT INTO users(username,password_hash,display_name,role,must_change_password) "
                    "VALUES(?,?,?,?,1)",
                    ("admin", generate_password_hash("admin123"), "系统管理员", "admin"))
    # 附件目录结构：把旧版平铺文件迁入 course_<id>/lecture_<id>[/student_<id>]/
    if upload_migrator is not None:
        try:
            upload_migrator(con)
        except OSError:
            _log.exception("附件目录迁移失败，旧路径仍可用")
    # 上次进程退出时未完成的批阅任务无法恢复，标记失败以便老师重新提交
    con.execute("UPDATE ai_operations SET status='failed', error=?, "
                "finished_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running')",
                ("服务重启，批阅任务已中断，请重新提交",))
    con.commit()
    con.close()
    try:
        os.chmod(DB_PATH, 0o600)  # 数据库含账号哈希与 API Key，仅当前用户可读写
    except OSError:
        pass
