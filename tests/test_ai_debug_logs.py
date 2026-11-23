# -*- coding: utf-8 -*-
"""教师 AI 原始调试日志、导出与权限隔离测试。"""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a  # noqa: E402
from utilities import ai_debug, assistant, llm  # noqa: E402
from utilities.sessions import session_epoch  # noqa: E402

CSRF = "ai-debug-test-token"
API_KEY = "sk-secret"
BASE_URL = "https://tokenhub.tencentmaas.com/v1"


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = headers or {"content-type": "application/json",
                                   "x-request-id": "header-request-id"}

    def read(self):
        return self.body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def chat_body(content="正常", **message_extra):
    message = {"role": "assistant", "content": content}
    message.update(message_extra)
    return json.dumps({
        "id": "provider-response-id", "model": "model-a",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                  "completion_tokens_details": {"reasoning_tokens": 1}},
        "vendor_extension": {"trace": "完整保留"},
    }, ensure_ascii=False).encode("utf-8")


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://ai.example/v1/chat/completions", code, "provider error",
        {"content-type": "application/json", "x-request-id": f"error-{code}"},
        io.BytesIO(body))


class AIDebugLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gradelogy-ai-debug-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.app = a.create_app({
            "DATA_DIR": self.root, "SECRET_KEY": "ai-debug-test-secret",
            "TESTING": True, "AUTO_BACKUP": False,
            "AUTO_MODEL_SYNC": False, "AUTO_GRADE": False,
        })
        a.initialize_app(self.app, start_workers=False)
        with self.app.app_context():
            db = a.get_db()
            db.executemany(
                "INSERT INTO users(id,username,password_hash,display_name,role,active,"
                "must_change_password) VALUES(?,?,'x',?,?,1,0)",
                [(2, "teacher", "张老师", "teacher"),
                 (3, "other", "其他老师", "teacher"),
                 (4, "student", "学生", "student")])
            db.execute(
                "INSERT INTO ai_api_keys(teacher_id,name,base_url,api_key,model,is_active) "
                "VALUES(2,'测试配置',?,?,?,1)", (BASE_URL, API_KEY, "model-a"))
            db.commit()
        self.teacher = self.login(2)
        self.other_teacher = self.login(3)
        self.student = self.login(4)

    def login(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = uid
            sess["auth_epoch"] = session_epoch(self.root)
            sess["_csrf"] = CSRF
        return client

    def url(self, endpoint, **values):
        with self.app.test_request_context():
            return a.url_for(endpoint, **values)

    def run_chat(self, effects, payload=None, retries=1, operation_type="grade",
                 stage="test.stage"):
        payload = payload or {
            "model": "model-a", "messages": [{"role": "user", "content": "学生答案"}],
            "max_tokens": 100,
        }
        with self.app.app_context():
            operation = a._start_ai_operation(2, operation_type, "model-a")
            with mock.patch.object(llm.urllib.request, "urlopen", side_effect=effects), \
                    mock.patch.object(llm.time, "sleep"):
                message, finish, error = llm.chat_completion(
                    "https://ai.example/v1?credential=url-secret", API_KEY, payload,
                    timeout=1, retries=retries, usage_hook=operation["usage_hook"],
                    stage=stage)
            a._finish_ai_operation(
                operation, "failed" if error else "success", error,
                result={"reply": message.get("content", "")} if not error else None)
            events = [dict(row) for row in a.get_db().execute(
                "SELECT * FROM ai_usage_events WHERE operation_id=? ORDER BY id",
                (operation["id"],))]
            return {"operation_id": operation["id"], "message": message,
                    "finish": finish, "error": error, "events": events,
                    "request_bytes": json.dumps(payload).encode()}

    def rows(self, sql, params=()):
        with self.app.app_context():
            return [dict(row) for row in a.get_db().execute(sql, params)]

    def payload(self, event, kind):
        with self.app.app_context():
            data, truncated = ai_debug.read_preview(
                event["operation_id"], event[f"{kind}_path"], limit=16 * 1024 * 1024)
            self.assertFalse(truncated)
            return data

    def debug_url(self, operation_id):
        return self.url("teacher_ai_operation_debug", operation_id=operation_id)

    def raw_url(self, event_id, kind="request", download=False):
        values = {"event_id": event_id, "kind": kind}
        if download:
            values["download"] = 1
        return self.url("teacher_ai_event_raw", **values)

    def zip_url(self, operation_id):
        return self.url("teacher_ai_operation_debug_zip", operation_id=operation_id)

    def test_raw_fidelity_base64_redaction_and_vendor_fields(self):
        image = "aW1hZ2UtYnl0ZXM=" * 20
        payload = {
            "model": "model-a", "max_tokens": 100,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": f"真实学生作答 {API_KEY}"},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image}"}},
            ]}],
        }
        response = FakeResponse(chat_body(
            "模型最终结果", reasoning_content="服务商返回的推理字段"))
        result = self.run_chat([response], payload=payload)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["message"]["reasoning_content"], "服务商返回的推理字段")
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["call_index"], 1)
        self.assertEqual(event["http_status"], 200)
        expected_request = result["request_bytes"].replace(API_KEY.encode(), b"[REDACTED]")
        self.assertEqual(self.payload(event, "request"), expected_request)
        self.assertEqual(self.payload(event, "response"), response.body)
        self.assertNotIn(API_KEY.encode(), self.payload(event, "request"))
        self.assertIn(image.encode(), self.payload(event, "request"))
        self.assertEqual(event["request_size"], len(result["request_bytes"]))
        self.assertEqual(event["request_sha256"],
                         hashlib.sha256(result["request_bytes"]).hexdigest())
        debug = json.loads(event["debug_json"])
        self.assertEqual(debug["url"], "https://ai.example/v1")
        self.assertNotIn("Authorization", json.dumps(debug))
        self.assertEqual(debug["redactions"][0]["kind"], "request")

    def test_retry_attempts_keep_error_body_and_share_call_index(self):
        error_body = json.dumps({"error": {"message": "限流", "request": "r1"}}).encode()
        result = self.run_chat(
            [http_error(429, error_body), FakeResponse(chat_body("重试成功"))], retries=2)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["message"]["content"], "重试成功")
        events = result["events"]
        self.assertEqual([row["attempt"] for row in events], [1, 2])
        self.assertEqual({row["call_index"] for row in events}, {1})
        self.assertEqual(events[0]["status"], "error")
        self.assertEqual(events[0]["http_status"], 429)
        self.assertEqual(self.payload(events[0], "response"), error_body)
        self.assertEqual(self.payload(events[1], "response"), chat_body("重试成功"))
        self.assertEqual(self.payload(events[0], "request"), result["request_bytes"])
        self.assertEqual(self.payload(events[1], "request"), result["request_bytes"])

    def test_non_assistant_retries_server_errors_but_assistant_policy_is_unchanged(self):
        body = json.dumps({"error": {"message": "provider unavailable"}}).encode()
        result = self.run_chat(
            [http_error(503, body), FakeResponse(chat_body("重试成功"))], retries=1)
        self.assertEqual(result["error"], "")
        self.assertEqual([row["attempt"] for row in result["events"]], [1, 2])

        assistant_result = self.run_chat(
            [http_error(503, body)], retries=1, stage="assistant.round.1")
        self.assertIn("HTTP 503", assistant_result["error"])
        self.assertEqual(len(assistant_result["events"]), 1)

        permanent = self.run_chat([http_error(400, body)], retries=1)
        self.assertIn("HTTP 400", permanent["error"])
        self.assertEqual(len(permanent["events"]), 1)

    def test_non_assistant_timeout_is_180_seconds_but_assistant_timeout_is_unchanged(self):
        for stage, expected in (("scope_analyze", 180), ("assistant.round.1", 12)):
            with self.subTest(stage=stage), mock.patch.object(
                    llm.urllib.request, "urlopen",
                    return_value=FakeResponse(chat_body("正常"))) as urlopen:
                llm.chat_completion(
                    "https://ai.example/v1", API_KEY,
                    {"model": "model-a", "messages": []},
                    timeout=12, retries=1, stage=stage)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], expected)

    def test_network_and_invalid_json_failures_keep_available_evidence(self):
        network = self.run_chat([urllib.error.URLError("connection down")
                                 for _ in range(2)])
        self.assertIn("connection down", network["error"])
        self.assertEqual([row["attempt"] for row in network["events"]], [1, 2])
        event = network["events"][-1]
        self.assertEqual(event["status"], "error")
        self.assertTrue(event["request_path"])
        self.assertFalse(event["response_path"])

        malformed = self.run_chat([FakeResponse(b"{not-json") for _ in range(2)])
        self.assertTrue(malformed["error"])
        event = malformed["events"][-1]
        self.assertEqual(self.payload(event, "response"), b"{not-json")
        self.assertEqual(json.loads(event["debug_json"])["exception"]["type"],
                         "JSONDecodeError")

    def test_payload_write_failure_does_not_change_model_result(self):
        with mock.patch.object(ai_debug, "save_payload", side_effect=OSError("disk full")):
            result = self.run_chat([FakeResponse(chat_body("业务结果不受影响"))])
        self.assertEqual(result["error"], "")
        self.assertEqual(result["message"]["content"], "业务结果不受影响")
        event = result["events"][0]
        self.assertIn("disk full", event["debug_error"])
        operation = self.rows("SELECT * FROM ai_operations WHERE id=?",
                              (result["operation_id"],))[0]
        self.assertEqual(operation["status"], "success")
        self.assertIn("disk full", operation["debug_error"])

    def test_debug_html_raw_routes_and_zip_are_safe_and_complete(self):
        script = "<script>alert('xss')</script>"
        payload = {"model": "model-a", "messages": [
            {"role": "user", "content": f"学生作答 {script} {API_KEY}"}]}
        response = FakeResponse(chat_body(script))
        result = self.run_chat([response], payload=payload)
        operation_id = result["operation_id"]
        event = result["events"][0]

        page = self.teacher.get(self.debug_url(operation_id))
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(script, page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertIn("下载完整 ZIP", page.text)

        raw = self.teacher.get(self.raw_url(event["id"]))
        self.assertEqual(raw.status_code, 200)
        self.assertTrue(raw.content_type.startswith("text/plain"))
        self.assertEqual(raw.headers["Cache-Control"], "no-store")
        self.assertIn(script, raw.text)
        self.assertNotIn(API_KEY, raw.text)

        archive = self.teacher.get(self.zip_url(operation_id))
        self.addCleanup(archive.close)
        self.assertEqual(archive.status_code, 200)
        self.assertEqual(archive.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
            names = set(bundle.namelist())
            self.assertTrue({"operation.json", "requests/index.json", "manifest.json"} <= names)
            index = json.loads(bundle.read("requests/index.json"))
            self.assertEqual(len(index), 1)
            self.assertTrue(index[0]["created_at"].endswith("+08:00"))
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(manifest["timezone"], "Asia/Shanghai")
            self.assertTrue(json.loads(bundle.read("operation.json"))["started_at"].endswith("+08:00"))
            request_name = index[0]["files"]["request"]
            response_name = index[0]["files"]["response"]
            self.assertEqual(bundle.read(request_name),
                             result["request_bytes"].replace(API_KEY.encode(), b"[REDACTED]"))
            self.assertEqual(bundle.read(response_name), response.body)
            self.assertNotIn(API_KEY.encode(), bundle.read(request_name))
            self.assertTrue(json.loads(bundle.read("manifest.json"))["complete"])
            exported_operation = json.loads(bundle.read("operation.json"))
            self.assertEqual(exported_operation["result"]["reply"], script)

        for url in (self.debug_url(operation_id), self.raw_url(event["id"]),
                    self.zip_url(operation_id)):
            self.assertEqual(self.other_teacher.get(url).status_code, 404)
            self.assertEqual(self.student.get(url).status_code, 403)

    def test_ai_usage_combines_request_and_operation_debug_logs(self):
        result = self.run_chat([FakeResponse(chat_body("用量页测试"))])
        page = self.teacher.get(self.url("teacher_ai_usage")).get_data(as_text=True)

        self.assertNotIn("AI 操作与调试日志", page)
        self.assertIn("模型请求", page)
        self.assertEqual(page.count("<table"), 1)
        headers = page[page.index("<thead>"):page.index("</thead>")]
        self.assertLess(headers.index("<th>详情</th>"), headers.index("<th>调试日志</th>"))
        self.assertIn(self.debug_url(result["operation_id"]), page)
        self.assertIn(self.zip_url(result["operation_id"]), page)
        self.assertIn("新窗口打开", page)
        self.assertIn("下载 ZIP", page)

    def test_large_payload_preview_is_capped_but_download_is_complete(self):
        response = FakeResponse(chat_body("A" * 300000))
        result = self.run_chat([response])
        event = result["events"][0]
        preview = self.teacher.get(self.raw_url(event["id"], "response"))
        self.assertEqual(preview.status_code, 200)
        self.assertIn("预览已截断", preview.text)
        self.assertLess(len(preview.data), len(response.body))
        download = self.teacher.get(self.raw_url(event["id"], "response", download=True))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, response.body)
        self.assertIn("attachment", download.headers["Content-Disposition"])

    def test_assistant_conversation_creates_no_usage_records_or_request_logs(self):
        first = chat_body("", tool_calls=[{
            "id": "call-1", "type": "function",
            "function": {"name": "ask_help", "arguments": '{"question":"如何批阅"}'},
        }])
        second = chat_body("最终答复")
        with mock.patch.object(llm.urllib.request, "urlopen",
                               side_effect=[FakeResponse(first), FakeResponse(second),
                                            FakeResponse(chat_body("学生答复"))]) as urlopen, \
                mock.patch.object(assistant, "call_tool",
                                  return_value='{"steps":["上传","批阅"],"count":2}') as tool, \
                mock.patch.object(llm._log, "info") as ai_info, \
                mock.patch.object(llm._log, "warning") as ai_warning, \
                mock.patch.object(a.log, "info") as access_info, \
                mock.patch.object(a.user_log, "info") as user_action_info:
            response = self.teacher.post(
                "/assistant/chat", json={"messages": [{"role": "user", "content": "怎么批阅"}]},
                headers={"X-CSRF-Token": CSRF})
            student_response = self.student.post(
                "/assistant/chat", json={"messages": [{"role": "user", "content": "我还有什么作业没交？"}]},
                headers={"X-CSRF-Token": CSRF})
            ai_info.assert_not_called()
            ai_warning.assert_not_called()
            access_info.assert_not_called()
            user_action_info.assert_not_called()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["reply"], "最终答复")
        self.assertEqual(data["trace"][0]["result"],
                         '{"steps":["上传","批阅"],"count":2}')
        tool.assert_called_once()
        self.assertEqual(student_response.status_code, 200)
        self.assertEqual(self.rows(
            "SELECT * FROM ai_operations WHERE operation_type='assistant'"), [])
        self.assertEqual(self.rows("SELECT * FROM ai_usage_events"), [])
        second_request = json.loads(urlopen.call_args_list[1].args[0].data)
        tool_messages = [m for m in second_request["messages"] if m["role"] == "tool"]
        self.assertEqual(tool_messages[0]["content"], data["trace"][0]["result"])

    def test_connection_test_records_transport(self):
        with mock.patch.object(llm.urllib.request, "urlopen",
                               side_effect=[FakeResponse(chat_body("正常"))]):
            response = self.teacher.post("/settings/test", headers={"X-CSRF-Token": CSRF})
        self.assertEqual(response.status_code, 302)
        operations = self.rows(
            "SELECT * FROM ai_operations WHERE operation_type='connection_test'")
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["status"], "success")
        events = self.rows("SELECT * FROM ai_usage_events WHERE operation_id=?",
                           (operations[0]["id"],))
        self.assertEqual(events[0]["stage"], "connection_test")
        request_data = json.loads(self.payload(events[0], "request"))
        self.assertEqual(request_data["messages"][0]["content"], "只回复两个字：正常")

    def test_manual_model_sync_records_get_without_inventing_request_body(self):
        body = json.dumps({"data": [
            {"id": "deepseek-v4.1-flash", "name": "DeepSeek", "status": "ready"}],
            "vendor_extension": {"request": "models-raw"}}, ensure_ascii=False).encode()
        with mock.patch.object(llm.urllib.request, "urlopen",
                               side_effect=[FakeResponse(body)]), \
                mock.patch.object(a, "fetch_tokenhub_prices",
                                  return_value={"prices": {}, "multimodal_ids": set()}):
            response = self.teacher.post("/settings/models/sync",
                                         headers={"X-CSRF-Token": CSRF})
        self.assertEqual(response.status_code, 302)
        operations = self.rows("SELECT * FROM ai_operations WHERE operation_type='model_sync'")
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["status"], "success")
        self.assertEqual(json.loads(operations[0]["result_json"])["model_count"], 1)
        event = self.rows("SELECT * FROM ai_usage_events WHERE operation_id=?",
                          (operations[0]["id"],))[0]
        self.assertEqual(event["request_kind"], "models")
        self.assertEqual(event["stage"], "model_sync.models")
        self.assertFalse(event["request_path"])
        self.assertEqual(self.payload(event, "response"), body)
        archive = self.teacher.get(self.zip_url(operations[0]["id"]))
        self.addCleanup(archive.close)
        with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
            self.assertTrue(json.loads(bundle.read("manifest.json"))["complete"])

    def test_payload_path_traversal_is_rejected(self):
        result = self.run_chat([FakeResponse(chat_body())])
        with self.app.app_context():
            with self.assertRaises(FileNotFoundError):
                ai_debug.resolve_payload(result["operation_id"], "../other/request.json.gz")
            with self.assertRaises(FileNotFoundError):
                ai_debug.resolve_payload(result["operation_id"], "/tmp/request.json.gz")


