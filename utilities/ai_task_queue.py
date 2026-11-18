# -*- coding: utf-8 -*-
"""通用 AI 异步任务队列。

与 grading_service.GradingQueue 平级，但不限定 operation_type：所有非批阅的
长耗时 AI 操作（讲次汇总、知识点分析、图谱体检、总评生成等）都通过这里入队，
状态统一落在 ai_operations 表，前端 /ai-queue 统一展示。

设计原则：
- 不导入 app / Flask，只通过构造参数注入数据库、操作记录、应用上下文。
- runner 签名为 runner(db, operation)，在 app_context 内执行，必须自己调用
  finish_operation 标记 success/failed，或抛出异常由队列兜底标记 failed。
- dedup_key 用于同一对象的同类操作去重（如 lecture_id + operation_type）。
"""

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("gradelogy.ai")


def _concurrency():
    try:
        return max(1, min(4, int(os.environ.get("AI_TASK_CONCURRENCY", "2"))))
    except (TypeError, ValueError):
        return 2


class AiTaskQueue:
    """通用 AI 后台任务调度器：入队即返回，真实执行在线程池。"""

    def __init__(self, *, get_db, close_db, start_operation, finish_operation,
                 app_context, concurrency=None):
        self._get_db = get_db
        self._close_db = close_db
        self._start_operation = start_operation
        self._finish_operation = finish_operation
        self._app_context = app_context
        self._active = set()        # (teacher_id, dedup_key)
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=concurrency or _concurrency(),
                                        thread_name_prefix="ai-task")
        self._started = False

    def submit(self, teacher_id, operation_type, *, model, runner,
               course_id=None, lecture_id=None, submission_id=None,
               material_id=None, dedup_key=None):
        """入队一个 AI 任务，返回 (operation, error)。

        dedup_key 缺省时用 operation_type 拼接各业务 id，保证同对象同类操作只排一份。
        """
        if dedup_key is None:
            dedup_key = self._default_dedup(operation_type, course_id, lecture_id,
                                            submission_id, material_id)
        key = (teacher_id, dedup_key)
        with self._lock:
            if key in self._active:
                return None, "该任务已在后台队列中，请等待完成后再操作。"
            self._active.add(key)

        operation = None
        try:
            operation = self._start_operation(
                teacher_id, operation_type, model,
                course_id=course_id, lecture_id=lecture_id,
                submission_id=submission_id, material_id=material_id,
                status="queued")
            self._pool.submit(self._execute, key, runner, operation)
        except Exception as e:
            log.exception("入队 AI 任务失败 type=%s teacher=%s", operation_type, teacher_id)
            if operation is not None:
                self._finish_operation(operation, "failed", str(e))
            self._discard(key)
            return None, "加入后台队列失败：" + str(e)
        return operation, None

    @staticmethod
    def _default_dedup(operation_type, course_id, lecture_id,
                       submission_id, material_id):
        return "|".join(str(x) for x in (
            operation_type, course_id, lecture_id, submission_id, material_id))

    def _execute(self, key, runner, operation):
        try:
            with self._app_context():
                db = self._get_db()
                try:
                    db.execute("UPDATE ai_operations SET status='running' WHERE id=?",
                               (operation["id"],))
                    db.commit()
                    runner(db, operation)
                except Exception as e:  # noqa: BLE001 兜底：绝不让线程静默崩溃
                    db.rollback()
                    log.exception("AI 后台任务异常 operation=%s type=%s",
                                  operation["id"], operation.get("operation_type"))
                    try:
                        self._finish_operation(operation, "failed", str(e))
                    except Exception:
                        log.exception("标记 AI 任务失败时再次出错 operation=%s", operation["id"])
                finally:
                    self._close_db()
        finally:
            self._discard(key)

    def _discard(self, key):
        with self._lock:
            self._active.discard(key)
