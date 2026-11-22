"""学生薄弱点生成规则：全对不建档、跨讲累计、连续全对自动收敛。"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as a


class WeakPointRuleTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="gradelogy-weak-")
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.path = str(self.root / "data.sqlite")
        for obj, attr, value in (
                (a, "DB_PATH", self.path), (a.database, "DB_PATH", self.path),
                (a, "UPLOAD_DIR", str(self.root / "uploads")),
                (a.cfg, "DATA_DIR", str(self.root)),
                (a.cfg, "DB_PATH", self.path),
                (a.cfg, "STORAGE_DIR", str(self.root / "uploads")),
                (a.cfg, "BACKUP_DIR", str(self.root / "backups"))):
            self.enterContext(mock.patch.object(obj, attr, value))
        a.cfg.ensure_dirs()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(a.database.SCHEMA)
        self.db.execute("DELETE FROM users")
        self.db.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role)"
            " VALUES(?,?,'x',?,?)",
            [(2, "t", "Teacher", "teacher"), (4, "s", "Student", "student")])
        self.db.execute("INSERT INTO courses(id,name,teacher_id) VALUES(10,'化学',2)")
        self.db.execute("INSERT INTO course_teachers VALUES(10,2,CURRENT_TIMESTAMP)")
        self.db.execute("INSERT INTO enrollments(course_id,student_id) VALUES(10,4)")
        # 两讲使用同一组标签，便于验证跨讲累计与收敛
        for lid in (20, 21, 22):
            self.db.execute(
                "INSERT INTO lectures(id,course_id,title) VALUES(?,10,?)",
                (lid, f"第{lid}讲"))
            self.db.executemany(
                "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
                " VALUES(?,?,?,?,'ai')",
                [(lid, "q0", "3", "氧化还原反应"), (lid, "q1", "4", "离子方程式")])
        self.db.commit()
        a.app.config.update(TESTING=True)

    def _submit(self, sid, lecture_id, verdicts):
        self.db.execute(
            "INSERT INTO submissions(id,lecture_id,student_id,file_path,status,report_published)"
            " VALUES(?,?,4,'x.pdf','已批改',1)", (sid, lecture_id))
        for no, verdict in verdicts.items():
            self.db.execute(
                "INSERT INTO questions(submission_id,no,verdict,reviewed)"
                " VALUES(?,?,?,1)", (sid, no, verdict))
        self.db.commit()
        sub = self.db.execute(
            "SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        a._update_student_weak_points(self.db, sub)

    def _rows(self):
        return {r["tag"]: r for r in self.db.execute(
            "SELECT * FROM student_weak_points WHERE student_id=4")}

    def test_all_correct_creates_no_weak_point(self):
        self._submit(30, 20, {"3": "对", "4": "对"})
        self.assertEqual(self._rows(), {})

    def test_only_wrong_tags_are_created(self):
        self._submit(30, 20, {"3": "错", "4": "对"})
        rows = self._rows()
        self.assertEqual(list(rows), ["氧化还原反应"])
        row = rows["氧化还原反应"]
        self.assertEqual((row["wrong_count"], row["right_count"], row["total_count"]),
                         (1, 0, 1))
        self.assertEqual(row["status"], "weak")

    def test_counts_accumulate_across_lectures(self):
        self._submit(30, 20, {"3": "错", "4": "对"})
        self._submit(31, 21, {"3": "对", "4": "对"})
        row = self._rows()["氧化还原反应"]
        self.assertEqual((row["wrong_count"], row["right_count"], row["total_count"]),
                         (1, 1, 2))
        self.assertEqual(row["right_streak"], 1)

    def test_consecutive_all_correct_auto_reviews(self):
        self._submit(30, 20, {"3": "错"})
        self._submit(31, 21, {"3": "对"})
        self.assertEqual(self._rows()["氧化还原反应"]["status"], "weak")
        self._submit(32, 22, {"3": "对"})
        row = self._rows()["氧化还原反应"]
        self.assertEqual(row["status"], "reviewed")
        self.assertEqual(row["right_streak"], 2)

    def test_error_in_later_lecture_resets_streak_and_status(self):
        self._submit(30, 20, {"3": "错"})
        self._submit(31, 21, {"3": "对"})
        self._submit(32, 22, {"3": "半对"})
        row = self._rows()["氧化还原反应"]
        self.assertEqual(row["status"], "weak")
        self.assertEqual(row["right_streak"], 0)
        self.assertEqual(row["partial_count"], 1)

    def test_galaxy_keeps_course_without_graph(self):
        self._submit(30, 20, {"3": "错"})
        client = a.app.test_client()
        with client.session_transaction() as session:
            session["uid"] = 4
            session["auth_epoch"] = a.session_epoch(str(self.root))
        data = client.get("/student/weak-points/galaxy.json").get_json()
        self.assertEqual(len(data["courses"]), 1)
        self.assertEqual(data["courses"][0]["name"], "化学")
        self.assertEqual(data["courses"][0]["nodes"], [])
        self.assertEqual(data["courses"][0]["unmatched"], 1)


class TagNodeBindingTest(unittest.TestCase):
    """标签到图谱节点：归一化匹配、教师手工绑定与历史薄弱点同步。"""

    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="gradelogy-taglink-")
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.path = str(self.root / "data.sqlite")
        for obj, attr, value in (
                (a, "DB_PATH", self.path), (a.database, "DB_PATH", self.path),
                (a, "UPLOAD_DIR", str(self.root / "uploads")),
                (a.cfg, "DATA_DIR", str(self.root)),
                (a.cfg, "DB_PATH", self.path),
                (a.cfg, "STORAGE_DIR", str(self.root / "uploads")),
                (a.cfg, "BACKUP_DIR", str(self.root / "backups"))):
            self.enterContext(mock.patch.object(obj, attr, value))
        a.cfg.ensure_dirs()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(a.database.SCHEMA)
        self.db.execute("DELETE FROM users")
        self.db.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role)"
            " VALUES(?,?,'x',?,?)",
            [(2, "t", "Teacher", "teacher"), (4, "s", "Student", "student")])
        self.db.execute("INSERT INTO courses(id,name,teacher_id) VALUES(10,'化学',2)")
        self.db.execute("INSERT INTO course_teachers VALUES(10,2,CURRENT_TIMESTAMP)")
        self.db.execute("INSERT INTO enrollments(course_id,student_id) VALUES(10,4)")
        self.db.execute("INSERT INTO lectures(id,course_id,title) VALUES(20,10,'第一讲')")
        self.db.executemany(
            "INSERT INTO question_tags(lecture_id,question_key,question_no,tag,source)"
            " VALUES(20,'q0','3',?,'ai')",
            [("氧化还原反应",), ("离子方程式（书写）",), ("化学平衡",)])
        self.db.execute(
            "INSERT INTO knowledge_graphs(id,subject,name) VALUES(5,'化学','高中化学')")
        self.db.execute(
            "INSERT INTO course_knowledge_graphs(course_id,graph_id) VALUES(10,5)")
        self.db.executemany(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized,definition)"
            " VALUES(?,5,?,?,?)",
            [(50, "氧化还原反应", "氧化还原反应", "电子转移"),
             (51, "离子方程式", "离子方程式", "离子反应"),
             (52, "化学平衡常数", "化学平衡常数", "平衡常数")])
        self.db.commit()
        a.app.config.update(TESTING=True)

    def _client(self):
        client = a.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = 2
            sess["_csrf"] = "test-token"
            sess["auth_epoch"] = a.session_epoch(str(self.root))
        return client

    @staticmethod
    def _url(endpoint, **values):
        with a.app.test_request_context():
            return a.url_for(endpoint, **values)

    def test_match_node_normalizes_brackets_and_spaces(self):
        nodes = self.db.execute("SELECT * FROM kg_nodes WHERE graph_id=5").fetchall()
        name_to_node = {n["name"]: n for n in nodes}
        node, kind = a._kg_match_node(nodes, name_to_node, "离子方程式 （书写）")
        self.assertEqual(node["id"], 51)
        self.assertIn(kind, ("normalized", "tag_contains_node", "node_contains_tag"))

    def test_match_node_rejects_ambiguous_containment(self):
        self.db.execute(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized)"
            " VALUES(53,5,'化学平衡移动','化学平衡移动')")
        self.db.commit()
        nodes = self.db.execute("SELECT * FROM kg_nodes WHERE graph_id=5").fetchall()
        name_to_node = {n["name"]: n for n in nodes}
        node, kind = a._kg_match_node(nodes, name_to_node, "化学平衡")
        self.assertIsNone(node)
        self.assertEqual(kind, "no_match")

    def test_binding_page_lists_tags_and_auto_match(self):
        resp = self._client().get(self._url("course_tag_links", cid=10))
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("氧化还原反应", body)
        self.assertIn("离子方程式", body)
        self.assertIn("未匹配", body)

    def test_binding_saves_and_syncs_existing_weak_points(self):
        self.db.execute(
            "INSERT INTO student_weak_points(student_id,course_id,tag,node_name)"
            " VALUES(4,10,'化学平衡','')")
        self.db.commit()
        resp = self._client().post(self._url("course_tag_links_save", cid=10), data={
            "_csrf": "test-token",
            "tag": ["氧化还原反应", "离子方程式（书写）", "化学平衡"],
            "node": ["0", "51", "52"],
        })
        self.assertEqual(resp.status_code, 302)
        links = {r["tag"]: r["node_id"] for r in self.db.execute(
            "SELECT tag,node_id FROM tag_node_links WHERE course_id=10")}
        self.assertEqual(links, {"离子方程式（书写）": 51, "化学平衡": 52})
        row = self.db.execute(
            "SELECT node_id,node_name FROM student_weak_points"
            " WHERE course_id=10 AND tag='化学平衡'").fetchone()
        self.assertEqual((row["node_id"], row["node_name"]), (52, "化学平衡常数"))

    def test_unbinding_removes_link(self):
        self.db.execute(
            "INSERT INTO tag_node_links(course_id,tag,graph_id,node_id) VALUES(10,'化学平衡',5,52)")
        self.db.commit()
        resp = self._client().post(self._url("course_tag_links_save", cid=10), data={
            "_csrf": "test-token", "tag": ["化学平衡"], "node": ["0"]})
        self.assertEqual(resp.status_code, 302)
        remaining = self.db.execute(
            "SELECT COUNT(*) FROM tag_node_links WHERE course_id=10").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_binding_ignores_nodes_from_other_courses_graphs(self):
        self.db.execute(
            "INSERT INTO knowledge_graphs(id,subject,name) VALUES(6,'物理','高中物理')")
        self.db.execute(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized) VALUES(60,6,'牛顿定律','牛顿定律')")
        self.db.commit()
        self._client().post(self._url("course_tag_links_save", cid=10), data={
            "_csrf": "test-token", "tag": ["化学平衡"], "node": ["60"]})
        count = self.db.execute(
            "SELECT COUNT(*) FROM tag_node_links WHERE course_id=10").fetchone()[0]
        self.assertEqual(count, 0)

    def _student_galaxy(self):
        client = a.app.test_client()
        with client.session_transaction() as sess:
            sess["uid"] = 4
            sess["auth_epoch"] = a.session_epoch(str(self.root))
        return client.get("/student/weak-points/galaxy.json").get_json()

    def _seed_weak_node(self, node_id, **counts):
        values = {"right_count": 0, "partial_count": 0, "wrong_count": 0,
                  "total_count": 0}
        values.update(counts)
        self.db.execute(
            "INSERT INTO student_weak_points(student_id,course_id,graph_id,node_id,"
            "tag,node_name,right_count,partial_count,wrong_count,total_count)"
            " VALUES(4,10,5,?,?,?,?,?,?,?)",
            (node_id, "氧化还原反应", "氧化还原反应", values["right_count"],
             values["partial_count"], values["wrong_count"], values["total_count"]))
        self.db.commit()

    def _seed_edge(self, src, dst, relation="prerequisite"):
        self.db.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation) VALUES(5,?,?,?)",
            (src, dst, relation))

    def test_galaxy_includes_context_edges_and_stats(self):
        self._seed_weak_node(50, right_count=1, partial_count=1, wrong_count=1,
                             total_count=3)
        self._seed_edge(50, 51)
        self._seed_edge(50, 52, "related")
        self._seed_edge(51, 52, "contains")
        self.db.commit()
        course = self._student_galaxy()["courses"][0]
        nodes = {n["id"]: n for n in course["nodes"]}
        self.assertEqual(set(nodes), {50, 51, 52})
        self.assertEqual(course["context_total"], 2)
        self.assertEqual(nodes[50]["error_rate"], 50)
        self.assertTrue(nodes[50]["weak"])
        self.assertFalse(nodes[51]["weak"])
        pairs = {(e["src"], e["dst"]) for e in course["edges"]}
        self.assertIn((51, 52), pairs)  # 背景知识之间的边也被补全

    def test_galaxy_caps_context_neighbors_per_weak_node(self):
        self._seed_weak_node(50, wrong_count=1, total_count=1)
        for i in range(8):
            nid = 100 + i
            self.db.execute(
                "INSERT INTO kg_nodes(id,graph_id,name,normalized)"
                " VALUES(?,5,?,?)", (nid, f"背景{i}", f"背景{i}"))
            self._seed_edge(50, nid)
        self.db.commit()
        course = self._student_galaxy()["courses"][0]
        self.assertEqual(course["context_total"], a.MAX_CONTEXT_PER_WEAK)
        self.assertEqual(len(course["nodes"]), a.MAX_CONTEXT_PER_WEAK + 1)


if __name__ == "__main__":
    unittest.main()
