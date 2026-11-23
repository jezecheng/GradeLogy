# -*- coding: utf-8 -*-
"""知识点标签与分析路由的集成测试（隔离数据目录 + mock AI）。"""
import json
import sqlite3
import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

try:
    import _env_bootstrap  # noqa: F401  确保 DATA_DIR 在 app 导入前就绪
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as app_module  # noqa: E402
from tests import patch_sync_queue  # noqa: E402

_TABLES = ("student_question_reviews", "student_weak_points", "tag_analyses",
           "question_tags", "questions",
           "submissions", "lectures", "exams", "exam_scores", "enrollments",
           "course_teachers", "course_knowledge_graphs", "kg_edges", "kg_nodes",
           "kg_category_maps", "knowledge_graphs",
           "courses", "users", "ai_usage_events", "ai_operations")


class TagRouteTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        patch_sync_queue(self)
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
        con.execute("DELETE FROM prompts WHERE is_default=0")
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
        cands = [{"key": "q0", "no": "3", "max": 5, "hint": "配平",
                  "stem": "在反应方程式中配平并说明电子转移数。"},
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

    def test_scope_save_does_not_call_ai_or_replace_existing_tags(self):
        with mock.patch.object(app_module, "_generate_question_tags") as tagging:
            # 保存相同范围应保持已有标签；改变范围则需确认并使旧标签失效。
            resp = self._post("lecture_scope_save", {"no": ["q0"]}, hid=20)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._tags(), [("3", "氧化还原反应")])
        con = self._db()
        selected = json.loads(con.execute(
            "SELECT scope_selected FROM lectures WHERE id=20").fetchone()[0])
        con.close()
        self.assertEqual(selected, ["q0"])
        tagging.assert_not_called()

    def test_scope_save_keeps_previous_tags_without_implicit_ai(self):
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "analyze_question_tags",
                                  return_value=(None, "AI 标注失败：超时")) as ai:
            resp = self._post("lecture_scope_save", {"no": ["q0"]}, hid=20)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        selected = json.loads(con.execute(
            "SELECT scope_selected FROM lectures WHERE id=20").fetchone()[0])
        con.close()
        self.assertEqual(selected, ["q0"])
        # 保存范围本身不触发 AI，已有标签保持不变。
        self.assertEqual(self._tags(), [("3", "氧化还原反应")])
        ai.assert_not_called()

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
        con = self._db()
        signature = con.execute(
            "SELECT tags_generated_scope FROM lectures WHERE id=20").fetchone()[0]
        con.close()
        self.assertEqual(signature, app_module.tag_scope_signature(["q0"]))

    def test_empty_tag_result_still_marks_current_scope_generation_complete(self):
        ai_rows = [{"key": "q0", "no": "3", "category": "", "tags": []}]
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "analyze_question_tags",
                                  return_value=(ai_rows, "")):
            response = self._post("lecture_tags_generate", hid=20)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._tags(), [])
        con = self._db()
        signature = con.execute(
            "SELECT tags_generated_scope FROM lectures WHERE id=20").fetchone()[0]
        con.close()
        self.assertEqual(signature, app_module.tag_scope_signature(["q0"]))

    def test_tag_routes_reject_other_teacher(self):
        other = self.login(3)
        self.assertEqual(
            self._post("lecture_tags_generate", client=other, hid=20).status_code, 403)
        self.assertEqual(
            self._post("lecture_tag_analysis", client=other, hid=20).status_code, 403)
        self.assertEqual(
            self._post("student_tag_analysis", client=other, cid=10, sid=4).status_code,
            403)

    def test_question_reviewed_saves_verdict_and_fields(self):
        """点「已复核」应同时保存判定/OCR/标准答案/评语，而不只是 reviewed 标记。"""
        resp = self.client.post(
            self.url("question_reviewed", qid=40),
            json={"reviewed": 1, "verdict": "半对",
                  "ocr_text": "学生作答修正", "standard_answer": "标准答案修正",
                  "note": "部分正确"},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["reviewed"], 1)
        self.assertEqual(data["verdict"], "半对")
        con = self._db()
        row = con.execute(
            "SELECT verdict,reviewed,ocr_text,standard_answer,note "
            "FROM questions WHERE id=40").fetchone()
        con.close()
        self.assertEqual(row["verdict"], "半对")
        self.assertEqual(row["reviewed"], 1)
        self.assertEqual(row["ocr_text"], "学生作答修正")
        self.assertEqual(row["standard_answer"], "标准答案修正")
        self.assertEqual(row["note"], "部分正确")

    def test_question_reviewed_rejects_other_teacher(self):
        other = self.login(3)
        resp = other.post(
            self.url("question_reviewed", qid=40),
            json={"reviewed": 1, "verdict": "对"},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        row = con.execute("SELECT verdict FROM questions WHERE id=40").fetchone()
        con.close()
        self.assertEqual(row["verdict"], "错")  # 未被越权修改

    def test_question_reviewed_rejects_invalid_verdict(self):
        resp = self.client.post(
            self.url("question_reviewed", qid=40),
            json={"reviewed": 1, "verdict": "优良"},
            headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(resp.status_code, 200)
        con = self._db()
        row = con.execute("SELECT verdict FROM questions WHERE id=40").fetchone()
        con.close()
        self.assertEqual(row["verdict"], "")  # 非法值被归一化为空

    def test_horizontal_analysis_saves_markdown(self):
        con = self._db()
        con.execute(
            "INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,kind,content_md) "
            "VALUES(2,10,20,'horizontal','旧分析')")
        con.commit()
        con.close()
        result = {"overall_summary": "整体判断",
                  "high_risk_topics": [{"topic_name": "氧化还原反应",
                                        "error_rate": "100%",
                                        "common_pitfall": "配平错误多"}],
                  "teaching_suggestions": ["专项练习配平"]}
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_horizontal_analysis",
                                  return_value=result) as ai:
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        ai.assert_called_once()
        self.assertIn("化学", ai.call_args[0][0]["subject_and_course"])
        con = self._db()
        row = con.execute(
            "SELECT * FROM tag_analyses WHERE kind='horizontal' AND lecture_id=20"
        ).fetchone()
        count = con.execute(
            "SELECT COUNT(*) FROM tag_analyses WHERE kind='horizontal' AND lecture_id=20"
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)
        self.assertIsNotNone(row)
        self.assertIn("整体判断", row["content_md"])
        self.assertIn("配平错误多", row["content_md"])
        self.assertIn("专项练习配平", row["content_md"])
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

    def test_lecture_class_tag_chips_explain_attention_ratio(self):
        page = self.client.get(self.url("lecture_detail", hid=20))
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("需关注判定数 / 已判定记录数", html)
        self.assertIn("分子是错 + 半对", html)
        self.assertIn("0/1</b> 不代表一定全对", html)

        con = self._db()
        con.execute("DELETE FROM question_tags WHERE lecture_id=20")
        con.commit()
        con.close()
        without_tags = self.client.get(self.url("lecture_detail", hid=20))
        self.assertIn("单独点击「生成/重新生成知识点标签」",
                      without_tags.get_data(as_text=True))

    def test_horizontal_analysis_keeps_previous_on_ai_failure(self):
        con = self._db()
        con.execute(
            "INSERT INTO tag_analyses(teacher_id,course_id,lecture_id,kind,content_md) "
            "VALUES(2,10,20,'horizontal','旧分析')")
        con.commit()
        con.close()
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_horizontal_analysis", return_value={}):
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        row = con.execute(
            "SELECT content_md FROM tag_analyses WHERE kind='horizontal' AND lecture_id=20"
        ).fetchone()
        count = con.execute("SELECT COUNT(*) FROM tag_analyses WHERE kind='horizontal'").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)
        self.assertEqual(row["content_md"], "旧分析")

    def test_vertical_analysis_saves_markdown(self):
        con = self._db()
        con.execute(
            "INSERT INTO tag_analyses(teacher_id,course_id,student_id,kind,content_md) "
            "VALUES(2,10,4,'vertical','旧报告')")
        con.commit()
        con.close()
        result = {"progress_highlights": ["计算稳定"],
                  "root_cause_diagnosis": [{"weakness_chain": "氧化还原 -> 电化学",
                                            "analysis": "电子守恒未掌握"}],
                  "habit_alerts": ["漏写单位"],
                  "action_plan": ["复习电子守恒"]}
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_vertical_analysis",
                                  return_value=result) as ai:
            resp = self._post("student_tag_analysis", cid=10, sid=4)
        self.assertEqual(resp.status_code, 302)
        ai.assert_called_once()
        context = ai.call_args[0][0]
        self.assertEqual(context["student_name"], "学生")
        self.assertIn("本课程未挂载知识图谱", context["kg_weakness_chains"])
        con = self._db()
        row = con.execute(
            "SELECT * FROM tag_analyses WHERE kind='vertical' AND student_id=4"
        ).fetchone()
        count = con.execute(
            "SELECT COUNT(*) FROM tag_analyses WHERE kind='vertical' AND student_id=4").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)
        self.assertIsNotNone(row)
        self.assertIn("计算稳定", row["content_md"])
        self.assertIn("电子守恒未掌握", row["content_md"])
        self.assertIn("漏写单位", row["content_md"])
        self.assertIn("复习电子守恒", row["content_md"])
        self.assertIn("mastery", row["stats_json"])

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

    def test_vertical_analysis_shows_queued_and_disables_duplicate_submit(self):
        con = self._db()
        cur = con.execute(
            "INSERT INTO ai_operations(teacher_id,operation_type,course_id,resource_key,"
            "model,status,started_at) VALUES(2,'tag_vertical',10,'student:4','mock','queued',"
            "CURRENT_TIMESTAMP)")
        operation_id = cur.lastrowid
        con.commit()
        con.close()
        page = self.client.get(self.url("student_detail", cid=10, sid=4))
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('disabled aria-busy="true"', html)
        self.assertIn("分析中…", html)
        self.assertIn(f'data-ai-operation-id="{operation_id}"', html)
        status = self.client.get(self.url("ai_operation_status", operation_id=operation_id))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["status"], "queued")

    def test_prompts_page_lists_all_kinds_and_placeholders(self):
        resp = self.client.get(self.url("prompts_page"))
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for text in ("横向分析提示词", "纵向分析提示词",
                     "默认横向知识点分析提示词", "默认纵向知识点分析提示词",
                     "{aggregated_question_stats}", "{module_mastery_stats}"):
            self.assertIn(text, html)

    def test_course_custom_horizontal_prompt_is_passed_to_ai(self):
        con = self._db()
        con.execute(
            "INSERT INTO prompts(teacher_id,kind,name,content) VALUES(2,?,?,?)",
            ("tag_horizontal", "自定义横向", "自定义横向口径"))
        pid = con.execute("SELECT id FROM prompts WHERE name='自定义横向'").fetchone()[0]
        con.execute("UPDATE courses SET tag_horizontal_prompt_id=? WHERE id=10", (pid,))
        con.commit()
        con.close()
        result = {"overall_summary": "s", "high_risk_topics": [],
                  "teaching_suggestions": []}
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("http://api", "key", "model", "")), \
                mock.patch.object(app_module, "ai_horizontal_analysis",
                                  return_value=result) as ai:
            resp = self._post("lecture_tag_analysis", hid=20)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ai.call_args.kwargs["prompt"], "自定义横向口径")

    def test_course_prompts_save_stores_tag_prompts(self):
        con = self._db()
        con.executemany(
            "INSERT INTO prompts(teacher_id,kind,name,content) VALUES(2,?,?,?)",
            [("tag_horizontal", "横向A", "a"), ("tag_vertical", "纵向A", "b")])
        hpid = con.execute("SELECT id FROM prompts WHERE name='横向A'").fetchone()[0]
        vpid = con.execute("SELECT id FROM prompts WHERE name='纵向A'").fetchone()[0]
        con.commit()
        con.close()
        resp = self._post("course_prompts_save", {
            "grade_prompt_id": "0", "report_prompt_id": "0",
            "tag_horizontal_prompt_id": str(hpid),
            "tag_vertical_prompt_id": str(vpid)}, cid=10)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        row = con.execute("SELECT tag_horizontal_prompt_id, tag_vertical_prompt_id "
                          "FROM courses WHERE id=10").fetchone()
        con.close()
        self.assertEqual(row["tag_horizontal_prompt_id"], hpid)
        self.assertEqual(row["tag_vertical_prompt_id"], vpid)

    def test_course_prompts_save_rejects_other_teachers_prompt(self):
        con = self._db()
        con.execute(
            "INSERT INTO prompts(teacher_id,kind,name,content) VALUES(3,?,?,?)",
            ("tag_vertical", "别人的纵向", "x"))
        pid = con.execute("SELECT id FROM prompts WHERE name='别人的纵向'").fetchone()[0]
        con.commit()
        con.close()
        resp = self._post("course_prompts_save", {
            "tag_vertical_prompt_id": str(pid)}, cid=10)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        stored = con.execute(
            "SELECT tag_vertical_prompt_id FROM courses WHERE id=10").fetchone()[0]
        con.close()
        self.assertEqual(stored, 0)

    def _assign_graphs(self, graph_ids, client=None, cid=10):
        payload = MultiDict([("_csrf", "test-token")])
        for gid in graph_ids:
            payload.add("graph_ids", str(gid))
        return (client or self.client).post(
            self.url("course_knowledge_graphs_save", cid=cid), data=payload)

    def _seed_graphs(self):
        con = self._db()
        con.executemany(
            "INSERT INTO knowledge_graphs(id,subject,name,created_by) VALUES(?,?,?,2)",
            [(31, "化学", "高中化学主图谱"), (32, "化学", "化学实验图谱")])
        con.commit()
        con.close()

    def test_course_can_assign_multiple_graphs(self):
        self._seed_graphs()
        resp = self._assign_graphs([31, 32])
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        rows = {r["graph_id"] for r in con.execute(
            "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=10")}
        con.close()
        self.assertEqual(rows, {31, 32})
        html = self.client.get(self.url("course_detail", cid=10)).get_data(as_text=True)
        self.assertIn("高中化学主图谱", html)
        self.assertIn("保存关联", html)

        self._assign_graphs([32])
        con = self._db()
        rows = {r["graph_id"] for r in con.execute(
            "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=10")}
        con.close()
        self.assertEqual(rows, {32})

    def test_course_graph_assignment_ignores_unknown_ids(self):
        self._seed_graphs()
        self._assign_graphs([9999, "abc", 31])
        con = self._db()
        rows = {r["graph_id"] for r in con.execute(
            "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=10")}
        con.close()
        self.assertEqual(rows, {31})

    def test_other_teacher_cannot_assign_graphs_to_course_they_do_not_own(self):
        self._seed_graphs()
        other = self.login(3)
        resp = self._assign_graphs([31], client=other)
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM course_knowledge_graphs").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_all_teachers_can_view_and_assign_system_graphs_to_their_courses(self):
        self._seed_graphs()
        other = self.login(3)

        listing = other.get(self.url("knowledge_graphs"))
        detail = other.get(self.url("knowledge_graph_detail", gid=31))
        self.assertEqual(listing.status_code, 200)
        self.assertIn("高中化学主图谱", listing.get_data(as_text=True))
        self.assertEqual(detail.status_code, 200)
        self.assertIn("只读", detail.get_data(as_text=True))

        resp = self._assign_graphs([31], client=other, cid=11)
        self.assertEqual(resp.status_code, 302)
        course_page = other.get(self.url("course_detail", cid=11))
        self.assertEqual(course_page.status_code, 200)
        self.assertIn("高中化学主图谱", course_page.get_data(as_text=True))
        con = self._db()
        assigned = con.execute(
            "SELECT graph_id FROM course_knowledge_graphs WHERE course_id=11").fetchall()
        con.close()
        self.assertEqual({row[0] for row in assigned}, {31})

    def _seed_weak_points(self):
        """两条标签命中同一 KG 节点，需合并成一张卡；另一条未匹配节点单独成卡。"""
        con = self._db()
        con.executemany(
            "INSERT INTO student_weak_points(student_id,course_id,graph_id,node_id,"
            "tag,node_name,definition,status,wrong_count,partial_count,right_count,"
            "total_count,last_seen_at,student_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(4, 10, 5, 100, "氧化还原反应", "氧化还原反应", "电子转移", "weak",
              1, 0, 0, 1, "2026-01-01 10:00:00", ""),
             (4, 10, 5, 100, "电子守恒", "氧化还原反应", "电子转移", "reviewed",
              0, 0, 0, 0, "2026-01-02 10:00:00", "记错电子数"),
             (4, 10, 0, 0, "未匹配标签", "", "", "weak",
              0, 0, 0, 0, "2026-01-03 10:00:00", "")])
        con.commit()
        con.close()

    def test_weak_points_group_by_kg_node_and_use_history(self):
        self._seed_weak_points()
        html = self.login(4).get(
            self.url("student_weak_points")).get_data(as_text=True)
        self.assertEqual(html.count('class="weak-point-card"'), 2)
        self.assertIn("合并标签：氧化还原反应、电子守恒", html)
        self.assertIn("✗错 1", html)
        self.assertEqual(html.count("（共 1 题 · 错误率 100%）"), 1)
        self.assertIn("全部 2", html)
        self.assertIn("待复习 2", html)
        self.assertIn("未匹配标签", html)

    def test_weak_point_status_updates_all_rows_in_group(self):
        self._seed_weak_points()
        student = self.login(4)
        resp = self._post("student_weak_point_status",
                          {"course_id": "10", "group": "n:5:100",
                           "status": "mastered", "note": "已弄懂"},
                          client=student)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        rows = con.execute(
            "SELECT status,student_note FROM student_weak_points "
            "WHERE student_id=4 AND graph_id=5 AND node_id=100").fetchall()
        con.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"mastered"})
        self.assertEqual({r["student_note"] for r in rows}, {"已弄懂"})

    def test_weak_point_status_rejects_other_roles(self):
        self._seed_weak_points()
        resp = self._post("student_weak_point_status",
                          {"course_id": "10", "group": "n:5:100",
                           "status": "mastered"})
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        status = con.execute(
            "SELECT status FROM student_weak_points WHERE tag='氧化还原反应'"
        ).fetchone()[0]
        con.close()
        self.assertEqual(status, "weak")

    def _review_save(self):
        con = self._db()
        con.execute("UPDATE submissions SET report_published=1,report_md='published' WHERE id=30")
        con.commit()
        con.close()
        self.client.post(self.url("question_reviewed", qid=40),
                         json={"reviewed": True, "verdict": "错"},
                         headers={"X-CSRF-Token": "test-token"})
        return self._post("submission_review_save", {
            "verdict_40": "错", "ocr_40": "第3题作答", "standard_40": "标准答案",
            "note_40": "", "overall": "继续加油"}, sid=30)

    def test_regrading_same_submission_keeps_one_weak_point_per_tag(self):
        self.assertEqual(self._review_save().status_code, 302)
        self._review_save()
        con = self._db()
        rows = con.execute(
            "SELECT COUNT(*) c, SUM(wrong_count) w, SUM(total_count) t "
            "FROM student_weak_points WHERE student_id=4 AND tag='氧化还原反应'"
        ).fetchone()
        con.close()
        self.assertEqual((rows["c"], rows["w"], rows["t"]), (1, 1, 1))

    def test_regrading_keeps_student_reviewed_status(self):
        self._review_save()
        student = self.login(4)
        self._post("student_weak_point_status",
                   {"course_id": "10", "group": "t:氧化还原反应",
                    "status": "reviewed", "note": "复习过"}, client=student)
        self._review_save()
        con = self._db()
        row = con.execute(
            "SELECT status, student_note, wrong_count FROM student_weak_points "
            "WHERE student_id=4 AND tag='氧化还原反应'").fetchone()
        count = con.execute(
            "SELECT COUNT(*) FROM student_weak_points "
            "WHERE student_id=4 AND tag='氧化还原反应'").fetchone()[0]
        con.close()
        self.assertEqual(count, 1)
        self.assertEqual((row["status"], row["student_note"], row["wrong_count"]),
                         ("reviewed", "复习过", 1))

    def test_weak_point_remove_persists_until_new_lecture_mistake(self):
        self._review_save()
        student = self.login(4)
        self._post("student_weak_point_remove",
                   {"course_id": "10", "group": "t:氧化还原反应"}, client=student)
        con = self._db()
        status = con.execute(
            "SELECT status FROM student_weak_points "
            "WHERE student_id=4 AND tag='氧化还原反应'").fetchone()[0]
        con.close()
        self.assertEqual(status, "removed")
        html = student.get(self.url("student_weak_points")).get_data(as_text=True)
        self.assertIn("还没有薄弱点记录", html)

        # 同一讲反复重批不会恢复
        self._review_save()
        con = self._db()
        status = con.execute(
            "SELECT status FROM student_weak_points "
            "WHERE student_id=4 AND tag='氧化还原反应'").fetchone()[0]
        con.close()
        self.assertEqual(status, "removed")

        # 新讲次再次做错 → 自动回退为薄弱
        con = self._db()
        con.execute("INSERT INTO lectures(id,course_id,title) VALUES(21,10,'第二讲')")
        con.execute(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_published,report_md)"
            " VALUES(31,21,4,'b.pdf','待批改',1,'published')")
        con.execute(
            "INSERT INTO questions(id,submission_id,no,verdict,reviewed) VALUES(50,31,'1','错',1)")
        con.execute(
            "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
            " VALUES(21,'q0','1','氧化还原反应','ai')")
        con.commit()
        con.close()
        self._post("submission_review_save",
                   {"verdict_50": "错", "overall": "继续加油"}, sid=31)
        con = self._db()
        row = con.execute(
            "SELECT status, wrong_count FROM student_weak_points "
            "WHERE student_id=4 AND tag='氧化还原反应'").fetchone()
        con.close()
        # 计数字段与展示口径一致：第一讲错 1 次 + 第二讲错 1 次 = 2
        self.assertEqual((row["status"], row["wrong_count"]), ("weak", 2))

    def test_weak_point_galaxy_lights_weak_nodes_with_neighbors(self):
        con = self._db()
        con.execute("UPDATE submissions SET report_published=1 WHERE id=30")
        con.execute("INSERT INTO knowledge_graphs(id,subject,name,created_by)"
                    " VALUES(31,'化学','主图谱',2)")
        con.executemany(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized,definition,category)"
            " VALUES(?,31,?,?,?,?)",
            [(600, "物质的量", "物质的量", "表示微粒集合体的物理量", "基本概念"),
             (601, "氧化还原反应", "氧化还原反应", "有电子转移的反应", "化学反应"),
             (602, "电化学", "电化学", "电能与化学能的转化", "化学反应")])
        con.executemany(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation) VALUES(31,?,?,?)",
            [(600, 601, "prerequisite"), (601, 602, "prerequisite")])
        con.executemany(
            "INSERT INTO student_weak_points(student_id,course_id,graph_id,node_id,tag,"
            "node_name,definition,status,wrong_count,partial_count,right_count,"
            "total_count) VALUES(?,?,?,?,?,?,?,?,0,0,0,0)",
            [(4, 10, 31, 601, "氧化还原反应", "氧化还原反应", "有电子转移的反应", "weak"),
             (4, 10, 0, 0, "未匹配标签", "", "", "weak"),
             (4, 10, 5, 100, "已删除节点", "", "", "weak")])
        con.commit()
        con.close()
        resp = self.login(4).get(self.url("student_weak_points_galaxy"))
        self.assertEqual(resp.status_code, 200)
        courses = resp.get_json()["courses"]
        self.assertEqual(len(courses), 1)
        course = courses[0]
        self.assertEqual(course["name"], "化学")
        self.assertEqual(course["weak_total"], 3)
        self.assertEqual(course["unmatched"], 2)
        nodes = {n["name"]: n for n in course["nodes"]}
        self.assertEqual(set(nodes), {"物质的量", "氧化还原反应", "电化学"})
        self.assertTrue(nodes["氧化还原反应"]["weak"])
        self.assertFalse(nodes["物质的量"]["weak"])
        self.assertEqual(nodes["氧化还原反应"]["wrong_count"], 1)
        self.assertEqual(nodes["氧化还原反应"]["key"], "n:31:601")
        self.assertEqual(len(course["edges"]), 2)

    def test_weak_point_galaxy_requires_student(self):
        resp = self.client.get(self.url("student_weak_points_galaxy"))
        self.assertEqual(resp.status_code, 403)

    def _reveal_submission(self):
        """把提交改成已批改，并补一道半对题与一道对题。"""
        con = self._db()
        con.execute("UPDATE submissions SET status='已批改',report_published=1 WHERE id=30")
        con.execute(
            "INSERT INTO questions(id,submission_id,no,verdict,ocr_text,ai_comment,"
            "standard_answer,note) VALUES(41,30,'4','半对','第4题作答','条件写错',"
            "'标准答案','')")
        con.execute(
            "INSERT INTO questions(id,submission_id,no,verdict) VALUES(42,30,'5','对')")
        con.execute(
            "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
            " VALUES(20,'q1','4','化学平衡','ai')")
        con.commit()
        con.close()

    def test_wrong_questions_lists_revealed_wrong_and_partial(self):
        self._reveal_submission()
        html = self.login(4).get(
            self.url("student_wrong_questions")).get_data(as_text=True)
        self.assertIn("第 3 题", html)
        self.assertIn("第 4 题", html)
        self.assertNotIn("第 5 题", html)
        self.assertIn("未配平", html)
        self.assertIn("条件写错", html)
        self.assertIn("标准答案", html)
        self.assertIn("全部 2", html)
        self.assertIn("错 1", html)
        self.assertIn("半对 1", html)
        self.assertIn("未复习 2", html)

    def test_wrong_question_link_jumps_to_visible_question_stem(self):
        self._reveal_submission()
        student = self.login(4)
        html = student.get(self.url("student_wrong_questions")).get_data(as_text=True)
        self.assertIn(f"{self.url('student_lecture_page', hid=20)}#question-40", html)
        lecture = student.get(self.url("student_lecture_page", hid=20)).get_data(as_text=True)
        self.assertIn('id="question-40"', lecture)
        self.assertIn("题干", lecture)
        self.assertIn("在反应方程式中配平并说明电子转移数。", lecture)

    def test_wrong_questions_hides_unpublished_results(self):
        html = self.login(4).get(
            self.url("student_wrong_questions")).get_data(as_text=True)
        self.assertIn("还没有错题", html)

    def test_wrong_questions_filter_by_tag(self):
        self._reveal_submission()
        html = self.login(4).get(self.url(
            "student_wrong_questions", tags="化学平衡")).get_data(as_text=True)
        self.assertIn("第 4 题", html)
        self.assertNotIn("第 3 题", html)
        self.assertIn("全部 1", html)

    def test_wrong_question_review_mark_and_cancel(self):
        self._reveal_submission()
        student = self.login(4)
        resp = self._post("student_question_review",
                          {"question_id": "40", "status": "reviewed"},
                          client=student)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        row = con.execute("SELECT status FROM student_question_reviews "
                          "WHERE student_id=4 AND question_id=40").fetchone()
        con.close()
        self.assertEqual(row["status"], "reviewed")
        html = student.get(
            self.url("student_wrong_questions")).get_data(as_text=True)
        self.assertIn("已复习 1", html)
        self._post("student_question_review",
                   {"question_id": "40", "status": "open"}, client=student)
        con = self._db()
        row = con.execute("SELECT status FROM student_question_reviews "
                          "WHERE student_id=4 AND question_id=40").fetchone()
        con.close()
        self.assertEqual(row["status"], "open")

    def test_wrong_question_review_rejects_other_roles(self):
        self._reveal_submission()
        resp = self._post("student_question_review",
                          {"question_id": "40", "status": "reviewed"})
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM student_question_reviews").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
