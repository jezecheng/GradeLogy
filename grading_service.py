"""后台批阅队列：去重、线程池执行与定时扫描。

只负责「什么时候批、批哪份、同一份只批一次」的调度，不包含批阅业务本身，
也不导入 app 或 Flask 路由；数据库、设置读取与真正的批阅执行通过构造参数注入。
"""

import fcntl
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("gradelogy.grading")

ACTIVE_STATUSES = ("queued", "running")

# 同一份作业默认只有前 3 次提交会自动批改，之后需要老师手动触发。
DEFAULT_AUTO_GRADE_ATTEMPTS = 3


def tag_scope_signature(scope_selected):
    """Canonical signature of selected independent questions covered by tag generation."""
    try:
        values = json.loads(scope_selected) if isinstance(scope_selected, str) else scope_selected
    except (TypeError, ValueError):
        return ""
    if not isinstance(values, list):
        return ""
    keys = sorted({str(value).strip() for value in values if str(value).strip()})
    return json.dumps(keys, ensure_ascii=False, separators=(",", ":")) if keys else ""


def latest_operation(db, sid):
    """返回这份作业最近一次批阅操作（status/error），没有则返回 None。"""
    return db.execute(
        "SELECT status,error FROM ai_operations WHERE submission_id=? "
        "AND operation_type='grade' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()


def _concurrency():
    try:
        return max(1, min(4, int(os.environ.get("GRADING_CONCURRENCY", "2"))))
    except (TypeError, ValueError):
        return 2


class GradingQueue:
    """批阅任务调度器：同一份作业同时只允许一个任务，真实批阅在后台线程池执行。"""

    def __init__(self, *, get_db, get_setting, start_operation, finish_operation,
                 run_job, app_context, offpeak_block=None, concurrency=None):
        self._get_db = get_db
        self._get_setting = get_setting
        self._start_operation = start_operation
        self._finish_operation = finish_operation
        self._run_job = run_job
        self._app_context = app_context
        self._offpeak_block = offpeak_block or (lambda _teacher_id: None)
        self._active = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=concurrency or _concurrency(),
                                        thread_name_prefix="grading")
        self._lock_file = None
        self._started = False

    def enqueue(self, teacher_id, sid, course_id, lecture_id):
        """统一入队入口：手动点击和定时扫描共用，返回 (operation, 错误提示)。"""
        db = self._get_db()
        with self._lock:
            if sid in self._active:
                return None, "这份作业已在批阅队列中，请等待完成后再操作。"
            self._active.add(sid)

        active = latest_operation(db, sid)
        if active is not None and active["status"] in ACTIVE_STATUSES:
            self._discard(sid)
            return None, "这份作业已在批阅队列中，请等待完成后再操作。"

        operation = None
        try:
            operation = self._start_operation(
                teacher_id, "grade", self._get_setting(teacher_id, "ai_model"),
                course_id=course_id, lecture_id=lecture_id,
                submission_id=sid, status="queued")
            # 重批一经入队，旧报告和「跳过人工复核」结论就不再对应即将生成的新结果。
            db.execute(
                "UPDATE submissions SET status='待批改',report_published=0,review_skipped=0,"
                "report_md='',report_detail='',grader='' WHERE id=?", (sid,))
            db.commit()
            self._pool.submit(self._execute, teacher_id, sid, operation)
        except Exception as e:
            log.exception("加入批阅队列失败 submission=%s", sid)
            self._finish_operation(operation, "failed", str(e))
            self._discard(sid)
            return None, "加入批阅队列失败：" + str(e)
        return operation, None

    def _execute(self, teacher_id, sid, operation):
        try:
            self._run_job(teacher_id, sid, operation)
        finally:
            self._discard(sid)

    def _discard(self, sid):
        with self._lock:
            self._active.discard(sid)

    def _auto_gate(self, teacher_id):
        """自动批改前的通用开关检查；返回跳过原因，可执行时返回空串。"""
        if self._get_setting(teacher_id, "ai_demo") == "1":
            return "演示模式不调用 AI"
        if not (self._get_setting(teacher_id, "ai_api_key") or "").strip():
            return "未配置 AI API Key"
        return self._offpeak_block(teacher_id) or ""

    def attempt_limit(self, teacher_id):
        """自动批改允许的提交次数上限；未配置或非法时使用默认值 3。"""
        raw = (self._get_setting(teacher_id, "ai_auto_grade_attempts", "") or "").strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else DEFAULT_AUTO_GRADE_ATTEMPTS

    def auto_enqueue(self, sid, gate_cache=None):
        """按自动批改规则尝试把一份作业加入队列，返回 (是否入队, 跳过原因)。

        自动批改需要同时满足：作业仍在「已提交」状态、讲次已上传教师版答案，
        已划定题目范围并完成该范围的知识点标签生成、老师已配置可用的 AI，
        且这份作业属于该学生前 N 次提交。
        """
        db = self._get_db()
        row = db.execute(
            "SELECT s.id,s.lecture_id,s.student_id,l.course_id,c.teacher_id,"
            "l.answer_pdf,l.scope_candidates,l.scope_selected,l.tags_generated_scope,"
            "(COALESCE(s.prior_attempts,0)+(SELECT COUNT(*) FROM submissions x "
            " WHERE x.lecture_id=s.lecture_id AND x.student_id=s.student_id "
            " AND x.id<=s.id)) AS attempt_no "
            "FROM submissions s JOIN lectures l ON l.id=s.lecture_id "
            "JOIN courses c ON c.id=l.course_id JOIN users t ON t.id=c.teacher_id "
            "WHERE s.id=? AND s.status='已提交' AND t.role='teacher' AND t.active=1",
            (sid,)).fetchone()
        if row is None:
            return False, "作业不在待批改状态"
        if not (row["answer_pdf"] or "").strip():
            return False, "教师版标准答案未上传"
        if not (row["scope_candidates"] or "").strip() \
                or not (row["scope_selected"] or "").strip():
            return False, "作业范围与题目未标注"
        signature = tag_scope_signature(row["scope_selected"])
        if not signature or signature != (row["tags_generated_scope"] or ""):
            return False, "当前作业范围的知识点标签尚未生成完成"
        tid = row["teacher_id"]
        if gate_cache is not None:
            if tid not in gate_cache:
                gate_cache[tid] = self._auto_gate(tid)
            block = gate_cache[tid]
        else:
            block = self._auto_gate(tid)
        if block:
            return False, block
        limit = self.attempt_limit(tid)
        if row["attempt_no"] > limit:
            return False, f"第 {row['attempt_no']} 次提交已超过自动批改上限（前 {limit} 次）"
        _, err = self.enqueue(tid, sid, row["course_id"], row["lecture_id"])
        if err:
            return False, err
        return True, ""

    def auto_enqueue_lecture(self, lecture_id):
        """讲次条件满足后，把该讲所有仍在「已提交」的作业按自动批改规则入队。"""
        db = self._get_db()
        rows = db.execute(
            "SELECT id FROM submissions WHERE lecture_id=? AND status='已提交' "
            "ORDER BY id", (lecture_id,)).fetchall()
        gate_cache = {}
        queued = 0
        for r in rows:
            ok, reason = self.auto_enqueue(r["id"], gate_cache=gate_cache)
            if ok:
                queued += 1
                log.info("自动批阅入队 submission=%s lecture=%s", r["id"], lecture_id)
            elif reason:
                log.info("自动批阅跳过 submission=%s：%s", r["id"], reason)
        if queued:
            log.info("讲次 %s 自动批阅入队 %s 份", lecture_id, queued)
        return queued

    def scan(self):
        """扫描「已提交」且讲次已上传教师版答案的作业，自动加入后台批阅队列。

        只处理从未批阅过的作业（status='已提交'）；没有答案 PDF、还没划定作业范围，
        或当前范围的知识点标签尚未生成完成的讲次一律跳过。演示模式、未配置 API Key、以及开启「仅闲时批阅」但正处
        高峰时段的老师也会跳过，避免无谓的失败记录和费用。超过自动批改次数上限
        的提交同样跳过，等待老师手动触发。
        """
        db = self._get_db()
        rows = db.execute(
            "SELECT s.id "
            "FROM submissions s JOIN lectures l ON l.id=s.lecture_id "
            "JOIN courses c ON c.id=l.course_id JOIN users t ON t.id=c.teacher_id "
            "WHERE s.status='已提交' AND t.role='teacher' AND t.active=1 "
            "AND TRIM(COALESCE(l.answer_pdf,''))<>'' "
            "AND TRIM(COALESCE(l.scope_candidates,''))<>'' "
            "AND TRIM(COALESCE(l.scope_selected,''))<>'' "
            "AND TRIM(COALESCE(l.tags_generated_scope,''))<>'' "
            "ORDER BY s.id").fetchall()

        gate_cache = {}   # teacher_id -> 跳过原因，避免逐份作业重复查设置
        queued = 0
        for r in rows:
            ok, reason = self.auto_enqueue(r["id"], gate_cache=gate_cache)
            if ok:
                queued += 1
                log.info("自动批阅入队 submission=%s", r["id"])
            elif reason:
                log.info("自动批阅跳过 submission=%s：%s", r["id"], reason)
        if queued:
            log.info("自动批阅扫描完成：新入队 %s 份", queued)
        return queued

    def _loop(self, interval):
        while True:
            try:
                with self._app_context():
                    self.scan()
            except Exception:
                log.exception("自动批阅扫描异常")
            time.sleep(interval)

    def start(self, *, enabled, interval_hours, lock_dir):
        """启动自动批阅线程；多进程部署时只让抢到文件锁的进程跑。"""
        if self._started or not enabled:
            return
        self._started = True
        try:
            os.makedirs(lock_dir, exist_ok=True)
            self._lock_file = open(os.path.join(lock_dir, ".auto_grade.lock"), "w")
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._started = False   # 别的进程已经在扫描
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None
            return
        interval = max(0.1, float(interval_hours)) * 3600
        threading.Thread(target=self._loop, args=(interval,), daemon=True,
                         name="auto-grade").start()
        log.info("自动批阅已启用：每 %s 小时检查一次已提交且已上传答案的作业",
                 interval_hours)
