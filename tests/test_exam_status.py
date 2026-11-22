"""考试状态和旧成绩迁移回归；所有数据均为临时合成数据。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a
from utilities.sessions import session_epoch
from werkzeug.security import generate_password_hash


class ExamStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gradelogy-exam-status-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = a.create_app({
            "DATA_DIR": str(self.root),
            "SECRET_KEY": "exam-status-test-secret",
            "TESTING": True,
            "AUTO_BACKUP": False,
            "AUTO_MODEL_SYNC": False,
            "AUTO_GRADE": False,
        })
        a.initialize_app(self.app, start_workers=False)
        with self.app.app_context():
            db = a.get_db()
            db.executemany(
                "INSERT INTO users(id,username,password_hash,display_name,role,active) "
                "VALUES(?,?,?,? ,?,1)",
                [(2, "exam_teacher", "x", "考试老师", "teacher"),
                 (3, "exam_student1", "x", "学生一", "student"),
                 (4, "exam_student2", "x", "学生二", "student"),
                 (5, "exam_student3", "x", "学生三", "student"),
                 (6, "exam_outsider", "x", "旁听用户", "student")])
            db.execute("INSERT INTO courses(id,name,teacher_id) VALUES(10,'测试课程',2)")
            db.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(10,2)")
            db.executemany("INSERT INTO enrollments(course_id,student_id) VALUES(10,?)",
                           [(3,), (4,), (5,)])
            db.execute("INSERT INTO exams(id,course_id,title,full_score) VALUES(20,10,'阶段考试',100)")
            db.commit()

    def client(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["uid"] = user_id
            session["auth_epoch"] = session_epoch(str(self.root))
            session["_csrf"] = "exam-status-token"
        return client

    def test_teacher_saves_recorded_absent_and_pending_independently(self):
        teacher = self.client(2)
        before = teacher.get("/exam/20")
        self.assertEqual(before.status_code, 200)
        self.assertIn("待录入", before.text)

        response = teacher.post("/exam/20/scores", data={
            "_csrf": "exam-status-token",
            # A typed score wins over the still-default pending dropdown.
            "status_3": "pending", "score_3": "0", "comment_3": "零分仍是已录入成绩",
            # The server discards a stale score when absence is explicitly selected.
            "status_4": "absent", "score_4": "88", "comment_4": "缺席记录",
            # Comment-only historic behavior remains a visible recorded entry.
            "status_5": "recorded", "score_5": "", "comment_5": "已录入评语",
            # An unrostered id in a forged form must not receive a score.
            "status_6": "recorded", "score_6": "99", "comment_6": "不应保存",
        })
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            rows = {row["student_id"]: row for row in a.get_db().execute(
                "SELECT student_id,score,comment,status FROM exam_scores WHERE exam_id=20")}
        self.assertEqual(rows[3]["status"], "recorded")
        self.assertEqual(rows[3]["score"], "0")
        self.assertEqual(rows[4]["status"], "absent")
        self.assertEqual(rows[4]["score"], "")
        self.assertEqual(rows[4]["comment"], "缺席记录")
        self.assertEqual(rows[5]["status"], "recorded")
        self.assertEqual(rows[5]["score"], "")
        self.assertNotIn(6, rows)

        student_one = self.client(3).get("/student")
        self.assertIn("已录入", student_one.text)
        self.assertIn("零分仍是已录入成绩", student_one.text)
        self.assertIn(">0</span>", student_one.text)
        self.assertNotIn("缺席记录", student_one.text)

        student_two = self.client(4).get("/student")
        self.assertIn("缺考", student_two.text)
        self.assertIn("缺席记录", student_two.text)
        self.assertNotIn(">88</span>", student_two.text)

        student_three = self.client(5).get("/student")
        self.assertIn("已录入评语", student_three.text)
        self.assertIn("已录入评语", student_three.text)
        self.assertNotIn("缺席记录", student_three.text)

        outsider = self.client(6).get("/student")
        self.assertNotIn("阶段考试", outsider.text)

    def test_pending_save_clears_prior_result_and_absent_is_not_zero(self):
        db = self.app.config["DB_PATH"]
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO exam_scores(exam_id,student_id,score,comment,status) "
                "VALUES(20,3,'75','旧评语','recorded')")
        response = self.client(2).post("/exam/20/scores", data={
            "_csrf": "exam-status-token",
            "status_3": "pending", "score_3": "", "comment_3": "",
        })
        self.assertEqual(response.status_code, 302)
        with sqlite3.connect(db) as con:
            row = con.execute(
                "SELECT status FROM exam_scores WHERE exam_id=20 AND student_id=3").fetchone()
        self.assertIsNone(row)

    def test_legacy_rows_migrate_idempotently_without_inventing_absence(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        legacy_app = a.create_app({
            "DATA_DIR": str(legacy_root), "SECRET_KEY": "legacy-exam-test-secret", "TESTING": True,
        })
        legacy_db = legacy_app.config["DB_PATH"]
        with sqlite3.connect(legacy_db) as con:
            con.execute("""CREATE TABLE exam_scores(
                exam_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
                score TEXT DEFAULT '', comment TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(exam_id,student_id))""")
            con.executemany(
                "INSERT INTO exam_scores(exam_id,student_id,score,comment) VALUES(20,?,?,?)",
                [(3, "0", ""), (4, "", "仅有评语"), (5, "", "")])
        with legacy_app.app_context():
            a.database.init_db()
            expected = [(3, "recorded"), (4, "recorded"), (5, "pending")]
            first = a.get_db().execute(
                "SELECT student_id,status FROM exam_scores ORDER BY student_id").fetchall()
            self.assertEqual([(r[0], r[1]) for r in first], expected)
            a.database.init_db()
            second = a.get_db().execute(
                "SELECT student_id,status FROM exam_scores ORDER BY student_id").fetchall()
            self.assertEqual([(r[0], r[1]) for r in second], expected)


if __name__ == "__main__":
    unittest.main()
