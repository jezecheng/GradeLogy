# -*- coding: utf-8 -*-
"""数字助手新增工具的查询与授权测试。

运行：.venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from db import SCHEMA
from utilities import assistant


class AssistantToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "test.sqlite")
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role) VALUES(?,?,?,?,?)",
            [(1, "teacher1", "x", "张老师", "teacher"),
             (2, "stu1", "x", "李一", "student"),
             (3, "stu2", "x", "王二", "student"),
             (4, "teacher2", "x", "别的老师", "teacher")])
        con.executemany("INSERT INTO courses(id,name,teacher_id) VALUES(?,?,?)",
                        [(10, "线性代数", 1), (11, "别的课", 4)])
        con.executemany("INSERT INTO course_teachers(course_id,teacher_id) VALUES(?,?)",
                        [(10, 1), (11, 4)])
        con.executemany("INSERT INTO enrollments(course_id,student_id) VALUES(?,?)",
                        [(10, 2), (10, 3)])
        con.executemany("INSERT INTO lectures(id,course_id,title,deadline) VALUES(?,?,?,?)",
                        [(20, 10, "第一讲", "2026-11-01"), (21, 10, "第二讲", "2026-11-08")])
        con.executemany(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_published)"
            " VALUES(?,?,?,?,?,?)",
            [(30, 20, 2, "a.pdf", "已批改", 1), (31, 20, 3, "b.pdf", "已提交", 0)])
        con.executemany(
            "INSERT INTO questions(submission_id,no,verdict,reviewed) VALUES(?,?,?,?)",
            [(30, "1", "对", 1), (30, "2", "错", 0)])
        con.execute("UPDATE questions SET ai_comment='符号错误，注意转置'"
                    " WHERE submission_id=30 AND no='2'")
        con.execute("UPDATE submissions SET report_md='本题计算失误较多，建议复习行列式性质。'"
                    " WHERE id=30")
        con.execute("UPDATE lectures SET answer_pdf='ans.pdf', answer_pages='2,3',"
                    " scope_selected='[\"k1\",\"k2\"]',"
                    " scope_candidates='[{\"key\":\"k1\",\"no\":\"1\",\"hint\":\"求行列式\"},"
                    "{\"key\":\"k2\",\"no\":\"2\",\"hint\":\"证明可逆\"}]' WHERE id=20")
        con.execute("INSERT INTO exams(id,course_id,title,exam_date,full_score)"
                    " VALUES(40,10,'期中考试','2026-11-05',100)")
        con.executemany(
            "INSERT INTO exam_scores(exam_id,student_id,score,status) VALUES(?,?,?,?)",
            [(40, 2, "85", "recorded"), (40, 3, "", "pending")])
        con.executemany(
            "INSERT INTO prompts(id,teacher_id,kind,name,content,is_default) VALUES(?,?,?,?,?,?)",
            [(50, 1, "grade", "我的批改提示词", "请严格按步骤评分", 0),
             (51, 0, "grade", "系统默认批改", "默认内容", 1),
             (52, 4, "grade", "别人的提示词", "不该看到", 0)])
        con.execute("INSERT INTO lesson_plans(course_id,title,content)"
                    " VALUES(10,'第一讲课案','行列式定义与性质')")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.execute("INSERT INTO ai_operations(teacher_id,operation_type,submission_id,"
                    "status,started_at) VALUES(1,'grade',31,'running',?)", (now,))
        con.execute("INSERT INTO ai_operations(teacher_id,operation_type,submission_id,"
                    "status,started_at,finished_at) VALUES(1,'grade',30,'success',?,?)",
                    (now, now))
        con.execute("INSERT INTO ai_usage_events(operation_id,attempt,total_tokens,"
                    "estimated_cost,status) VALUES"
                    "((SELECT MAX(id) FROM ai_operations),1,1000,0.12,'success')")
        con.commit()
        con.close()
        self._patch = mock.patch.object(assistant, "DB_PATH", path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_list_exams_counts_scored_and_missing(self):
        out = assistant.list_exams(1, "线性代数")
        self.assertEqual(out["course"], "线性代数")
        self.assertEqual(len(out["exams"]), 1)
        self.assertEqual(out["exams"][0]["scored"], 1)
        self.assertEqual(out["exams"][0]["missing"], 1)

    def test_exam_scores_statistics_ignore_blank(self):
        out = assistant.exam_scores(1, "线性代数", "期中")
        self.assertEqual(out["scored"], 1)
        self.assertEqual(out["average"], 85.0)
        self.assertEqual(len(out["students"]), 2)

    def test_exam_scores_rejects_other_teachers_course(self):
        self.assertIn("error", assistant.exam_scores(1, "别的课", "期中"))

    def test_course_overview_aggregates_progress(self):
        out = assistant.course_overview(1, "线性代数")
        self.assertEqual(out["enrolled"], 2)
        self.assertEqual(out["lecture_count"], 2)
        self.assertEqual(out["totals"]["submitted"], 2)
        self.assertEqual(out["totals"]["graded"], 1)
        self.assertEqual(out["totals"]["published"], 1)
        self.assertEqual(out["totals"]["pending_review"], 1)
        first = next(h for h in out["lectures"] if h["title"] == "第一讲")
        self.assertEqual(first["missing"], 0)

    def test_pending_work_splits_three_buckets(self):
        out = assistant.pending_work(1, "线性代数")
        self.assertEqual(out["counts"]["not_submitted"], 2)
        self.assertEqual(out["counts"]["not_graded"], 1)
        self.assertEqual(out["counts"]["pending_review"], 1)
        self.assertEqual(out["not_graded"][0]["submission_id"], 31)
        self.assertEqual(out["pending_review"][0]["unreviewed_questions"], 1)

    def test_pending_work_respects_limit(self):
        out = assistant.pending_work(1, "线性代数", limit=1)
        self.assertEqual(len(out["not_submitted"]), 1)
        self.assertEqual(out["counts"]["not_submitted"], 2)

    def test_pending_work_rejects_other_teachers_course(self):
        self.assertIn("error", assistant.pending_work(1, "别的课"))

    def test_grading_queue_lists_running_job(self):
        out = assistant.grading_queue(1)
        self.assertEqual(out["running"], 1)
        self.assertEqual(out["queued"], 0)
        self.assertEqual(out["active"][0]["student"], "王二")
        self.assertEqual(out["active"][0]["status_label"], "进行中")

    def test_grading_queue_is_scoped_to_teacher(self):
        out = assistant.grading_queue(4)
        self.assertEqual(out["active"], [])

    def test_search_reports_hits_report_and_comments(self):
        out = assistant.search_reports(1, "计算失误")
        self.assertEqual(out["matched"], 1)
        self.assertEqual(out["hits"][0]["field"], "批改报告")
        self.assertIn("计算失误", out["hits"][0]["snippet"])

        out = assistant.search_reports(1, "符号错误")
        self.assertEqual(out["hits"][0]["field"], "题目评语")
        self.assertEqual(out["hits"][0]["question"], "2")

    def test_search_reports_is_scoped_to_teacher(self):
        self.assertEqual(assistant.search_reports(4, "计算失误")["matched"], 0)

    def test_search_reports_requires_two_characters(self):
        self.assertIn("error", assistant.search_reports(1, "计"))

    def test_ai_usage_aggregates_by_operation(self):
        out = assistant.ai_usage(1, 7)
        self.assertEqual(out["summary"]["operations"], 2)
        self.assertEqual(out["summary"]["requests"], 1)
        self.assertEqual(out["summary"]["total_tokens"], 1000)
        self.assertEqual(out["summary"]["estimated_cost"], 0.12)
        self.assertEqual(out["summary"]["active"], 1)
        self.assertEqual(out["by_type"][0]["operation_type"], "grade")

    def test_ai_usage_excludes_other_teachers(self):
        self.assertEqual(assistant.ai_usage(4, 7)["summary"]["operations"], 0)

    def test_list_prompts_hides_other_teachers(self):
        prompts = {p["id"]: p for p in assistant.list_prompts(1)["prompts"]}
        self.assertEqual(sorted(prompts), [50, 51])
        self.assertTrue(prompts[50]["editable"])
        self.assertFalse(prompts[51]["editable"])

    def test_get_prompt_respects_visibility(self):
        self.assertEqual(assistant.get_prompt(1, 50)["name"], "我的批改提示词")
        self.assertEqual(assistant.get_prompt(1, 51)["name"], "系统默认批改")
        self.assertIn("error", assistant.get_prompt(1, 52))

    def test_lesson_plan_reads_content(self):
        out = assistant.lesson_plan(1, "线性代数")
        self.assertEqual(out["plan_count"], 1)
        self.assertIn("行列式", out["plans"][0]["content"])

    def test_lecture_scope_parses_range_and_questions(self):
        out = assistant.lecture_scope(1, "线性代数", "第一讲")
        self.assertEqual(out["answer_pages"], [2, 3])
        self.assertEqual(out["scope_question_count"], 2)
        self.assertEqual(out["scope_questions"][0]["no"], "1")
        self.assertTrue(out["ready_for_grading"])

    def test_lecture_scope_rejects_other_teachers_course(self):
        self.assertIn("error", assistant.lecture_scope(4, "线性代数", "第一讲"))

    def test_student_tools_only_see_own_data(self):
        self.assertEqual([c["id"] for c in assistant.my_courses(2)["courses"]], [10])
        self.assertEqual(assistant.my_courses(3)["courses"][0]["not_submitted"], 1)

    def test_student_pending_and_lectures(self):
        pending = assistant.my_pending(2)
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["pending"][0]["title"], "第二讲")
        lectures = {x["title"]: x for x in assistant.my_lectures(2)["lectures"]}
        self.assertEqual(lectures["第一讲"]["my_status"], "已批改")
        self.assertEqual(lectures["第一讲"]["accuracy"], "50%")
        self.assertEqual(lectures["第二讲"]["my_status"], "未提交")

    def test_student_report_hidden_until_published(self):
        shown = assistant.my_report(2, "线性代数", "第一讲")
        self.assertTrue(shown["published"])
        self.assertEqual(shown["questions"][0]["verdict"], "对")
        hidden = assistant.my_report(3, "线性代数", "第一讲")
        self.assertFalse(hidden["published"])
        self.assertNotIn("questions", hidden)

    def test_student_exam_scores(self):
        rows = {x["title"]: x for x in assistant.my_exam_scores(2)["exams"]}
        self.assertEqual(rows["期中考试"]["score"], "85")
        self.assertEqual(assistant.my_exam_scores(3)["exams"][0]["score"], "待录入")

    def test_exam_absence_is_separate_from_pending_and_zero_score(self):
        with sqlite3.connect(assistant.DB_PATH) as con:
            con.execute("UPDATE exam_scores SET status='absent' WHERE exam_id=40 AND student_id=3")
            con.execute("UPDATE exam_scores SET score='0' WHERE exam_id=40 AND student_id=2")
        overview = assistant.list_exams(1, "线性代数")["exams"][0]
        self.assertEqual(overview["scored"], 1)
        self.assertEqual(overview["absent"], 1)
        self.assertEqual(overview["pending"], 0)
        scores = assistant.exam_scores(1, "线性代数", "期中")
        self.assertEqual(scores["scored"], 1)
        self.assertEqual(scores["average"], 0.0)
        by_student = {row["student"]: row for row in scores["students"]}
        self.assertEqual(by_student["李一"]["status"], "已录入")
        self.assertEqual(by_student["王二"]["status"], "缺考")
        mine = assistant.my_exam_scores(3)["exams"][0]
        self.assertEqual(mine["status"], "缺考")
        self.assertEqual(mine["score"], "—")

    def test_comment_only_exam_entry_is_recorded_but_not_counted_as_scored(self):
        with sqlite3.connect(assistant.DB_PATH) as con:
            con.execute("UPDATE exam_scores SET status='recorded',comment='说明' "
                         "WHERE exam_id=40 AND student_id=3")
        overview = assistant.list_exams(1, "线性代数")["exams"][0]
        self.assertEqual(overview["recorded"], 2)
        self.assertEqual(overview["scored"], 1)
        self.assertEqual(overview["pending"], 0)
        score_data = assistant.exam_scores(1, "线性代数", "期中")
        self.assertEqual(score_data["recorded"], 2)
        self.assertEqual(score_data["scored"], 1)
        student_row = assistant.my_exam_scores(3)["exams"][0]
        self.assertEqual(student_row["status"], "已录入评语")
        self.assertEqual(student_row["score"], "已录入评语（暂无分数）")

    def test_student_cannot_read_other_course(self):
        self.assertIn("error", assistant.my_lectures(2, "别的课"))
        self.assertIn("error", assistant.my_report(2, "别的课"))

    def test_role_tools_are_scoped(self):
        student_names = {t["name"] for t in assistant.tools_for_role("student")}
        self.assertIn("my_report", student_names)
        self.assertNotIn("list_courses", student_names)
        admin_names = {t["name"] for t in assistant.tools_for_role("admin")}
        self.assertIn("admin_overview", admin_names)
        self.assertNotIn("student_report", admin_names)

    def test_admin_tools(self):
        overview = assistant.admin_overview()
        self.assertEqual(overview["counts"]["courses"], 2)
        self.assertEqual(overview["counts"]["users"], 4)
        self.assertEqual(len(assistant.admin_list_users(role="teacher")["users"]), 2)
        courses = {c["name"]: c for c in assistant.admin_list_courses()["courses"]}
        self.assertEqual(courses["线性代数"]["students"], 2)
        self.assertEqual(assistant.admin_ai_usage(7)["summary"]["operations"], 2)

    def test_call_tool_routes_by_role(self):
        out = json.loads(assistant.call_tool("my_pending", {}, 2, "student"))
        self.assertEqual(out["count"], 1)
        # 学生不能调用老师/管理员工具
        self.assertIn("error", json.loads(assistant.call_tool("admin_overview", {}, 2, "student")))
        self.assertIn("error", json.loads(assistant.call_tool("list_courses", {}, 2, "student")))

    def test_call_tool_returns_json(self):
        text = assistant.call_tool("pending_work", {"course": "线性代数"}, 1)
        self.assertEqual(json.loads(text)["counts"]["not_graded"], 1)


if __name__ == "__main__":
    unittest.main()
