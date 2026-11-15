# -*- coding: utf-8 -*-
"""知识点标签生成、聚合统计与横向/纵向分析的单元测试。"""
import json
import sqlite3
import unittest
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401  确保 DATA_DIR 在 config 导入前就绪
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

from db import SCHEMA
from utilities import pdfgrade, tags


class TagStatsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role) VALUES(?,?,?,?,?)",
            [(1, "t1", "x", "张老师", "teacher"),
             (2, "s1", "x", "李一", "student"),
             (3, "s2", "x", "王二", "student")])
        self.con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(10,'化学',1)")
        self.con.executemany(
            "INSERT INTO lectures(id,course_id,title) VALUES(?,?,?)",
            [(20, 10, "第一讲"), (21, 10, "第二讲")])
        self.con.executemany(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path) VALUES(?,?,?,?)",
            [(30, 20, 2, "a.pdf"), (31, 20, 3, "b.pdf"), (32, 21, 2, "c.pdf")])
        self.con.executemany(
            "INSERT INTO questions(id,submission_id,no,verdict,ai_comment) VALUES(?,?,?,?,?)",
            [(40, 30, "3", "错", "未配平"),
             (41, 30, "9.1", "对", ""),
             (42, 31, "3", "半对", "条件写错"),
             (43, 31, "9", "对", ""),
             (44, 32, "3", "错", "电子转移数错误")])
        # 题号带小问时也要能匹配到同一知识点
        self.con.executemany(
            "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
            " VALUES(?,?,?,?,'ai')",
            [(20, "q0", "3", "氧化还原反应"),
             (20, "q0", "3", "离子方程式"),
             (20, "q1", "9", "化学平衡"),
             (21, "q0", "3", "氧化还原反应")])
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_lecture_stats_aggregates_students_and_verdicts(self):
        stats = tags.lecture_stats(self.con, 20)
        redox = next(s for s in stats if s["tag"] == "氧化还原反应")
        self.assertEqual(redox["n_students"], 2)
        self.assertEqual(redox["n_wrong"], 1)
        self.assertEqual(redox["n_partial"], 1)
        self.assertEqual(redox["n_total"], 2)
        self.assertIn("3", redox["question_nos"])
        self.assertIn("未配平", redox["sample_comments"])

    def test_lecture_tags_by_key_supports_scope_key_and_no(self):
        mapping = tags.lecture_tags_by_key(self.con, 20)
        self.assertIn("q0", mapping)
        self.assertIn("3", mapping)
        self.assertIn("化学平衡", mapping["q1"])

    def test_student_stats_groups_same_tag_across_lectures(self):
        stats = tags.student_stats(self.con, 10, 2)
        redox = next(s for s in stats if s["tag"] == "氧化还原反应")
        self.assertEqual(redox["n_wrong"], 2)
        self.assertEqual(redox["n_lectures"], 2)
        self.assertEqual([e["lecture"] for e in redox["episodes"]],
                         ["第一讲", "第二讲"])

    def test_render_helpers_include_tag_evidence(self):
        horizontal = tags.render_horizontal_md(
            {"summary": "整体偏弱", "points": [{"tag": "氧化还原反应",
                                              "problem": "配平不熟", "advice": "专项训练"}]},
            tags.lecture_stats(self.con, 20))
        self.assertIn("配平不熟", horizontal)
        self.assertIn("涉及 2 人", horizontal)
        vertical = tags.render_vertical_md(
            {"summary": "反复出错", "weak_tags": [{"tag": "氧化还原反应",
                                                  "trend": "两讲都错", "advice": "复习电子守恒"}],
             "strengths": "计算较稳"},
            tags.student_stats(self.con, 10, 2))
        self.assertIn("两讲都错", vertical)
        self.assertIn("复习电子守恒", vertical)

    def test_lecture_stats_empty_when_no_tags(self):
        self.assertEqual(tags.lecture_stats(self.con, 999), [])

    def test_lecture_stats_keeps_ungraded_tag_with_zero_total(self):
        self.con.execute("INSERT INTO lectures(id,course_id,title) VALUES(22,10,'第三讲')")
        self.con.execute("INSERT INTO question_tags(lecture_id,question_key,question_no,tag)"
                         " VALUES(22,'q0','1','物质的量')")
        stats = tags.lecture_stats(self.con, 22)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["n_total"], 0)
        self.assertEqual(stats[0]["n_students"], 0)

    def test_question_number_with_prefix_still_matches(self):
        self.con.execute("INSERT INTO users(id,username,password_hash,display_name,role)"
                         " VALUES(6,'s3','x','赵六','student')")
        self.con.execute("INSERT INTO submissions(id,lecture_id,student_id,file_path)"
                         " VALUES(33,21,6,'d.pdf')")
        self.con.execute("INSERT INTO questions(submission_id,no,verdict)"
                         " VALUES(33,'第3题','错')")
        stats = tags.student_stats(self.con, 10, 6)
        redox = next(s for s in stats if s["tag"] == "氧化还原反应")
        self.assertEqual(redox["n_wrong"], 1)

    def test_lecture_tags_by_key_deduplicates_same_tag(self):
        self.con.execute("INSERT INTO question_tags(lecture_id,question_key,question_no,tag)"
                         " VALUES(20,'q0','3','氧化还原反应')")
        mapping = tags.lecture_tags_by_key(self.con, 20)
        self.assertEqual(mapping["q0"].count("氧化还原反应"), 1)

    def test_student_stats_ignores_tags_without_errors(self):
        self.con.execute("INSERT INTO users(id,username,password_hash,display_name,role)"
                         " VALUES(7,'s4','x','钱七','student')")
        self.con.execute("INSERT INTO submissions(id,lecture_id,student_id,file_path)"
                         " VALUES(34,20,7,'e.pdf')")
        self.con.execute("INSERT INTO questions(submission_id,no,verdict)"
                         " VALUES(34,'3','对')")
        self.assertEqual(tags.student_stats(self.con, 10, 7), [])

    def test_student_stats_empty_when_course_has_no_tags(self):
        self.assertEqual(tags.student_stats(self.con, 999, 2), [])

    def test_render_helpers_handle_empty_analysis(self):
        horizontal = tags.render_horizontal_md({"summary": "", "points": []}, [])
        self.assertIn("本次数据还不足以归纳共性问题", horizontal)
        vertical = tags.render_vertical_md({"summary": "", "weak_tags": []}, [])
        self.assertIn("暂时没有发现跨讲次反复出错的知识点", vertical)
        self.assertNotIn("相对优势", vertical)


