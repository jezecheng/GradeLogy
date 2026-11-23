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
               material_id=None, resource_key="", dedup_key=None):
        operation = app_module._start_ai_operation(
            teacher_id, operation_type, model, course_id=course_id,
            lecture_id=lecture_id, submission_id=submission_id,
            material_id=material_id, resource_key=resource_key, status="queued")
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
        for table in ("ai_usage_events", "ai_operations", "student_question_reviews",
                      "questions", "submissions", "student_weak_points", "tag_analyses",
                      "question_assessments", "question_tags", "lectures",
                      "course_teachers", "courses", "users"):
            con.execute(f"DELETE FROM {table}")
        con.execute(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(2,'teacher','x','老师','teacher',1,0)")
        con.execute(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(4,'student','x','学生','student',1,0)")
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

    def test_page_picker_button_stays_in_generation_state_while_queued(self):
        with app_module.app.app_context():
            app_module._start_ai_operation(
                2, "scope_analyze", "model", course_id=10, lecture_id=20,
                status="queued")
        response = self.client.get(self._url("lecture_detail", hid=20))
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("正在生成题目列表…", html)
        self.assertIn('class="is-busy" disabled aria-busy="true"', html)

    def test_tag_button_stays_busy_for_queued_job_and_status_is_pollable(self):
        with app_module.app.app_context():
            operation = app_module._start_ai_operation(
                2, "question_tagging", "model", course_id=10, lecture_id=20,
                status="queued")
        page = self.client.get(self._url("lecture_detail", hid=20))
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("生成中…", html)
        self.assertIn('data-tag-busy="1"', html)
        self.assertIn('disabled aria-busy="true"', html)

        status = self.client.get(self._url(
            "lecture_tags_status", hid=20) + "?operation_id=" + str(operation["id"]))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["status"], "queued")

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

    def test_subquestion_scope_selects_each_child_and_parent_selects_all(self):
        candidates = [{
            "key": "q0", "no": "1", "source_no": "例7", "hint": "计算",
            "stem": "母题公共条件",
            "subquestions": [
                {"key": "q0.1", "no": "1.1", "source_no": "(1)",
                 "hint": "求速率", "stem": "求速率常数"},
                {"key": "q0.2", "no": "1.2", "source_no": "(2)",
                 "hint": "求级数", "stem": "求反应级数"},
            ],
        }]
        con = self._db()
        con.execute("UPDATE lectures SET scope_candidates=?,scope_selected='' WHERE id=20",
                    (json.dumps(candidates, ensure_ascii=False),))
        con.commit()
        con.close()
        with mock.patch.object(app_module._grading_queue, "auto_enqueue_lecture", return_value=0):
            response = self._post("lecture_scope_save", {"no": ["q0.2"]}, hid=20)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(json.loads(self._lecture()["scope_selected"]), ["q0.2"])
        with app_module.app.app_context():
            hw = app_module.get_db().execute("SELECT * FROM lectures WHERE id=20").fetchone()
            instruction = app_module._scope_instruction(hw)
        self.assertIn("1.2", instruction)
        self.assertIn("母题公共条件", instruction)
        self.assertNotIn("1.1", instruction)

        with mock.patch.object(app_module._grading_queue, "auto_enqueue_lecture", return_value=0):
            response = self._post("lecture_scope_save", {"no": ["q0"]}, hid=20)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(json.loads(self._lecture()["scope_selected"]), ["q0"])
        page = self.client.get(self._url("lecture_detail", hid=20)).get_data(as_text=True)
        self.assertIn("1.1", page)
        self.assertIn("1.2", page)
        self.assertIn('class="scope-table"', page)
        self.assertIn('class="question-stem scope-child-stem"', page)
        self.assertIn("<colgroup>", page)
        self.assertIn("data-scope-parent=\"1\"", page)
        self.assertIn("data-scope-child=\"1\"", page)

    def test_scope_status_endpoint_reports_ready_for_in_place_refresh(self):
        con = self._db()
        con.execute("UPDATE lectures SET scope_candidates='',scope_selected='' WHERE id=20")
        con.commit()
        con.close()
        with app_module.app.app_context():
            operation = app_module._start_ai_operation(
                2, "scope_analyze", "model", course_id=10, lecture_id=20, status="running")
        page = self.client.get(self._url("lecture_detail", hid=20)).get_data(as_text=True)
        self.assertIn('id="scope-build-status"', page)
        self.assertIn('data-poll="1"', page)

        status_url = self._url("lecture_scope_status", hid=20)
        response = self.client.get(status_url, query_string={"operation_id": operation["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "running")
        self.assertFalse(response.get_json()["ready"])

        con = self._db()
        con.execute("UPDATE lectures SET scope_candidates=? WHERE id=20",
                    (json.dumps([{"key": "q0", "no": "1", "stem": "新题干"}]),))
        con.execute("UPDATE ai_operations SET status='success' WHERE id=?", (operation["id"],))
        con.commit()
        con.close()
        response = self.client.get(status_url, query_string={"operation_id": operation["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "success")
        self.assertTrue(response.get_json()["ready"])

    def test_scope_change_requires_confirmation_and_recomputes_old_results(self):
        con = self._db()
        con.execute("UPDATE lectures SET tags_generated_scope=? WHERE id=20",
                    (app_module.tag_scope_signature(["q0"]),))
        con.executemany(
            "INSERT INTO lectures(id,course_id,title) VALUES(?,10,?)",
            [(21, "第二讲")])
        con.execute("UPDATE lectures SET summary_md='旧讲次汇总',summary_at='now' WHERE id=20")
        con.execute(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,score,feedback,"
            "ai_summary,report_md,report_detail,report_published) "
            "VALUES(30,20,4,'student/original.pdf','已批改','4','旧反馈','{}','旧报告','{}',1)")
        con.execute(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_published) "
            "VALUES(31,21,4,'student/other.pdf','已批改',1)")
        con.executemany(
            "INSERT INTO questions(id,submission_id,no,verdict,reviewed) VALUES(?,?,?,?,1)",
            [(40, 30, "3", "错"), (41, 30, "4", "错"), (42, 31, "2.1", "半对")])
        con.executemany(
            "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source) "
            "VALUES(?,?,?,?, 'ai')",
            [(20, "q0", "3", "仅本讲标签"), (20, "q1", "4", "跨讲共享标签"),
             (21, "q0.1", "2.1", "跨讲共享标签")])
        con.executemany(
            "INSERT INTO student_weak_points(student_id,course_id,tag,node_name,status,"
            "wrong_count,partial_count,right_count,total_count,source_submission_id,"
            "status_lecture_id,student_note) VALUES(4,10,?,'节点','mastered',?,?,?,? ,30,20,'我的备注')",
            [("仅本讲标签", 1, 0, 0, 1), ("跨讲共享标签", 1, 0, 0, 1)])
        con.execute(
            "INSERT INTO student_question_reviews(student_id,question_id,status) "
            "VALUES(4,40,'reviewed')")
        con.execute(
            "INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,student_id,kind,content_md) "
            "VALUES(2,10,20,0,'horizontal','旧横向分析')")
        con.execute(
            "INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,student_id,kind,content_md) "
            "VALUES(2,10,0,4,'vertical','旧纵向分析')")
        con.commit()
        con.close()

        with mock.patch.object(app_module._grading_queue, "auto_enqueue_lecture", return_value=0):
            refused = self._post("lecture_scope_save", {"no": ["q1"]}, hid=20)
        self.assertEqual(refused.status_code, 302)
        self.assertEqual(json.loads(self._lecture()["scope_selected"]), ["q0"])
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM questions WHERE submission_id=30").fetchone()[0], 2)
        con.close()

        with mock.patch.object(app_module._grading_queue, "auto_enqueue_lecture", return_value=0):
            confirmed = self._post("lecture_scope_save",
                                   {"no": ["q1"], "confirm_scope_reset": "1"}, hid=20)
        self.assertEqual(confirmed.status_code, 302)
        con = self._db()
        self.assertEqual(json.loads(con.execute(
            "SELECT scope_selected FROM lectures WHERE id=20").fetchone()[0]), ["q1"])
        self.assertEqual(con.execute(
            "SELECT tags_generated_scope FROM lectures WHERE id=20").fetchone()[0], "")
        sub = con.execute(
            "SELECT status,file_path,score,feedback,ai_summary,report_md,report_published "
            "FROM submissions WHERE id=30").fetchone()
        self.assertEqual(tuple(sub), ("已提交", "student/original.pdf", "", "", "", "", 0))
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM questions WHERE submission_id=30").fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM student_question_reviews WHERE question_id=40").fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM question_tags WHERE lecture_id=20").fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT summary_md FROM lectures WHERE id=20").fetchone()[0], "")
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM tag_analyses WHERE lecture_id=20 OR student_id=4").fetchone()[0], 0)
        weak = {row["tag"]: row for row in con.execute(
            "SELECT * FROM student_weak_points WHERE student_id=4 AND course_id=10")}
        self.assertNotIn("仅本讲标签", weak)
        self.assertEqual((weak["跨讲共享标签"]["wrong_count"],
                          weak["跨讲共享标签"]["partial_count"],
                          weak["跨讲共享标签"]["total_count"]), (0, 1, 1))
        self.assertEqual(weak["跨讲共享标签"]["source_submission_id"], 31)
        self.assertEqual(weak["跨讲共享标签"]["status"], "mastered")
        self.assertEqual(weak["跨讲共享标签"]["student_note"], "我的备注")
        con.close()

    def test_reextracting_scope_list_requires_confirmation_before_clearing_grades(self):
        con = self._db()
        con.execute(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_md,"
            "report_published) VALUES(30,20,4,'student/original.pdf','已批改','旧报告',1)")
        con.execute("INSERT INTO questions(id,submission_id,no,verdict,reviewed) "
                    "VALUES(40,30,'3','错',1)")
        con.commit()
        con.close()

        queue = DeferredQueue()
        patches = self._page_route_mocks(queue)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(app_module, "analyze_answer_pdf", return_value=([{
                    "key": "q0", "no": "1", "source_no": "3", "stem": "新题干",
                    "subquestions": [],
                }], "")) as analyze:
            refused = self._post("lecture_answer_pages", {"page": ["2"]}, hid=20)
            self.assertEqual(refused.status_code, 302)
            con = self._db()
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM questions WHERE submission_id=30").fetchone()[0], 1)
            con.close()
            analyze.assert_not_called()

            confirmed = self._post("lecture_answer_pages",
                                  {"page": ["2"], "confirm_scope_reset": "1"}, hid=20)
            self.assertEqual(confirmed.status_code, 302)
            self.assertEqual(len(queue.pending), 1)
            DeferredQueue.run(queue.pending[0])
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM questions WHERE submission_id=30").fetchone()[0], 0)
        sub = con.execute(
            "SELECT status,report_md,report_published,file_path FROM submissions WHERE id=30"
        ).fetchone()
        self.assertEqual(tuple(sub), ("已提交", "", 0, "student/original.pdf"))
        con.close()
        analyze.assert_called_once()

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
        self.assertIn("<th>题干/小问</th>", page)
        self.assertNotIn("<th>满分</th>", page)
        self.assertIn('<details class="stem-details">', page)  # 默认折叠
        self.assertNotIn('class="scope-stem-row"', page)
        # 勾选框通过 form 属性关联，避免在表格里嵌套表单
        self.assertIn('form="scope-form"', page)
        self.assertIn("编辑标签", page)
        self.assertIn("<th>难度</th>", page)
        self.assertIn("data-difficulty-widget", page)

    def test_manual_difficulty_can_override_ai_score_and_restore_it(self):
        con = self._db()
        con.execute(
            "INSERT INTO question_assessments(lecture_id,question_key,question_no,difficulty) "
            "VALUES(20,'q0','3',4.0)")
        con.commit()
        con.close()

        response = self._post("lecture_difficulty_save", {
            "question_key": "q0", "difficulty": "2.5"}, hid=20)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "rating": 2.5, "manual": True})
        restored = self._post("lecture_difficulty_save", {
            "question_key": "q0", "difficulty": ""}, hid=20)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json(), {"ok": True, "rating": 4.0, "manual": False})
        invalid = self._post("lecture_difficulty_save", {
            "question_key": "q0", "difficulty": "3.2"}, hid=20)
        self.assertEqual(invalid.status_code, 400)

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
        con = self._db()
        con.execute(
            "INSERT INTO question_assessments(lecture_id,question_key,question_no,difficulty,manual_difficulty) "
            "VALUES(20,'q0','3',3.0,2.5)")
        con.commit()
        con.close()
        queue = DeferredQueue()
        rows = [{"key": "q0", "no": "3", "category": "化学",
                 "tags": ["氧化还原反应", "离子方程式"], "difficulty": 4.5}]
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
        self.assertEqual(sorted((r["tag"], r["source"]) for r in got),
                         sorted([("离子方程式", "ai"), ("氧化还原反应", "manual")]))
        assessment = con.execute(
            "SELECT difficulty,manual_difficulty FROM question_assessments "
            "WHERE lecture_id=20 AND question_key='q0'").fetchone()
        self.assertEqual(tuple(assessment), (4.5, 2.5))
        con.close()


if __name__ == "__main__":
    unittest.main()
