"""后台批阅队列：去重、线程池执行与定时扫描。

只负责「什么时候批、批哪份、同一份只批一次」的调度，不包含批阅业务本身，
也不导入 app 或 Flask 路由；数据库、设置读取与真正的批阅执行通过构造参数注入。
"""

import fcntl
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")


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

    def scan(self):
        """扫描「已提交」且讲次已上传教师版答案的作业，自动加入后台批阅队列。

        只处理从未批阅过的作业（status='已提交'）；没有答案 PDF 或还没划定作业范围
        的讲次一律跳过。演示模式、未配置 API Key、以及开启「仅闲时批阅」但正处
        高峰时段的老师也会跳过，避免无谓的失败记录和费用。
        """
        db = self._get_db()
        rows = db.execute(
            "SELECT s.id,s.lecture_id,l.course_id,c.teacher_id "
            "FROM submissions s JOIN lectures l ON l.id=s.lecture_id "
            "JOIN courses c ON c.id=l.course_id JOIN users t ON t.id=c.teacher_id "
            "WHERE s.status='已提交' AND t.role='teacher' AND t.active=1 "
            "AND TRIM(COALESCE(l.answer_pdf,''))<>'' "
            "AND TRIM(COALESCE(l.scope_candidates,''))<>'' "
            "AND TRIM(COALESCE(l.scope_selected,''))<>'' "
            "ORDER BY s.id").fetchall()

        teacher_block = {}   # teacher_id -> 跳过原因，缓存后避免逐份作业重复查设置
        queued = 0
        for r in rows:
            tid = r["teacher_id"]
            if tid not in teacher_block:
                if self._get_setting(tid, "ai_demo") == "1":
                    reason = "演示模式不调用 AI"
                elif not (self._get_setting(tid, "ai_api_key") or "").strip():
                    reason = "未配置 AI API Key"
                else:
                    reason = self._offpeak_block(tid) or ""
                teacher_block[tid] = reason
            if teacher_block[tid]:
                continue
            _, err = self.enqueue(tid, r["id"], r["course_id"], r["lecture_id"])
            if err:
                log.info("自动批阅跳过 submission=%s：%s", r["id"], err)
            else:
                queued += 1
                log.info("自动批阅入队 submission=%s lecture=%s teacher=%s",
                         r["id"], r["lecture_id"], tid)
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
