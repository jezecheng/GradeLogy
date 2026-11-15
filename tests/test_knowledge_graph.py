# -*- coding: utf-8 -*-
"""学科知识图谱：增量合并、出处回收、AI 抽取与路由的测试。"""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

try:
    import _env_bootstrap  # noqa: F401  确保 DATA_DIR 在 app 导入前就绪
except ImportError:
    from tests import _env_bootstrap  # noqa: F401

import app as app_module  # noqa: E402
import db as database  # noqa: E402
from utilities import kg_text, knowledge_graph as kg  # noqa: E402

_TABLES = ("kg_chunks", "kg_edges", "kg_nodes", "kg_materials", "knowledge_graphs",
           "tag_analyses", "question_tags", "questions", "submissions", "lectures",
           "exams", "exam_scores", "enrollments", "course_teachers", "courses",
           "users", "ai_usage_events", "ai_operations")


class KGLogicTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(database.SCHEMA)
        self.con.execute(
            "INSERT INTO knowledge_graphs(id,subject,name,created_by)"
            " VALUES(1,'化学','必修一',2)")
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def _material(self, filename="m.txt", sha="a" * 64):
        cur = self.con.execute(
            "INSERT INTO kg_materials(graph_id,filename,stored_name,sha256,uploaded_by)"
            " VALUES(1,?,?,?,2)", (filename, f"kg/1/{filename}", sha))
        self.con.commit()
        return cur.lastrowid

    def test_normalize_concept_merges_synonyms(self):
        self.assertEqual(kg_text.normalize_concept("氧化还原 反应"),
                         kg_text.normalize_concept("氧化还原反应"))
        self.assertNotEqual(kg_text.normalize_concept("氧化"),
                            kg_text.normalize_concept("还原"))

    def test_merge_is_incremental_and_idempotent(self):
        concepts = [
            {"name": "氧化还原反应", "category": "反应类型", "definition": "电子转移",
             "aliases": ["氧化还原"], "confidence": 0.8},
            {"name": "电子守恒", "category": "", "definition": "",
             "aliases": [], "confidence": 0.7},
        ]
        relations = [{"from": "电子守恒", "to": "氧化还原反应",
                      "relation": "prerequisite", "evidence": "配平依据",
                      "confidence": 0.9}]
        nodes, edges, new_nodes, new_edges = kg.merge_extraction(
            self.con, 1, 101, concepts, relations)
        self.assertEqual((nodes, edges, new_nodes, new_edges), (2, 1, 2, 1))
        # 再次合并同一批结果：节点/边不重复，但出处合并
        kg.merge_extraction(self.con, 1, 102, concepts, relations)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 2)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_edges").fetchone()[0], 1)
        node = self.con.execute(
            "SELECT * FROM kg_nodes WHERE name='氧化还原反应'").fetchone()
        self.assertIn("氧化还原", json.loads(node["aliases"]))
        self.assertEqual(node["source_materials"], ",101,102,")

    def test_merge_resolves_alias_to_existing_node(self):
        kg.merge_extraction(self.con, 1, 101, [
            {"name": "氧化还原反应", "category": "", "definition": "",
             "aliases": ["氧化还原"], "confidence": 0.8}], [])
        kg.merge_extraction(self.con, 1, 102, [
            {"name": "氧化还原", "category": "", "definition": "",
             "aliases": [], "confidence": 0.9},
            {"name": "电子守恒", "category": "", "definition": "",
             "aliases": [], "confidence": 0.9}], [])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 2)

    def test_delete_material_prunes_only_its_own_nodes_and_edges(self):
        m1 = self._material("a.txt", "a" * 64)
        m2 = self._material("b.txt", "b" * 64)
        kg.merge_extraction(self.con, 1, m1, [
            {"name": "氧化还原反应", "category": "", "definition": "",
             "aliases": [], "confidence": 0.8}], [])
        kg.merge_extraction(self.con, 1, m2, [
            {"name": "氧化还原反应", "category": "", "definition": "",
             "aliases": [], "confidence": 0.8},
            {"name": "电子守恒", "category": "", "definition": "",
             "aliases": [], "confidence": 0.8}],
            [{"from": "电子守恒", "to": "氧化还原反应",
              "relation": "prerequisite", "evidence": "依据", "confidence": 0.8}])
        material = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                                    (m2,)).fetchone()
        kg.delete_material(self.con, material)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 1)
        names = [r[0] for r in self.con.execute("SELECT name FROM kg_nodes")]
        self.assertEqual(names, ["氧化还原反应"])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_edges").fetchone()[0], 0)
        node = self.con.execute("SELECT * FROM kg_nodes").fetchone()
        self.assertEqual(node["source_materials"], f",{m1},")

    def test_analyze_batch_cleans_model_output(self):
        reply = json.dumps({
            "concepts": [{"name": "氧化还原反应", "category": "反应",
                          "definition": "电子转移", "aliases": "氧化还原",
                          "confidence": 0.9},
                         {"name": ""}],
            "relations": [{"from": "电子守恒", "to": "氧化还原反应",
                           "relation": "unknown", "evidence": "依据",
                           "confidence": 2}]}, ensure_ascii=False)
        with mock.patch.object(kg.llm, "chat",
                               return_value=(reply, "stop", "")):
            concepts, relations, error = kg.analyze_batch(
                ["资料正文"], "u", "k", "m")
        self.assertEqual(error, "")
        self.assertEqual(len(concepts), 1)
        self.assertEqual(concepts[0]["aliases"], ["氧化还原"])
        self.assertEqual(relations[0]["relation"], "related")
        self.assertEqual(relations[0]["confidence"], 1.0)

    def test_analyze_batch_reports_invalid_json(self):
        with mock.patch.object(kg.llm, "chat",
                               return_value=("not json", "stop", "")):
            concepts, relations, error = kg.analyze_batch(["x"], "u", "k", "m")
        self.assertEqual((concepts, relations), ([], []))
        self.assertIn("JSON", error)

    def test_extract_and_build_material_from_text(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("氧化还原反应的实质是电子转移。电子守恒是配平的基础。" * 20)
        try:
            material_id = self._material()
            material = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                                        (material_id,)).fetchone()
            chunks, _ = kg.extract_material(self.con, material, path)
            self.assertGreater(chunks, 0)
            row = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                                   (material_id,)).fetchone()
            self.assertEqual(row["status"], kg.STATUS_PENDING)
            self.assertGreater(row["char_count"], 0)
            concepts = [{"name": "氧化还原反应", "category": "", "definition": "",
                         "aliases": [], "confidence": 0.8}]
            relations = [{"from": "电子守恒", "to": "氧化还原反应",
                          "relation": "prerequisite", "evidence": "", "confidence": 0.8}]
            with mock.patch.object(kg, "analyze_batch",
                                   return_value=(concepts, relations, "")):
                result = kg.build_material(self.con, material_id, "u", "k", "m")
            self.assertEqual(result["new_nodes"], 1)
            self.assertEqual(self.con.execute(
                "SELECT status FROM kg_materials WHERE id=?",
                (material_id,)).fetchone()[0], kg.STATUS_DONE)
        finally:
            os.remove(path)

    def test_extract_material_without_text_marks_failed(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("   \n\n  ")
        try:
            material_id = self._material()
            material = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                                        (material_id,)).fetchone()
            chunks, _ = kg.extract_material(self.con, material, path)
            self.assertEqual(chunks, 0)
            row = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                                   (material_id,)).fetchone()
            self.assertEqual(row["status"], kg.STATUS_FAILED)
            self.assertIn("扫描版", row["error"])
        finally:
            os.remove(path)

    def test_chunk_pages_keeps_page_numbers(self):
        chunks = kg_text.chunk_pages([(1, "甲" * 5000), (2, "乙" * 100)])
        self.assertEqual(chunks[0][0], 1)
        self.assertEqual(chunks[-1][0], 2)
        self.assertTrue(all(len(text) <= kg_text.CHUNK_CHARS for _p, text in chunks))


