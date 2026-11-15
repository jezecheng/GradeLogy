# -*- coding: utf-8 -*-
"""管理员 AI 总览、实时日志与课程备份的隔离测试。"""
import atexit
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from unittest import mock

_TEST_ROOT = tempfile.mkdtemp(prefix="gradelogy-admin-tests-")
os.environ["DATA_DIR"] = _TEST_ROOT
os.environ["AUTO_BACKUP"] = "0"
os.environ["SECRET_KEY"] = "admin-test-secret"

import app as app_module  # noqa: E402
from utilities import logview  # noqa: E402

# 多个测试类共用同一套隔离数据目录，进程退出时再统一清理
atexit.register(shutil.rmtree, _TEST_ROOT, ignore_errors=True)


class AdminInsightsTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        for n in range(6):
            name = "app.log" if n == 0 else f"app.log.{n}"
            try:
                os.remove(os.path.join(app_module.cfg.LOG_DIR, name))
            except OSError:
                pass
        con = sqlite3.connect(app_module.DB_PATH)
        for table in ("ai_usage_events", "ai_operations", "course_teachers", "courses", "users"):
            con.execute(f"DELETE FROM {table}")
        users = [
            (1, "admin", "管理员", "admin"),
            (2, "teacher1", "老师一", "teacher"),
            (3, "teacher2", "老师二", "teacher"),
            (4, "student", "学生", "student"),
        ]
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(?,?, 'x',?,?,1,0)", users)
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(10,'数学',2)")
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(20,'物理',3)")
        con.executemany("INSERT INTO course_teachers(course_id,teacher_id) VALUES(?,?)",
                        [(10, 2), (20, 3)])
        con.commit()
        con.close()
        self.client = app_module.app.test_client()

    def login(self, uid=1):
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["uid"] = uid
            sess["_csrf"] = "test-token"
        return self.client

    @staticmethod
    def filters(**updates):
        values = {"period": "30", "teacher_id": "", "course_id": "",
                  "operation_type": "", "model": "", "status": ""}
        values.update(updates)
        return values

    def operation(self, oid, teacher_id, kind, model, status, started, course_id=10):
        con = sqlite3.connect(app_module.DB_PATH)
        finished = None if status == "running" else (
            datetime.fromisoformat(started) + timedelta(seconds=2)).isoformat(sep=" ")
        con.execute(
            "INSERT INTO ai_operations(id,teacher_id,operation_type,course_id,model,status,"
            "started_at,finished_at,error) VALUES(?,?,?,?,?,?,?,?,?)",
            (oid, teacher_id, kind, course_id, model, status, started, finished,
             "模拟失败" if status == "failed" else ""))
        con.commit()
        con.close()

    def event(self, operation_id, attempt, prompt, completion, total, cost, latency,
              model="model", cached=0, provider_request_id=None, status="success", error=""):
        con = sqlite3.connect(app_module.DB_PATH)
        con.execute(
            "INSERT INTO ai_usage_events(operation_id,attempt,provider_request_id,model,"
            "prompt_tokens,completion_tokens,total_tokens,cached_tokens,reasoning_tokens,"
            "latency_ms,estimated_cost,status,error) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?)",
            (operation_id, attempt, provider_request_id, model, prompt, completion, total,
             cached, latency, cost, status, error))
        con.commit()
        con.close()

    def seed_operations(self):
        now = datetime.now().replace(microsecond=0)
        today = now.isoformat(sep=" ")
        self.operation(1, 2, "grade", "model-a", "success", today)
        self.event(1, 1, 100, 20, 120, 0.001, 1000, model="model-a", cached=40,
                   provider_request_id="req-grade-1")
        self.event(1, 2, 200, 30, 230, 0.002, 3000, model="model-a", cached=60,
                   provider_request_id="req-grade-2")
        self.operation(2, 3, "report_comment", "model-b", "failed", today, 20)
        self.event(2, 1, None, None, None, None, None, model="model-b", cached=None,
                   provider_request_id="req-failed-429", status="error", error="HTTP 429")
        self.operation(3, 2, "scope_analyze", "model-a", "running", today)
        old = (now - timedelta(days=40)).isoformat(sep=" ")
        self.operation(4, 2, "lecture_summary", "model-a", "success", old)
        self.event(4, 1, 40, 10, 50, 0.005, 500, model="model-a",
                   provider_request_id="req-old")

    def dashboard(self, **filters):
        with app_module.app.app_context():
            return app_module._ai_dashboard_data(self.filters(**filters))

    def test_dashboard_aggregates_retries_and_nulls(self):
        self.seed_operations()
        summary = self.dashboard()["summary"]
        self.assertEqual(summary["operation_count"], 3)
        self.assertEqual((summary["success_count"], summary["failed_count"],
                          summary["running_count"]), (1, 1, 1))
        self.assertEqual(summary["success_rate"], 50.0)
        self.assertEqual(summary["request_count"], 3)
        self.assertEqual(summary["no_request_count"], 1)
        self.assertEqual(summary["total_tokens"], 350)
        self.assertAlmostEqual(summary["estimated_cost"], 0.003)
        self.assertEqual(summary["unknown_usage_count"], 1)
        self.assertEqual(summary["unknown_cost_count"], 1)
        self.assertEqual(summary["avg_latency_s"], 2.0)

        all_data = self.dashboard(period="all")
        self.assertEqual(all_data["summary"]["operation_count"], 4)
        self.assertEqual(all_data["summary"]["total_tokens"], 400)
        self.assertAlmostEqual(all_data["summary"]["estimated_cost"], 0.008)
        today = datetime.now().date().isoformat()
        daily = next(row for row in all_data["daily"] if row["label"] == today)
        self.assertEqual((daily["operation_count"], daily["request_count"]), (3, 3))
        grade = next(row for row in all_data["groups"]["operations"] if row["label"] == "grade")
        self.assertEqual((grade["operation_count"], grade["request_count"]), (1, 2))

    def test_request_logs_model_ranking_and_stacked_trend(self):
        self.seed_operations()
        data = self.dashboard()
        self.assertEqual(data["summary"]["cached_tokens"], 100)
        logs = data["request_logs"]
        self.assertEqual([row["provider_request_id"] for row in logs],
                         ["req-failed-429", "req-grade-2", "req-grade-1"])
        self.assertEqual(logs[0]["event_status"], "error")
        self.assertIsNone(logs[0]["total_tokens"])
        self.assertEqual(logs[0]["event_error"], "HTTP 429")

        ranking = {row["label"]: row for row in data["model_ranking"]}
        self.assertEqual((ranking["model-a"]["total_tokens"],
                          ranking["model-a"]["request_count"]), (350, 2))
        self.assertIsNone(ranking["model-b"]["total_tokens"])
        self.assertEqual(ranking["model-b"]["unknown_usage_count"], 1)

        trend = data["token_trend"]
        self.assertEqual(len(trend["days"]), 30)
        today = datetime.now().date().isoformat()
        day = next(row for row in trend["days"] if row["label"] == today)
        self.assertEqual((day["total_tokens"], day["request_count"]), (350, 3))
        segments = {row["label"]: row for row in day["segments"]}
        self.assertEqual(segments["model-a"]["value"], 350)
        self.assertIsNone(segments["model-b"]["value"])
        self.assertEqual(segments["model-b"]["request_count"], 1)
        self.assertEqual(len(self.dashboard(period="all")["token_trend"]["days"]), 2)

        html = self.login().get("/admin/ai-usage?period=1").get_data(as_text=True)
        self.assertIn("模型用量排行", html)
        self.assertIn("Token 消耗走势", html)
        self.assertIn("请求日志", html)
        self.assertIn("req-failed-429", html)
        self.assertIn("HTTP 429", html)

    def test_request_id_and_operation_id_search(self):
        self.seed_operations()
        by_request = self.dashboard(q="grade-2")
        self.assertEqual(by_request["summary"]["operation_count"], 1)
        self.assertEqual(by_request["summary"]["request_count"], 2)
        self.assertEqual(by_request["summary"]["total_tokens"], 350)
        self.assertEqual(len(by_request["request_logs"]), 2)

        by_operation = self.dashboard(q="#2")
        self.assertEqual(by_operation["summary"]["operation_count"], 1)
        self.assertEqual(by_operation["summary"]["request_count"], 1)
        self.assertIsNone(by_operation["summary"]["total_tokens"])
        self.assertEqual(by_operation["request_logs"][0]["provider_request_id"],
                         "req-failed-429")

        html = self.login().get("/admin/ai-usage?q=req-grade-2").get_data(as_text=True)
        self.assertIn("req-grade-1", html)
        self.assertNotIn("req-failed-429", html)

    def test_dashboard_filters_and_admin_pages(self):
        self.seed_operations()
        self.assertEqual(self.dashboard(teacher_id=3)["summary"]["operation_count"], 1)
        self.assertEqual(self.dashboard(course_id=10)["summary"]["operation_count"], 2)
        self.assertEqual(self.dashboard(operation_type="grade")["summary"]["operation_count"], 1)
        self.assertEqual(self.dashboard(model="model-b")["summary"]["operation_count"], 1)
        failed = self.dashboard(status="failed")["summary"]
        self.assertIsNone(failed["total_tokens"])
        self.assertIsNone(failed["estimated_cost"])

        client = self.login()
        for path in ("/admin/users", "/admin/ai-usage", "/admin/logs", "/admin/consistency"):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn(b'class="active"', resp.data)
        resp = client.get("/admin/ai-usage?period=all&model=model-b")
        self.assertIn("model-b".encode(), resp.data)
        self.assertIn("重新生成总评".encode(), resp.data)
        self.assertEqual(client.get("/admin/ai-usage?period=invalid").status_code, 200)

    def test_admin_routes_reject_other_roles_and_anonymous(self):
        paths = ("/admin/ai-usage", "/admin/logs", "/admin/logs/tail",
                 "/admin/logs/download/app.log")
        for uid in (2, 4):
            client = self.login(uid)
            for path in paths:
                self.assertEqual(client.get(path).status_code, 403, (uid, path))
        anonymous = app_module.app.test_client()
        for path in paths:
            self.assertEqual(anonymous.get(path).status_code, 302, path)

    def write_log(self, name, data):
        path = os.path.join(app_module.cfg.LOG_DIR, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_log_tail_increment_partial_and_bad_utf8(self):
        content = b"".join(f"line {n}\n".encode() for n in range(1, 251))
        self.write_log("app.log", content)
        first = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log")
        self.assertEqual(len(first["lines"]), 200)
        self.assertEqual(first["lines"][0], "line 51")
        self.assertEqual(first["lines"][-1], "line 250")

        with open(os.path.join(app_module.cfg.LOG_DIR, "app.log"), "ab") as f:
            f.write(b"partial")
        unchanged = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log", first["cursor"])
        self.assertEqual(unchanged["lines"], [])
        self.assertFalse(unchanged["has_more"])
        self.assertEqual(unchanged["cursor"]["offset"], first["cursor"]["offset"])
        with open(os.path.join(app_module.cfg.LOG_DIR, "app.log"), "ab") as f:
            f.write(b" done \xff\n")
        incremental = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log", first["cursor"])
        self.assertEqual(len(incremental["lines"]), 1)
        self.assertIn("partial done", incremental["lines"][0])
        self.assertIn("\ufffd", incremental["lines"][0])

    def test_log_rotation_truncation_and_read_bounds(self):
        self.write_log("app.log", b"old line\n" * 30)
        first = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log")
        os.replace(os.path.join(app_module.cfg.LOG_DIR, "app.log"),
                   os.path.join(app_module.cfg.LOG_DIR, "app.log.1"))
        self.write_log("app.log", b"new line\n")
        rotated = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log", first["cursor"])
        self.assertTrue(rotated["reset"])
        self.assertEqual(rotated["lines"], ["new line"])
        self.assertTrue(rotated["message"])

        self.write_log("app.log", b"".join(b"line %d\n" % n for n in range(600)))
        bounded = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log")
        self.assertEqual(len(bounded["lines"]), 200)
        self.assertTrue(bounded["has_more"])
        rest = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log", bounded["cursor"])
        self.assertEqual(len(rest["lines"]), 100)
        self.write_log("app.log", b"x\n")
        truncated = logview.read_chunk(app_module.cfg.LOG_DIR, "app.log", rest["cursor"])
        self.assertTrue(truncated["reset"])
        self.assertEqual(truncated["lines"], ["x"])

    def test_log_whitelist_routes_and_download(self):
        path = self.write_log("app.log", b"2026-01-01 00:00:00,000 INFO test: hello\n")
        self.write_log("app.log.6", b"must not be listed")
        outside = os.path.join(_TEST_ROOT, "outside.log")
        with open(outside, "wb") as f:
            f.write(b"secret")
        try:
            os.symlink(outside, os.path.join(app_module.cfg.LOG_DIR, "app.log.1"))
        except FileExistsError:
            pass
        names = [item["name"] for item in logview.list_logs(app_module.cfg.LOG_DIR)]
        self.assertEqual(names, ["app.log"])
        for bad in ("../data.sqlite", "other.log", "app.log.6", "app.log.1"):
            with self.subTest(bad=bad):
                with self.assertRaises(FileNotFoundError):
                    logview.resolve_log(app_module.cfg.LOG_DIR, bad)

        client = self.login()
        tail = client.get("/admin/logs/tail?name=app.log")
        self.assertEqual(tail.status_code, 200)
        self.assertEqual(tail.headers["Cache-Control"], "no-store")
        self.assertIn("hello", tail.get_json()["lines"][0])
        self.assertEqual(client.get("/admin/logs/tail?name=../data.sqlite").status_code, 404)
        self.assertEqual(client.get("/admin/logs/tail?name=app.log&offset=x&dev=1&ino=1").status_code, 400)
        download = client.get("/admin/logs/download/app.log")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["Cache-Control"], "no-store")
        with open(path, "rb") as f:
            self.assertEqual(download.data, f.read())
        self.assertEqual(client.get("/admin/logs/download/%2E%2E%2Fdata.sqlite").status_code, 404)


