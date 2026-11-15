# -*- coding: utf-8 -*-
"""学科知识图谱：增量合并、出处回收、AI 抽取与路由的测试。"""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
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

    def test_analyze_batch_reports_truncated_output(self):
        with mock.patch.object(kg.llm, "chat",
                               return_value=("", "length", "")):
            concepts, relations, error = kg.analyze_batch(["x"], "u", "k", "m")
        self.assertEqual((concepts, relations), ([], []))
        self.assertTrue(error.startswith(kg.TRUNCATED_MARK))

    def test_build_material_splits_truncated_batch(self):
        material_id = self._material()
        for number in range(2):
            self.con.execute(
                "INSERT INTO kg_chunks(graph_id,material_id,chunk_no,content)"
                " VALUES(1,?,?,?)", (material_id, number, "乙" * 200))
        self.con.commit()
        concepts = [{"name": "氧化还原反应", "category": "", "definition": "",
                     "aliases": [], "confidence": 0.8}]
        truncated = ([], [], f"{kg.TRUNCATED_MARK}（max_tokens=32768）")
        with mock.patch.object(kg, "analyze_batch",
                               side_effect=[truncated,
                                            (concepts, [], ""),
                                            (concepts, [], "")]) as mocked:
            result = kg.build_material(self.con, material_id, "u", "k", "m")
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["new_nodes"], 1)
        row = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                               (material_id,)).fetchone()
        self.assertEqual(row["status"], kg.STATUS_DONE)

    def _node(self, name, category="概念"):
        cur = self.con.execute(
            "INSERT INTO kg_nodes(graph_id,name,normalized,category,aliases,definition,confidence)"
            " VALUES(1,?,?,?,?,?,0.9)", (name, name, category, "[]", f"{name}的定义"))
        self.con.commit()
        return cur.lastrowid

    def _edge(self, src, dst, relation="prerequisite"):
        self.con.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,evidence,confidence)"
            " VALUES(1,?,?,?,?,0.8)", (src, dst, relation, ""))
        self.con.commit()

    def test_layered_graph_orders_prerequisites_by_layer(self):
        basic = self._node("物质分类")
        middle = self._node("物质的量")
        advanced = self._node("物质的量浓度")
        self._edge(basic, middle)
        self._edge(middle, advanced)
        layout = kg.layered_graph(self.con, 1)
        position = {node["name"]: node for node in layout["nodes"]}
        self.assertLess(position["物质分类"]["y"], position["物质的量"]["y"])
        self.assertLess(position["物质的量"]["y"], position["物质的量浓度"]["y"])
        self.assertEqual(len(layout["edges"]), 2)
        self.assertFalse(layout["truncated"])
        self.assertGreaterEqual(layout["width"], 320)

    def test_layered_graph_handles_cycles(self):
        first = self._node("甲")
        second = self._node("乙")
        self._edge(first, second)
        self._edge(second, first)
        layout = kg.layered_graph(self.con, 1)
        self.assertTrue(layout["has_cycle"])
        self.assertEqual(len(layout["nodes"]), 2)

    def test_layered_graph_limits_nodes(self):
        for number in range(5):
            self._node(f"概念{number}")
        layout = kg.layered_graph(self.con, 1, limit=3)
        self.assertTrue(layout["truncated"])
        self.assertEqual(len(layout["nodes"]), 3)
        self.assertEqual(layout["hidden_nodes"], 2)

    def test_concept_neighbors_groups_directions(self):
        basic = self._node("物质分类")
        current = self._node("物质的量")
        advanced = self._node("物质的量浓度")
        self._edge(basic, current)
        self._edge(current, advanced)
        self._edge(advanced, current, "related")
        detail = kg.concept_neighbors(self.con, 1, current)
        self.assertEqual([item["name"] for item in detail["prerequisites"]],
                         ["物质分类"])
        self.assertEqual([item["name"] for item in detail["dependents"]],
                         ["物质的量浓度"])
        self.assertEqual(len(detail["others"]), 1)
        self.assertEqual(detail["others"][0]["direction"], "←")
        self.assertIsNone(kg.concept_neighbors(self.con, 1, 999))

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

    def test_relations_to_unknown_nodes_and_self_loops_are_ignored(self):
        nodes, edges, _new_nodes, _new_edges = kg.merge_extraction(
            self.con, 1, 101,
            [{"name": "氧化还原反应", "category": "", "definition": "",
              "aliases": [], "confidence": 0.8}],
            [{"from": "氧化还原反应", "to": "未收录概念", "relation": "related",
              "evidence": "", "confidence": 0.5},
             {"from": "氧化还原反应", "to": "氧化还原反应", "relation": "related",
              "evidence": "", "confidence": 0.5}])
        self.assertEqual((nodes, edges), (1, 0))

    def test_build_material_marks_failed_when_no_concepts(self):
        material_id = self._material()
        self.con.execute("INSERT INTO kg_chunks(graph_id,material_id,content)"
                         " VALUES(1,?,'内容')", (material_id,))
        self.con.commit()
        with mock.patch.object(kg, "analyze_batch", return_value=([], [], "")):
            result = kg.build_material(self.con, material_id, "u", "k", "m")
        self.assertIsNone(result)
        row = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                               (material_id,)).fetchone()
        self.assertEqual(row["status"], kg.STATUS_FAILED)
        self.assertIn("未抽取到任何概念", row["error"])

    def test_build_material_records_partial_batch_failure(self):
        material_id = self._material()
        for number in range(2):
            self.con.execute(
                "INSERT INTO kg_chunks(graph_id,material_id,chunk_no,content)"
                " VALUES(1,?,?,?)", (material_id, number, "甲" * 9000))
        self.con.commit()
        concepts = [{"name": "氧化还原反应", "category": "", "definition": "",
                     "aliases": [], "confidence": 0.8}]
        with mock.patch.object(kg, "analyze_batch",
                               side_effect=[(concepts, [], ""), ([], [], "超时")]):
            result = kg.build_material(self.con, material_id, "u", "k", "m")
        self.assertEqual(result["new_nodes"], 1)
        self.assertEqual(result["errors"], 1)
        row = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                               (material_id,)).fetchone()
        self.assertEqual(row["status"], kg.STATUS_DONE)
        self.assertIn("部分片段失败", row["error"])

    def test_build_material_without_chunks_marks_failed(self):
        material_id = self._material()
        self.assertIsNone(kg.build_material(self.con, material_id, "u", "k", "m"))
        row = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                               (material_id,)).fetchone()
        self.assertEqual(row["status"], kg.STATUS_FAILED)
        self.assertIn("没有可用的资料文本", row["error"])

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
             (3, "other", "别的老师", "teacher"),
             (4, "student", "学生", "student")])
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

    def test_detail_shows_batch_progress_and_status_badges(self):
        graph = self._create_graph()
        con = self._db()
        for number, status in enumerate(
                [kg.STATUS_PENDING, kg.STATUS_BUILDING,
                 kg.STATUS_DONE, kg.STATUS_FAILED]):
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
                "status,uploaded_by) VALUES(?,?,?,?,?,?,?)",
                (90 + number, graph["id"], f"第{number}.pdf",
                 f"kg/{graph['id']}/{number}.pdf", str(number) * 64,
                 status, 2))
        con.commit()
        con.close()
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("待处理", html)
        self.assertIn("处理中", html)
        self.assertIn("已完成", html)
        self.assertIn("处理失败", html)
        self.assertIn('data-status-count="done">1</b>', html)
        self.assertIn('data-status-count="pending">1</b>', html)
        self.assertIn('data-status-count="building">1</b>', html)
        self.assertIn('data-status-count="failed">1</b>', html)
        self.assertIn('name="files" multiple', html)
        self.assertIn("file-icon pdf", html)

    def test_build_worker_serializes_queued_materials(self):
        graph = self._create_graph()
        con = self._db()
        for number in range(2):
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
                "status,uploaded_by) VALUES(?,?,?,?,?,?,?)",
                (95 + number, graph["id"], f"{number}.txt",
                 f"kg/{graph['id']}/{number}.txt", str(number) * 64,
                 kg.STATUS_PENDING, 2))
        con.commit()
        con.close()
        state = {"active": 0, "peak": 0}
        state_lock = threading.Lock()

        def fake_build(_db, _material_id, *_args, **_kwargs):
            with state_lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.2)
            with state_lock:
                state["active"] -= 1
            return {"nodes": 0, "edges": 0, "new_nodes": 0,
                    "new_edges": 0, "errors": 0}

        def run(material_id):
            app_module._kg_build_worker(graph["id"], material_id, 2)

        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("u", "k", "m", "")), \
                mock.patch.object(app_module, "_start_ai_operation",
                                  return_value={"usage_hook": None}), \
                mock.patch.object(app_module, "_finish_ai_operation"), \
                mock.patch.object(app_module.kg_utils, "build_material",
                                  side_effect=fake_build):
            threads = [threading.Thread(target=run, args=(mid,))
                       for mid in (95, 96)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(state["peak"], 1)

    def test_detail_renders_layered_svg_and_concept_links(self):
        graph = self._create_graph()
        basic = self._concept(graph["id"], "物质分类")
        middle = self._concept(graph["id"], "物质的量")
        advanced = self._concept(graph["id"], "物质的量浓度")
        self._link(graph["id"], basic, middle)
        self._link(graph["id"], middle, advanced)
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("前置关系分层图", html)
        self.assertIn("<svg", html)
        self.assertIn('class="kg-node"', html)
        self.assertIn("kg-arrow", html)
        self.assertIn(self.url("knowledge_graph_concept", gid=graph["id"],
                               nid=middle), html)

    def test_concept_page_shows_neighbors(self):
        graph = self._create_graph()
        basic = self._concept(graph["id"], "物质分类")
        middle = self._concept(graph["id"], "物质的量")
        advanced = self._concept(graph["id"], "物质的量浓度")
        self._link(graph["id"], basic, middle)
        self._link(graph["id"], middle, advanced)
        resp = self.client.get(
            self.url("knowledge_graph_concept", gid=graph["id"], nid=middle))
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("物质分类", html)
        self.assertIn("物质的量浓度", html)
        self.assertIn("直接前置", html)
        self.assertIn("直接延伸", html)
        self.assertIn("教材原句", html)
        self.assertIn("物质的量的定义", html)

    def test_concept_page_rejects_node_from_other_graph(self):
        first = self._create_graph()
        second = self._create_graph()
        node_id = self._concept(first["id"], "物质的量")
        resp = self.client.get(
            self.url("knowledge_graph_concept", gid=second["id"], nid=node_id))
        self.assertEqual(resp.status_code, 404)

    def test_student_cannot_open_concept_page(self):
        graph = self._create_graph()
        node_id = self._concept(graph["id"], "物质的量")
        resp = self.login(4).get(
            self.url("knowledge_graph_concept", gid=graph["id"], nid=node_id))
        self.assertEqual(resp.status_code, 403)

    def _concept(self, graph_id, name, category="概念"):
        con = self._db()
        cur = con.execute(
            "INSERT INTO kg_nodes(graph_id,name,normalized,category,aliases,definition,confidence)"
            " VALUES(?,?,?,?,?,?,0.9)", (graph_id, name, name, category, "[]", f"{name}的定义"))
        con.commit()
        con.close()
        return cur.lastrowid

    def _link(self, graph_id, src, dst, relation="prerequisite"):
        con = self._db()
        con.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,evidence,confidence)"
            " VALUES(?,?,?,?,?,0.8)", (graph_id, src, dst, relation, "教材原句"))
        con.commit()
        con.close()

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

    def test_list_page_wires_dialog_script(self):
        """回归：新建图谱按钮必须绑定弹窗脚本，否则点击无反应。"""
        resp = self.client.get(self.url("knowledge_graphs"))
        html = resp.get_data(as_text=True)
        self.assertIn('data-dialog-open="kg-create"', html)
        self.assertIn('id="kg-create"', html)
        self.assertIn("showModal", html)
        self.assertIn("data-dialog-close", html)

    def test_guest_is_redirected_to_login(self):
        guest = app_module.app.test_client()
        resp = guest.get(self.url("knowledge_graphs"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_student_cannot_access_knowledge_graphs(self):
        student = self.login(4)
        self.assertEqual(student.get(self.url("knowledge_graphs")).status_code, 403)

    def test_admin_can_upload_to_others_graph(self):
        graph = self._create_graph()
        admin = self.login(1)
        with mock.patch.object(app_module, "_kg_start_build") as start:
            resp = admin.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"admin upload"), "notes.txt")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        start.assert_called_once()
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 1)
        con.close()

    def test_other_teacher_cannot_delete_material(self):
        graph = self._create_graph()
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
            "uploaded_by) VALUES(70,?,?,?,?,2)",
            (graph["id"], "x.txt", f"kg/{graph['id']}/x.txt", "b" * 64))
        con.commit()
        con.close()
        other = self.login(3)
        resp = other.post(self.url("knowledge_graph_material_delete", mid=70),
                          data={"_csrf": "test-token"})
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 1)
        con.close()

    def test_other_teacher_cannot_delete_graph(self):
        graph = self._create_graph()
        other = self.login(3)
        resp = other.post(self.url("knowledge_graph_delete", gid=graph["id"]),
                          data={"_csrf": "test-token"})
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM knowledge_graphs").fetchone()[0], 1)
        con.close()

    def test_mixed_upload_imports_only_supported_files(self):
        graph = self._create_graph()
        with mock.patch.object(app_module, "_kg_start_build") as start:
            self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": [(io.BytesIO(b"good"), "good.txt"),
                                (io.BytesIO(b"bad"), "bad.docx")]},
                content_type="multipart/form-data")
        self.assertEqual(start.call_count, 1)
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 1)
        con.close()

    def test_empty_text_file_fails_without_starting_build(self):
        graph = self._create_graph()
        with mock.patch.object(app_module, "_kg_start_build") as start:
            resp = self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"   \n\n "), "blank.txt")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        start.assert_not_called()
        con = self._db()
        row = con.execute("SELECT * FROM kg_materials").fetchone()
        con.close()
        self.assertEqual(row["status"], kg.STATUS_FAILED)
        self.assertIn("扫描版", row["error"])

    def test_oversized_upload_is_rejected_before_processing(self):
        graph = self._create_graph()
        with mock.patch.object(app_module.cfg, "KG_MAX_UPLOAD_MB", 0.000001), \
                mock.patch.object(app_module, "_kg_start_build") as start:
            resp = self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"payload"), "big.txt")},
                content_type="multipart/form-data")
        # 知识图谱资料用独立上限，超限时全局请求体拦截先于路由生效
        self.assertEqual(resp.status_code, 413)
        start.assert_not_called()
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 0)
        con.close()
        abs_dir = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]))
        self.assertEqual(os.listdir(abs_dir) if os.path.isdir(abs_dir) else [], [])

    def test_kg_upload_ignores_general_upload_cap(self):
        graph = self._create_graph()
        with mock.patch.object(app_module.cfg, "MAX_UPLOAD_MB", 0.000001), \
                mock.patch.object(app_module, "_kg_start_build") as start:
            resp = self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"\xe7\x9f\xa5\xe8\xaf\x86"), "a.txt")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        start.assert_called_once()
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 1)
        con.close()

    def test_status_endpoint_reports_progress(self):
        graph = self._create_graph()
        con = self._db()
        for number, status in enumerate([kg.STATUS_PENDING, kg.STATUS_DONE]):
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
                "status,uploaded_by,error) VALUES(?,?,?,?,?,?,?,?)",
                (70 + number, graph["id"], f"{number}.txt",
                 f"kg/{graph['id']}/{number}.txt", str(number) * 64,
                 status, 2, "出错了" if number else ""))
        con.commit()
        con.close()
        resp = self.client.get(self.url("knowledge_graph_status", gid=graph["id"]))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["building"])
        self.assertEqual(data["status_counts"]["pending"], 1)
        self.assertEqual(data["status_counts"]["done"], 1)
        self.assertEqual(len(data["materials"]), 2)
        self.assertEqual(data["materials"][0]["label"], "已完成")

        con = self._db()
        con.execute("UPDATE kg_materials SET status=? WHERE graph_id=?",
                    (kg.STATUS_DONE, graph["id"]))
        con.commit()
        con.close()
        data = self.client.get(
            self.url("knowledge_graph_status", gid=graph["id"])).get_json()
        self.assertFalse(data["building"])

    def test_detail_uses_polling_and_collapsible_sections(self):
        graph = self._create_graph()
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
            "status,uploaded_by) VALUES(?,?,?,?,?,?,?)",
            (80, graph["id"], "a.pdf", f"kg/{graph['id']}/a.pdf",
             "b" * 64, kg.STATUS_PENDING, 2))
        con.commit()
        con.close()
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("status.json", html)
        self.assertIn("data-material-id", html)
        self.assertIn("kg-refresh-hint", html)
        self.assertIn('<details class="card kg-fold"', html)
        self.assertIn("建图过程中也可以继续上传", html)
        self.assertNotIn("window.location.reload()", html)

    def test_background_worker_marks_failed_without_ai_config(self):
        graph = self._create_graph()
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
            "uploaded_by) VALUES(71,?,?,?,?,2)",
            (graph["id"], "x.txt", f"kg/{graph['id']}/x.txt", "c" * 64))
        con.commit()
        con.close()
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("", "", "", "未配置 AI")):
            app_module._kg_build_worker(graph["id"], 71, 2)
        con = self._db()
        row = con.execute("SELECT * FROM kg_materials WHERE id=71").fetchone()
        con.close()
        self.assertEqual(row["status"], kg.STATUS_FAILED)
        self.assertIn("未配置 AI", row["error"])

    def test_backup_includes_graph_tables_and_materials(self):
        graph = self._create_graph()
        abs_dir = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]))
        os.makedirs(abs_dir, exist_ok=True)
        with open(os.path.join(abs_dir, "source.txt"), "w", encoding="utf-8") as fh:
            fh.write("氧化还原")
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
            "uploaded_by) VALUES(72,?,?,?,?,2)",
            (graph["id"], "source.txt", f"kg/{graph['id']}/source.txt", "d" * 64))
        con.execute("INSERT INTO kg_nodes(id,graph_id,name,normalized,source_materials)"
                    " VALUES(80,?,'氧化还原反应','氧化还原反应',',72,')", (graph["id"],))
        con.execute("INSERT INTO kg_nodes(id,graph_id,name,normalized,source_materials)"
                    " VALUES(81,?,'氧化还原','氧化还原','')", (graph["id"],))
        con.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,source_materials)"
            " VALUES(?,81,80,'related',',72,')", (graph["id"],))
        con.commit()
        con.close()
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        try:
            manifest = app_module._build_backup_zip(handle.name)
            self.assertEqual(manifest["counts"]["knowledge_graphs"], 1)
            self.assertEqual(manifest["counts"]["kg_materials"], 1)
            self.assertEqual(manifest["counts"]["kg_nodes"], 2)
            self.assertEqual(manifest["counts"]["kg_edges"], 1)
            with zipfile.ZipFile(handle.name) as archive:
                names = archive.namelist()
            self.assertIn("data.sqlite", names)
            self.assertIn(f"uploads/kg/{graph['id']}/source.txt", names)
        finally:
            os.remove(handle.name)


if __name__ == "__main__":
    unittest.main()
