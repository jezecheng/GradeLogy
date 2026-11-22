"""学生跨课程截止任务区的隔离回归测试。"""
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a
from utilities.sessions import session_epoch


class StudentDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gradelogy-student-deadlines-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = a.create_app({
            "DATA_DIR": str(self.root),
            "SECRET_KEY": "student-deadline-test-secret",
            "TESTING": True,
            "AUTO_BACKUP": False,
            "AUTO_MODEL_SYNC": False,
            "AUTO_GRADE": False,
        })
        a.initialize_app(self.app, start_workers=False)
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.asia_shanghai = ZoneInfo("Asia/Shanghai")
        with self.app.app_context():
            db = a.get_db()
            db.executemany(
                "INSERT INTO users(id,username,password_hash,display_name,role,active) "
                "VALUES(?,?,?,? ,?,1)",
                [(2, "teacher", "x", "任课老师", "teacher"),
                 (3, "student", "x", "当前学生", "student"),
                 (4, "other", "x", "另一学生", "student"),
                 (6, "no_courses", "x", "未选课学生", "student")])
            db.executemany("INSERT INTO courses(id,name,teacher_id) VALUES(?,?,2)", [
                (10, "课程甲"), (11, "课程乙"), (12, "其他课程")])
            db.executemany("INSERT INTO course_teachers(course_id,teacher_id) VALUES(?,2)",
                           [(10,), (11,), (12,)])
            db.executemany("INSERT INTO enrollments(course_id,student_id) VALUES(?,?)", [
                (10, 3), (11, 3), (12, 4)])
            deadlines = [
                (20, 10, "OVERDUE_UNSUBMITTED", self.deadline(-2), ""),
                (21, 11, "DUE_SOON_LATER", self.deadline(2), ""),
                (22, 10, "DUE_SOON_GRADED", self.deadline(1), ""),
                (23, 11, "FAR_FUTURE", self.deadline(10), ""),
                (24, 10, "NO_DEADLINE", "", ""),
                (25, 10, "OVERDUE_SUBMITTED", self.deadline(-1), ""),
                (26, 12, "NOT_MY_COURSE", self.deadline(1), ""),
            ]
            db.executemany(
                "INSERT INTO lectures(id,course_id,title,deadline) VALUES(?,?,?,?)",
                [(lid, cid, title, deadline) for lid, cid, title, deadline, _ in deadlines])
            db.executemany(
                "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_published) "
                "VALUES(?,?,3,?,?,?)",
                [(30, 22, "course_10/student-3.pdf", "已批改", 1),
                 (31, 25, "course_10/student-3.pdf", "已批改", 1)])
            db.execute("INSERT INTO settings(teacher_id,key,value) VALUES(3,'display_timezone','UTC')")
            db.commit()

    def deadline(self, day_offset):
        due = (self.now + timedelta(days=day_offset)).astimezone(self.asia_shanghai)
        return due.replace(tzinfo=None).isoformat(timespec="minutes")

    def student_client(self, uid=3):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["uid"] = uid
            session["auth_epoch"] = session_epoch(str(self.root))
            session["_csrf"] = "deadline-test-token"
        return client

    def test_deadline_panel_scopes_sorts_formats_and_uses_latest_submission(self):
        response = self.student_client().get("/student")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'<section class="student-deadlines".*?</section>',
                          response.text, flags=re.DOTALL)
        self.assertIsNotNone(match)
        panel = match.group(0)

        self.assertIn("OVERDUE_UNSUBMITTED", panel)
        self.assertIn("DUE_SOON_GRADED", panel)
        self.assertIn("DUE_SOON_LATER", panel)
        self.assertNotIn("OVERDUE_SUBMITTED", panel)
        self.assertNotIn("FAR_FUTURE", panel)
        self.assertNotIn("NO_DEADLINE", panel)
        self.assertNotIn("NOT_MY_COURSE", response.text)
        self.assertLess(panel.index("DUE_SOON_GRADED"), panel.index("DUE_SOON_LATER"))
        self.assertIn("查看批改", panel)
        self.assertIn("去提交", panel)

        expected_beijing = (self.now + timedelta(days=1)).astimezone(self.asia_shanghai).strftime(
            "%Y-%m-%d %H:%M")
        self.assertIn(expected_beijing, panel)

    def test_no_deadline_or_other_students_course_is_exposed_in_panel(self):
        response = self.student_client(uid=4).get("/student")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'<section class="student-deadlines".*?</section>',
                          response.text, flags=re.DOTALL)
        self.assertIsNotNone(match)
        panel = match.group(0)
        self.assertIn("NOT_MY_COURSE", panel)
        self.assertNotIn("OVERDUE_UNSUBMITTED", panel)
        self.assertNotIn("DUE_SOON_GRADED", panel)

    def test_empty_course_list_has_clear_deadline_empty_state(self):
        response = self.student_client(uid=6).get("/student")
        self.assertEqual(response.status_code, 200)
        self.assertIn("未来 7 天没有截止任务", response.text)
        self.assertIn("你还没有加入任何课程", response.text)


if __name__ == "__main__":
    unittest.main()