class CourseExportTest(unittest.TestCase):
    """课程备份：zip 目录结构、权限、保留期与下载安全。"""

    ROOT_NAME = "线性代数 & 导数_测试"

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        for name in os.listdir(app_module.cfg.EXPORT_DIR):
            try:
                os.remove(os.path.join(app_module.cfg.EXPORT_DIR, name))
            except OSError:
                pass
        con = sqlite3.connect(app_module.DB_PATH)
        for table in ("ai_usage_events", "ai_operations", "questions", "submissions",
                      "lectures", "enrollments", "course_teachers", "courses", "users"):
            con.execute(f"DELETE FROM {table}")
        users = [
            (1, "admin", "管理员", "admin"),
            (2, "teacher1", "老师一", "teacher"),
            (3, "teacher2", "老师二", "teacher"),
            (4, "student", "学生", "student"),
        ]
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(?,?, 'x',?,?,1,0)", users)
        con.execute("INSERT INTO courses(id,name,teacher_id) "
                    "VALUES(30,'线性代数 & 导数/测试',2)")
        con.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(30,2)")
        con.execute("INSERT INTO enrollments(course_id,student_id) VALUES(30,4)")
        con.executemany(
            "INSERT INTO lectures(id,course_id,title,student_pdf,answer_pdf) "
            "VALUES(?,30,?,?,?)",
            [(100, "第1讲 极限", "20260101120000_aaaaaa_讲义.pdf",
              "20260101120001_bbbbbb_答案.pdf"),
             (101, "第1讲 极限", "", "")])
        con.executemany(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,report_md) "
            "VALUES(?,?,?,?,?)",
            [(1, 100, 4, json.dumps(["20260101120002_cccccc_作业.pdf",
                                     "20260101120003_cccccc_作业.pdf"]),
              "# 批改报告\n全部正确"),
             (2, 101, 4, "20260101120004_dddddd_第二讲作业.pdf", "")])
        con.commit()
        con.close()
        for name in ("20260101120000_aaaaaa_讲义.pdf", "20260101120001_bbbbbb_答案.pdf",
                     "20260101120002_cccccc_作业.pdf", "20260101120003_cccccc_作业.pdf",
                     "20260101120004_dddddd_第二讲作业.pdf"):
            with open(os.path.join(app_module.UPLOAD_DIR, name), "wb") as f:
                f.write(b"%PDF-1.4 test")

    def login(self, uid):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = uid
            sess["_csrf"] = "test-token"
        return client

    @staticmethod
    def signed(cid):
        return app_module._id_signer.dumps(cid)

    def post_export(self, uid=2, cid=30):
        return self.login(uid).post(f"/course/{self.signed(cid)}/export",
                                    data={"_csrf": "test-token"})

    def test_zip_structure(self):
        resp = self.post_export()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(resp.data)) as archive:
            names = set(archive.namelist())
            report = archive.read(
                f"{self.ROOT_NAME}/第1讲 极限/student/批改报告.md").decode()
        self.assertIn(f"{self.ROOT_NAME}/", names)
        self.assertIn(f"{self.ROOT_NAME}/第1讲 极限/教案-学生版.pdf", names)
        self.assertIn(f"{self.ROOT_NAME}/第1讲 极限/教案-教师版.pdf", names)
        self.assertIn(f"{self.ROOT_NAME}/第1讲 极限/student/提交作业/作业.pdf", names)
        self.assertIn(f"{self.ROOT_NAME}/第1讲 极限/student/提交作业/作业 (2).pdf", names)
        self.assertIn(f"{self.ROOT_NAME}/第1讲 极限 (2)/student/提交作业/第二讲作业.pdf",
                      names)
        self.assertIn("全部正确", report)
        resp.close()

    def test_page_entry_and_retained_download(self):
        page = self.login(2).get(f"/course/{self.signed(30)}")
        self.assertIn("导出课程备份", page.get_data(as_text=True))

        resp = self.post_export()
        self.assertEqual(resp.status_code, 200)
        stored = [n for n in os.listdir(app_module.cfg.EXPORT_DIR)
                  if n.startswith("course_30_")]
        self.assertEqual(len(stored), 1)
        self.assertIn(stored[0], self.login(2).get(
            f"/course/{self.signed(30)}").get_data(as_text=True))

        got = self.login(2).get(f"/course/{self.signed(30)}/export/{stored[0]}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data, resp.data)
        got.close()
        resp.close()

    def test_permissions(self):
        url = f"/course/{self.signed(30)}/export"
        self.assertEqual(self.login(3).post(url, data={"_csrf": "test-token"}).status_code, 403)
        self.assertEqual(self.login(4).post(url, data={"_csrf": "test-token"}).status_code, 403)
        # 匿名请求没有会话，会被全局 CSRF 校验先拦下
        self.assertEqual(app_module.app.test_client().post(
            url, data={"_csrf": "test-token"}).status_code, 400)

    def test_download_rejects_foreign_or_traversal(self):
        foreign = "course_20_20260101_120000.zip"
        with open(os.path.join(app_module.cfg.EXPORT_DIR, foreign), "wb") as f:
            f.write(b"fake")
        base = f"/course/{self.signed(30)}/export/"
        self.assertEqual(self.login(2).get(base + foreign).status_code, 404)
        self.assertEqual(self.login(2).get(base + "%2E%2E%2Fdata.sqlite").status_code, 404)
        self.assertEqual(self.login(3).get(base + foreign).status_code, 403)

    def test_one_day_retention_and_unsafe_files(self):
        resp = self.post_export()
        self.assertEqual(resp.status_code, 200)
        resp.close()
        stored = [n for n in os.listdir(app_module.cfg.EXPORT_DIR)
                  if n.startswith("course_30_")][0]
        path = os.path.join(app_module.cfg.EXPORT_DIR, stored)
        old = time.time() - app_module.COURSE_EXPORT_KEEP_HOURS * 3600 - 60
        os.utime(path, (old, old))
        app_module._prune_course_exports()
        self.assertFalse(os.path.exists(path))

        # 越界或缺失的提交文件会被跳过，不会打进压缩包
        con = sqlite3.connect(app_module.DB_PATH)
        con.execute("UPDATE submissions SET file_path=? WHERE id=2",
                    (json.dumps(["../outside.pdf", "missing.pdf"]),))
        con.commit()
        con.close()
        resp = self.post_export()
        with zipfile.ZipFile(io.BytesIO(resp.data)) as archive:
            names = archive.namelist()
        self.assertNotIn(f"{self.ROOT_NAME}/第1讲 极限 (2)/student/", names)
        self.assertFalse(any("outside" in name for name in names))
        resp.close()


class LectureScopeTest(unittest.TestCase):
    """保存教师版页码即生成题目清单，不再需要独立的分析按钮。"""

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        con = sqlite3.connect(app_module.DB_PATH)
        for table in ("ai_usage_events", "ai_operations", "questions", "submissions",
                      "lectures", "enrollments", "course_teachers", "courses", "users"):
            con.execute(f"DELETE FROM {table}")
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(?,?,'x',?,?,1,0)",
            [(2, "teacher1", "老师一", "teacher"), (3, "teacher2", "老师二", "teacher")])
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(30,'数学',2)")
        con.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(30,2)")
        con.execute("INSERT INTO lectures(id,course_id,title,answer_pdf) "
                    "VALUES(100,30,'第1讲','ans.pdf')")
        con.commit()
        con.close()
        with open(os.path.join(app_module.UPLOAD_DIR, "ans.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 test")

    def login(self, uid):
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = uid
            sess["_csrf"] = "test-token"
        return client

    def test_save_pages_generates_scope_in_one_step(self):
        settings = {"ai_api_url": "http://ai.local", "ai_api_key": "k", "ai_model": "m"}
        questions = [{"no": "1", "key": "1", "max": 10, "hint": "求极限"}]
        with mock.patch.object(app_module, "pdf_page_count", return_value=10), \
                mock.patch.object(app_module, "get_setting",
                                  side_effect=lambda tid, key: settings.get(key, "")), \
                mock.patch.object(app_module, "analyze_answer_pdf",
                                  return_value=(questions, "")) as analyze:
            resp = self.login(2).post(
                f"/lecture/{app_module._id_signer.dumps(100)}/answer/pages",
                data={"_csrf": "test-token", "page": ["2", "3"]})
        self.assertEqual(resp.status_code, 302)
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.kwargs["pages"], [2, 3])

        con = sqlite3.connect(app_module.DB_PATH)
        row = con.execute("SELECT answer_pages, scope_candidates, scope_selected "
                         "FROM lectures WHERE id=100").fetchone()
        con.close()
        self.assertEqual(row[0], "2,3")
        self.assertIn("求极限", row[1])
        self.assertEqual(row[2], "")

        # 独立分析入口已合并，旧地址不再可用
        self.assertEqual(self.login(2).post(
            f"/lecture/{app_module._id_signer.dumps(100)}/scope/analyze",
            data={"_csrf": "test-token"}).status_code, 404)

        with mock.patch.object(app_module, "pdf_page_count", return_value=10):
            page = self.login(2).get(f"/lecture/{app_module._id_signer.dumps(100)}")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("保存并生成题目列表", html)
        self.assertNotIn("scope/analyze", html)


