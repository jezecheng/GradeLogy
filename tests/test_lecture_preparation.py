# -*- coding: utf-8 -*-
"""答案、页码与作业范围一致性测试；所有 AI 函数均使用 mock。"""
import io
import json
import sqlite3
import unittest
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as app_module  # noqa: E402

app_module.init_db()


class DeferredQueue:
    """保留 runner 供测试在配置变更后再执行，模拟迟到的后台任务。"""

    def __init__(self):
        self.pending = []

    def submit(self, teacher_id, operation_type, *, model, runner,
               course_id=None, lecture_id=None, submission_id=None,
               material_id=None, dedup_key=None):
        operation = app_module._start_ai_operation(
            teacher_id, operation_type, model, course_id=course_id,
            lecture_id=lecture_id, submission_id=submission_id,
            material_id=material_id, status="queued")
        db = app_module.get_db()
        db.execute("UPDATE ai_operations SET status='running' WHERE id=?",
                   (operation["id"],))
        db.commit()
        self.pending.append((runner, operation))
        return operation, None

    @staticmethod
    def run(item):
        runner, operation = item
        with app_module.app.app_context():
            database = app_module.get_db()
            try:
                runner(database, operation)
            finally:
                app_module.close_db()


class LecturePreparationTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        con = self._db()
        for table in ("ai_usage_events", "ai_operations", "question_tags",
                      "lectures", "course_teachers", "courses", "users"):
            con.execute(f"DELETE FROM {table}")
        con.execute(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(2,'teacher','x','老师','teacher',1,0)")
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(10,'化学',2)")
        con.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(10,2)")
        candidates = [{"key": "q0", "no": "3", "max": 5, "hint": "配平",
                       "stem": "写出 $\\mathrm{Fe^{3+}}$ 与 $\\mathrm{Cl^-}$ 反应的离子方程式。"},
                      {"key": "q1", "no": "4", "max": 5, "hint": "平衡"}]
        con.execute(
            "INSERT INTO lectures(id,course_id,title,answer_pdf,answer_pages,"
            "scope_candidates,scope_selected) VALUES(20,10,'第一讲','answer.pdf','1',?,?)",
            (json.dumps(candidates, ensure_ascii=False), json.dumps(["q0"])))
        con.commit()
        con.close()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["uid"] = 2
            sess["_csrf"] = "test-token"

    @staticmethod
    def _db():
        con = sqlite3.connect(app_module.DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _url(endpoint, **values):
        with app_module.app.test_request_context():
            return app_module.url_for(endpoint, **values)

    def _post(self, endpoint, data=None, **values):
        payload = {"_csrf": "test-token"}
        payload.update(data or {})
        return self.client.post(self._url(endpoint, **values), data=payload)

    def _lecture(self):
        con = self._db()
        row = con.execute("SELECT * FROM lectures WHERE id=20").fetchone()
        con.close()
        return row

    def _page_route_mocks(self, queue):
        return (
            mock.patch.object(app_module, "_ai_task_queue", queue),
            mock.patch.object(app_module, "pdf_page_count", return_value=3),
            mock.patch.object(app_module, "get_setting", return_value="configured"),
            mock.patch.object(app_module, "_offpeak_block_if_enabled", return_value=""),
            mock.patch.object(app_module, "_get_max_tokens", return_value=512),
        )

    def test_replacing_answer_clears_saved_pages_and_scope(self):
        with mock.patch.object(app_module, "save_upload", return_value="new-answer.pdf"), \
                mock.patch.object(app_module, "analyze_answer_pdf") as analyze:
            response = self._post(
                "lecture_answer", {"answer_pdf": (io.BytesIO(b"pdf"), "new.pdf")}, hid=20)
        self.assertEqual(response.status_code, 302)
        row = self._lecture()
        self.assertEqual(row["answer_pdf"], "new-answer.pdf")
        self.assertEqual(row["answer_pages"], "")
        self.assertEqual(row["scope_candidates"], "")
        self.assertEqual(row["scope_selected"], "")
        analyze.assert_not_called()

    def test_changed_pages_immediately_clear_range(self):
        queue = DeferredQueue()
        patches = self._page_route_mocks(queue)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(app_module, "analyze_answer_pdf") as analyze:
            response = self._post("lecture_answer_pages", {"page": ["2"]}, hid=20)
        self.assertEqual(response.status_code, 302)
        row = self._lecture()
        self.assertEqual(row["answer_pages"], "2")
        self.assertEqual(row["scope_candidates"], "")
        self.assertEqual(row["scope_selected"], "")
        self.assertEqual(len(queue.pending), 1)
        analyze.assert_not_called()

    def test_stale_analysis_cannot_overwrite_new_page_selection(self):
        queue = DeferredQueue()
        patches = self._page_route_mocks(queue)
        old_questions = [{"key": "old", "no": "3", "max": 5, "hint": "旧页"}]
        middle_questions = [{"key": "middle", "no": "6", "max": 5, "hint": "中间页"}]
        new_questions = [{"key": "new", "no": "8", "max": 6, "hint": "新页"}]
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(app_module, "analyze_answer_pdf",
                                  side_effect=[(old_questions, ""), (middle_questions, ""),
                                               (new_questions, "")]) as analyze:
            self._post("lecture_answer_pages", {"page": ["2"]}, hid=20)
            self._post("lecture_answer_pages", {"page": ["3"]}, hid=20)
            # 恢复成第一个任务启动时的页码，也不能让其旧结果通过配置值校验。
            self._post("lecture_answer_pages", {"page": ["2"]}, hid=20)
            self.assertEqual(len(queue.pending), 3)
            DeferredQueue.run(queue.pending[0])
            self.assertEqual(self._lecture()["scope_candidates"], "")
            DeferredQueue.run(queue.pending[1])
            self.assertEqual(self._lecture()["scope_candidates"], "")
            DeferredQueue.run(queue.pending[2])

        row = self._lecture()
        self.assertEqual(row["answer_pages"], "2")
        self.assertEqual(json.loads(row["scope_candidates"]), new_questions)
        self.assertEqual(row["scope_selected"], "")
        self.assertEqual(analyze.call_count, 3)

    def test_scope_save_does_not_trigger_ai(self):
        with mock.patch.object(app_module, "analyze_question_tags") as analyze:
            response = self._post("lecture_scope_save", {"no": ["q1"]}, hid=20)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(json.loads(self._lecture()["scope_selected"]), ["q1"])
        analyze.assert_not_called()

    def test_stale_tag_task_cannot_write_after_scope_changes(self):
        queue = DeferredQueue()
        rows = [{"key": "q0", "no": "3", "category": "化学", "tags": ["旧标签"]}]
        with mock.patch.object(app_module, "_ai_task_queue", queue), \
                mock.patch.object(app_module, "_ai_endpoint",
                                  return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "_get_max_tokens", return_value=256), \
                mock.patch.object(app_module, "analyze_question_tags", return_value=(rows, "")):
            queued = self._post("lecture_tags_generate", hid=20)
            self.assertEqual(queued.status_code, 302)
            self.assertEqual(len(queue.pending), 1)
            changed = self._post("lecture_scope_save", {"no": ["q1"]}, hid=20)
            self.assertEqual(changed.status_code, 302)
            DeferredQueue.run(queue.pending[0])
        con = self._db()
        tags = con.execute(
            "SELECT tag FROM question_tags WHERE lecture_id=20 AND source='ai'").fetchall()
        con.close()
        self.assertEqual(tags, [])

    def test_lecture_page_shows_stem_and_manual_tag_editor(self):
        page = self.client.get(self._url("lecture_detail", hid=20)).get_data(as_text=True)
        self.assertIn("查看题干", page)
        self.assertIn("Fe^{3+}", page)
        self.assertIn("<th>题干</th>", page)
        self.assertNotIn("<th>满分</th>", page)
        self.assertIn('<details class="stem-details">', page)  # 默认折叠
        self.assertNotIn('class="scope-stem-row"', page)
        # 勾选框通过 form 属性关联，避免在表格里嵌套表单
        self.assertIn('form="scope-form"', page)
        self.assertIn("编辑标签", page)

    def test_manual_tag_save_replaces_and_clears(self):
        response = self._post("lecture_tag_save",
                              {"question_key": "q1", "tags": "化学平衡，勒夏特列原理"},
                              hid=20)
        self.assertEqual(response.status_code, 302)
        con = self._db()
        rows = con.execute(
            "SELECT tag,question_key,question_no,source FROM question_tags"
            " WHERE lecture_id=20 AND question_no='4' ORDER BY tag").fetchall()
        con.close()
        self.assertEqual(sorted(r["tag"] for r in rows), sorted(["化学平衡", "勒夏特列原理"]))
        # 手动标签按题号保存，避免题目清单重建后 q0/q1 错位
        self.assertTrue(all(r["question_key"] == "" for r in rows))
        self.assertTrue(all(r["source"] == "manual" for r in rows))

        self._post("lecture_tag_save", {"question_key": "q1", "tags": "  "}, hid=20)
        con = self._db()
        left = con.execute(
            "SELECT COUNT(*) FROM question_tags WHERE lecture_id=20"
            " AND question_no='4'").fetchone()[0]
        con.close()
        self.assertEqual(left, 0)

    def test_manual_tag_save_rejects_unknown_question(self):
        self._post("lecture_tag_save", {"question_key": "q9", "tags": "化学平衡"}, hid=20)
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM question_tags WHERE lecture_id=20").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_ai_regenerate_skips_duplicate_of_manual_tag(self):
        self._post("lecture_tag_save", {"question_key": "q0", "tags": "氧化还原反应"}, hid=20)
        queue = DeferredQueue()
        rows = [{"key": "q0", "no": "3", "category": "化学",
                 "tags": ["氧化还原反应", "离子方程式"]}]
        with mock.patch.object(app_module, "_ai_task_queue", queue), \
                mock.patch.object(app_module, "_ai_endpoint",
                                  return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "_get_max_tokens", return_value=256), \
                mock.patch.object(app_module, "analyze_question_tags", return_value=(rows, "")):
            self._post("lecture_tags_generate", hid=20)
            DeferredQueue.run(queue.pending[0])
        con = self._db()
        got = con.execute(
            "SELECT tag,source FROM question_tags WHERE lecture_id=20 ORDER BY tag").fetchall()
        con.close()
        self.assertEqual(sorted((r["tag"], r["source"]) for r in got),
                         sorted([("离子方程式", "ai"), ("氧化还原反应", "manual")]))


if __name__ == "__main__":
    unittest.main()
