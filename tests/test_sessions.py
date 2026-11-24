"""服务端空闲会话回归测试；使用临时 SQLite，不等待真实超时时间。"""
import tempfile
import unittest
import re
from pathlib import Path
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a
from utilities.sessions import _session_hash
from werkzeug.security import generate_password_hash


class SessionTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gradelogy-session-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = a.create_app({
            "DATA_DIR": str(self.root),
            "SECRET_KEY": "session-timeout-test-secret",
            "TESTING": True,
            "SESSION_IDLE_TIMEOUT_SECONDS": 45 * 60,
            "SESSION_IDLE_WARNING_SECONDS": 40 * 60,
            "AUTO_BACKUP": False,
            "AUTO_MODEL_SYNC": False,
            "AUTO_GRADE": False,
        })
        a.initialize_app(self.app, start_workers=False)
        with self.app.app_context():
            con = a.get_db()
            con.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active,"
                "must_change_password) VALUES(?,?,?,?,1,0)",
                ("session_teacher", generate_password_hash("test-password"), "测试老师", "teacher"))
            con.commit()
        self.client = self.app.test_client()
        self.start_time = 1_000_000
        self.login()

    def at(self, timestamp):
        return mock.patch("utilities.sessions.time.time", return_value=timestamp)

    def login(self):
        with self.at(self.start_time):
            response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        csrf = re.search(r'name="_csrf" value="([^"]+)"', response.text).group(1)
        with self.at(self.start_time):
            response = self.client.post("/login", data={
                "_csrf": csrf,
                "username": "session_teacher",
                "password": "test-password",
            })
        self.assertEqual(response.status_code, 302)
        self.user_id = 2
        with self.at(self.start_time):
            response = self.client.get("/courses", headers=self.document_headers())
        self.assertEqual(response.status_code, 200)
        self.csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"',
                              response.text).group(1)

    @staticmethod
    def document_headers():
        return {
            "Accept": "text/html",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }

    def last_activity(self):
        with self.app.app_context():
            row = a.get_db().execute(
                "SELECT last_activity_at FROM auth_sessions WHERE user_id=?",
                (self.user_id,)).fetchone()
            return int(row[0]) if row else None

    def test_cookie_and_server_store_use_the_45_minute_limit(self):
        self.assertEqual(self.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds(), 2700)
        self.assertFalse(self.app.config["SESSION_REFRESH_EACH_REQUEST"])
        with self.app.app_context():
            row = a.get_db().execute(
                "SELECT session_hash,user_id,last_activity_at FROM auth_sessions WHERE user_id=?",
                (self.user_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(len(row["session_hash"]), 64)
        self.assertEqual(row["last_activity_at"], self.start_time)

    def test_document_navigation_counts_but_json_status_poll_does_not(self):
        with self.at(self.start_time + 500):
            page = self.client.get("/courses", headers=self.document_headers())
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.last_activity(), self.start_time + 500)

        with self.at(self.start_time + 1000):
            poll = self.client.get("/session/status", headers={
                "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(self.last_activity(), self.start_time + 500)

        expired_at = self.start_time + 500 + 2700
        with self.at(expired_at):
            expired = self.client.get("/session/status", headers={
                "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.headers.get("X-Session-Expired"), "1")
        self.assertIsNone(self.last_activity())

    def test_explicit_activity_renews_only_after_server_validation(self):
        activity_at = self.start_time + 2400
        with self.at(activity_at):
            resumed = self.client.post("/session/activity", json={}, headers={
                "X-CSRF-Token": self.csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            })
        self.assertEqual(resumed.status_code, 200)
        self.assertTrue(resumed.json["ok"])
        self.assertEqual(resumed.json["lastActivityAt"], activity_at)
        self.assertEqual(self.last_activity(), activity_at)

        with self.at(activity_at + 2699):
            valid = self.client.get("/session/status", headers={
                "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(self.last_activity(), activity_at)

        with self.at(activity_at + 2700):
            expired = self.client.get("/session/status", headers={
                "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(expired.status_code, 401)

    def test_invalid_csrf_does_not_extend_idle_deadline(self):
        with self.at(self.start_time + 2000):
            response = self.client.post("/session/activity", json={}, headers={
                "X-CSRF-Token": "wrong-token",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.last_activity(), self.start_time)

    def test_csrf_validated_form_action_counts_as_activity(self):
        action_at = self.start_time + 2000
        with self.at(action_at):
            response = self.client.post("/password", data={
                "_csrf": self.csrf, "old": "incorrect", "new": "new-password"},
                headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("原密码不正确", response.text)
        self.assertEqual(self.last_activity(), action_at)

    def test_expired_form_submission_redirects_to_login_not_csrf_error(self):
        expired_at = self.start_time + 2700
        with self.at(expired_at):
            response = self.client.post("/password", data={
                "_csrf": "any-token", "old": "x", "new": "new-password"},
                headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        self.assertIsNone(self.last_activity())
        with self.at(expired_at):
            login_page = self.client.get("/login")
        self.assertIn("登录状态已失效，请重新登录。", login_page.text)

    def test_expired_document_refresh_redirects_to_login_even_if_accept_includes_json(self):
        expired_at = self.start_time + 2700
        headers = self.document_headers()
        headers["Accept"] = "application/json, text/html"
        with self.at(expired_at):
            response = self.client.get("/courses", headers=headers)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=/courses", response.location)
        self.assertIsNone(self.last_activity())

    def test_expired_ajax_request_still_receives_json_401(self):
        expired_at = self.start_time + 2700
        with self.at(expired_at):
            response = self.client.get("/courses", headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            })
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json["ok"] is False)
        self.assertEqual(response.headers.get("X-Session-Expired"), "1")

    def test_login_page_contains_accessible_idle_warning_for_authenticated_user(self):
        with self.at(self.start_time + 10):
            response = self.client.get("/courses", headers=self.document_headers())
        self.assertIn('id="session-idle-dialog"', response.text)
        self.assertIn('id="session-idle-continue"', response.text)
        self.assertIn('id="session-idle-logout"', response.text)
        self.assertIn("连续 40 分钟无操作", response.text)

    def test_logout_revokes_only_this_browser_session(self):
        with self.app.app_context():
            second_id, _ = a.create_auth_session(a.get_db(), self.user_id, now=self.start_time)
            a.get_db().commit()
        with self.at(self.start_time + 1):
            response = self.client.get("/logout", headers=self.document_headers())
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            rows = a.get_db().execute(
                "SELECT session_hash FROM auth_sessions WHERE user_id=?", (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], _session_hash(second_id))


if __name__ == "__main__":
    unittest.main()