class TokenHubModelSyncTest(unittest.TestCase):
    """TokenHub 每日同步：只保留适合图片批改的模型，并刷新公开价目。"""

    PRICE_HTML = """
    <table>
      <tr><th>模型名称</th><th>model（调用参数）</th><th>条件</th><th>峰谷计费</th>
          <th>推理输入（元/百万 tokens）</th><th>推理输出（元/百万 tokens）</th><th>缓存命中</th></tr>
      <tr><td>纯文本 Hy4</td><td>hy4</td><td>-</td><td>-</td><td>6</td><td>18</td><td>0.3</td></tr>
      <tr><td>DeepSeek-V4.1-Flash</td><td>deepseek-v4.1-flash</td><td>-</td><td>空闲时段</td>
          <td>1</td><td>4</td><td>0.02</td></tr>
      <tr><td></td><td></td><td></td><td>高峰时段</td><td>2</td><td>8</td><td>0.04</td></tr>
      <tr><td>GLM-5.3-Flash</td><td>glm-5.3-flash</td><td>-</td><td>-</td><td>0.8</td><td>2.8</td><td>0.23</td></tr>
    </table>
    <table>
      <tr><th>模型名称</th><th>推理输入（元/百万 tokens）</th><th>推理输出（元/百万 tokens）</th></tr>
      <tr><td>YT-VITA</td><td>1.2</td><td>3.5</td></tr>
    </table>
    <table>
      <tr><th>模型名称</th><th>功能</th><th>Token 单价</th></tr>
      <tr><td>语音合成 TTS</td><td>语音合成</td><td>10 元/百万 tokens</td></tr>
    </table>
    """

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        con = sqlite3.connect(app_module.DB_PATH)
        for table in ("model_sync", "model_sync_state", "model_prices", "settings", "users"):
            con.execute(f"DELETE FROM {table}")
        con.execute(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(2,'teacher1','x','老师一','teacher',1,0)")
        con.executemany(
            "INSERT INTO settings(teacher_id,key,value) VALUES(2,?,?)",
            [("ai_api_url", "https://tokenhub.tencentmaas.com/v1"), ("ai_api_key", "secret")])
        con.commit()
        con.close()

    def test_parse_tokenhub_prices_and_suitable_filter(self):
        data = app_module.parse_tokenhub_prices(self.PRICE_HTML)
        self.assertEqual(data["prices"]["deepseek-v4.1-flash"]["price_in"], 1.0)
        self.assertEqual(data["prices"]["deepseek-v4.1-flash"]["price_out"], 4.0)
        self.assertEqual(data["prices"]["glm-5.3-flash"]["price_out"], 2.8)
        self.assertEqual(data["prices"]["yt-vita"]["price_in"], 1.2)
        self.assertIn("hy4", data["prices"])
        suitable = app_module.tokenhub_suitable_model_ids(data)
        self.assertIn("deepseek-v4.1-flash", suitable)
        self.assertIn("yt-vita", suitable)
        self.assertNotIn("hy4", suitable)

    def test_sync_keeps_only_suitable_models_and_updates_prices(self):
        available = [
            {"model_id": "hy4", "display_name": "Hy4", "status": ""},
            {"model_id": "deepseek-v4.1-flash", "display_name": "DeepSeek-V4.1-Flash", "status": ""},
            {"model_id": "glm-5.3-flash", "display_name": "GLM-5.3-Flash", "status": ""},
            {"model_id": "tenlai-tts-v1", "display_name": "TTS", "status": ""},
        ]
        prices = app_module.parse_tokenhub_prices(self.PRICE_HTML)
        with mock.patch.object(app_module, "fetch_available_models", return_value=available), \
                mock.patch.object(app_module, "fetch_tokenhub_prices", return_value=prices):
            count, err = app_module.sync_models_for_teacher(2, force=True)
        self.assertEqual(err, "")
        self.assertEqual(count, 2)

        con = sqlite3.connect(app_module.DB_PATH)
        rows = con.execute(
            "SELECT model_id FROM model_sync WHERE teacher_id=2 "
            "ORDER BY model_id").fetchall()
        self.assertEqual([r[0] for r in rows], ["deepseek-v4.1-flash", "glm-5.3-flash"])
        price = con.execute(
            "SELECT price_in,price_out,source FROM model_prices WHERE model='deepseek-v4.1-flash'"
        ).fetchone()
        con.close()
        self.assertEqual(price[0], 1.0)
        self.assertEqual(price[1], 4.0)
        self.assertEqual(price[2], "TokenHub 公开价目")


class ApiKeyManagementTest(unittest.TestCase):
    """老师页面的多组 AI API Key：新增、切换、删除与状态展示。"""

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        con = sqlite3.connect(app_module.DB_PATH)
        for table in ("ai_api_keys", "settings", "users"):
            con.execute(f"DELETE FROM {table}")
        con.execute(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(2,'teacher1','x','老师一','teacher',1,0)")
        con.commit()
        con.close()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess["uid"] = 2
            sess["_csrf"] = "test-token"

    def post(self, path, **data):
        data.setdefault("_csrf", "test-token")
        return self.client.post(path, data=data, follow_redirects=False)

    def keys(self):
        con = sqlite3.connect(app_module.DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id,name,base_url,api_key,is_active FROM ai_api_keys "
            "WHERE teacher_id=2 ORDER BY id").fetchall()
        setting = con.execute(
            "SELECT value FROM settings WHERE teacher_id=2 AND key='ai_api_key'").fetchone()
        con.close()
        return rows, (setting["value"] if setting else None)

    def test_add_activate_and_delete_keys(self):
        self.assertEqual(self.client.get("/settings").status_code, 200)
        self.post("/settings/api-keys/add", name="主账号", base_url="https://ai.one/v1",
                  api_key="sk-first-123456", use_now="1")
        rows, active_key = self.keys()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_active"], 1)
        self.assertEqual(active_key, "sk-first-123456")

        self.post("/settings/api-keys/add", name="备用", base_url="https://ai.two/v1",
                  api_key="sk-second-654321")
        rows, active_key = self.keys()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["is_active"] for r in rows], [1, 0])
        self.assertEqual(active_key, "sk-first-123456")

        self.post(f"/settings/api-keys/{rows[1]['id']}/activate")
        rows, active_key = self.keys()
        self.assertEqual([r["is_active"] for r in rows], [0, 1])
        self.assertEqual(active_key, "sk-second-654321")

        self.post(f"/settings/api-keys/{rows[1]['id']}/delete")
        rows, active_key = self.keys()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_active"], 1)
        self.assertEqual(active_key, "sk-first-123456")

    def test_settings_page_shows_configured_status_and_masks_key(self):
        self.post("/settings/api-keys/add", name="主账号", base_url="https://ai.one/v1",
                  api_key="sk-secret-abcdef", use_now="1")
        html = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("当前 AI API 已配置", html)
        self.assertIn("主账号", html)
        self.assertIn("当前使用", html)
        self.assertIn("sk-s", html)
        self.assertNotIn("sk-secret-abcdef", html)
        self.assertGreater(html.find("AI API 配置"), -1)
        self.assertLess(html.find("AI API 配置"), html.find("AI 模型清单与批阅设置"))

    def test_invalid_key_is_rejected(self):
        self.post("/settings/api-keys/add", name="坏地址", base_url="ftp://nope",
                  api_key="sk-x")
        rows, _ = self.keys()
        self.assertEqual(rows, [])

    def test_other_teachers_key_cannot_be_activated(self):
        con = sqlite3.connect(app_module.DB_PATH)
        con.execute(
            "INSERT INTO ai_api_keys(teacher_id,name,base_url,api_key,is_active) "
            "VALUES(3,'别人','https://ai.one/v1','sk-other',0)")
        con.commit()
        con.close()
        self.post("/settings/api-keys/1/activate")
        rows, active_key = self.keys()
        self.assertEqual(rows, [])
        self.assertIsNone(active_key)


if __name__ == "__main__":
    unittest.main()
