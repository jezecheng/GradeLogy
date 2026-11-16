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
           "users", "settings", "ai_usage_events", "ai_operations")


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

    def test_build_material_splits_truncated_single_chunk(self):
        material_id = self._material()
        self.con.execute(
            "INSERT INTO kg_chunks(graph_id,material_id,chunk_no,content)"
            " VALUES(1,?,0,?)", (material_id, "甲。" * 700))
        self.con.commit()
        first = [{"name": "氧化还原反应", "category": "", "definition": "",
                  "aliases": [], "confidence": 0.8}]
        second = [{"name": "电子守恒", "category": "", "definition": "",
                   "aliases": [], "confidence": 0.8}]
        truncated = ([], [], f"{kg.TRUNCATED_MARK}（max_tokens=32768）")
        with mock.patch.object(kg, "analyze_batch",
                               side_effect=[truncated, (first, [], ""),
                                            (second, [], "")]) as mocked:
            result = kg.build_material(self.con, material_id, "u", "k", "m")
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["new_nodes"], 2)
        material = self.con.execute("SELECT * FROM kg_materials WHERE id=?",
                                    (material_id,)).fetchone()
        chunk = self.con.execute("SELECT * FROM kg_chunks WHERE material_id=?",
                                 (material_id,)).fetchone()
        self.assertEqual(material["status"], kg.STATUS_DONE)
        self.assertEqual(chunk["status"], kg.CHUNK_SUCCESS)
        self.assertEqual(len(json.loads(chunk["extraction_json"])["concepts"]), 2)

    def test_build_material_resumes_chunks_and_replays_merge_idempotently(self):
        material_id = self._material()
        saved = json.dumps({
            "concepts": [{"name": "概念甲", "category": "", "definition": "",
                          "aliases": [], "confidence": 0.8}],
            "relations": [{"from": "概念甲", "to": "概念乙",
                           "relation": "prerequisite", "evidence": "跨块关系",
                           "confidence": 0.9}],
        }, ensure_ascii=False)
        statuses = [kg.CHUNK_SUCCESS, kg.CHUNK_RUNNING, kg.CHUNK_FAILED, kg.CHUNK_PENDING]
        for number, status in enumerate(statuses):
            self.con.execute(
                "INSERT INTO kg_chunks(graph_id,material_id,chunk_no,content,status,"
                "extraction_json) VALUES(1,?,?,?,?,?)",
                (material_id, number, f"第{number + 1}块", status,
                 saved if number == 0 else ""))
        self.con.commit()

        def extraction(name):
            return ([{"name": name, "category": "", "definition": "",
                      "aliases": [], "confidence": 0.8}], [], "")

        with mock.patch.object(kg, "analyze_batch",
                               side_effect=[extraction("概念乙"), extraction("概念丙"),
                                            extraction("概念丁")]) as mocked:
            result = kg.build_material(self.con, material_id, "u", "k", "m")
        self.assertEqual([call.args[0][0] for call in mocked.call_args_list],
                         ["第2块", "第3块", "第4块"])
        self.assertEqual((result["new_nodes"], result["new_edges"], result["errors"]),
                         (4, 1, 0))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_chunks WHERE material_id=? AND status='success'",
            (material_id,)).fetchone()[0], 4)

        mocked.reset_mock()
        replay = kg.build_material(self.con, material_id, "u", "k", "m")
        mocked.assert_not_called()
        self.assertEqual((replay["new_nodes"], replay["new_edges"]), (0, 0))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 4)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0], 1)
        edge = self.con.execute("SELECT * FROM kg_edges").fetchone()
        self.assertEqual(edge["source_materials"], f",{material_id},")

    def test_llm_running_event_is_updated_for_each_retry(self):
        events = []

        def hook(event):
            events.append(dict(event))
            return len(events)

        class Response:
            headers = {"x-request-id": "request-2"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return json.dumps({
                    "id": "response-2", "choices": [{"message": {"content": "完成"},
                                                      "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
                }).encode()

        limited = kg.llm.urllib.error.HTTPError(
            "http://ai.local", 429, "busy", None, io.BytesIO(b"busy"))
        with mock.patch.object(kg.llm.urllib.request, "urlopen",
                               side_effect=[limited, Response()]), \
                mock.patch.object(kg.llm.time, "sleep"):
            reply, finish, error = kg.llm.chat(
                "http://ai.local", "k", "m", "正文", retries=2,
                usage_hook=hook, report_running=True)
        self.assertEqual((reply, finish, error), ("完成", "stop", ""))
        self.assertEqual([event["status"] for event in events],
                         ["running", "error", "running", "success"])
        self.assertEqual([event["attempt"] for event in events], [1, 1, 2, 2])
        self.assertEqual(events[1]["usage_event_id"], 1)
        self.assertEqual(events[3]["usage_event_id"], 3)
        self.assertEqual(events[-1]["total_tokens"], 9)

    def test_legacy_database_migrates_progress_columns_idempotently(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        handle.close()
        try:
            con = sqlite3.connect(handle.name)
            con.executescript(database.SCHEMA)
            con.execute("ALTER TABLE ai_operations DROP COLUMN material_id")
            con.execute("ALTER TABLE ai_usage_events DROP COLUMN chunk_id")
            for column in ("started_at", "finished_at"):
                con.execute(f"ALTER TABLE kg_materials DROP COLUMN {column}")
            for column in ("status", "error", "extraction_json", "started_at", "finished_at"):
                con.execute(f"ALTER TABLE kg_chunks DROP COLUMN {column}")
            con.close()
            with mock.patch.object(database, "DB_PATH", handle.name):
                database.init_db()
                database.init_db()
            con = sqlite3.connect(handle.name)
            expected = {
                "ai_operations": {"material_id"},
                "ai_usage_events": {"chunk_id"},
                "kg_materials": {"started_at", "finished_at"},
                "kg_chunks": {"status", "error", "extraction_json", "started_at", "finished_at"},
            }
            for table, columns in expected.items():
                actual = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(columns.issubset(actual))
            indexes = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_ai_operations_material", indexes)
            self.assertIn("idx_ai_usage_chunk", indexes)
            con.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(handle.name + suffix):
                    os.remove(handle.name + suffix)

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
        app_module.cfg.DISPLAY_TIMEZONE = "Asia/Shanghai"
        app_module.cfg.LEGACY_TIMEZONE = "Asia/Shanghai"
        with app_module._kg_schedule_lock:
            app_module._kg_enqueued.clear()
        self._clear()
        self._seed()
        self.client = self.login(2)

    def tearDown(self):
        self._clear()
        with app_module._kg_schedule_lock:
            app_module._kg_enqueued.clear()
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

    def test_graph_times_use_default_and_per_user_display_timezones(self):
        graph = self._create_graph()
        con = self._db()
        con.execute("UPDATE knowledge_graphs SET created_at='2026-01-02 03:04:05',"
                    "updated_at='2026-01-02 04:05:06' WHERE id=?", (graph["id"],))
        con.commit()
        con.close()

        detail_url = self.url("knowledge_graph_detail", gid=graph["id"])
        html = self.client.get(detail_url).get_data(as_text=True)
        self.assertIn("创建于 2026-01-02 11:04，最近更新 2026-01-02 12:05", html)
        listing = self.client.get(self.url("knowledge_graphs")).get_data(as_text=True)
        self.assertIn("更新于 2026-01-02 12:05", listing)

        con = self._db()
        con.executemany(
            "INSERT INTO settings(teacher_id,key,value) VALUES(?,'display_timezone',?)",
            [(2, "UTC"), (3, "America/New_York")])
        con.commit()
        con.close()
        utc_html = self.client.get(detail_url).get_data(as_text=True)
        self.assertIn("创建于 2026-01-02 03:04，最近更新 2026-01-02 04:05", utc_html)
        ny_html = self.login(3).get(detail_url).get_data(as_text=True)
        self.assertIn("创建于 2026-01-01 22:04，最近更新 2026-01-01 23:05", ny_html)

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

    def test_startup_recovery_restores_only_active_materials_in_order(self):
        graph = self._create_graph()
        con = self._db()
        for material_id, status in ((60, kg.STATUS_PENDING), (61, kg.STATUS_DONE),
                                    (62, kg.STATUS_BUILDING), (63, kg.STATUS_FAILED)):
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
                "status,uploaded_by) VALUES(?,?,?,?,?,?,2)",
                (material_id, graph["id"], f"{material_id}.txt",
                 f"kg/{graph['id']}/{material_id}.txt", str(material_id) * 64, status))
        con.commit()
        con.close()
        with mock.patch.object(app_module, "_kg_start_build") as start:
            self.assertEqual(app_module.start_kg_recovery(), 2)
        self.assertEqual(start.call_args_list, [
            mock.call(graph["id"], 60, 2), mock.call(graph["id"], 62, 2)])

    def test_build_scheduler_prevents_duplicate_enqueue(self):
        graph = self._create_graph()
        with mock.patch.object(app_module.threading, "Thread") as thread_class:
            self.assertTrue(app_module._kg_start_build(graph["id"], 64, 2))
            self.assertFalse(app_module._kg_start_build(graph["id"], 64, 2))
        thread_class.assert_called_once_with(
            target=app_module._kg_build_worker, args=(graph["id"], 64, 2), daemon=True)
        thread_class.return_value.start.assert_called_once_with()

    def test_build_worker_ignores_missing_or_terminal_material(self):
        graph = self._create_graph()
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
            "status,uploaded_by) VALUES(65,?,?,?,?,?,2)",
            (graph["id"], "done.txt", f"kg/{graph['id']}/done.txt", "e" * 64,
             kg.STATUS_DONE))
        con.commit()
        con.close()
        with mock.patch.object(app_module, "_ai_endpoint") as endpoint:
            app_module._kg_build_worker(graph["id"], 65, 2)
            app_module._kg_build_worker(graph["id"], 999, 2)
        endpoint.assert_not_called()

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

    def test_materials_section_collapses_when_many_files(self):
        graph = self._create_graph()
        empty_html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        empty_tag = empty_html.split('id="kg-materials"', 1)[1].split(">", 1)[0]
        self.assertIn("open", empty_tag)
        con = self._db()
        for index in range(9):
            con.execute(
                "INSERT INTO kg_materials(graph_id,filename,stored_name,sha256,status)"
                " VALUES(?,?,?,?,?)",
                (graph["id"], f"资料{index}.pdf", f"{index}.pdf",
                 f"sha-{index}", "待建图"))
        con.commit()
        con.close()
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        tag = html.split('id="kg-materials"', 1)[1].split(">", 1)[0]
        self.assertNotIn("open", tag)
        self.assertIn("9 份", html)
        self.assertIn("kg-table-wrap", html)
        self.assertIn("资料8.pdf", html)

    def test_creator_can_edit_graph_info(self):
        graph = self._create_graph()
        self._post("knowledge_graph_edit",
                   {"name": "必修一化学图谱", "description": "覆盖必修一"}, gid=graph["id"])
        con = self._db()
        row = con.execute("SELECT name,description FROM knowledge_graphs WHERE id=?",
                          (graph["id"],)).fetchone()
        con.close()
        self.assertEqual(row["name"], "必修一化学图谱")
        self.assertEqual(row["description"], "覆盖必修一")
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("必修一化学图谱", html)
        self.assertIn("编辑信息", html)

    def test_edit_requires_name(self):
        graph = self._create_graph()
        self._post("knowledge_graph_edit", {"name": "  "}, gid=graph["id"])
        con = self._db()
        row = con.execute("SELECT name FROM knowledge_graphs WHERE id=?",
                          (graph["id"],)).fetchone()
        con.close()
        self.assertEqual(row["name"], "必修一知识图谱")

    def test_other_teacher_cannot_edit_graph(self):
        graph = self._create_graph()
        other = self.login(3)
        resp = self._post("knowledge_graph_edit", {"name": "被改名"}, client=other,
                          gid=graph["id"])
        self.assertEqual(resp.status_code, 403)
        con = self._db()
        row = con.execute("SELECT name FROM knowledge_graphs WHERE id=?",
                          (graph["id"],)).fetchone()
        con.close()
        self.assertEqual(row["name"], "必修一知识图谱")

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

    def test_status_reports_local_times_chunk_counts_duration_and_eta(self):
        graph = self._create_graph()
        con = self._db()
        materials = [
            (170, kg.STATUS_BUILDING, "2026-01-02 03:05:00", None),
            (171, kg.STATUS_DONE, "2026-01-02 03:05:00", "2026-01-02 03:06:05"),
            (172, kg.STATUS_PENDING, None, None),
        ]
        for material_id, status, started, finished in materials:
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,status,"
                "uploaded_by,created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,2,?,?,?)",
                (material_id, graph["id"], f"{material_id}.txt",
                 f"kg/{graph['id']}/{material_id}.txt", str(material_id) * 64, status,
                 "2026-01-02 03:04:05", started, finished))
        chunks = [
            (0, kg.CHUNK_SUCCESS, "2026-01-02 00:00:00", "2026-01-02 00:00:10"),
            (1, kg.CHUNK_SUCCESS, "2026-01-02 00:01:00", "2026-01-02 00:01:30"),
            (2, kg.CHUNK_RUNNING, None, None),
            (3, kg.CHUNK_PENDING, None, None),
            (4, kg.CHUNK_FAILED, "2026-01-02 00:02:00", "2026-01-02 00:02:05"),
        ]
        for chunk_no, status, started, finished in chunks:
            con.execute(
                "INSERT INTO kg_chunks(graph_id,material_id,chunk_no,content,status,"
                "started_at,finished_at) VALUES(?,170,?, '正文',?,?,?)",
                (graph["id"], chunk_no, status, started, finished))
        con.execute("UPDATE kg_chunks SET started_at=CURRENT_TIMESTAMP "
                    "WHERE material_id=170 AND chunk_no=2")
        con.execute("INSERT INTO kg_chunks(graph_id,material_id,chunk_no,content) "
                    "VALUES(?,172,0,'正文')", (graph["id"],))
        con.commit()
        con.close()

        local_created = "2026-01-02 11:04"
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn(local_created, html)
        self.assertIn("共 5 块 · 成功 2 · 处理中 1 · 待处理 1 · 失败 1", html)
        self.assertIn("1 分 5 秒", html)

        data = self.client.get(
            self.url("knowledge_graph_status", gid=graph["id"])).get_json()
        items = {item["id"]: item for item in data["materials"]}
        building = items[170]
        self.assertEqual(building["created_at_local"], local_created)
        self.assertEqual(building["chunks"], {
            "total": 5, "pending": 1, "running": 1, "success": 2, "failed": 1})
        self.assertTrue(building["elapsed_text"])
        self.assertGreaterEqual(building["eta_s"], 35)
        self.assertLessEqual(building["eta_s"], 40)
        self.assertEqual(items[171]["elapsed_text"], "1 分 5 秒")
        self.assertEqual(items[171]["finished_at_local"], "2026-01-02 11:06")
        self.assertIsNone(items[172]["eta_s"])
        self.assertEqual(items[172]["started_at_local"], "")

    def test_material_request_details_reuse_event_and_check_graph(self):
        graph = self._create_graph()
        con = self._db()
        other_graph_id = con.execute(
            "INSERT INTO knowledge_graphs(subject,name,created_by) VALUES('化学','另一图谱',2)"
        ).lastrowid
        for material_id, graph_id in ((180, graph["id"]), (181, other_graph_id)):
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
                "status,uploaded_by) VALUES(?,?,?,?,?,?,2)",
                (material_id, graph_id, f"{material_id}.txt",
                 f"kg/{graph_id}/{material_id}.txt", str(material_id) * 64,
                 kg.STATUS_BUILDING))
        con.execute(
            "INSERT INTO kg_chunks(id,graph_id,material_id,chunk_no,content) "
            "VALUES(190,?,180,0,'正文')", (graph["id"],))
        con.commit()
        con.close()

        with app_module.app.app_context():
            first = app_module._start_ai_operation(2, "kg_build", "m", material_id=180)
            event_id = first["usage_hook"]({
                "chunk_id": 190, "attempt": 1, "model": "m", "status": "running"})
            first["usage_hook"]({
                "usage_event_id": event_id, "chunk_id": 190, "attempt": 1, "model": "m",
                "status": "success", "latency_ms": 13000, "prompt_tokens": 10,
                "completion_tokens": 20, "total_tokens": 30, "finish_reason": "stop"})
            second = app_module._start_ai_operation(2, "kg_build", "m", material_id=180)
            second["usage_hook"]({
                "chunk_id": 190, "attempt": 2, "model": "m", "status": "error",
                "error": "请求超时", "latency_ms": 2000})

        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM ai_usage_events WHERE operation_id=?",
            (first["id"],)).fetchone()[0], 1)
        con.execute("UPDATE ai_usage_events SET created_at='2026-01-02 03:04:05' "
                    "WHERE operation_id=?", (first["id"],))
        con.commit()
        con.close()
        data = self.client.get(
            self.url("knowledge_graph_status", gid=graph["id"]) + "?material_id=180").get_json()
        self.assertEqual(data["request_material_id"], 180)
        self.assertEqual(len(data["requests"]), 2)
        success = data["requests"][0]
        self.assertEqual((success["chunk_no"], success["attempt"], success["status"]),
                         (1, 1, "success"))
        self.assertEqual(success["elapsed_text"], "13 秒")
        self.assertEqual(success["total_tokens"], 30)
        self.assertEqual(success["started_at"], "2026-01-02 11:04")
        self.assertEqual(data["requests"][1]["error"], "请求超时")
        self.assertEqual(data["requests"][1]["operation_id"], second["id"])

        foreign = self.client.get(
            self.url("knowledge_graph_status", gid=graph["id"]) + "?material_id=181")
        self.assertEqual(foreign.status_code, 404)

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
        self.assertIn("data-material-toggle", html)
        self.assertIn("data-request-content", html)
        self.assertIn("setInterval(poll, 4000)", html)
        self.assertIn("error.textContent = request.error", html)
        self.assertIn("if (active) { poll(); }", html)
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