class TagAITest(unittest.TestCase):
    def test_analyze_question_tags_normalizes_and_matches_numbers(self):
        reply = json.dumps({"questions": [
            {"no": "3.", "category": "化学计算", "tags": ["氧化还原反应", "电子守恒"]},
            {"no": "9", "tags": ["化学平衡"]},
            {"no": "99", "tags": ["不存在的题"]},
        ]}, ensure_ascii=False)
        with mock.patch.object(pdfgrade, "_file_parts", return_value=([], "")), \
                mock.patch.object(pdfgrade.llm, "chat",
                                  return_value=(reply, "stop", "")):
            rows, err = pdfgrade.analyze_question_tags(
                "/tmp/answer.pdf",
                [{"key": "q0", "no": "3", "hint": "配平"},
                 {"key": "q1", "no": "9", "hint": ""},
                 {"key": "q2", "no": "10", "hint": ""}],
                "http://api", "key", "model")
        self.assertEqual(err, "")
        self.assertEqual([r["key"] for r in rows], ["q0", "q1", "q2"])
        self.assertEqual(rows[0]["tags"], ["氧化还原反应", "电子守恒"])
        self.assertEqual(rows[1]["tags"], ["化学平衡"])
        self.assertEqual(rows[2]["tags"], [])

    def test_analyze_question_tags_reports_no_tags(self):
        with mock.patch.object(pdfgrade, "_file_parts", return_value=([], "")), \
                mock.patch.object(pdfgrade.llm, "chat",
                                  return_value=('{"questions": []}', "stop", "")):
            rows, err = pdfgrade.analyze_question_tags(
                "/tmp/answer.pdf", [{"no": "1"}], "http://api", "key", "model")
        self.assertIsNone(rows)
        self.assertIn("生成知识点标签", err)

    def test_horizontal_analysis_parses_points(self):
        reply = json.dumps({"summary": "整体判断", "points": [
            {"tag": "氧化还原反应", "problem": "配平错误多", "advice": "专项练习"}]},
            ensure_ascii=False)
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=(reply, "stop", "")):
            result = pdfgrade.ai_horizontal_analysis(
                [{"tag": "氧化还原反应", "n_wrong": 3, "question_nos": ["3"]}],
                "http://api", "key", "model")
        self.assertEqual(result["summary"], "整体判断")
        self.assertEqual(result["points"][0]["tag"], "氧化还原反应")

    def test_vertical_analysis_parses_weak_tags(self):
        reply = json.dumps({"summary": "纵向判断", "weak_tags": [
            {"tag": "化学平衡", "trend": "两讲都错", "advice": "回看例题"}],
            "strengths": "计算稳定"}, ensure_ascii=False)
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=(reply, "stop", "")):
            result = pdfgrade.ai_vertical_analysis(
                [{"tag": "化学平衡", "n_wrong": 2, "n_lectures": 2}],
                "李一", "化学", "http://api", "key", "model")
        self.assertEqual(result["weak_tags"][0]["tag"], "化学平衡")
        self.assertEqual(result["strengths"], "计算稳定")

    def test_analyze_question_tags_without_questions(self):
        rows, err = pdfgrade.analyze_question_tags("/tmp/a.pdf", [], "u", "k", "m")
        self.assertIsNone(rows)
        self.assertIn("没有可标注的题目", err)

    def test_analyze_question_tags_requires_answer_file(self):
        rows, err = pdfgrade.analyze_question_tags("", [{"no": "1"}], "u", "k", "m")
        self.assertIsNone(rows)
        self.assertIn("教师版标准答案缺失", err)

    def test_analyze_question_tags_reports_model_error(self):
        with mock.patch.object(pdfgrade, "_file_parts", return_value=([], "")), \
                mock.patch.object(pdfgrade.llm, "chat",
                                  return_value=("", "stop", "timeout")):
            rows, err = pdfgrade.analyze_question_tags(
                "/tmp/a.pdf", [{"no": "1"}], "u", "k", "m")
        self.assertIsNone(rows)
        self.assertIn("AI 标注失败", err)

    def test_analyze_question_tags_reports_truncated_output(self):
        with mock.patch.object(pdfgrade, "_file_parts", return_value=([], "")), \
                mock.patch.object(pdfgrade.llm, "chat",
                                  return_value=("{", "length", "")):
            rows, err = pdfgrade.analyze_question_tags(
                "/tmp/a.pdf", [{"no": "1"}], "u", "k", "m")
        self.assertIsNone(rows)
        self.assertIn("截断", err)

    def test_analyze_question_tags_cleans_tags(self):
        reply = json.dumps({"questions": [{
            "no": "1", "category": "概念",
            "tags": ["重复", "重复", "", "这个标签非常非常非常非常非常长超过二十个字",
                     {"tag": "概念辨析"}, "一", "二", "三", "四"]}]},
            ensure_ascii=False)
        with mock.patch.object(pdfgrade, "_file_parts", return_value=([], "")), \
                mock.patch.object(pdfgrade.llm, "chat",
                                  return_value=(reply, "stop", "")):
            rows, err = pdfgrade.analyze_question_tags(
                "/tmp/a.pdf", [{"no": "1"}], "u", "k", "m")
        self.assertEqual(err, "")
        self.assertEqual(rows[0]["tags"], ["重复", "概念辨析", "一", "二"])
        self.assertEqual(rows[0]["category"], "概念")

    def test_horizontal_analysis_filters_ungraded_tags(self):
        captured = {}

        def fake_chat(*args, **kwargs):
            captured["content"] = args[3]
            return (json.dumps({"summary": "s", "points": []}), "stop", "")

        stats = [
            {"tag": "未批改标签", "n_right": 0, "n_partial": 0,
             "n_wrong": 0, "n_unknown": 0},
            {"tag": "有数据标签", "n_right": 1, "n_partial": 0,
             "n_wrong": 2, "n_unknown": 0},
        ]
        with mock.patch.object(pdfgrade.llm, "chat", side_effect=fake_chat):
            result = pdfgrade.ai_horizontal_analysis(stats, "u", "k", "m")
        self.assertEqual(result["summary"], "s")
        prompt = captured["content"][0]["text"]
        self.assertNotIn("未批改标签", prompt)
        self.assertIn("有数据标签", prompt)

    def test_analysis_functions_return_empty_on_model_error(self):
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=("", "stop", "boom")):
            self.assertEqual(pdfgrade.ai_horizontal_analysis(
                [{"tag": "x", "n_wrong": 1}], "u", "k", "m"), {})
            self.assertEqual(pdfgrade.ai_vertical_analysis(
                [{"tag": "x", "n_wrong": 1}], "李一", "化学", "u", "k", "m"), {})

    def test_analysis_functions_return_empty_on_invalid_json(self):
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=("not json", "stop", "")):
            self.assertEqual(pdfgrade.ai_horizontal_analysis(
                [{"tag": "x", "n_wrong": 1}], "u", "k", "m"), {})
            self.assertEqual(pdfgrade.ai_vertical_analysis(
                [{"tag": "x", "n_wrong": 1}], "李一", "化学", "u", "k", "m"), {})


if __name__ == "__main__":
    unittest.main()
