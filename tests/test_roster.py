# -*- coding: utf-8 -*-
"""用户名拼音生成规则单元测试。

运行：.venv/bin/python -m unittest discover -s tests -v
"""
import unittest

from utilities import roster


class UsernameFromNameTest(unittest.TestCase):
    """姓取 zh/ch/sh 两字母或首字母，名取全拼；复姓保留完整拼音。"""

    def test_retroflex_surname_keeps_two_letters(self):
        # 用户明确给出的一对示例
        self.assertEqual(roster.username_from_name("程诺"), "chnuo")
        self.assertEqual(roster.username_from_name("张三"), "zhsan")
        self.assertEqual(roster.username_from_name("沈括"), "shkuo")

    def test_single_letter_surname(self):
        self.assertEqual(roster.username_from_name("李四"), "lsi")
        self.assertEqual(roster.username_from_name("王小明"), "wxiaoming")
        self.assertEqual(roster.username_from_name("张伟"), "zhwei")

    def test_compound_surname_keeps_full_pinyin(self):
        self.assertEqual(roster.username_from_name("欧阳修"), "ouyangxiu")
        self.assertEqual(roster.username_from_name("司马光"), "simaguang")

    def test_single_character_name(self):
        self.assertEqual(roster.username_from_name("李"), "l")

    def test_whitespace_is_ignored(self):
        self.assertEqual(roster.username_from_name(" 程 诺 "), "chnuo")
        self.assertEqual(roster.username_from_name("张\u3000三"), "zhsan")

    def test_latin_name_is_lowercased(self):
        self.assertEqual(roster.username_from_name("Tom"), "tom")
        self.assertEqual(roster.username_from_name("Alice Wang"), "alicewang")

    def test_empty_input_returns_empty_string(self):
        for value in ("", "   ", "\u3000", None):
            with self.subTest(value=value):
                self.assertEqual(roster.username_from_name(value), "")

    def test_output_is_lowercase_and_url_safe(self):
        for name in ("程诺", "张三", "李四", "欧阳修", "王小明", "Tom"):
            with self.subTest(name=name):
                username = roster.username_from_name(name)
                self.assertRegex(username, roster.USERNAME_RE)
                self.assertEqual(username, username.lower())
                self.assertNotIn(" ", username)


if __name__ == "__main__":
    unittest.main()
