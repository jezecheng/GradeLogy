"""审阅缺陷的隔离回归测试；不调用真实 AI 或使用本地教学数据。"""
import json
import sqlite3
import tempfile
import os
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a
from utilities import pdfgrade


class ReviewRegressionTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="gradelogy-regression-")
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
            "INSERT INTO users(id,username,password_hash,display_name,role) VALUES(?,?,'x',?,?)",
            [(1, "admin", "Admin", "admin"), (2, "ta", "A", "teacher"),
             (3, "tb", "B", "teacher"), (4, "s", "Student", "student")])
        self.db.executemany("INSERT INTO courses(id,name,teacher_id) VALUES(?,?,?)",
                            [(10, "A", 2), (11, "B", 3)])
        self.db.executemany("INSERT INTO course_teachers VALUES(?,?,CURRENT_TIMESTAMP)",
                            [(10, 2), (11, 3)])
        self.db.executemany("INSERT INTO enrollments(course_id,student_id) VALUES(?,4)",
                            [(10,), (11,)])
        self.db.execute("INSERT INTO lectures(id,course_id,title,answer_pdf,student_pdf) "
                        "VALUES(20,10,'Lecture','course_10/answer.pdf','course_10/handout.pdf')")
        self.db.execute("INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_published,report_md) "
                        "VALUES(30,20,4,'course_10/student.pdf','已批改',1,'Old report')")
        self.db.execute("INSERT INTO questions(id,submission_id,no,verdict,reviewed) VALUES(40,30,'1','错',1)")
        self.db.commit()
        a.app.config.update(TESTING=True)

    def client(self, uid):
        c = a.app.test_client()
        with c.session_transaction() as s:
            s["uid"] = uid
            s["auth_epoch"] = a.session_epoch(str(self.root))
            s["_csrf"] = "test-token"
        return c

    def url(self, endpoint, **values):
        with a.app.test_request_context():
            return a.url_for(endpoint, **values)

    def post(self, endpoint, user=2, **values):
        return self.client(user).post(self.url(endpoint, **values), data={"_csrf": "test-token"})

    def test_student_lecture_collapses_question_details_and_highlights_report_link(self):
        html = self.client(4).get(self.url("student_lecture_page", hid=20)).get_data(as_text=True)
        self.assertIn('<details class="student-question-details">', html)
        self.assertNotIn('<details class="student-question-details" open', html)
        self.assertIn("点击展开，查看每题的作答与批改结果", html)
        self.assertIn('class="student-report-button"', html)
        self.assertIn("点击查看详细报告", html)

    def test_assistant_route_rejects_open_chat_before_calling_model(self):
        with mock.patch.object(a.assistant, "reply") as reply:
            response = self.client(4).post(
                "/assistant/chat", json={"messages": [
                    {"role": "user", "content": "写一首关于月亮的诗"}]},
                headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持闲聊", response.json["error"])
        reply.assert_not_called()

    def test_course_files_authorize_all_read_modes(self):
        p = self.root / "uploads/course_10/answer.pdf"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"%PDF-1.4 private")
        for suffix in ("", "?meta=1", "?page=1"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client(3).get("/files/course_10/answer.pdf" + suffix).status_code, 403)
                self.assertEqual(self.client(4).get("/files/course_10/answer.pdf" + suffix).status_code, 403)
        for uid in (1, 2):
            r = self.client(uid).get("/files/course_10/answer.pdf")
            self.assertEqual(r.status_code, 200)
            r.close()
        # Unknown files in an authorized directory are not automatically authorized.
        (p.parent / "untracked.pdf").write_bytes(b"private")
        self.assertEqual(self.client(2).get("/files/course_10/untracked.pdf").status_code, 403)

    def test_graph_source_files_require_membership(self):
        self.db.execute("INSERT INTO knowledge_graphs(id,subject,name,created_by) VALUES(90,'S','G',2)")
        self.db.execute("INSERT INTO kg_materials(graph_id,filename,stored_name,sha256) VALUES(90,'a.txt','kg/90/a.txt','hash')")
        self.db.commit()
        p = self.root / "uploads/kg/90/a.txt"
        p.parent.mkdir(parents=True)
        p.write_text("private")
        self.assertEqual(self.client(3).get("/files/kg/90/a.txt").status_code, 403)
        self.db.execute("INSERT INTO kg_members(graph_id,user_id,role) VALUES(90,3,'use')")
        self.db.commit()
        r = self.client(3).get("/files/kg/90/a.txt")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_vertical_analysis_is_scoped_to_course(self):
        self.db.executemany(
            "INSERT INTO tag_analyses(teacher_id,course_id,student_id,kind,content_md) VALUES(?,?,4,'vertical',?)",
            [(2, 10, "COURSE_A_ANALYSIS"), (3, 11, "PRIVATE_COURSE_B_ANALYSIS")])
        self.db.commit()
        r = self.client(2).get(self.url("student_detail", cid=10, sid=4))
        self.assertEqual(r.status_code, 200)
        self.assertIn("COURSE_A_ANALYSIS", r.text)
        self.assertNotIn("PRIVATE_COURSE_B_ANALYSIS", r.text)
        with self.assertRaises(ValueError):
            a.tag_utils.latest_analysis(self.db, "vertical", student_id=4)

    def test_restore_revokes_other_browsers_with_fixed_secret(self):
        teacher = self.client(2)
        admin = self.client(1)
        self.assertEqual(teacher.get("/admin/users").status_code, 403)
        source = self.root / "restore"
        source.mkdir()
        dst = sqlite3.connect(source / "data.sqlite")
        self.db.backup(dst)
        dst.execute("UPDATE users SET role='admin',username='restored-admin' WHERE id=2")
        dst.commit()
        dst.close()
        with a.app.test_request_context("/admin/restore", method="POST"):
            a._apply_restore(str(source))
        for c in (teacher, admin):
            r = c.get("/admin/users")
            self.assertEqual(r.status_code, 302)
            self.assertIn("/login", r.location)
        self.assertEqual(self.client(2).get("/admin/users").status_code, 200)

    def test_legacy_cookie_is_invalid_after_restore_epoch_changes(self):
        c = self.client(2)
        with c.session_transaction() as s:
            s.pop("auth_epoch")
        a.revoke_sessions(str(self.root))
        self.assertIn("/login", c.get("/courses").location)

    def test_regrade_hides_new_results_in_page_report_wrong_book_and_assistant(self):
        self.db.execute("INSERT INTO student_question_reviews(student_id,question_id) VALUES(4,40)")
        self.db.commit()
        with a.app.app_context():
            a._save_grading_result(a.get_db(), 30, {"questions": [
                {"no": "1", "verdict": "错", "comment": "PRIVATE_NEW_RESULT"}]})
        s = self.client(4)
        self.assertNotIn("PRIVATE_NEW_RESULT", s.get(self.url("student_lecture_page", hid=20)).text)
        self.assertNotIn("PRIVATE_NEW_RESULT", s.get("/student/wrong-questions").text)
        self.assertEqual(s.get(self.url("student_report", sid=30)).status_code, 404)
        def connect():
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            return db
        with mock.patch.object(a.assistant, "_db", side_effect=connect):
            report = a.assistant.my_report(4, "10", "20")
        self.assertFalse(report["published"])
        sub = self.db.execute("SELECT * FROM submissions WHERE id=30").fetchone()
        self.assertEqual(sub["report_published"], 0)
        self.assertEqual(sub["report_md"], "")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM student_question_reviews").fetchone()[0], 0)

    def test_incomplete_review_save_and_publish_remain_private(self):
        self.db.execute("UPDATE questions SET reviewed=0")
        self.db.execute("UPDATE submissions SET status='待批改',report_published=0")
        self.db.commit()
        with mock.patch.object(a, "generate_report", return_value="draft"):
            self.post("submission_review_save", sid=30)
            self.post("submission_report_publish", sid=30)
        row = self.db.execute("SELECT status,report_published FROM submissions WHERE id=30").fetchone()
        self.assertEqual(tuple(row), ("待批改", 0))
        self.db.execute("UPDATE questions SET reviewed=1")
        self.db.commit()
        with mock.patch.object(a, "generate_report", return_value="draft"):
            self.post("submission_review_save", sid=30)
        self.post("submission_report_publish", sid=30)
        self.assertEqual(self.db.execute("SELECT report_published FROM submissions WHERE id=30").fetchone()[0], 1)

    def test_single_report_publish_releases_report_and_question_results_together(self):
        self.db.execute("UPDATE submissions SET status='待批改',report_published=0,report_md='Report'")
        self.db.execute("UPDATE questions SET reviewed=1,verdict='错'")
        self.db.commit()
        student = self.client(4)
        lecture_url = self.url("student_lecture_page", hid=20)
        self.assertEqual(student.get(self.url("student_report", sid=30)).status_code, 404)
        self.assertNotIn('id="question-40"', student.get(lecture_url).get_data(as_text=True))
        self.assertNotIn("第 1 题", student.get(self.url("student_wrong_questions")).get_data(as_text=True))

        self.client(2).post(self.url("submission_review_save", sid=30), data={
            "_csrf": "test-token", "action": "complete", "verdict_40": "错"})
        self.assertEqual(tuple(self.db.execute(
            "SELECT status,report_published FROM submissions WHERE id=30").fetchone()),
            ("已批改", 0))
        self.assertEqual(student.get(self.url("student_report", sid=30)).status_code, 404)

        self.post("submission_report_publish", sid=30)
        self.assertEqual(tuple(self.db.execute(
            "SELECT status,report_published FROM submissions WHERE id=30").fetchone()),
            ("已批改", 1))
        self.assertEqual(student.get(self.url("student_report", sid=30)).status_code, 200)
        self.assertIn('id="question-40"', student.get(lecture_url).get_data(as_text=True))
        self.assertIn("第 1 题", student.get(self.url("student_wrong_questions")).get_data(as_text=True))

        self.post("submission_report_publish", sid=30)
        self.assertEqual(student.get(self.url("student_report", sid=30)).status_code, 404)
        self.assertNotIn('id="question-40"', student.get(lecture_url).get_data(as_text=True))

    def test_page_match_is_conservative_and_persists_source_page(self):
        normalized = pdfgrade._normalize([
            {"no": "1", "student_page": 8, "verdict": "对", "score": 5,
             "confidence": 0.9, "comment": "看起来匹配但没有确认字段"},
            {"no": "2", "student_page": 9, "page_match": "confirmed",
             "verdict": "对", "score": 5, "confidence": 0.82, "comment": "题干核对一致"},
        ])
        self.assertEqual((normalized[0]["student_page"], normalized[0]["page_match"],
                          normalized[0]["verdict"], normalized[0]["score"]),
                         (0, "uncertain", "无法判定", 0.0))
        self.assertEqual((normalized[1]["student_page"], normalized[1]["page_match"]),
                         (9, "confirmed"))
        self.assertEqual(normalized[0]["confidence"], 0.35)
        self.assertEqual(normalized[1]["confidence"], 0.82)
        a._save_grading_result(self.db, 30, {"questions": normalized, "summary": {}})
        rows = self.db.execute(
            "SELECT no,page_no,page_match,verdict,confidence FROM questions ORDER BY no").fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("1", 0, "uncertain", "无法判定", 0.35),
            ("2", 9, "confirmed", "对", 0.82),
        ])

    def test_ai_grade_request_labels_source_pdf_pages_and_requires_stem_mapping(self):
        image_part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}
        response = {"questions": [{"no": "例1", "ocr": "A", "standard_answer": "A",
                                   "verdict": "对", "comment": "正确", "student_page": 1,
                                   "page_match": "confirmed", "answer_page": 1}],
                    "weak_points": [], "evaluation": "", "total_score": ""}
        with mock.patch.object(pdfgrade, "_file_parts",
                               side_effect=[([image_part], ""), ([image_part], "")]), \
                mock.patch.object(pdfgrade.llm, "chat",
                                  return_value=(json.dumps(response), "stop", "")) as chat:
            result = pdfgrade.grade_pdf_submission(
                "student.pdf", "answer.pdf", "https://api.example/v1", "secret", "vision",
                extra="参考题干：例1 气体体积如何变化", answer_pages=[4])
        self.assertEqual(result["questions"][0]["page_match"], "confirmed")
        payload = chat.call_args.args[3]
        labels = [part.get("text", "") for part in payload]
        self.assertTrue(any("题干比对" in text for text in labels))
        self.assertIn("【学生作业原 PDF 全局第 1 页】", labels)
        self.assertIn("【标准答案本次输入第 1 页；教师 PDF 第 4 页】", labels)
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 32768)

    def test_report_merges_question_and_knowledge_graph_columns(self):
        md = a.build_report_md(
            "学生", "讲次", "6月1日", [], {},
            {"question_comments": {"例6": "结合速率常数标签，本题求算正确。",
                                   "例7": "放射性计算遗漏了衰变关系。"}},
            kg_diagnosis={"matrix": {
                "columns": [{"tag": "速率常数", "name": "速率常数"},
                            {"tag": "初始速率", "name": "初始速率"}],
                "rows": [{"no": "例6", "brief": "速率常数求算",
                          "tags": ["速率常数", "初始速率"],
                          "cells": {"速率常数": "⚠️", "初始速率": "⚠️"},
                          "analysis": "速率常数掌握不完整；前置薄弱：初始速率"},
                          {"no": "例7", "brief": "放射性年代计算",
                           "tags": ["放射性衰变"],
                           "cells": {"速率常数": "🔴", "初始速率": "⚠️"},
                           "analysis": "计算题未完成，需关注相关前置知识",
                           "review_comment": "复核意见：漏写衰变关系"}],
            }, "diagnoses": []})
        self.assertIn("| 题号 | 判定 | 批改说明 | 指示标签 | 单题评语 |", md)
        self.assertIn("## 一、正确率与失分归因", md)
        self.assertIn("## 二、逐题明细", md)
        self.assertNotIn("知识图谱诊断", md)
        self.assertIn("## 三、评价", md)
        self.assertLess(md.index("正确率"), md.index("## 二、逐题明细"))
        self.assertIn("| **例6** | — | — | 速率常数、初始速率 | 结合速率常数标签，本题求算正确。 |", md)
        self.assertIn("| **例7** | — | — | 放射性衰变 | 放射性计算遗漏了衰变关系。 |", md)
        self.assertNotIn("速率常数求算", md)
        self.assertIn("请结合本次讲义复习相关知识点", md)
        self.assertNotIn("| 速率常数 |", md)
        self.assertNotIn("纵向批阅", md)
        self.assertIn("| **例6** |", md)
        self.assertIn("| **例7** |", md)
        rendered = a.render_markdown(md)
        self.assertIn("report-question-table report-kg-question-table", rendered)
        self.assertIn("<th>题号</th>", rendered)

    def test_report_without_knowledge_graph_keeps_evaluation_as_section_three(self):
        md = a.build_report_md("学生", "讲次", "6月1日", [], {},
                               {"good": "完成情况不错。"})
        self.assertIn("## 三、评价", md)
        self.assertNotIn("## 四、评价", md)

    def test_legacy_wide_knowledge_graph_table_is_compacted_for_page_width(self):
        md = (
            "## 四、知识图谱诊断\n\n"
            "| 题号 | 题目简述 | 反应级数 | 半衰期 | 速率常数 | 初始浓度 | 知识点点评 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 例1 | 速率计算 | ✅ | — | ⚠️ | — | 速率常数计算不完整 |\n"
        )
        rendered = a.render_markdown(md)
        self.assertIn("report-kg-question-table", rendered)
        self.assertIn("<th>指示标签</th>", rendered)
        self.assertIn("反应级数：✅", rendered)
        self.assertIn("速率常数：⚠️", rendered)
        self.assertIn("速率常数计算不完整", rendered)
        self.assertNotIn("题目简述", rendered)
        self.assertNotIn("report-dynamic-table", rendered)

    def test_report_renders_parent_stem_and_independent_child_rows(self):
        md = a.build_report_md(
            "学生", "讲次", "6月1日",
            [{"no": "1.1", "parent_no": "1", "parent_stem": "母题公共条件",
              "stem": "第一小问", "verdict": "对", "note": "正确"},
              {"no": "1.2", "parent_no": "1", "parent_stem": "母题公共条件",
              "stem": "第二小问", "verdict": "无法判定", "note": "需核对"}],
            {}, {})
        self.assertIn("1.1", md)
        self.assertIn("1.2", md)
        self.assertIn("| **第1题** |", md)
        self.assertIn("| 　**1.1** |", md)
        self.assertIn("| 　**1.2** |", md)
        self.assertNotIn("母题公共条件", md)
        self.assertIn("以下小问独立判定", md)
        rendered = a.render_markdown(md)
        self.assertIn("report-question-table", rendered)
        self.assertIn("<strong>第1题</strong>", rendered)
        self.assertIn("　<strong>1.1</strong>", rendered)
        self.assertIn("正确率 50%（完全答对 1/2 个独立批改项）", md)
        self.assertEqual(a.accuracy_text(1, 0, 0, 2), "50%")
        summary = a._build_summary(
            [{"no": "1.1", "verdict": "对"},
             {"no": "1.2", "verdict": "无法判定"}], [], "", [])
        self.assertEqual(summary["accuracy"], "50%")
        self.assertEqual(summary["total"], 2)

    def test_legacy_report_table_formats_question_numbers_and_indents_subquestions(self):
        md = ("| 题号 | 判定 | 批改说明 |\n| --- | --- | --- |\n"
              "| 1 | ✅ | 正确 |\n| 8.1 | ⚠️ | 小问待补充 |\n"
              "| 第 8 题 | — | 母题 |\n")
        rendered = a.render_markdown(md)
        self.assertIn("<strong>第1题</strong>", rendered)
        self.assertIn("　<strong>8.1</strong>", rendered)
        self.assertEqual(rendered.count("<strong>第8题</strong>"), 1)

    def test_ai_report_comment_generates_independent_kg_question_comments(self):
        reply = json.dumps({
            "good": "整体认真",
            "improve": [],
            "next": "再核对条件",
            "question_comments": [{"no": "1.1", "comment": "该题条件遗漏，建议对照氧化还原反应规律。"}],
        }, ensure_ascii=False)
        question = {"no": "1.1", "verdict": "错", "note": "复核后确认遗漏",
                    "ocr_text": "学生作答内容", "standard_answer": "标准答案内容"}
        with mock.patch.object(pdfgrade.llm, "chat", return_value=(reply, "stop", "")) as chat:
            result = pdfgrade.ai_report_comment(
                [question], [], "", "https://fixture", "key", "model",
                kg_question_context='[{"no":"1.1","tags":["氧化还原反应"]}]')
        sent = chat.call_args.args[3][0]["text"]
        self.assertIn("student_answer", sent)
        self.assertIn("学生作答内容", sent)
        self.assertIn("标准答案内容", sent)
        self.assertIn("复核后确认遗漏", sent)
        self.assertIn("氧化还原反应", sent)
        self.assertEqual(chat.call_args.kwargs["timeout"], 120)
        self.assertEqual(chat.call_args.kwargs["retries"], 2)
        self.assertEqual(result["question_comments"], {
            "1.1": "该题条件遗漏，建议对照氧化还原反应规律。"})

    def test_review_uses_manual_review_label_and_report_button_is_conditional(self):
        html = self.client(2).get(self.url("submission_review", sid=30)).get_data(as_text=True)
        self.assertIn("人工复核：Student", html)
        self.assertNotIn("AI 汇总与失分归因", html)
        self.assertNotIn("总评（学生可见）", html)
        self.assertNotIn('name="overall"', html)
        self.assertIn('class="report-open-button"', html)
        self.assertIn("打开评阅报告", html)
        self.assertLess(html.index("保存草稿"), html.index("打开评阅报告"))
        self.assertLess(html.index("打开评阅报告"), html.index("重新生成评阅报告"))
        self.db.execute("UPDATE submissions SET report_md='' WHERE id=30")
        self.db.commit()
        html_without_report = self.client(2).get(
            self.url("submission_review", sid=30)).get_data(as_text=True)
        self.assertNotIn('class="report-open-button"', html_without_report)
        self.assertIn("完成并生成评阅报告", html_without_report)
        self.assertNotIn("重新生成评阅报告", html_without_report)

    def test_student_report_keeps_latex_for_client_katex_rendering(self):
        latex = r"$\mathrm{H_2O}$"
        self.db.execute("UPDATE submissions SET report_published=1,report_md=? WHERE id=30",
                        (f"# 报告\n\n化学式：{latex}",))
        self.db.commit()
        response = self.client(4).get(self.url("student_report", sid=30))
        self.assertEqual(response.status_code, 200)
        self.assertIn(latex, response.get_data(as_text=True))
        self.assertNotIn("<math ", response.get_data(as_text=True))

    def test_explicit_draft_hides_even_fully_reviewed_results(self):
        with mock.patch.object(a, "generate_report", return_value="draft"):
            response = self.client(2).post(self.url("submission_review_save", sid=30),
                data={"_csrf": "test-token", "action": "draft", "note_40": "new draft"})
        self.assertEqual(response.status_code, 302)
        row = self.db.execute("SELECT status,report_published FROM submissions WHERE id=30").fetchone()
        self.assertEqual(tuple(row), ("待批改", 0))
        self.assertEqual(self.client(4).get(self.url("student_report", sid=30)).status_code, 404)

    def test_complete_and_generate_saves_review_without_publishing(self):
        self.db.execute("UPDATE submissions SET status='待批改',report_published=0")
        self.db.commit()
        response = self.client(2).post(self.url("submission_review_save", sid=30),
            data={"_csrf": "test-token", "then": "report"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("generate=1", response.headers["Location"])
        row = self.db.execute(
            "SELECT status,report_published FROM submissions WHERE id=30").fetchone()
        self.assertEqual(tuple(row), ("已批改", 0))

    def test_report_page_tracks_async_generation_and_refreshes_saved_report(self):
        self.db.execute("UPDATE submissions SET report_md='',report_published=0 WHERE id=30")
        self.db.execute(
            "INSERT INTO ai_operations(teacher_id,operation_type,submission_id,model,status,started_at) "
            "VALUES(2,'report_generate',30,'fixture','running',CURRENT_TIMESTAMP)")
        operation_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.db.commit()

        page = self.client(2).get(self.url("submission_report", sid=30)).get_data(as_text=True)
        self.assertIn('id="report-build-status"', page)
        self.assertIn("报告生成中…", page)
        self.assertIn("disabled", page[page.index('class="report-build-form"'):])
        self.assertIn("window.location.reload()", page)
        review_page = self.client(2).get(self.url(
            "submission_review", sid=30)).get_data(as_text=True)
        self.assertIn('id="report-build-status"', review_page)
        self.assertIn("评阅报告生成中…", review_page)

        status = self.client(2).get(self.url(
            "submission_report_build_status", sid=30, operation_id=operation_id))
        self.assertEqual(status.json, {"ok": True, "status": "running", "error": "",
                                       "has_report": False})

        self.db.execute("UPDATE submissions SET report_md='Generated report' WHERE id=30")
        self.db.execute("UPDATE ai_operations SET status='success' WHERE id=?", (operation_id,))
        self.db.commit()
        status = self.client(2).get(self.url(
            "submission_report_build_status", sid=30, operation_id=operation_id))
        self.assertTrue(status.json["has_report"])
        self.assertEqual(status.json["status"], "success")
        refreshed = self.client(2).get(self.url("submission_report", sid=30))
        self.assertIn("Generated report", refreshed.get_data(as_text=True))
        self.assertIn("重新生成报告", refreshed.get_data(as_text=True))
        refreshed_review = self.client(2).get(self.url(
            "submission_review", sid=30)).get_data(as_text=True)
        self.assertIn("打开评阅报告", refreshed_review)
        self.assertNotIn("评阅报告生成中…", refreshed_review)

    def test_subquestions_are_grouped_but_each_has_an_independent_review_row(self):
        candidates = [{
            "key": "q0", "no": "1", "source_no": "例7", "stem": "母题公共条件。",
            "subquestions": [
                {"key": "q0.1", "no": "1.1", "source_no": "(1)", "stem": "求速率。"},
                {"key": "q0.2", "no": "1.2", "source_no": "(2)", "stem": "求级数。"},
            ],
        }]
        self.db.execute("UPDATE lectures SET scope_candidates=?,scope_selected=? WHERE id=20",
                        (json.dumps(candidates, ensure_ascii=False), json.dumps(["q0"])))
        self.db.execute("UPDATE questions SET no='1.1',confidence=0.73 WHERE id=40")
        self.db.execute(
            "INSERT INTO questions(id,submission_id,no,verdict,reviewed) VALUES(41,30,'1.2','对',1)")
        self.db.commit()

        teacher_page = self.client(2).get(
            self.url("submission_review", sid=30)).get_data(as_text=True)
        self.assertIn('class="review-parent" data-parent-no="1"', teacher_page)
        self.assertIn("母题公共条件。", teacher_page)
        self.assertIn("第 1.1 小问", teacher_page)
        self.assertIn("第 1.2 小问", teacher_page)
        self.assertIn("AI 信心 73%", teacher_page)
        self.assertIn('name="verdict_40"', teacher_page)
        self.assertIn('name="verdict_41"', teacher_page)

        student_page = self.client(4).get(
            self.url("student_lecture_page", hid=20)).get_data(as_text=True)
        self.assertIn('class="student-question-group"', student_page)
        self.assertIn("母题公共条件。", student_page)
        self.assertIn("第 1.1 小问", student_page)
        self.assertIn("第 1.2 小问", student_page)

    def test_lecture_confidence_average_and_ai_only_report_skip_review_flow(self):
        self.db.execute("UPDATE questions SET reviewed=0,confidence=0.64 WHERE id=40")
        self.db.execute("UPDATE submissions SET report_md='',report_published=0 WHERE id=30")
        self.db.commit()
        page = self.client(2).get(self.url("lecture_detail", hid=20)).get_data(as_text=True)
        self.assertIn("AI 信心均值 64%", page)
        self.assertIn("--confidence-hue:77", page)
        self.assertIn("跳过人工复核并生成报告", page)

        captured = []

        def capture(teacher_id, operation_type, **kwargs):
            captured.append((teacher_id, operation_type, kwargs))
            return {"id": len(captured)}, None

        with mock.patch.object(a._ai_task_queue, "submit", side_effect=capture):
            response = self.post("submission_review_skip_and_report", sid=30)
        self.assertEqual(response.status_code, 302)
        saved = self.db.execute(
            "SELECT status,review_skipped,report_published FROM submissions WHERE id=30").fetchone()
        self.assertEqual(tuple(saved), ("已批改", 1, 0))
        self.assertEqual(self.db.execute(
            "SELECT reviewed FROM questions WHERE id=40").fetchone()[0], 0)
        self.assertEqual(captured[0][1], "report_generate")
        review_page = self.client(2).get(self.url("submission_review", sid=30)).get_data(as_text=True)
        self.assertIn("AI 信心 64%", review_page)
        self.assertIn("人工复核已跳过", review_page)

        # 跳过复核后仍允许显式重生成报告，而不是被未审核题目拦回复核页。
        with mock.patch.object(a._ai_task_queue, "submit", side_effect=capture):
            response = self.post("submission_report_build", sid=30)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(captured[1][1], "report_generate")

        self.db.execute("UPDATE submissions SET report_md='AI report' WHERE id=30")
        self.db.commit()
        report_page = self.client(2).get(self.url("submission_report", sid=30)).get_data(as_text=True)
        self.assertIn("人工复核已跳过", report_page)
        self.assertIn("一键发布给学生", report_page)
        self.post("submission_report_publish", sid=30)
        self.assertEqual(self.db.execute(
            "SELECT report_published FROM submissions WHERE id=30").fetchone()[0], 1)

    def test_explicit_publication_requires_all_questions_and_no_running_grade(self):
        def publish():
            with mock.patch.object(a, "generate_report", return_value="draft"):
                self.client(2).post(self.url("submission_review_save", sid=30),
                    data={"_csrf": "test-token", "action": "publish"})
            return self.db.execute("SELECT status FROM submissions WHERE id=30").fetchone()[0]
        self.db.execute("UPDATE questions SET reviewed=0"); self.db.commit()
        self.assertEqual(publish(), "待批改")
        self.db.execute("UPDATE questions SET reviewed=1")
        self.db.execute("INSERT INTO ai_operations(teacher_id,operation_type,submission_id,status,started_at) VALUES(2,'grade',30,'running',CURRENT_TIMESTAMP)")
        self.db.commit()
        self.assertEqual(publish(), "待批改")
        self.db.execute("UPDATE ai_operations SET status='success'"); self.db.commit()
        self.assertEqual(publish(), "已批改")

    def test_workload_respects_course_enrollment_and_running_regrade(self):
        from utilities.teacher_views import workload
        self.assertEqual(workload(self.db, [11]), {})
        self.db.execute("UPDATE submissions SET status='待批改'"); self.db.commit()
        self.assertEqual(workload(self.db, [10])[10]['review'], 1)
        self.db.execute("INSERT INTO ai_operations(teacher_id,operation_type,submission_id,status,started_at) VALUES(2,'grade',30,'running',CURRENT_TIMESTAMP)")
        self.db.commit()
        self.assertEqual(workload(self.db, [10], by_lecture=True)[20], {'pending': 0, 'review': 0, 'running': 1})
        self.db.execute("DELETE FROM enrollments WHERE course_id=10"); self.db.commit()
        self.assertEqual(workload(self.db, [10]), {})

    def test_markdown_links_escape_attributes_and_reject_script_schemes(self):
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links = []
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.links.append(dict(attrs))
        parser = Parser()
        parser.feed(a.render_markdown('[review](https://example.invalid/"onmouseover="alert(1))'))
        self.assertEqual(len(parser.links), 1)
        self.assertNotIn("onmouseover", parser.links[0])
        for url in ("javascript:alert(1)", "data:text/html,payload", "//evil.example"):
            p = Parser()
            p.feed(a.render_markdown(f"[review]({url})"))
            self.assertEqual(p.links, [])
        p = Parser()
        p.feed(a.render_markdown("[OK](https://example.org/?a=1&b=2)"))
        self.assertEqual(p.links[0]["href"], "https://example.org/?a=1&b=2")

    def graph_snapshot(self):
        self.db.execute("INSERT INTO knowledge_graphs(id,subject,name,created_by) VALUES(90,'S','G',2)")
        self.db.execute("INSERT INTO kg_nodes(id,graph_id,name,normalized,category) VALUES(900,90,'Original','original','old')")
        self.db.commit()
        snapshot = a.kg_utils.apply_category_map(self.db, 90, [{"from": "old", "to": "new"}])
        self.db.execute("INSERT INTO kg_category_maps(id,graph_id,status,snapshot,created_by) VALUES(90,90,?,?,2)",
                        (a.kg_utils.CATEGORY_APPLIED, json.dumps(snapshot)))
        self.db.commit()
        return snapshot

    def test_rollback_rejects_later_build_and_preserves_all_nodes(self):
        self.graph_snapshot()
        self.db.execute("INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,status) VALUES(90,90,'later.txt','kg/90/later.txt','hash','完成')")
        self.db.execute("INSERT INTO kg_nodes(id,graph_id,name,normalized) VALUES(901,90,'Later','later')")
        self.db.commit()
        self.post("knowledge_graph_category_rollback", gid=90, mid=90)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM kg_nodes WHERE graph_id=90").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT status FROM kg_category_maps WHERE id=90").fetchone()[0], a.kg_utils.CATEGORY_APPLIED)

    def test_rollback_rejects_other_type_of_edit_and_legacy_snapshot(self):
        snapshot = self.graph_snapshot()
        a.kg_utils.apply_audit(self.db, 90, [{"action": "drop_node", "node_id": 900}])
        with self.assertRaisesRegex(ValueError, "之后已有"):
            a.kg_utils.restore_snapshot(self.db, 90, snapshot)
        self.db.rollback()
        with self.assertRaisesRegex(ValueError, "旧快照"):
            a.kg_utils.restore_snapshot(self.db, 90, {"nodes": [], "edges": []})
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 0)

    def test_unenroll_clears_derived_records_only_in_selected_course(self):
        self.db.executemany("INSERT INTO student_weak_points(student_id,course_id,tag) VALUES(4,?,?)",
                            [(10, "REMOVE_ME"), (11, "KEEP_ME")])
        self.db.execute("INSERT INTO student_question_reviews(student_id,question_id) VALUES(4,40)")
        self.db.executemany("INSERT INTO tag_analyses(teacher_id,course_id,student_id,kind) VALUES(2,?,4,'vertical')",
                            [(10,), (11,)])
        self.db.commit()
        self.post("course_unenroll", cid=10, sid=4)
        for table in ("submissions", "questions", "student_question_reviews"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual([r[0] for r in self.db.execute("SELECT course_id FROM student_weak_points")], [11])
        self.assertEqual([r[0] for r in self.db.execute("SELECT course_id FROM tag_analyses")], [11])
        self.assertNotIn("REMOVE_ME", self.client(4).get("/student/weak-points").text)

    def test_lecture_delete_clears_questions_tags_and_review_marks(self):
        self.db.execute("INSERT INTO student_question_reviews(student_id,question_id) VALUES(4,40)")
        self.db.execute("INSERT INTO question_tags(lecture_id,question_no,tag) VALUES(20,'1','Tag')")
        self.db.commit()
        self.post("lecture_delete", hid=20)
        for table in ("lectures", "submissions", "questions", "question_tags", "student_question_reviews"):
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_course_delete_is_atomic_on_child_cleanup_failure(self):
        self.db.execute("CREATE TRIGGER stop_delete BEFORE DELETE ON lectures BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.post("course_delete", cid=10)
        for table in ("courses", "lectures", "submissions", "questions"):
            self.assertGreater(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_user_delete_clears_credentials_and_transfers_shared_assets(self):
        self.db.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(10,3)")
        self.db.execute("INSERT INTO ai_api_keys(teacher_id,api_key) VALUES(2,'private')")
        self.db.execute("INSERT INTO settings VALUES(2,'ai_api_key','private')")
        self.db.execute("INSERT INTO knowledge_graphs(id,subject,name,created_by) VALUES(90,'S','G',2)")
        self.db.commit()
        self.post("admin_user_delete", user=1, uid=2)
        self.assertEqual(self.db.execute("SELECT teacher_id FROM courses WHERE id=10").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT created_by FROM knowledge_graphs WHERE id=90").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ai_api_keys WHERE teacher_id=2").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM settings WHERE teacher_id=2").fetchone()[0], 0)

    def test_failed_result_write_marks_task_failed_and_preserves_old_tags(self):
        self.db.execute("UPDATE lectures SET scope_candidates=?,scope_selected=? WHERE id=20",
                        (json.dumps([{"key": "q0", "no": "1"}]), json.dumps(["q0"])))
        self.db.execute("INSERT INTO question_tags(lecture_id,question_no,tag,source) VALUES(20,'1','Old','ai')")
        self.db.execute("CREATE TRIGGER fail_tags BEFORE INSERT ON question_tags BEGIN SELECT RAISE(ABORT,'fixture write failure'); END")
        self.db.commit()
        captured = {}
        def capture(tid, kind, **kw):
            op = a._start_ai_operation(tid, kind, kw["model"], lecture_id=20, status="queued")
            captured.update(runner=kw["runner"], operation=op)
            return op, None
        with (mock.patch.object(a, "_ai_endpoint", return_value=("https://fixture", "key", "model", "")),
              mock.patch.object(a._ai_task_queue, "submit", side_effect=capture)):
            self.post("lecture_tags_generate", hid=20)
        with mock.patch.object(a, "analyze_question_tags", return_value=(
                [{"key": "q0", "no": "1", "tags": ["New"], "category": "S"}], "")):
            a._ai_task_queue._execute(("fixture", "fixture"), captured["runner"], captured["operation"])
        status = self.db.execute("SELECT status FROM ai_operations WHERE id=?", (captured["operation"]["id"],)).fetchone()[0]
        self.assertEqual(status, "failed")
        self.assertEqual([r[0] for r in self.db.execute("SELECT tag FROM question_tags")], ["Old"])

    def test_report_uses_submission_course_prompt_with_different_ids(self):
        self.db.executemany("INSERT INTO prompts(id,teacher_id,kind,name,content) VALUES(?,?,'report',?,?)",
                            [(100, 2, "A", "PROMPT_A"), (101, 3, "B", "PROMPT_B")])
        self.db.execute("UPDATE courses SET report_prompt_id=100 WHERE id=10")
        self.db.execute("INSERT INTO courses(id,name,teacher_id,report_prompt_id) VALUES(20,'Other',3,101)")
        self.db.execute("UPDATE submissions SET review_skipped=1 WHERE id=30")
        self.db.execute(
            "UPDATE questions SET ocr_text='学生遗漏反应条件',standard_answer='应写反应条件',"
            "note='复核：条件缺失' WHERE id=40")
        self.db.commit()
        kg = {"diagnoses": [], "matrix": {
            "columns": [{"tag": "氧化还原反应", "name": "氧化还原反应"}],
            "rows": [{"no": "1", "brief": "反应条件判断", "tags": ["氧化还原反应"],
                      "analysis": "本题知识点判定为可能存在问题",
                      "review_comment": "复核：条件缺失"}]}}
        with (a.app.app_context(),
              mock.patch.object(a, "get_setting", side_effect=lambda tid, key, default="": {
                     "ai_api_url": "https://fixture", "ai_api_key": "key", "ai_model": "model"}.get(key, default)),
              mock.patch.object(a, "_kg_diagnosis", return_value=kg),
              mock.patch.object(a, "ai_report_comment", return_value={
                  "good": "OK", "question_comments": {"1": "结合标准答案，本题反应条件遗漏。"}}) as call):
            a.generate_report(a.get_db(), 30, 2)
        self.assertEqual(call.call_args.kwargs["prompt"], "PROMPT_A")
        self.assertIn("氧化还原反应", call.call_args.kwargs["kg_question_context"])
        self.assertEqual(call.call_args.args[0][0]["ocr_text"], "学生遗漏反应条件")
        self.assertEqual(call.call_args.args[0][0]["standard_answer"], "应写反应条件")
        self.assertEqual(call.call_args.args[0][0]["note"], "复核：条件缺失")
        saved = self.db.execute(
            "SELECT report_md,report_detail FROM submissions WHERE id=30").fetchone()
        self.assertIn("OK", saved["report_md"])
        self.assertIn("人工复核已跳过", saved["report_md"])
        self.assertIn("| 题号 | 判定 | 批改说明 | 指示标签 | 单题评语 |", saved["report_md"])
        self.assertIn("| **第1题** |", saved["report_md"])
        self.assertIn("| 氧化还原反应 | 结合标准答案，本题反应条件遗漏。 |",
                      saved["report_md"])
        self.assertEqual(json.loads(saved["report_detail"]), {
            "good": "OK", "question_comments": {"1": "结合标准答案，本题反应条件遗漏。"}})
        operation = self.db.execute(
            "SELECT status FROM ai_operations WHERE submission_id=30 "
            "AND operation_type='report_generate' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(operation["status"], "success")

    def test_report_page_exports_pdf_from_the_browser_rendered_view(self):
        self.db.execute(
            "UPDATE submissions SET report_md=? WHERE id=30",
            ("# 化学作业报告\n\n| 题号 | 题干/小问 | 判定 | 批改说明 |\n"
             "| --- | --- | --- | --- |\n"
             "| 1.1 | 不需要显示的题干 | 对 | $\\mathrm{Cl^-}$ 判断正确 |",))
        self.db.commit()
        response = self.client(2).get(self.url("submission_report", sid=30))
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('id="report-export-content"', page)
        self.assertIn('id="download-report-pdf"', page)
        self.assertIn('id="report-pdf-progress"', page)
        self.assertIn('class="report-download-button"', page)
        self.assertIn('data-filename="Student_Lecture_批阅报告.pdf"', page)
        self.assertIn("vendor/html2canvas/html2canvas.min.js", page)
        self.assertIn("vendor/jspdf/jspdf.umd.min.js", page)
        self.assertIn('id="report-pdf-button-spinner"', page)
        self.assertIn("pageBreaks", page)
        self.assertIn("化学作业报告", page)
        visible_report = page.split('<div class="report-body">', 1)[1].split(
            '<details class="report-editor"', 1)[0]
        self.assertNotIn("不需要显示的题干", visible_report)
        self.assertIn(r"$\mathrm{Cl^-}$", visible_report)
        markdown_download = self.client(2).get(
            self.url("submission_report_download", sid=30))
        self.assertEqual(markdown_download.status_code, 200)
        self.assertIn("Student_Lecture_批阅报告.md",
                      unquote(markdown_download.headers["Content-Disposition"]))

    def test_env_key_takes_over_entire_ai_configuration(self):
        import ai_config
        self.db.execute("INSERT INTO ai_api_keys(teacher_id,base_url,api_key,model,is_active) VALUES(2,'https://db','db-key','db-model',1)")
        self.db.commit()
        with mock.patch.dict(ai_config.ENV_AI_SETTINGS, {"ai_api_url": "https://env/", "ai_api_key": "env-key", "ai_model": "env-model"}):
            conf = ai_config.resolve(2, db=self.db)
            self.assertEqual((conf["api_url"], conf["api_key"], conf["model"]), ("https://env", "env-key", "env-model"))
        with mock.patch.dict(ai_config.ENV_AI_SETTINGS, {"ai_api_url": "", "ai_api_key": "env-key", "ai_model": ""}):
            conf = ai_config.resolve(2, db=self.db)
            self.assertEqual((conf["api_url"], conf["api_key"], conf["model"]), ("", "env-key", ""))
        with mock.patch.dict(ai_config.ENV_AI_SETTINGS, {"ai_api_url": "", "ai_api_key": "", "ai_model": "override"}):
            self.assertEqual(ai_config.resolve(2, db=self.db)["model"], "db-model")

    def test_factory_instances_keep_databases_signers_and_queues_separate(self):
        apps = []
        for name in ("one", "two"):
            application = a.create_app({"DATA_DIR": str(self.root / name), "SECRET_KEY": name, "TESTING": True})
            a.initialize_app(application, start_workers=False)
            apps.append(application)
            for key in ("ai_task_queue", "grading_queue"):
                self.addCleanup(application.extensions[key]._pool.shutdown, wait=True)
            with application.app_context():
                a.get_db().execute("UPDATE users SET display_name=?", (name,))
                a.get_db().commit()
        first, second = apps
        self.assertIsNot(first.extensions["ai_task_queue"], second.extensions["ai_task_queue"])
        with first.test_request_context():
            original = a.url_for("course_detail", cid=10)
            self.assertEqual(a.get_db().execute("SELECT display_name FROM users").fetchone()[0], "one")
            self.assertEqual(a._upload_dir(), str(self.root / "one/uploads"))
        with second.test_request_context():
            self.assertNotEqual(a.url_for("course_detail", cid=10), original)
            self.assertEqual(a.get_db().execute("SELECT display_name FROM users").fetchone()[0], "two")
        with first.test_request_context():
            self.assertEqual(a.url_for("course_detail", cid=10), original)
        # A real asynchronous runner must write into its owning instance, not the default app.
        def runner(db, operation):
            db.execute("UPDATE users SET display_name='async-one'")
            a._finish_ai_operation(operation, "success")
        with first.app_context():
            op, err = first.extensions["ai_task_queue"].submit(1, "fixture", model="mock", runner=runner)
            self.assertIsNone(err)
        first.extensions["ai_task_queue"]._pool.shutdown(wait=True)
        with first.app_context():
            self.assertEqual(a.get_db().execute("SELECT display_name FROM users").fetchone()[0], "async-one")
            self.assertEqual(a.get_db().execute("SELECT status FROM ai_operations WHERE id=?", (op["id"],)).fetchone()[0], "success")
        with second.app_context():
            self.assertEqual(a.get_db().execute("SELECT display_name FROM users").fetchone()[0], "two")

    def test_factory_upload_limit_uses_new_instance_configuration(self):
        with a.app.app_context():
            application = a.create_app({"DATA_DIR": str(self.root / "limits"), "SECRET_KEY": "fixture",
                                        "MAX_UPLOAD_MB": 2, "MAX_RESTORE_MB": 3, "KG_MAX_UPLOAD_MB": 4})
        self.assertEqual(application.config["MAX_CONTENT_LENGTH"], 4 * 1024 * 1024)
        for key in ("ai_task_queue", "grading_queue"):
            self.addCleanup(application.extensions[key]._pool.shutdown, wait=True)

    def test_regrading_enqueue_hides_published_results_before_worker_runs(self):
        with a.app.app_context():
            queue = a.app.extensions["grading_queue"]
            self.db.execute(
                "UPDATE submissions SET report_detail='Old detail',review_skipped=1,"
                "grader='旧批改来源' WHERE id=30")
            self.db.commit()
            with mock.patch.object(queue._pool, "submit") as submit:
                operation, error = queue.enqueue(2, 30, 10, 20)
                self.addCleanup(queue._discard, 30)
                self.assertIsNone(error)
                self.assertIsNotNone(operation)
                submit.assert_called_once()
            row = self.db.execute(
                "SELECT status,report_published,review_skipped,report_md,report_detail,grader "
                "FROM submissions WHERE id=30").fetchone()
            self.assertEqual(tuple(row), ("待批改", 0, 0, "", "", ""))
        response = self.client(4).get(self.url("student_report", sid=30))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"Old report", response.data)
        active_page = self.client(2).get(self.url("lecture_detail", hid=20)).get_data(as_text=True)
        self.assertNotIn("打开评阅报告", active_page)
        self.assertNotIn('class="skip-review-form"', active_page)

        # 新批阅成功且新题目已保存后，才重新出现跳过复核入口。
        with a.app.app_context():
            a._save_grading_result(a.get_db(), 30, {
                "questions": [{"no": "1", "verdict": "对", "comment": "新批阅"}],
                "summary": {"accuracy": "100%"},
            })
            self.db.execute("UPDATE ai_operations SET status='success' WHERE id=?",
                            (operation["id"],))
            self.db.commit()
        success_page = self.client(2).get(self.url("lecture_detail", hid=20)).get_data(as_text=True)
        self.assertNotIn("打开评阅报告", success_page)
        self.assertIn('class="skip-review-form"', success_page)

    def test_import_does_not_create_database_or_start_workers(self):
        root = self.root / "import-only"
        env = dict(os.environ, DATA_DIR=str(root), SECRET_KEY="fixture", AUTO_BACKUP="1", AUTO_GRADE="1", AUTO_MODEL_SYNC="1")
        code = "import app, threading; print([t.name for t in threading.enumerate() if t.name != 'MainThread'])"
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")
        self.assertFalse((root / "data.sqlite").exists())

    def test_schema_migration_does_not_fail_an_active_task(self):
        self.db.execute("INSERT INTO ai_operations(teacher_id,operation_type,status,started_at) VALUES(2,'grade','running',CURRENT_TIMESTAMP)")
        self.db.commit()
        a.database.init_db()
        self.assertEqual(self.db.execute("SELECT status FROM ai_operations").fetchone()[0], "running")
