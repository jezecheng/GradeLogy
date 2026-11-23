# -*- coding: utf-8 -*-
"""上传即自动批改、学生端锁定与自动批改次数上限的集成测试。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a  # noqa: E402
from utilities.sessions import session_epoch  # noqa: E402

CSRF = "auto-grade-test-token"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class InlinePool:
    """把队列线程池替换为同步执行，保证请求返回时批改结果已落库。"""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class AutoGradeFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gradelogy-auto-grade-")
        self.addCleanup(self.temp.cleanup)
        self.root = str(Path(self.temp.name))
        self.app = a.create_app({
            "DATA_DIR": self.root,
            "SECRET_KEY": "auto-grade-test-secret",
            "TESTING": True,
            "AUTO_BACKUP": False,
            "AUTO_MODEL_SYNC": False,
            "AUTO_GRADE": False,
        })
        a.initialize_app(self.app, start_workers=False)
        self.app.extensions["grading_queue"]._pool = InlinePool()
        self.settings = {"ai_api_url": "http://ai.local", "ai_api_key": "key",
                         "ai_model": "model-a"}
        with self.app.app_context():
            db = a.get_db()
            db.executemany(
                "INSERT INTO users(id,username,password_hash,display_name,role,active,"
                "must_change_password) VALUES(?,?,'x',?,?,1,0)",
                [(2, "teacher", "老师", "teacher"), (4, "student", "学生", "student")])
            db.execute("INSERT INTO courses(id,name,teacher_id) VALUES(30,'数学',2)")
            db.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(30,2)")
            db.execute("INSERT INTO enrollments(course_id,student_id) VALUES(30,4)")
            candidates = [{"key": "q0", "no": "1", "hint": "第一题"},
                          {"key": "q1", "no": "2", "hint": "第二题"}]
            db.execute(
                "INSERT INTO lectures(id,course_id,title,answer_pdf,scope_candidates,"
                "scope_selected,tags_generated_scope) VALUES(100,30,'第1讲','ans.pdf',?,?,?)",
                (json.dumps(candidates, ensure_ascii=False), json.dumps(["q0"]),
                 a.tag_scope_signature(["q0"])))
            db.commit()
        self.lecture_url = self.url("student_lecture_page", hid=100)
        self.teacher_lecture_url = self.url("lecture_scope_save", hid=100)
        self.student = self.login(4)
        self.teacher = self.login(2)

    def url(self, endpoint, **values):
        with self.app.test_request_context():
            return a.url_for(endpoint, **values)

    def login(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = uid
            sess["auth_epoch"] = session_epoch(self.root)
            sess["_csrf"] = CSRF
        return client

    def setting(self, _teacher_id, key, default=""):
        return self.settings.get(key, default)

    @staticmethod
    def payload():
        return {"questions": [{"no": "1", "ocr": "答案", "verdict": "对", "comment": "很好"}],
                "summary": {"evaluation": "完成度不错"}, "error": ""}

    def upload(self, client, filename="a.png"):
        return client.post(
            self.lecture_url,
            data={"_csrf": CSRF, "files": (io.BytesIO(PNG), filename)},
            content_type="multipart/form-data", follow_redirects=True)

    def submissions(self):
        with self.app.app_context():
            return a.get_db().execute(
                "SELECT id,status FROM submissions ORDER BY id").fetchall()

    def test_upload_auto_enqueues_when_prerequisites_ready(self):
        with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                mock.patch.object(a, "run_grading",
                                  return_value=self.payload()) as run:
            resp = self.upload(self.student)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("已自动进入批改队列", resp.text)
        run.assert_called_once()
        rows = self.submissions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "待批改")

    def test_upload_stays_pending_without_scope(self):
        with self.app.app_context():
            db = a.get_db()
            db.execute("UPDATE lectures SET scope_selected='' WHERE id=100")
            db.commit()
        with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                mock.patch.object(a, "run_grading") as run:
            resp = self.upload(self.student)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("等待老师批改", resp.text)
        run.assert_not_called()
        self.assertEqual(self.submissions()[0]["status"], "已提交")

    def test_upload_waits_for_scope_tags_then_pending_submission_auto_enqueues(self):
        with self.app.app_context():
            db = a.get_db()
            db.execute("UPDATE lectures SET tags_generated_scope='' WHERE id=100")
            db.commit()
        with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                mock.patch.object(a, "run_grading", return_value=self.payload()) as run:
            response = self.upload(self.student)
            self.assertIn("等待老师批改", response.get_data(as_text=True))
            run.assert_not_called()

            with self.app.app_context():
                db = a.get_db()
                db.execute("UPDATE lectures SET tags_generated_scope=? WHERE id=100",
                           (a.tag_scope_signature(["q0"]),))
                db.commit()
                queued = a._grading_queue.auto_enqueue_lecture(100)

        self.assertEqual(queued, 1)
        run.assert_called_once()
        self.assertEqual(self.submissions()[0]["status"], "待批改")

    def test_upload_waits_for_tags_then_auto_enqueues_when_tags_finish(self):
        with self.app.app_context():
            a.get_db().execute("UPDATE lectures SET tags_generated_scope='' WHERE id=100")
            a.get_db().commit()
        with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                mock.patch.object(a, "run_grading", return_value=self.payload()) as run:
            resp = self.upload(self.student)
            self.assertIn("等待老师批改", resp.get_data(as_text=True))
            run.assert_not_called()
            sid = self.submissions()[0]["id"]

            with self.app.app_context():
                a.get_db().execute(
                    "UPDATE lectures SET tags_generated_scope=? WHERE id=100",
                    (a.tag_scope_signature(["q0"]),))
                a.get_db().commit()
                queued = a._grading_queue.auto_enqueue_lecture(100)

        self.assertEqual(queued, 1)
        run.assert_called_once()
        self.assertEqual(self.submissions()[0]["status"], "待批改")

    def test_student_page_locks_while_grading_and_rejects_resubmit(self):
        with self.app.app_context():
            db = a.get_db()
            sid = db.execute(
                "INSERT INTO submissions(lecture_id,student_id,file_path,status) "
                "VALUES(100,4,'stu.pdf','待批改')").lastrowid
            db.execute("INSERT INTO ai_operations(teacher_id,operation_type,submission_id,"
                       "status,started_at) VALUES(2,'grade',?,'running',CURRENT_TIMESTAMP)",
                       (sid,))
            db.commit()

        page = self.student.get(self.lecture_url).get_data(as_text=True)
        self.assertIn("锁定中", page)
        self.assertNotIn("data-draft-final", page)
        self.assertIn("tag-批改中", page)

        home = self.student.get("/student").get_data(as_text=True)
        self.assertIn("批改中", home)

        with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                mock.patch.object(a, "run_grading") as run:
            resp = self.upload(self.student)
        self.assertIn("正在批改中", resp.get_data(as_text=True))
        run.assert_not_called()
        self.assertEqual(len(self.submissions()), 1)

    def test_auto_grade_only_first_three_submissions(self):
        with self.app.app_context():
            db = a.get_db()
            ids = [db.execute(
                "INSERT INTO submissions(lecture_id,student_id,file_path,status) "
                "VALUES(100,4,?,'已提交')", (f"stu{i}.pdf",)).lastrowid
                for i in range(1, 5)]
            db.commit()
            queue = self.app.extensions["grading_queue"]
            with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                    mock.patch.object(a, "run_grading",
                                      return_value=self.payload()) as run:
                results = [queue.auto_enqueue(sid) for sid in ids]
        self.assertEqual([ok for ok, _ in results], [True, True, True, False])
        self.assertIn("上限", results[3][1])
        self.assertEqual(run.call_count, 3)

    def test_auto_grade_limit_is_configurable(self):
        self.settings["ai_auto_grade_attempts"] = "5"
        with self.app.app_context():
            db = a.get_db()
            ids = [db.execute(
                "INSERT INTO submissions(lecture_id,student_id,file_path,status) "
                "VALUES(100,4,?,'已提交')", (f"stu{i}.pdf",)).lastrowid
                for i in range(1, 5)]
            db.commit()
            queue = self.app.extensions["grading_queue"]
            with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                    mock.patch.object(a, "run_grading",
                                      return_value=self.payload()) as run:
                results = [queue.auto_enqueue(sid) for sid in ids]
        self.assertTrue(all(ok for ok, _ in results))
        self.assertEqual(run.call_count, 4)

    def test_saving_scope_waits_for_tag_generation_before_enqueuing_pending_submission(self):
        with self.app.app_context():
            db = a.get_db()
            db.execute("UPDATE lectures SET scope_selected='' WHERE id=100")
            db.execute("INSERT INTO submissions(lecture_id,student_id,file_path,status) "
                       "VALUES(100,4,'stu.pdf','已提交')")
            db.commit()
        with mock.patch.object(a, "get_setting", side_effect=self.setting), \
                mock.patch.object(a, "run_grading",
                                  return_value=self.payload()) as run:
            resp = self.teacher.post(self.teacher_lecture_url,
                                     data={"_csrf": CSRF, "no": ["q0", "q1"]},
                                     follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        run.assert_not_called()
        self.assertEqual(self.submissions()[0]["status"], "已提交")


if __name__ == "__main__":
    unittest.main()