class KGRouteTest(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self._clear()
        self._seed()
        self.client = self.login(2)

    def tearDown(self):
        self._clear()
        shutil.rmtree(os.path.join(app_module.UPLOAD_DIR, "kg"), ignore_errors=True)

    @staticmethod
    def _db():
        con = sqlite3.connect(app_module.DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    def _clear(self):
        con = self._db()
        for table in _TABLES:
            con.execute(f"DELETE FROM {table}")
        con.commit()
        con.close()

    def _seed(self):
        con = self._db()
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(?,?, 'x',?,?,1,0)",
            [(1, "admin", "管理员", "admin"),
             (2, "teacher", "老师", "teacher"),
             (3, "other", "别的老师", "teacher")])
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

    def _create_graph(self):
        self._post("knowledge_graph_create",
                   {"subject": "高中化学", "name": "必修一知识图谱"})
        con = self._db()
        graph = con.execute("SELECT * FROM knowledge_graphs ORDER BY id DESC"
                            ).fetchone()
        con.close()
        return graph

    def test_create_and_view_graph(self):
        graph = self._create_graph()
        self.assertEqual(graph["subject"], "高中化学")
        self.assertEqual(graph["created_by"], 2)
        resp = self.client.get(self.url("knowledge_graph_detail", gid=graph["id"]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("必修一知识图谱", resp.get_data(as_text=True))
        listing = self.client.get(self.url("knowledge_graphs"))
        self.assertEqual(listing.status_code, 200)
        self.assertIn("高中化学", listing.get_data(as_text=True))

    def test_create_requires_subject_and_name(self):
        self._post("knowledge_graph_create", {"subject": "", "name": "x"})
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM knowledge_graphs").fetchone()[0], 0)
        con.close()

    def test_upload_text_builds_chunks_and_starts_background(self):
        graph = self._create_graph()
        content = ("氧化还原反应的实质是电子转移。" * 30).encode("utf-8")
        with mock.patch.object(app_module, "_kg_start_build") as start:
            resp = self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(content), "教材.txt")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        start.assert_called_once()
        con = self._db()
        material = con.execute("SELECT * FROM kg_materials").fetchone()
        chunks = con.execute("SELECT COUNT(*) FROM kg_chunks").fetchone()[0]
        con.close()
        self.assertEqual(material["filename"], "教材.txt")
        self.assertEqual(material["status"], kg.STATUS_PENDING)
        self.assertGreater(chunks, 0)
        self.assertTrue(material["stored_name"].startswith(f"kg/{graph['id']}/"))
        self.assertTrue(os.path.exists(
            os.path.join(app_module.UPLOAD_DIR, material["stored_name"])))

    def test_upload_skips_duplicate_content(self):
        graph = self._create_graph()
        content = b"same content"
        with mock.patch.object(app_module, "_kg_start_build"):
            for _ in range(2):
                self.client.post(
                    self.url("knowledge_graph_upload", gid=graph["id"]),
                    data={"_csrf": "test-token",
                          "files": (io.BytesIO(content), "same.txt")},
                    content_type="multipart/form-data")
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 1)
        con.close()

    def test_upload_rejects_unsupported_extension(self):
        graph = self._create_graph()
        with mock.patch.object(app_module, "_kg_start_build") as start:
            self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"docx"), "讲义.docx")},
                content_type="multipart/form-data")
        start.assert_not_called()
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 0)
        con.close()

    def test_other_teacher_can_view_but_not_upload(self):
        graph = self._create_graph()
        other = self.login(3)
        resp = other.get(self.url("knowledge_graph_detail", gid=graph["id"]))
        self.assertEqual(resp.status_code, 200)
        with mock.patch.object(app_module, "_kg_start_build"):
            resp = other.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"x"), "notes.txt")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 403)

    def test_delete_material_removes_file_chunks_and_nodes(self):
        graph = self._create_graph()
        con = self._db()
        abs_dir = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]))
        os.makedirs(abs_dir, exist_ok=True)
        path = os.path.join(abs_dir, "x.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("氧化还原")
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,status,"
            "uploaded_by) VALUES(50,?,?,?,?,?,2)",
            (graph["id"], "x.txt", f"kg/{graph['id']}/x.txt", "a" * 64, "完成"))
        con.execute("INSERT INTO kg_chunks(graph_id,material_id,content)"
                    " VALUES(?,50,'氧化还原')", (graph["id"],))
        con.execute(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized,source_materials)"
            " VALUES(60,?,'氧化还原','氧化还原',',50,')", (graph["id"],))
        con.commit()
        con.close()
        resp = self._post("knowledge_graph_material_delete", mid=50)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(os.path.exists(path))
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 0)
        con.close()

    def test_delete_graph_cleans_database_and_files(self):
        graph = self._create_graph()
        abs_dir = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]))
        os.makedirs(abs_dir, exist_ok=True)
        with open(os.path.join(abs_dir, "x.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        resp = self._post("knowledge_graph_delete", gid=graph["id"])
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(os.path.exists(abs_dir))
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM knowledge_graphs").fetchone()[0], 0)
        con.close()


if __name__ == "__main__":
    unittest.main()
