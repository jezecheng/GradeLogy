"""课程批量选课隔离回归测试；使用合成账号，不调用 AI。"""
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a


class CourseEnrollmentBatchTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="gradelogy-enrollment-")
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.path = str(self.root / "data.sqlite")
        for obj, attr, value in (
                (a, "DB_PATH", self.path), (a.database, "DB_PATH", self.path),
                (a, "UPLOAD_DIR", str(self.root / "uploads")),
                (a.cfg, "DATA_DIR", str(self.root)),
                (a.cfg, "DB_PATH", self.path),
                (a.cfg, "STORAGE_DIR", str(self.root / "uploads")),
                (a.cfg, "BACKUP_DIR", str(self.root / "backups")),
                (a.cfg, "EXPORT_DIR", str(self.root / "exports"))):
            self.enterContext(mock.patch.object(obj, attr, value))
        a.cfg.ensure_dirs()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(a.database.SCHEMA)
        a.database.init_db()
        self.db.execute("DELETE FROM users")
        self.db.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,student_no) "
            "VALUES(?,?,'x',?,?,?,?)",
            [(1, "admin", "Admin", "admin", 1, ""),
             (2, "teacher_a", "Teacher A", "teacher", 1, ""),
             (3, "teacher_b", "Teacher B", "teacher", 1, ""),
             (4, "already", "Already Enrolled", "student", 1, "S004"),
             (5, "active_one", "Active One", "student", 1, "S005"),
             (6, "inactive", "Inactive Student", "student", 0, "S006"),
             (7, "not_student", "Not A Student", "teacher", 1, ""),
             (8, "active_two", "Active Two", "student", 1, "S008"),
             (9, "legacy", "Legacy Student", "student", 1, "S009")])
        self.db.executemany("INSERT INTO courses(id,name,teacher_id) VALUES(?,?,?)",
                            [(10, "Course A", 2), (11, "Course B", 3)])
        self.db.executemany("INSERT INTO course_teachers(course_id,teacher_id) VALUES(?,?)",
                            [(10, 2), (11, 3)])
        self.db.executemany("INSERT INTO enrollments(course_id,student_id) VALUES(?,?)",
                            [(10, 4), (11, 4), (11, 5)])
        self.db.execute("INSERT INTO lectures(id,course_id,title) VALUES(20,11,'B Lecture')")
        self.db.execute("INSERT INTO submissions(id,lecture_id,student_id,file_path) "
                        "VALUES(30,20,5,'course_11/student.pdf')")
        self.db.execute("INSERT INTO exams(id,course_id,title) VALUES(40,11,'B Exam')")
        self.db.execute("INSERT INTO exam_scores(exam_id,student_id,score) VALUES(40,5,'88')")
        self.db.commit()
        a.app.config.update(TESTING=True)

    def client(self, uid):
        client = a.app.test_client()
        with client.session_transaction() as session:
            session["uid"] = uid
            session["auth_epoch"] = a.session_epoch(str(self.root))
            session["_csrf"] = "test-token"
        return client

    def post(self, data, user=2, follow_redirects=True):
        return self.client(user).post(
            self.url("course_enroll", cid=10),
            data={"_csrf": "test-token", **data},
            follow_redirects=follow_redirects)

    @staticmethod
    def url(endpoint, **values):
        with a.app.test_request_context():
            return a.url_for(endpoint, **values)

    def enrollment_rows(self, course_id):
        return [row[0] for row in self.db.execute(
            "SELECT student_id FROM enrollments WHERE course_id=? ORDER BY student_id",
            (course_id,))]

    def test_course_page_offers_unchecked_multi_selection_and_search(self):
        response = self.client(2).get(self.url("course_detail", cid=10))
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="student_ids"', response.text)
        self.assertIn('id="enrollment-search"', response.text)
        self.assertIn("未默认勾选任何人", response.text)
        self.assertIn("Active One", response.text)
        self.assertIn("Active Two", response.text)
        self.assertNotIn('data-enrollment-candidate data-search="Already Enrolled', response.text)
        self.assertNotIn('data-enrollment-candidate data-search="Inactive Student', response.text)
        self.assertNotIn('data-enrollment-candidate data-search="Not A Student', response.text)

        class EnrollmentCheckboxParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.checkboxes = []

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "input" and attributes.get("name") == "student_ids":
                    self.checkboxes.append(attributes)

        parser = EnrollmentCheckboxParser()
        parser.feed(response.text)
        self.assertEqual(len(parser.checkboxes), 3)
        self.assertTrue(all("checked" not in checkbox for checkbox in parser.checkboxes))

    def test_batch_revalidates_each_item_reports_counts_and_preserves_other_course_data(self):
        response = self.post({"student_ids": [
            "5", "8", "4", "5", "6", "999", "7", "not-an-id"]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('<div class="flash">批量加入结果：新增 2 人，已在课程中 2 人，不可用 4 项。</div>',
                      response.text)
        self.assertEqual(self.enrollment_rows(10), [4, 5, 8])
        self.assertEqual(self.enrollment_rows(11), [4, 5])
        self.assertEqual(self.db.execute("SELECT file_path FROM submissions WHERE id=30").fetchone()[0],
                         "course_11/student.pdf")
        self.assertEqual(self.db.execute("SELECT score FROM exam_scores WHERE exam_id=40 AND student_id=5").fetchone()[0],
                         "88")

    def test_repeated_batch_is_idempotent_and_legacy_single_value_is_supported(self):
        first = self.post({"student_ids": ["5", "8"]})
        self.assertIn("新增 2 人，已在课程中 0 人，不可用 0 项", first.text)
        second = self.post({"student_ids": ["5", "8"]})
        self.assertIn("新增 0 人，已在课程中 2 人，不可用 0 项", second.text)
        legacy = self.post({"student_id": "9"})
        self.assertIn("新增 1 人，已在课程中 0 人，不可用 0 项", legacy.text)
        self.assertEqual(self.enrollment_rows(10), [4, 5, 8, 9])

    def test_empty_selection_reports_zero_counts(self):
        response = self.post({})
        self.assertIn("新增 0 人，已在课程中 0 人，不可用 0 项", response.text)
        self.assertIn("请选择至少一名学生", response.text)
        self.assertEqual(self.enrollment_rows(10), [4])

    def test_only_course_teachers_can_manage_enrollment(self):
        denied = self.post({"student_ids": ["5"]}, user=3, follow_redirects=False)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.enrollment_rows(10), [4])

        self.db.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(10,3)")
        self.db.commit()
        allowed = self.post({"student_ids": ["5"]}, user=3)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("新增 1 人", allowed.text)
        self.assertEqual(self.enrollment_rows(10), [4, 5])


if __name__ == "__main__":
    unittest.main()
