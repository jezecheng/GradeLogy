"""公开登录帮助与匿名登录失败信息回归测试。"""
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a
from werkzeug.security import generate_password_hash


class LoginHelpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gradelogy-login-help-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = a.create_app({
            "DATA_DIR": str(self.root),
            "SECRET_KEY": "login-help-test-secret",
            "TESTING": True,
            "AUTO_BACKUP": False,
            "AUTO_MODEL_SYNC": False,
            "AUTO_GRADE": False,
        })
        a.initialize_app(self.app, start_workers=False)
        with self.app.app_context():
            a.get_db().execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) "
                "VALUES(?,?,?,?,1)",
                ("known-user", generate_password_hash("correct-password"), "测试教师", "teacher"))
            a.get_db().commit()
        self.client = self.app.test_client()

    def test_anonymous_login_shows_public_help_and_keeps_form_and_csrf(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn("uid", session)

        class LoginFormParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_help = False
                self.help_text = []
                self.form = None
                self.inputs = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "section" and attrs.get("aria-labelledby") == "login-help-title":
                    self.in_help = True
                if tag == "form" and attrs.get("method") == "post":
                    self.form = attrs
                if tag == "input":
                    self.inputs.append(attrs)

            def handle_endtag(self, tag):
                if tag == "section" and self.in_help:
                    self.in_help = False

            def handle_data(self, data):
                if self.in_help:
                    self.help_text.append(data)

        parser = LoginFormParser()
        parser.feed(response.get_data(as_text=True))
        help_text = "".join(parser.help_text)
        for expected in (
                "登录帮助", "管理员、教师和学生共用当前登录入口",
                "账号由管理员或教师发放", "忘记密码请联系管理员",
                "教师也可联系管理员"):
            with self.subTest(expected=expected):
                self.assertIn(expected, help_text)

        self.assertIsNotNone(parser.form)
        by_name = {field.get("name"): field for field in parser.inputs}
        self.assertIn("required", by_name["username"])
        self.assertEqual(by_name["password"].get("type"), "password")
        self.assertIn("required", by_name["password"])
        self.assertEqual(by_name["_csrf"].get("type"), "hidden")
        self.assertTrue(by_name["_csrf"].get("value"))

    def test_failed_login_does_not_disclose_account_existence(self):
        page = self.client.get("/login")
        csrf = re.search(r'name="_csrf" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        unknown = self.client.post("/login", data={
            "_csrf": csrf, "username": "missing-user", "password": "anything",
        })
        wrong_password = self.client.post("/login", data={
            "_csrf": csrf, "username": "known-user", "password": "wrong-password",
        })
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(wrong_password.status_code, 200)
        self.assertEqual(unknown.get_data(as_text=True), wrong_password.get_data(as_text=True))
        self.assertIn("用户名或密码错误", unknown.get_data(as_text=True))
        for disclosure in ("用户不存在", "账号不存在", "未注册", "missing-user", "known-user"):
            self.assertNotIn(disclosure, unknown.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