class AIDebugMigrationTest(unittest.TestCase):
    def test_legacy_ai_tables_gain_debug_columns_without_fabricating_payloads(self):
        with tempfile.TemporaryDirectory(prefix="gradelogy-ai-debug-legacy-") as root:
            legacy = a.create_app({"DATA_DIR": root, "SECRET_KEY": "legacy-debug-secret",
                                   "TESTING": True, "AUTO_BACKUP": False,
                                   "AUTO_MODEL_SYNC": False, "AUTO_GRADE": False})
            path = legacy.config["DB_PATH"]
            with sqlite3.connect(path) as con:
                con.execute("""CREATE TABLE ai_operations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,teacher_id INTEGER NOT NULL,
                    operation_type TEXT NOT NULL,course_id INTEGER,lecture_id INTEGER,
                    submission_id INTEGER,material_id INTEGER,model TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',started_at TEXT NOT NULL,
                    finished_at TEXT,error TEXT DEFAULT '')""")
                con.execute("""CREATE TABLE ai_usage_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,operation_id INTEGER NOT NULL,
                    chunk_id INTEGER,attempt INTEGER NOT NULL,provider_request_id TEXT,
                    model TEXT DEFAULT '',prompt_tokens INTEGER,completion_tokens INTEGER,
                    total_tokens INTEGER,cached_tokens INTEGER,reasoning_tokens INTEGER,
                    latency_ms INTEGER,finish_reason TEXT DEFAULT '',estimated_cost REAL,
                    pricing_json TEXT DEFAULT '',usage_json TEXT DEFAULT '',
                    status TEXT NOT NULL,error TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
                con.execute("INSERT INTO ai_operations(id,teacher_id,operation_type,status,"
                            "started_at) VALUES(1,2,'grade','success','2026-01-01 00:00:00')")
                con.execute("INSERT INTO ai_usage_events(operation_id,attempt,status) "
                            "VALUES(1,1,'success')")
            a.initialize_app(legacy, start_workers=False)
            a.initialize_app(legacy, start_workers=False)
            with sqlite3.connect(path) as con:
                con.row_factory = sqlite3.Row
                operation_columns = {row[1] for row in con.execute(
                    "PRAGMA table_info(ai_operations)")}
                event_columns = {row[1] for row in con.execute(
                    "PRAGMA table_info(ai_usage_events)")}
                self.assertTrue({"result_json", "debug_error"} <= operation_columns)
                self.assertTrue({"request_kind", "stage", "call_index", "http_status",
                                 "request_path", "response_path", "request_size",
                                 "response_size", "request_sha256", "response_sha256",
                                 "debug_json", "debug_error"} <= event_columns)
                row = con.execute("SELECT request_path,response_path,debug_json "
                                  "FROM ai_usage_events WHERE id=1").fetchone()
                self.assertEqual(tuple(row), ("", "", ""))
                indexes = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertIn("idx_ai_usage_call", indexes)


if __name__ == "__main__":
    unittest.main()
