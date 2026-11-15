# -*- coding: utf-8 -*-
"""知识点标签与分析路由的集成测试（隔离数据目录 + mock AI）。"""
import json
import sqlite3
import unittest
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401  确保 DATA_DIR 在 app 导入前就绪
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as app_module  # noqa: E402

_TABLES = ("tag_analyses", "question_tags", "questions", "submissions", "lectures",
           "exams", "exam_scores", "enrollments", "course_teachers", "courses",
           "users", "ai_usage_events", "ai_operations")


class TagRouteTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self._clear()
        self._seed()
        self.client = self.login(2)

    def tearDown(self):
        self._clear()

    @staticmethod
    def _db():
        con = sqlite3.connect(app_module.DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    def _clear(self):
        con = self._db()
        for table in _TABLES:
            con.execute(f"DELETE FROM {table}")
        con.commit()
        con.close()

    def _seed(self):
        con = self._db()
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(?,?, 'x',?,?,1,0)",
            [(1, "admin", "管理员", "admin"),
             (2, "teacher", "老师", "teacher"),
             (3, "other", "别的老师", "teacher"),
             (4, "student", "学生", "student")])
        con.executemany(
            "INSERT INTO courses(id,name,teacher_id) VALUES(?,?,?)",
            [(10, "化学", 2), (11, "物理", 3)])
        con.executemany(
            "INSERT INTO course_teachers(course_id,teacher_id) VALUES(?,?)",
            [(10, 2), (11, 3)])
        con.execute("INSERT INTO enrollments(course_id,student_id) VALUES(10,4)")
        cands = [{"key": "q0", "no": "3", "max": 5, "hint": "配平"},
                 {"key": "q1", "no": "4", "max": 5, "hint": "平衡"}]
        con.execute(
            "INSERT INTO lectures(id,course_id,title,answer_pdf,answer_pages,"
            "scope_candidates,scope_selected) VALUES(20,10,'第一讲','answer.pdf','0',?,?)",
            (json.dumps(cands, ensure_ascii=False), json.dumps(["q0"])))
        con.execute(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status)"
            " VALUES(30,20,4,'a.pdf','待批改')")
        con.execute(
            "INSERT INTO questions(id,submission_id,no,verdict,ai_comment)"
            " VALUES(40,30,'3','错','未配平')")
        con.execute(
            "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
            " VALUES(20,'q0','3','氧化还原反应','ai')")
        con.commit()
        con.close()

    @staticmethod
    def login(uid):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = uid
            sess["_csrf"] = "test-token"
        return client

    @staticmethod
    def url(endpoint, **values):
        with app_module.app.test_request_context():
            return app_module.url_for(endpoint, **values)

    def _post(self, endpoint, data=None, client=None, **values):
        payload = {"_csrf": "test-token"}
        payload.update(data or {})
        return (client or self.client).post(self.url(endpoint, **values), data=payload)

    def _tags(self, lecture_id=20):
        con = self._db()
        rows = con.execute(
            "SELECT question_no,tag FROM question_tags WHERE lecture_id=? ORDER BY id",
            (lecture_id,)).fetchall()
        con.close()
        return [(r["question_no"], r["tag"]) for r in rows]

    def test_scope_save_generates_tags_and_replaces_old(self):
        ai_rows = [
            {"key": "q0", "no": "3", "category": "化学计算",
             "tags": ["氧化还原反应", "电子守恒"]},
            {"key": "q1", "no": "4", "category": "", "tags": ["化学平衡"]},
        ]
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "analyze_question_tags",
                                  return_value=(ai_rows, "")) as ai:
            resp = self._post("lecture_scope_save", {"no": ["q0", "q1"]}, hid=20)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._tags(), [
            ("3", "氧化还原反应"), ("3", "电子守恒"), ("4", "化学平衡")])
        con = self._db()
        selected = json.loads(con.execute(
            "SELECT scope_selected FROM lectures WHERE id=20").fetchone()[0])
        con.close()
        self.assertEqual(selected, ["q0", "q1"])
        ai.assert_called_once()

    def test_scope_save_keeps_scope_when_tagging_fails(self):
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "analyze_question_tags",
                                  return_value=(None, "AI 标注失败：超时")):
            resp = self._post("lecture_scope_save", {"no": ["q0"]}, hid=20)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        selected = json.loads(con.execute(
            "SELECT scope_selected FROM lectures WHERE id=20").fetchone()[0])
        con.close()
        self.assertEqual(selected, ["q0"])
        # 失败时保留旧标签，便于重试前继续展示
        self.assertEqual(self._tags(), [("3", "氧化还原反应")])

    def test_tags_generate_requires_saved_scope(self):
        con = self._db()
        con.execute("UPDATE lectures SET scope_selected='[]' WHERE id=20")
        con.commit()
        con.close()
        with mock.patch.object(app_module, "analyze_question_tags") as ai:
            resp = self._post("lecture_tags_generate", hid=20)
        self.assertEqual(resp.status_code, 302)
        ai.assert_not_called()

    def test_tags_generate_replaces_tags(self):
        ai_rows = [{"key": "q0", "no": "3", "category": "",
                    "tags": ["离子方程式"]}]
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "analyze_question_tags",
                                  return_value=(ai_rows, "")):
            resp = self._post("lecture_tags_generate", hid=20)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._tags(), [("3", "离子方程式")])

    def test_tag_routes_reject_other_teacher(self):
        other = self.login(3)
        self.assertEqual(
            self._post("lecture_tags_generate", client=other, hid=20).status_code, 403)
        self.assertEqual(
            self._post("lecture_tag_analysis", client=other, hid=20).status_code, 403)
        self.assertEqual(
            self._post("student_tag_analysis", client=other, cid=10, sid=4).status_code,
            403)

    def test_horizontal_analysis_saves_markdown(self):
        result = {"summary": "整体判断", "points": [
            {"tag": "氧化还原反应", "problem": "配平错误多", "advice": "专项练习"}]}
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_horizontal_analysis",
                                  return_value=result) as ai:
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        ai.assert_called_once()
        con = self._db()
        row = con.execute(
            "SELECT * FROM tag_analyses WHERE kind='horizontal' AND lecture_id=20"
        ).fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertIn("整体判断", row["content_md"])
        self.assertIn("配平错误多", row["content_md"])
        self.assertIn("氧化还原反应", row["stats_json"])

    def test_horizontal_analysis_requires_tags(self):
        con = self._db()
        con.execute("DELETE FROM question_tags WHERE lecture_id=20")
        con.commit()
        con.close()
        with mock.patch.object(app_module, "ai_horizontal_analysis") as ai:
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        ai.assert_not_called()
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM tag_analyses WHERE kind='horizontal'").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_horizontal_analysis_requires_graded_questions(self):
        con = self._db()
        con.execute("DELETE FROM questions WHERE submission_id=30")
        con.commit()
        con.close()
        with mock.patch.object(app_module, "ai_horizontal_analysis") as ai:
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        ai.assert_not_called()

    def test_horizontal_analysis_keeps_previous_on_ai_failure(self):
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_horizontal_analysis", return_value={}):
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM tag_analyses WHERE kind='horizontal'").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_vertical_analysis_saves_markdown(self):
        result = {"summary": "纵向判断",
                  "weak_tags": [{"tag": "氧化还原反应", "trend": "多讲反复出错",
                                 "advice": "复习电子守恒"}],
                  "strengths": "计算稳定"}
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_vertical_analysis",
                                  return_value=result) as ai:
            resp = self._post("student_tag_analysis", cid=10, sid=4)
        self.assertEqual(resp.status_code, 302)
        ai.assert_called_once()
        con = self._db()
        row = con.execute(
            "SELECT * FROM tag_analyses WHERE kind='vertical' AND student_id=4"
        ).fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertIn("纵向判断", row["content_md"])
        self.assertIn("复习电子守恒", row["content_md"])
        self.assertIn("计算稳定", row["content_md"])

    def test_vertical_analysis_requires_tagged_errors(self):
        con = self._db()
        con.execute("DELETE FROM question_tags WHERE lecture_id=20")
        con.commit()
        con.close()
        with mock.patch.object(app_module, "ai_vertical_analysis") as ai:
            resp = self._post("student_tag_analysis", cid=10, sid=4)
        self.assertEqual(resp.status_code, 302)
        ai.assert_not_called()
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM tag_analyses WHERE kind='vertical'").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
