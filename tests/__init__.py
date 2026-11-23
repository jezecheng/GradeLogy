# -*- coding: utf-8 -*-
"""测试辅助：把异步 AI 任务队列替换为同步执行，便于单元/集成测试断言。"""
from tests import _env_bootstrap  # noqa: F401
import app as app_module

app_module.init_db()


class SyncAiTaskQueue:
    """在测试中同步执行 runner，保证断言时 AI mock 已被调用、结果已落库。"""

    def submit(self, teacher_id, operation_type, *, model, runner,
               course_id=None, lecture_id=None, submission_id=None,
               material_id=None, resource_key="", dedup_key=None):
        op = app_module._start_ai_operation(
            teacher_id, operation_type, model, course_id=course_id,
            lecture_id=lecture_id, submission_id=submission_id,
            material_id=material_id, resource_key=resource_key, status="queued")
        db = app_module.get_db()
        db.execute("UPDATE ai_operations SET status='running' WHERE id=?", (op["id"],))
        db.commit()
        try:
            runner(db, op)
        finally:
            app_module.close_db()
        return op, None


def patch_sync_queue(testcase):
    """在测试用例上把 app._ai_task_queue 替换为同步队列，结束时还原。"""
    patcher = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        app_module, "_ai_task_queue", SyncAiTaskQueue())
    testcase.addCleanup(patcher.stop)
    patcher.start()
