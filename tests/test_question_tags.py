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

    def test_lecture_question_stats_aggregates_by_no(self):
        stats = tags.lecture_question_stats(self.con, 20)
        third = next(s for s in stats if s["no"] == "3")
        self.assertEqual(third["total"], 2)
        self.assertEqual(third["wrong"], 1)
        self.assertEqual(third["partial"], 1)
        self.assertEqual(third["error_rate"], 100.0)
        self.assertIn("氧化还原反应", third["tags"])
        self.assertIn("未配平", third["comments"])
        # 9.1 应能匹配到题号 9 的标签
        sub = next(s for s in stats if s["no"] == "9.1")
        self.assertEqual(sub["error_rate"], 0.0)
        self.assertIn("化学平衡", sub["tags"])

    def test_lecture_question_stats_empty(self):
        self.assertEqual(tags.lecture_question_stats(self.con, 999), [])

    def test_student_mastery_stats_includes_all_verdicts(self):
        stats = tags.student_mastery_stats(self.con, 10, 2)
        redox = next(s for s in stats if s["tag"] == "氧化还原反应")
        self.assertEqual(redox["n_total"], 2)
        self.assertEqual(redox["n_wrong"], 2)
        self.assertEqual(redox["mastery"], 0.0)
        self.assertEqual(redox["n_lectures"], 2)
        self.assertEqual([e["lecture"] for e in redox["episodes"]],
                         ["第一讲", "第二讲"])

    def test_student_mastery_stats_empty(self):
        self.assertEqual(tags.student_mastery_stats(self.con, 999, 2), [])

    def test_student_trend_lists_each_lecture(self):
        trend = tags.student_trend(self.con, 10, 2)
        self.assertEqual([t["lecture"] for t in trend], ["第一讲", "第二讲"])
        self.assertEqual(trend[0]["error_rate"], 50.0)
        self.assertEqual(trend[1]["error_rate"], 100.0)

    def test_habit_stats_counts_keywords(self):
        self.con.execute("INSERT INTO questions(submission_id,no,verdict,ai_comment)"
                         " VALUES(32,'4','错','未作答，单位写错')")
        habits = {h["type"]: h["count"]
                  for h in tags.habit_stats(self.con, 10, 2)}
        self.assertEqual(habits.get("未作答/空白"), 1)
        self.assertEqual(habits.get("单位/有效数字"), 1)
        self.assertNotIn("计算/粗心", habits)

    def test_render_helpers_include_tag_evidence(self):
        horizontal = tags.render_horizontal_md(
            {"overall_summary": "整体偏弱",
             "high_risk_topics": [{"topic_name": "氧化还原反应",
                                   "error_rate": "100%", "common_pitfall": "配平不熟"}],
             "teaching_suggestions": ["专项训练"]},
            tags.lecture_question_stats(self.con, 20))
        self.assertIn("整体偏弱", horizontal)
        self.assertIn("配平不熟", horizontal)
        self.assertIn("错误率 100%", horizontal)
        self.assertIn("专项训练", horizontal)
        vertical = tags.render_vertical_md(
            {"progress_highlights": ["计算较稳"],
             "root_cause_diagnosis": [{"weakness_chain": "氧化还原 -> 电化学",
                                       "analysis": "电子守恒没掌握"}],
             "habit_alerts": ["漏写单位"],
             "action_plan": ["复习电子守恒"]},
            tags.student_mastery_stats(self.con, 10, 2))
        self.assertIn("计算较稳", vertical)
        self.assertIn("电子守恒没掌握", vertical)
        self.assertIn("漏写单位", vertical)
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
        horizontal = tags.render_horizontal_md(
            {"overall_summary": "", "high_risk_topics": [], "teaching_suggestions": []})
        self.assertIn("本次数据还不足以归纳高风险知识点", horizontal)
        vertical = tags.render_vertical_md(
            {"progress_highlights": [], "root_cause_diagnosis": [],
             "habit_alerts": [], "action_plan": []})
        self.assertIn("暂未发现明确的知识断层", vertical)
        self.assertIn("暂未发现明显进步点", vertical)
        self.assertNotIn("作答习惯提醒", vertical)


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

    def test_horizontal_analysis_parses_topics(self):
        reply = json.dumps({
            "overall_summary": "整体判断",
            "high_risk_topics": [{"topic_name": "氧化还原反应",
                                  "error_rate": "62.5%",
                                  "common_pitfall": "配平错误多"}],
            "teaching_suggestions": ["专项练习", "重讲电子守恒", "补前置概念", "第四条应被截断"]},
            ensure_ascii=False)
        context = {
            "subject_and_course": "高一化学 · 必修一",
            "lecture_name": "第一讲",
            "total_students": 32,
            "aggregated_question_stats": [
                {"no": "3", "tags": ["氧化还原反应"], "error_rate": 62.5}],
        }
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=(reply, "stop", "")):
            result = pdfgrade.ai_horizontal_analysis(context, "u", "k", "m")
        self.assertEqual(result["overall_summary"], "整体判断")
        self.assertEqual(result["high_risk_topics"][0]["topic_name"], "氧化还原反应")
        self.assertEqual(len(result["teaching_suggestions"]), 3)

    def test_vertical_analysis_parses_diagnosis(self):
        reply = json.dumps({
            "progress_highlights": ["计算较稳定"],
            "root_cause_diagnosis": [{"weakness_chain": "氧化还原 -> 电化学",
                                      "analysis": "电子守恒未掌握"}],
            "habit_alerts": ["漏写单位"],
            "action_plan": ["先补氧化还原", "再练综合题", "第三条", "第四条应被截断"]},
            ensure_ascii=False)
        context = {"student_name": "李一", "subject_and_course": "化学",
                   "lecture_count": 2, "module_mastery_stats": []}
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=(reply, "stop", "")):
            result = pdfgrade.ai_vertical_analysis(context, "u", "k", "m")
        self.assertEqual(result["progress_highlights"], ["计算较稳定"])
        self.assertEqual(result["root_cause_diagnosis"][0]["weakness_chain"],
                         "氧化还原 -> 电化学")
        self.assertEqual(result["habit_alerts"], ["漏写单位"])
        self.assertEqual(len(result["action_plan"]), 3)

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

    def test_horizontal_analysis_uses_custom_prompt_and_injects_data(self):
        captured = {}

        def fake_chat(*args, **kwargs):
            captured["content"] = args[3]
            return (json.dumps({"overall_summary": "s", "high_risk_topics": [],
                                "teaching_suggestions": []}), "stop", "")

        context = {
            "subject_and_course": "高一化学",
            "lecture_name": "第一讲",
            "total_students": 30,
            "aggregated_question_stats": [{"no": "3", "tags": ["有数据标签"],
                                           "error_rate": 80.0}],
        }
        with mock.patch.object(pdfgrade.llm, "chat", side_effect=fake_chat):
            pdfgrade.ai_horizontal_analysis(
                context, "u", "k", "m", prompt="自定义口径：只分析计算题")
        prompt = captured["content"][0]["text"]
        self.assertIn("自定义口径：只分析计算题", prompt)
        self.assertIn("有数据标签", prompt)
        self.assertIn("高一化学", prompt)
        self.assertIn("overall_summary", prompt)   # 系统仍追加 JSON 输出契约

    def test_vertical_analysis_uses_custom_prompt_and_injects_data(self):
        captured = {}

        def fake_chat(*args, **kwargs):
            captured["content"] = args[3]
            return (json.dumps({"progress_highlights": ["好"], "root_cause_diagnosis": [],
                                "habit_alerts": [], "action_plan": []}), "stop", "")

        context = {
            "lecture_count": 3,
            "subject_and_course": "高一化学",
            "student_name": "李一",
            "lecture_range": "第一讲 - 第三讲",
            "module_mastery_stats": [{"tag": "化学平衡", "mastery": 40}],
            "kg_weakness_chains": "系统暂未内置学科知识图谱",
            "error_habit_stats": [{"type": "计算/粗心", "count": 2}],
            "trend_data": [{"lecture": "第一讲", "error_rate": 50.0}],
        }
        with mock.patch.object(pdfgrade.llm, "chat", side_effect=fake_chat):
            pdfgrade.ai_vertical_analysis(
                context, "u", "k", "m", prompt="自定义口径：先补前置再练综合")
        prompt = captured["content"][0]["text"]
        self.assertIn("自定义口径：先补前置再练综合", prompt)
        self.assertIn("李一", prompt)
        self.assertIn("化学平衡", prompt)
        self.assertIn("系统暂未内置学科知识图谱", prompt)
        self.assertIn("action_plan", prompt)

    def test_analysis_functions_return_empty_on_model_error(self):
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=("", "stop", "boom")):
            self.assertEqual(pdfgrade.ai_horizontal_analysis({}, "u", "k", "m"), {})
            self.assertEqual(pdfgrade.ai_vertical_analysis({}, "u", "k", "m"), {})

    def test_analysis_functions_return_empty_on_invalid_json(self):
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=("not json", "stop", "")):
            self.assertEqual(pdfgrade.ai_horizontal_analysis({}, "u", "k", "m"), {})
            self.assertEqual(pdfgrade.ai_vertical_analysis({}, "u", "k", "m"), {})

    def test_analysis_functions_return_empty_when_result_blank(self):
        blank = json.dumps({"overall_summary": "", "high_risk_topics": [],
                            "teaching_suggestions": [], "progress_highlights": [],
                            "root_cause_diagnosis": [], "habit_alerts": [],
                            "action_plan": []})
        with mock.patch.object(pdfgrade.llm, "chat",
                               return_value=(blank, "stop", "")):
            self.assertEqual(pdfgrade.ai_horizontal_analysis({}, "u", "k", "m"), {})
            self.assertEqual(pdfgrade.ai_vertical_analysis({}, "u", "k", "m"), {})


if __name__ == "__main__":
    unittest.main()
