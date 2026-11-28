# -*- coding: utf-8 -*-
"""学科知识图谱：增量合并、出处回收、AI 抽取与路由的测试。"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
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
from utilities import kg_cli, kg_dedup as kd, kg_text, kg_transfer, knowledge_graph as kg  # noqa: E402

_TABLES = ("kg_category_maps", "kg_members", "kg_audits", "kg_chunks", "kg_edges",
           "kg_nodes", "kg_materials", "knowledge_graphs", "tag_analyses",
           "question_assessments", "question_tags", "questions", "submissions", "lectures", "exams",
           "exam_scores", "enrollments", "course_teachers", "courses", "users",
           "settings", "ai_usage_events", "ai_operations")


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
            status = 200
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

    def test_graph_payload_includes_degree_and_categories(self):
        basic = self._node("物质分类", "基本概念")
        middle = self._node("物质的量", "基本概念")
        advanced = self._node("物质的量浓度", "溶液")
        self._edge(basic, middle)
        self._edge(middle, advanced)
        self._edge(advanced, basic, "related")
        payload = kg.graph_payload(self.con, 1)
        self.assertEqual(payload["stats"],
                         {"nodes": 3, "edges": 3, "categories": 2})
        nodes = {node["name"]: node for node in payload["nodes"]}
        self.assertEqual(nodes["物质的量"]["degree"], 2)
        self.assertEqual(nodes["物质的量"]["category"], "基本概念")
        self.assertEqual(nodes["物质分类"]["definition"], "物质分类的定义")
        self.assertEqual(payload["edges"][0]["src_id"], basic)
        self.assertEqual(payload["edges"][0]["relation"], "prerequisite")
        counts = {item["name"]: item["count"] for item in payload["categories"]}
        self.assertEqual(counts, {"基本概念": 2, "溶液": 1})

    def test_graph_payload_marks_uncategorized_nodes(self):
        self._node("无分类概念", "")
        payload = kg.graph_payload(self.con, 1)
        self.assertEqual(payload["nodes"][0]["category"], "未分类")
        self.assertEqual(payload["categories"][0]["name"], "未分类")

    def test_graph_payload_has_no_node_limit(self):
        for number in range(120):
            self._node(f"概念{number}")
        payload = kg.graph_payload(self.con, 1)
        self.assertEqual(len(payload["nodes"]), 120)
        self.assertEqual(payload["stats"]["nodes"], 120)

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
            self.assertIn("未抽取到文本", row["error"])
        finally:
            os.remove(path)

    def test_chunk_pages_keeps_page_numbers(self):
        chunks = kg_text.chunk_pages([(1, "甲" * 5000), (2, "乙" * 100)])
        self.assertEqual(chunks[0][0], 1)
        self.assertEqual(chunks[-1][0], 2)
        self.assertTrue(all(len(text) <= kg_text.CHUNK_CHARS for _p, text in chunks))

    def test_extract_docx_pages_reads_paragraphs_and_page_breaks(self):
        document = (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>氧化还原反应的实质</w:t></w:r></w:p>'
            '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
            '<w:p><w:r><w:t>是电子转移</w:t></w:r></w:p>'
            '</w:body></w:document>')
        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        try:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document)
            pages = kg_text.extract_pages(path)
        finally:
            os.remove(path)
        self.assertEqual([number for number, _text in pages], [1, 2])
        self.assertIn("氧化还原反应的实质", pages[0][1])
        self.assertIn("是电子转移", pages[1][1])

    def test_word97_piece_table_decodes_compressed_and_unicode(self):
        word = bytearray(2048)
        struct.pack_into("<H", word, 0, kg_text._WORD_IDENT)
        struct.pack_into("<H", word, 2, 0x00C1)
        struct.pack_into("<I", word, 0x4C, 5)
        word[1024:1027] = b"abc"
        word[1030:1034] = "化学".encode("utf-16-le")
        plc = (
            struct.pack("<3I", 0, 3, 5)
            + b"\x00\x00" + struct.pack("<I", (1024 << 1) | 0x40000000) + b"\x00\x00"
            + b"\x00\x00" + struct.pack("<I", 1030) + b"\x00\x00")
        clx = b"\x02" + struct.pack("<I", len(plc)) + plc
        table = bytearray(64)
        table[:len(clx)] = clx
        struct.pack_into("<II", word, 0x01A2, 0, len(clx))
        self.assertEqual(kg_text._word97_text(bytes(word), bytes(table)), "abc化学")

    @staticmethod
    def _concepts(*names):
        return [{"name": name, "category": "", "definition": "",
                 "aliases": [], "confidence": 0.8} for name in names]

    def test_merge_extraction_skips_cycle_edges(self):
        kg.merge_extraction(self.con, 1, 101, self._concepts("A", "B"), [
            {"from": "A", "to": "B", "relation": "prerequisite",
             "evidence": "", "confidence": 0.8}])
        stats = {}
        result = kg.merge_extraction(
            self.con, 1, 102, self._concepts("A", "B"), [
                {"from": "B", "to": "A", "relation": "prerequisite",
                 "evidence": "", "confidence": 0.7}], stats=stats)
        self.assertEqual(stats["skipped_cycles"], 1)
        self.assertEqual(result[1], 1)
        edges = self.con.execute("SELECT * FROM kg_edges").fetchall()
        self.assertEqual([(edge["src_id"], edge["dst_id"]) for edge in edges],
                         [(1, 2)])

    def test_prerequisite_cycles_reports_weakest_edge(self):
        kg.merge_extraction(self.con, 1, 101, self._concepts("A", "B", "C"), [])
        ids = {row["name"]: row["id"] for row in self.con.execute(
            "SELECT id,name FROM kg_nodes")}
        for src, dst, confidence in (("A", "B", 0.9), ("B", "C", 0.8),
                                     ("C", "A", 0.3)):
            self.con.execute(
                "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,confidence)"
                " VALUES(1,?,?,'prerequisite',?)",
                (ids[src], ids[dst], confidence))
        self.con.commit()
        reports = kg.prerequisite_cycles(self.con, 1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(len(reports[0]["edges"]), 3)
        self.assertAlmostEqual(reports[0]["edges"][0]["confidence"], 0.3)
        self.assertIn("C → A", reports[0]["suggested_label"])

    def test_audit_validation_rejects_conflicts_and_cycles(self):
        kg.merge_extraction(self.con, 1, 101, self._concepts("A", "B", "C", "孤立"), [
            {"from": "A", "to": "C", "relation": "prerequisite",
             "evidence": "", "confidence": 0.8},
            {"from": "C", "to": "B", "relation": "prerequisite",
             "evidence": "", "confidence": 0.8},
            {"from": "A", "to": "B", "relation": "prerequisite",
             "evidence": "", "confidence": 0.9}])
        ids = {row["name"]: row["id"] for row in self.con.execute(
            "SELECT id,name FROM kg_nodes")}
        edge_id = self.con.execute(
            "SELECT id FROM kg_edges WHERE src_id=? AND dst_id=?",
            (ids["A"], ids["B"])).fetchone()["id"]
        valid, rejected, error = kg.validate_audit_suggestions(self.con, 1, [
            {"action": "reverse_edge", "edge_id": edge_id, "reason": "方向反了"},
            {"action": "drop_node", "node_id": ids["A"], "reason": "想删"},
            {"action": "merge_nodes", "node_ids": [ids["A"], 999], "keep_id": ids["A"],
             "reason": "重复"},
            {"action": "drop_node", "node_id": ids["孤立"], "reason": "孤立无用"},
        ])
        self.assertEqual(error, "")
        self.assertEqual([item["action"] for item in valid], ["drop_node"])
        self.assertEqual(valid[0]["node_id"], ids["孤立"])
        reasons = [item["reason"] for item in rejected]
        self.assertTrue(any("形成环" in reason for reason in reasons))
        self.assertTrue(any("还有" in reason and "关系" in reason for reason in reasons))
        self.assertTrue(any("不属于该图谱" in reason for reason in reasons))

    def test_apply_audit_and_restore_snapshot(self):
        kg.merge_extraction(self.con, 1, 101, self._concepts("A", "B"), [
            {"from": "A", "to": "B", "relation": "prerequisite",
             "evidence": "", "confidence": 0.8}])
        edge_id = self.con.execute("SELECT id FROM kg_edges").fetchone()["id"]
        snapshot = kg.apply_audit(self.con, 1, [
            {"action": "delete_edge", "edge_id": edge_id, "reason": "测试"}])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_edges").fetchone()[0], 0)
        kg.restore_snapshot(self.con, 1, snapshot)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_edges").fetchone()[0], 1)

    def test_analyze_graph_parses_and_validates_model_output(self):
        kg.merge_extraction(self.con, 1, 101, self._concepts("A", "B"), [
            {"from": "A", "to": "B", "relation": "prerequisite",
             "evidence": "", "confidence": 0.8}])
        edge_id = self.con.execute("SELECT id FROM kg_edges").fetchone()["id"]
        reply = json.dumps({"suggestions": [
            {"action": "delete_edge", "edge_id": edge_id, "reason": "资料无依据"},
            {"action": "delete_edge", "edge_id": 9999, "reason": "不存在"}]},
            ensure_ascii=False)
        with mock.patch.object(kg.llm, "chat", return_value=(reply, "stop", "")):
            valid, rejected, error = kg.analyze_graph(self.con, 1, "u", "k", "m")
        self.assertEqual(error, "")
        self.assertEqual([item["action"] for item in valid], ["delete_edge"])
        self.assertEqual(rejected[0]["reason"], "关系 9999 不属于该图谱")

    def test_category_map_validation_apply_and_restore(self):
        kg.merge_extraction(self.con, 1, 101, [
            {"name": "氧化还原反应", "category": "反应类型", "definition": "",
             "aliases": [], "confidence": 0.8},
            {"name": "离子反应", "category": "反应类型", "definition": "",
             "aliases": [], "confidence": 0.8},
            {"name": "核外电子排布", "category": "原子结构", "definition": "",
             "aliases": [], "confidence": 0.8},
            {"name": "阿伏加德罗常数", "category": "", "definition": "",
             "aliases": [], "confidence": 0.8}], [])
        valid, rejected, error = kg.validate_category_map(self.con, 1, [
            {"from": "反应类型", "to": "化学反应/氧化还原", "reason": "归入反应"},
            {"from": "原子结构", "to": "物质结构/原子结构/核外电子/更细", "reason": "太深"},
            {"from": "", "to": "化学计量/物质的量", "reason": "未分类"},
            {"from": "不存在的分类", "to": "其他", "reason": "不存在"},
            {"from": "反应类型", "to": "化学反应/离子反应", "reason": "重复"},
        ])
        self.assertEqual(error, "")
        self.assertEqual([item["to"] for item in valid],
                         ["化学反应/氧化还原", "化学计量/物质的量"])
        reasons = [item["reason"] for item in rejected]
        self.assertTrue(any("最多 3 层" in reason for reason in reasons))
        self.assertTrue(any("不在当前图谱" in reason for reason in reasons))
        self.assertTrue(any("多条映射" in reason for reason in reasons))

        snapshot = kg.apply_category_map(self.con, 1, valid)
        categories = {row["name"]: row["category"] for row in self.con.execute(
            "SELECT name,category FROM kg_nodes")}
        self.assertEqual(categories["氧化还原反应"], "化学反应/氧化还原")
        self.assertEqual(categories["离子反应"], "化学反应/氧化还原")
        self.assertEqual(categories["阿伏加德罗常数"], "化学计量/物质的量")
        self.assertEqual(categories["核外电子排布"], "原子结构")

        kg.restore_snapshot(self.con, 1, snapshot)
        categories = {row["name"]: row["category"] for row in self.con.execute(
            "SELECT name,category FROM kg_nodes")}
        self.assertEqual(categories["氧化还原反应"], "反应类型")
        self.assertEqual(categories["阿伏加德罗常数"], "")

    def test_analyze_categories_parses_and_validates_model_output(self):
        kg.merge_extraction(self.con, 1, 101, [
            {"name": "过滤操作", "category": "方法", "definition": "",
             "aliases": [], "confidence": 0.8}], [])
        reply = json.dumps({"mappings": [
            {"from": "方法", "to": "实验方法/基本操作", "reason": "归入实验"},
            {"from": "不存在", "to": "其他", "reason": "无效"}]},
            ensure_ascii=False)
        with mock.patch.object(kg.llm, "chat", return_value=(reply, "stop", "")):
            valid, rejected, error = kg.analyze_categories(
                self.con, 1, "u", "k", "m")
        self.assertEqual(error, "")
        self.assertEqual([item["to"] for item in valid], ["实验方法/基本操作"])
        self.assertEqual(valid[0]["count"], 1)
        self.assertEqual(rejected[0]["reason"], "该分类不在当前图谱中")

    def test_merge_nodes_moves_edges_and_dedupes(self):
        kg.merge_extraction(self.con, 1, 101, self._concepts("A", "B", "C"), [
            {"from": "A", "to": "C", "relation": "related",
             "evidence": "", "confidence": 0.8},
            {"from": "B", "to": "C", "relation": "related",
             "evidence": "", "confidence": 0.7}])
        ids = {row["name"]: row["id"] for row in self.con.execute(
            "SELECT id,name FROM kg_nodes")}
        kg.apply_audit(self.con, 1, [{
            "action": "merge_nodes", "node_ids": [ids["A"], ids["B"]],
            "keep_id": ids["A"], "reason": "同义"}])
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 2)
        edges = self.con.execute("SELECT * FROM kg_edges").fetchall()
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["src_id"], edges[0]["dst_id"]),
                         (ids["A"], ids["C"]))
        keep = self.con.execute("SELECT * FROM kg_nodes WHERE id=?",
                                (ids["A"],)).fetchone()
        self.assertIn("B", json.loads(keep["aliases"]))


    def _dedup_node(self, name, category="", aliases=(), definition=""):
        cur = self.con.execute(
            "INSERT INTO kg_nodes(graph_id,name,normalized,category,aliases,"
            "definition,confidence) VALUES(1,?,?,?,?,?,0.9)",
            (name, kg_text.normalize_concept(name), category,
             json.dumps(list(aliases), ensure_ascii=False), definition))
        self.con.commit()
        return cur.lastrowid

    def _dedup_edge(self, src, dst, relation="prerequisite"):
        self.con.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,evidence,confidence)"
            " VALUES(1,?,?,?,?,0.8)", (src, dst, relation, ""))
        self.con.commit()

    def test_dedup_candidates_recall_alias_conflict(self):
        self._dedup_node("牛顿第二定律", "力学", ["牛顿第二运动定律"])
        self._dedup_node("牛顿第二运动定律", "力学")
        names = {tuple(sorted(n["name"] for n in g))
                 for g in kd.build_dedup_candidates(self.con, 1)}
        self.assertIn(("牛顿第二定律", "牛顿第二运动定律"), names)

    def test_dedup_candidates_recall_shared_neighbor_cross_category(self):
        x = self._dedup_node("氧化还原反应", "反应类型")
        y = self._dedup_node("氧化还原", "电化学")
        z = self._dedup_node("电子转移", "基础")
        self._dedup_edge(x, z)
        self._dedup_edge(y, z)
        names = {tuple(sorted(n["name"] for n in g))
                 for g in kd.build_dedup_candidates(self.con, 1)}
        self.assertIn(("氧化还原", "氧化还原反应"), names)

    def test_dedup_candidates_recall_same_category_token_overlap(self):
        self._dedup_node("化学平衡常数", "化学平衡")
        self._dedup_node("化学平衡移动", "化学平衡")
        self._dedup_node("氧化还原反应", "反应类型")
        names = {tuple(sorted(n["name"] for n in g))
                 for g in kd.build_dedup_candidates(self.con, 1)}
        self.assertIn(("化学平衡常数", "化学平衡移动"), names)
        self.assertNotIn(("化学平衡常数", "氧化还原反应"), names)

    def test_run_dedup_flattens_validates_and_restores(self):
        a = self._dedup_node("动能定理", "力学")
        b = self._dedup_node("动能定律", "力学")
        reply = json.dumps({"groups": [
            {"node_ids": [a, b], "keep_id": a, "reason": "同一概念不同写法",
             "confidence": 0.9}]}, ensure_ascii=False)
        with mock.patch.object(kd.llm, "chat", return_value=(reply, "stop", "")):
            valid, rejected, error = kd.run_dedup(self.con, 1, "u", "k", "m")
        self.assertEqual(error, "")
        self.assertEqual(len(valid), 1)
        self.assertEqual(set(valid[0]["node_ids"]), {a, b})
        flat = kd.flatten_dedup_suggestions(valid)
        self.assertEqual(flat[0]["action"], "merge_nodes")
        snapshot = kg.apply_audit(self.con, 1, flat, commit=False)
        self.con.commit()
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 1)
        kg.restore_snapshot(self.con, 1, snapshot)
        self.con.commit()
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 2)

    def test_run_dedup_rejects_merges_that_introduce_cycle(self):
        a = self._dedup_node("概念A", "分类一")
        b = self._dedup_node("概念B", "分类一")
        c = self._dedup_node("概念C", "分类一")
        self._dedup_edge(a, b)
        self._dedup_edge(b, c)
        reply = json.dumps({"groups": [
            {"node_ids": [a, c], "keep_id": a, "reason": "想合并",
             "confidence": 0.8}]}, ensure_ascii=False)
        with mock.patch.object(kd.llm, "chat", return_value=(reply, "stop", "")):
            valid, rejected, error = kd.run_dedup(self.con, 1, "u", "k", "m")
        self.assertEqual(error, "")
        self.assertEqual(valid, [])
        self.assertTrue(rejected)
        self.assertIn("环", rejected[0]["reason"])

    def test_run_dedup_single_group_failure_does_not_abort(self):
        a = self._dedup_node("动量定理", "力学")
        b = self._dedup_node("动量定律", "力学")
        self._dedup_node("能量守恒", "热学")
        self._dedup_node("能量守恒定律", "热学")
        ok_reply = json.dumps({"groups": [
            {"node_ids": [a, b], "keep_id": a, "reason": "同义",
             "confidence": 0.9}]}, ensure_ascii=False)

        def fake_chat(api, key, model, content, **kwargs):
            text = content[0]["text"] if isinstance(content, list) else content
            if "动量定理" in text:
                return ok_reply, "stop", ""
            raise RuntimeError("boom")

        with mock.patch.object(kd.llm, "chat", side_effect=fake_chat):
            valid, rejected, error = kd.run_dedup(self.con, 1, "u", "k", "m")
        self.assertEqual(len(valid), 1)
        self.assertIn("boom", error)


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

    def test_graph_times_always_use_beijing_display_timezone(self):
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
        self.assertIn("创建于 2026-01-02 11:04，最近更新 2026-01-02 12:05", utc_html)
        ny_html = self.login(3).get(detail_url).get_data(as_text=True)
        self.assertIn("创建于 2026-01-02 11:04，最近更新 2026-01-02 12:05", ny_html)

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
            target=app_module._kg_build_worker, args=(graph["id"], 64, 2),
            kwargs={"application": app_module.app}, daemon=True)
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

    def test_detail_renders_galaxy_shell_and_data_urls(self):
        graph = self._create_graph()
        basic = self._concept(graph["id"], "物质分类")
        middle = self._concept(graph["id"], "物质的量")
        advanced = self._concept(graph["id"], "物质的量浓度")
        self._link(graph["id"], basic, middle)
        self._link(graph["id"], middle, advanced)
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("知识星系", html)
        self.assertIn('id="kg-galaxy"', html)
        self.assertIn('id="kg-drawer"', html)
        self.assertIn("kg_echarts.js", html)
        self.assertIn("echarts.min.js", html)
        self.assertIn(self.url("knowledge_graph_graph_data",
                               gid=graph["id"]), html)
        self.assertIn(self.url("knowledge_graph_neighbors_data",
                               gid=graph["id"]), html)
        self.assertNotIn("前置关系分层图", html)
        self.assertNotIn('class="kg-node"', html)
        self.assertIn(self.url("knowledge_graph_concept", gid=graph["id"],
                               nid=middle), html)

    def test_graph_data_route_returns_all_nodes_with_degree(self):
        graph = self._create_graph()
        for index in range(90):
            self._concept(graph["id"], f"概念{index}", category="模块甲")
        con = self._db()
        rows = con.execute(
            "SELECT id FROM kg_nodes WHERE graph_id=? ORDER BY id",
            (graph["id"],)).fetchall()
        con.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation)"
            " VALUES(?,?,?,'prerequisite')",
            (graph["id"], rows[0]["id"], rows[1]["id"]))
        con.commit()
        con.close()
        payload = self.client.get(
            self.url("knowledge_graph_graph_data", gid=graph["id"])).get_json()
        self.assertEqual(payload["stats"]["nodes"], 90)
        self.assertEqual(payload["stats"]["edges"], 1)
        self.assertEqual(payload["categories"],
                         [{"name": "模块甲", "count": 90}])
        degrees = {node["id"]: node["degree"] for node in payload["nodes"]}
        self.assertEqual(degrees[rows[0]["id"]], 1)
        self.assertEqual(degrees[rows[2]["id"]], 0)

    def test_neighbors_route_returns_links_and_directions(self):
        graph = self._create_graph()
        basic = self._concept(graph["id"], "物质分类")
        middle = self._concept(graph["id"], "物质的量")
        advanced = self._concept(graph["id"], "物质的量浓度")
        self._link(graph["id"], basic, middle)
        self._link(graph["id"], middle, advanced)
        payload = self.client.get(self.url(
            "knowledge_graph_neighbors_data", gid=graph["id"],
            node_id=middle)).get_json()
        self.assertEqual(payload["node"]["name"], "物质的量")
        self.assertEqual([item["name"] for item in payload["prerequisites"]],
                         ["物质分类"])
        self.assertEqual([item["name"] for item in payload["dependents"]],
                         ["物质的量浓度"])
        self.assertTrue(payload["prerequisites"][0]["url"].startswith(
            "/knowledge-graphs/"))
        self.assertEqual(self.client.get(self.url(
            "knowledge_graph_neighbors_data",
            gid=graph["id"])).status_code, 400)
        self.assertEqual(self.client.get(self.url(
            "knowledge_graph_neighbors_data", gid=graph["id"],
            node_id=99999)).status_code, 404)

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

    def test_other_teacher_can_edit_graph_content_and_metadata_but_not_delete_graph(self):
        graph = self._create_graph()
        other = self.login(3)
        resp = self._post("knowledge_graph_edit", {"name": "被改名"}, client=other,
                          gid=graph["id"])
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        self.assertEqual(con.execute("SELECT name FROM knowledge_graphs WHERE id=?",
                                     (graph["id"],)).fetchone()[0], "被改名")
        con.close()
        removed = self._post("knowledge_graph_delete", client=other, gid=graph["id"])
        self.assertEqual(removed.status_code, 403)

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

    def test_upload_docx_extracts_text_and_builds_chunks(self):
        graph = self._create_graph()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                + "氧化还原反应的实质是电子转移。" * 30
                + '</w:t></w:r></w:p></w:body></w:document>'))
        buffer.seek(0)
        with mock.patch.object(app_module, "_kg_start_build") as start:
            resp = self.client.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (buffer, "讲义.docx")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        start.assert_called_once()
        con = self._db()
        material = con.execute("SELECT * FROM kg_materials").fetchone()
        chunks = con.execute("SELECT COUNT(*) FROM kg_chunks").fetchone()[0]
        con.close()
        self.assertEqual(material["filename"], "讲义.docx")
        self.assertEqual(material["status"], kg.STATUS_PENDING)
        self.assertGreater(chunks, 0)

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
                      "files": (io.BytesIO(b"xlsx"), "讲义.xlsx")},
                content_type="multipart/form-data")
        start.assert_not_called()
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 0)
        con.close()

    def test_other_teacher_can_view_upload_and_edit_graph_metadata(self):
        graph = self._create_graph()
        other = self.login(3)
        resp = other.get(self.url("knowledge_graph_detail", gid=graph["id"]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("编辑信息", resp.get_data(as_text=True))
        with mock.patch.object(app_module, "_kg_start_build"):
            resp = other.post(
                self.url("knowledge_graph_upload", gid=graph["id"]),
                data={"_csrf": "test-token",
                      "files": (io.BytesIO(b"x"), "notes.txt")},
                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials WHERE graph_id=?", (graph["id"],)).fetchone()[0], 1)
        con.close()

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

    def test_other_teacher_can_delete_graph_material_in_temporary_open_mode(self):
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
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_materials").fetchone()[0], 0)
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
                                (io.BytesIO(b"bad"), "bad.xlsx")]},
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
        self.assertIn("未抽取到文本", row["error"])

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

    def _add_graph_nodes(self, graph_id, *names):
        con = self._db()
        ids = {}
        for name in names:
            ids[name] = con.execute(
                "INSERT INTO kg_nodes(graph_id,name,normalized,confidence)"
                " VALUES(?,?,?,0.8)", (graph_id, name, name)).lastrowid
        con.commit()
        con.close()
        return ids

    def _add_graph_edge(self, graph_id, src_id, dst_id,
                        relation="prerequisite", confidence=0.6):
        con = self._db()
        edge_id = con.execute(
            "INSERT INTO kg_edges(graph_id,src_id,dst_id,relation,evidence,"
            "confidence) VALUES(?,?,?,?, '依据', ?)",
            (graph_id, src_id, dst_id, relation, confidence)).lastrowid
        con.commit()
        con.close()
        return edge_id

    def test_edge_reverse_flips_prerequisite(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B")
        edge_id = self._add_graph_edge(graph["id"], ids["A"], ids["B"])
        resp = self._post("knowledge_graph_edge_reverse", gid=graph["id"], eid=edge_id)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        edge = con.execute("SELECT * FROM kg_edges WHERE id=?", (edge_id,)).fetchone()
        con.close()
        self.assertEqual((edge["src_id"], edge["dst_id"]), (ids["B"], ids["A"]))

    def test_edge_reverse_refuses_to_create_cycle(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B", "C")
        self._add_graph_edge(graph["id"], ids["A"], ids["C"])
        self._add_graph_edge(graph["id"], ids["C"], ids["B"])
        edge_id = self._add_graph_edge(graph["id"], ids["A"], ids["B"])
        resp = self.client.post(
            self.url("knowledge_graph_edge_reverse", gid=graph["id"], eid=edge_id),
            data={"_csrf": "test-token"}, follow_redirects=True)
        self.assertIn("形成环", resp.get_data(as_text=True))
        con = self._db()
        edge = con.execute("SELECT * FROM kg_edges WHERE id=?", (edge_id,)).fetchone()
        con.close()
        self.assertEqual((edge["src_id"], edge["dst_id"]), (ids["A"], ids["B"]))

    def test_edge_delete_removes_row(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B")
        edge_id = self._add_graph_edge(graph["id"], ids["A"], ids["B"], "related")
        self._post("knowledge_graph_edge_delete", gid=graph["id"], eid=edge_id)
        con = self._db()
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_edges").fetchone()[0], 0)
        con.close()

    def test_detail_reports_prerequisite_cycles(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B")
        self._add_graph_edge(graph["id"], ids["A"], ids["B"], confidence=0.9)
        self._add_graph_edge(graph["id"], ids["B"], ids["A"], confidence=0.2)
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("发现 1 处前置关系成环", html)
        self.assertIn("B → A", html)

    def test_audit_apply_and_rollback_restores_edges(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B")
        edge_id = self._add_graph_edge(graph["id"], ids["A"], ids["B"])
        con = self._db()
        audit_id = con.execute(
            "INSERT INTO kg_audits(graph_id,status,suggestions,created_by)"
            " VALUES(?, '待确认', ?, 2)",
            (graph["id"], json.dumps([{
                "action": "delete_edge", "edge_id": edge_id,
                "reason": "资料无依据", "label": "删除关系"}],
                ensure_ascii=False))).lastrowid
        con.commit()
        con.close()

        self._post("knowledge_graph_audit_apply", gid=graph["id"], aid=audit_id)
        con = self._db()
        audit = con.execute("SELECT * FROM kg_audits WHERE id=?", (audit_id,)).fetchone()
        edges = con.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
        con.close()
        self.assertEqual(audit["status"], kg.AUDIT_APPLIED)
        self.assertTrue(audit["snapshot"])
        self.assertEqual(edges, 0)

        self._post("knowledge_graph_audit_rollback", gid=graph["id"], aid=audit_id)
        con = self._db()
        audit = con.execute("SELECT * FROM kg_audits WHERE id=?", (audit_id,)).fetchone()
        edges = con.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
        con.close()
        self.assertEqual(audit["status"], kg.AUDIT_ROLLED_BACK)
        self.assertEqual(edges, 1)

    def test_audit_run_without_ai_config_records_failure(self):
        graph = self._create_graph()
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("", "", "", "未配置 AI")):
            resp = self.client.post(
                self.url("knowledge_graph_audit_run", gid=graph["id"]),
                data={"_csrf": "test-token"}, follow_redirects=True)
        self.assertIn("未配置 AI", resp.get_data(as_text=True))
        con = self._db()
        audit = con.execute("SELECT * FROM kg_audits").fetchone()
        con.close()
        self.assertEqual(audit["status"], kg.AUDIT_FAILED)
        self.assertIn("未配置 AI", audit["error"])

    def test_category_run_without_ai_config_records_failure(self):
        graph = self._create_graph()
        with mock.patch.object(app_module, "_ai_endpoint",
                               return_value=("", "", "", "未配置 AI")):
            resp = self.client.post(
                self.url("knowledge_graph_category_run", gid=graph["id"]),
                data={"_csrf": "test-token"}, follow_redirects=True)
        self.assertIn("分类重构未执行", resp.get_data(as_text=True))
        con = self._db()
        record = con.execute("SELECT * FROM kg_category_maps").fetchone()
        con.close()
        self.assertEqual(record["status"], kg.CATEGORY_FAILED)
        self.assertIn("未配置 AI", record["error"])

    def test_category_apply_and_rollback_restores_categories(self):
        graph = self._create_graph()
        con = self._db()
        node_id = con.execute(
            "INSERT INTO kg_nodes(graph_id,name,normalized,category,confidence)"
            " VALUES(?,?,?,?,0.8)",
            (graph["id"], "过滤", "过滤", "方法")).lastrowid
        other_id = con.execute(
            "INSERT INTO kg_nodes(graph_id,name,normalized,category,confidence)"
            " VALUES(?,?,?,?,0.8)",
            (graph["id"], "蒸馏", "蒸馏", "操作")).lastrowid
        map_id = con.execute(
            "INSERT INTO kg_category_maps(graph_id,status,mappings,created_by)"
            " VALUES(?, '待确认', ?, 2)",
            (graph["id"], json.dumps([
                {"from": "方法", "to": "实验方法/基本操作", "count": 1,
                 "reason": "归入实验", "label": "「方法」→「实验方法/基本操作」"},
                {"from": "操作", "to": "实验方法/分离提纯", "count": 1,
                 "reason": "归入实验", "label": "「操作」→「实验方法/分离提纯」"},
            ], ensure_ascii=False))).lastrowid
        con.commit()
        con.close()

        self._post("knowledge_graph_category_apply", data={"selected": ["0"]},
                   gid=graph["id"], mid=map_id)
        con = self._db()
        record = con.execute("SELECT * FROM kg_category_maps WHERE id=?",
                             (map_id,)).fetchone()
        category = con.execute("SELECT category FROM kg_nodes WHERE id=?",
                               (node_id,)).fetchone()["category"]
        other_category = con.execute("SELECT category FROM kg_nodes WHERE id=?",
                                     (other_id,)).fetchone()["category"]
        con.close()
        self.assertEqual(record["status"], kg.CATEGORY_APPLIED)
        self.assertTrue(record["snapshot"])
        self.assertEqual(category, "实验方法/基本操作")
        self.assertEqual(other_category, "操作")
        self.assertIn("本次未勾选，未应用", record["rejected"])
        self.assertEqual(len(json.loads(record["mappings"])), 1)

        self._post("knowledge_graph_category_rollback", gid=graph["id"], mid=map_id)
        con = self._db()
        record = con.execute("SELECT * FROM kg_category_maps WHERE id=?",
                             (map_id,)).fetchone()
        category = con.execute("SELECT category FROM kg_nodes WHERE id=?",
                               (node_id,)).fetchone()["category"]
        con.close()
        self.assertEqual(record["status"], kg.CATEGORY_ROLLED_BACK)
        self.assertEqual(category, "方法")

    def test_scope_json_filters_nodes_and_edges(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B", "C")
        self._add_graph_edge(graph["id"], ids["A"], ids["B"])
        self._add_graph_edge(graph["id"], ids["B"], ids["C"])
        resp = self.client.get(
            self.url("knowledge_graph_scope_data", gid=graph["id"]),
            query_string={"ids": f"{ids['A']},{ids['B']}"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual([node["name"] for node in payload["nodes"]], ["A", "B"])
        self.assertEqual(len(payload["edges"]), 1)
        self.assertEqual(payload["edge_total"], 1)
        self.assertTrue(payload["can_edit"])
        self.assertEqual(payload["nodes"][0]["url"],
                         self.url("knowledge_graph_concept", gid=graph["id"],
                                  nid=ids["A"]))

    def test_detail_shows_storage_time_tokens_and_model(self):
        graph = self._create_graph()
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(graph_id,filename,stored_name,sha256,size,"
            "status,uploaded_by,started_at,finished_at)"
            " VALUES(?,?,?,?,1536,'已完成',2,'2026-01-02 03:00:00',"
            "'2026-01-02 03:01:05')",
            (graph["id"], "讲义.pdf", f"kg/{graph['id']}/a.pdf", "a" * 64))
        material_id = con.execute(
            "SELECT id FROM kg_materials").fetchone()["id"]
        operation_id = con.execute(
            "INSERT INTO ai_operations(teacher_id,operation_type,material_id,model,"
            "status,started_at,finished_at) VALUES(2,'kg_build',?,'test-model',"
            "'success','2026-01-02 03:00:00','2026-01-02 03:01:05')",
            (material_id,)).lastrowid
        con.execute(
            "INSERT INTO ai_usage_events(operation_id,attempt,total_tokens,status)"
            " VALUES(?,1,12345,'success')", (operation_id,))
        con.commit()
        con.close()
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("1.5 KB", html)
        self.assertIn("1 分 5 秒", html)
        self.assertIn("12,345", html)
        self.assertIn("test-model", html)

    def test_edit_member_can_modify_but_not_rename(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B")
        edge_id = self._add_graph_edge(graph["id"], ids["A"], ids["B"])
        con = self._db()
        con.execute("INSERT INTO kg_members(graph_id,user_id,role) VALUES(?,?,?)",
                    (graph["id"], 3, "edit"))
        con.commit()
        con.close()
        other = self.login(3)
        resp = self._post("knowledge_graph_edge_delete", client=other,
                          gid=graph["id"], eid=edge_id)
        self.assertEqual(resp.status_code, 302)
        resp = self._post("knowledge_graph_edit", client=other,
                          gid=graph["id"], data={"name": "改名尝试"})
        self.assertEqual(resp.status_code, 403)

    def test_use_member_can_attach_graph_to_own_course(self):
        graph = self._create_graph()
        con = self._db()
        con.execute("INSERT INTO kg_members(graph_id,user_id,role) VALUES(?,?,?)",
                    (graph["id"], 3, "use"))
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(9,'我的课',3)")
        con.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(9,3)")
        con.commit()
        con.close()
        other = self.login(3)
        self._post("course_knowledge_graphs_save", client=other, cid=9,
                   data={"graph_ids": [str(graph["id"])]})
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM course_knowledge_graphs WHERE course_id=9"
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)

    def test_non_member_can_view_and_attach_graph_to_own_course(self):
        graph = self._create_graph()
        con = self._db()
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(9,'我的课',3)")
        con.execute("INSERT INTO course_teachers(course_id,teacher_id) VALUES(9,3)")
        con.commit()
        con.close()
        other = self.login(3)
        detail = other.get(self.url("knowledge_graph_detail", gid=graph["id"]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("只读", detail.get_data(as_text=True))
        self.assertIn("上传资料", detail.get_data(as_text=True))
        self._post("course_knowledge_graphs_save", client=other, cid=9,
                   data={"graph_ids": [str(graph["id"])]})
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM course_knowledge_graphs WHERE course_id=9"
        ).fetchone()[0]
        con.close()
        self.assertEqual(count, 1)

    def test_member_management_requires_manage_role(self):
        graph = self._create_graph()
        con = self._db()
        con.execute("INSERT INTO kg_members(graph_id,user_id,role) VALUES(?,?,?)",
                    (graph["id"], 3, "edit"))
        con.commit()
        con.close()
        other = self.login(3)
        resp = self._post("knowledge_graph_member_save", client=other,
                          gid=graph["id"], data={"user_id": "3", "role": "use"})
        self.assertEqual(resp.status_code, 403)
        resp = self._post("knowledge_graph_member_save",
                          gid=graph["id"], data={"user_id": "3", "role": "use"})
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        role = con.execute(
            "SELECT role FROM kg_members WHERE graph_id=? AND user_id=3",
            (graph["id"],)).fetchone()["role"]
        con.close()
        self.assertEqual(role, "use")

    def test_detail_shows_db_table_usage(self):
        graph = self._create_graph()
        ids = self._add_graph_nodes(graph["id"], "A", "B")
        self._add_graph_edge(graph["id"], ids["A"], ids["B"])
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("存储明细", html)
        self.assertIn("kg_nodes", html)
        self.assertIn("kg_edges", html)
        self.assertIn("数据库表", html)

    def test_manage_member_can_manage_collaborators(self):
        graph = self._create_graph()
        con = self._db()
        con.execute("INSERT INTO users(id,username,password_hash,display_name,role,"
                    "active,must_change_password) VALUES(5,'teacher5','x','第五位老师',"
                    "'teacher',1,0)")
        con.execute("INSERT INTO kg_members(graph_id,user_id,role) VALUES(?,?,?)",
                    (graph["id"], 3, "manage"))
        con.commit()
        con.close()
        manager = self.login(3)
        resp = self._post("knowledge_graph_member_save", client=manager,
                          gid=graph["id"], data={"user_id": "5", "role": "use"})
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        role = con.execute(
            "SELECT role FROM kg_members WHERE graph_id=? AND user_id=5",
            (graph["id"],)).fetchone()["role"]
        con.close()
        self.assertEqual(role, "use")
        resp = self._post("knowledge_graph_member_remove", client=manager,
                          gid=graph["id"], uid=5)
        self.assertEqual(resp.status_code, 302)
        con = self._db()
        count = con.execute(
            "SELECT COUNT(*) FROM kg_members WHERE graph_id=? AND user_id=5",
            (graph["id"],)).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_all_teachers_can_export_graph_and_download_package(self):
        graph = self._create_graph()

        def write_package(db, gid, upload_dir, output_path, scope="full",
                          archive_dir=None, **kwargs):
            self.assertEqual(gid, graph["id"])
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("manifest.json", "{}")
            return {"graph_id": gid, "scope": scope}

        with mock.patch.object(app_module.kg_transfer, "export_graph",
                               side_effect=write_package) as export:
            response = self.login(3).get(
                self.url("knowledge_graph_export", gid=graph["id"]))
            self.assertEqual(response.status_code, 200)
            response.close()
            self.assertEqual(export.call_args.kwargs["scope"], "full")
            response = self.client.get(self.url("knowledge_graph_export", gid=graph["id"]))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data.startswith(b"PK"))
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(export.call_args.kwargs["scope"], "full")
            response.close()
            response = self.client.get(
                self.url("knowledge_graph_export", gid=graph["id"]) + "?scope=graph")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(export.call_args.kwargs["scope"], "graph")
            response.close()
        self.assertEqual(export.call_count, 3)

    def test_import_route_uploads_package_as_current_user(self):
        created = {}

        def import_package(db, package_path, upload_dir, owner_id, name=None):
            self.assertTrue(zipfile.is_zipfile(package_path))
            cur = db.execute("INSERT INTO knowledge_graphs(subject,name,created_by) "
                             "VALUES('化学',?,?)", (name, owner_id))
            db.commit()
            created.update(id=cur.lastrowid, owner=owner_id, name=name)
            return {"graph_id": cur.lastrowid}

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("manifest.json", "{}")
        payload.seek(0)
        with mock.patch.object(app_module.kg_transfer, "import_graph",
                               side_effect=import_package) as importer:
            response = self.client.post(
                self.url("knowledge_graph_import"),
                data={"_csrf": "test-token", "name": "导入后的图谱",
                      "package": (payload, "graph.zip")},
                content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(created["owner"], 2)
        self.assertEqual(created["name"], "导入后的图谱")
        self.assertTrue(response.headers["Location"].endswith(
            self.url("knowledge_graph_detail", gid=created["id"])))
        importer.assert_called_once()
        html = self.client.get(self.url("knowledge_graphs")).get_data(as_text=True)
        self.assertIn("导入迁移包", html)
        self.assertIn('data-dialog-open="kg-import"', html)

    def test_import_route_roundtrip_with_real_package(self):
        """不经 mock：导出真实迁移包并通过导入路由创建新图谱。"""
        graph = self._create_graph()
        content = b"prerequisite concept"
        abs_dir = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]))
        os.makedirs(abs_dir, exist_ok=True)
        with open(os.path.join(abs_dir, "book.txt"), "wb") as fh:
            fh.write(content)
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,size,sha256,"
            "status,uploaded_by) VALUES(110,?,?,?,?,?,'完成',2)",
            (graph["id"], "教材.txt", f"kg/{graph['id']}/book.txt",
             len(content), hashlib.sha256(content).hexdigest()))
        con.executemany(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized) VALUES(?,?,?,?)",
            [(120, graph["id"], "A", "A"), (121, graph["id"], "B", "B")])
        con.execute(
            "INSERT INTO kg_edges(id,graph_id,src_id,dst_id,relation) "
            "VALUES(130,?,120,121,'prerequisite')", (graph["id"],))
        con.commit()
        con.close()

        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        try:
            con = self._db()
            kg_transfer.export_graph(
                con, graph["id"], app_module.UPLOAD_DIR, handle.name)
            con.close()
            with open(handle.name, "rb") as fh:
                payload = io.BytesIO(fh.read())
            response = self.client.post(
                self.url("knowledge_graph_import"),
                data={"_csrf": "test-token", "name": "路由导入图谱",
                      "package": (payload, "graph.zip")},
                content_type="multipart/form-data")
            self.assertEqual(response.status_code, 302)

            con = self._db()
            imported = con.execute(
                "SELECT * FROM knowledge_graphs WHERE name='路由导入图谱'").fetchone()
            self.assertIsNotNone(imported)
            self.assertNotEqual(imported["id"], graph["id"])
            self.assertTrue(response.headers["Location"].endswith(
                self.url("knowledge_graph_detail", gid=imported["id"])))
            counts = {
                table: con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE graph_id=?",
                    (imported["id"],)).fetchone()[0]
                for table in ("kg_materials", "kg_nodes", "kg_edges")}
            material = con.execute(
                "SELECT stored_name FROM kg_materials WHERE graph_id=?",
                (imported["id"],)).fetchone()
            con.close()
        finally:
            os.remove(handle.name)

        self.assertEqual(counts, {"kg_materials": 1, "kg_nodes": 2, "kg_edges": 1})
        self.assertTrue(material["stored_name"].startswith(f"kg/{imported['id']}/"))
        imported_file = os.path.join(app_module.UPLOAD_DIR, material["stored_name"])
        with open(imported_file, "rb") as fh:
            self.assertEqual(fh.read(), content)
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=imported["id"])).get_data(as_text=True)
        self.assertIn("路由导入图谱", html)

    def test_failed_material_retry_reuses_successful_chunks(self):
        graph = self._create_graph()
        con = self._db()
        con.execute("INSERT INTO kg_materials(id,graph_id,filename,stored_name,sha256,"
                    "status,error,uploaded_by) VALUES(80,?,'x.txt','kg/x.txt',?,'失败',"
                    "'模型超时',2)", (graph["id"], "e" * 64))
        con.execute("INSERT INTO kg_chunks(id,graph_id,material_id,content,status,"
                    "extraction_json,started_at,finished_at) VALUES(81,?,80,'A','success',"
                    "'{\"concepts\":[]}','2026-01-01','2026-01-01')", (graph["id"],))
        con.execute("INSERT INTO kg_chunks(id,graph_id,material_id,content,status,error) "
                    "VALUES(82,?,80,'B','failed','模型超时')", (graph["id"],))
        con.execute("INSERT INTO kg_chunks(id,graph_id,material_id,content,status) "
                    "VALUES(83,?,80,'C','running')", (graph["id"],))
        con.commit()
        con.close()
        html = self.client.get(
            self.url("knowledge_graph_detail", gid=graph["id"])).get_data(as_text=True)
        self.assertIn("重试建图", html)
        with mock.patch.object(app_module, "_kg_start_build", return_value=True) as start:
            response = self._post("knowledge_graph_material_retry", mid=80)
            self.assertEqual(response.status_code, 302)
            start.assert_called_once_with(graph["id"], 80, 2)
            self._post("knowledge_graph_material_retry", mid=80)
            start.assert_called_once()
        con = self._db()
        material = con.execute("SELECT * FROM kg_materials WHERE id=80").fetchone()
        self.assertEqual(material["status"], kg.STATUS_PENDING)
        self.assertEqual(material["error"], "")
        chunks = {row["id"]: row for row in con.execute(
            "SELECT * FROM kg_chunks WHERE material_id=80")}
        self.assertEqual(chunks[81]["status"], "success")
        self.assertEqual(chunks[81]["started_at"], "2026-01-01")
        self.assertEqual(chunks[82]["status"], "pending")
        self.assertEqual(chunks[82]["error"], "")
        self.assertEqual(chunks[83]["status"], "pending")
        con.close()

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


class KGTransferTest(unittest.TestCase):
    """完整迁移包在两个独立 SQLite 实例之间往返，不依赖 Flask。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source_upload = os.path.join(self.temp.name, "source-uploads")
        self.target_upload = os.path.join(self.temp.name, "target-uploads")
        self.package = os.path.join(self.temp.name, "graph.zip")
        self.source = self._database()
        self.target = self._database()
        self.content = "氧化还原反应与电子守恒\n".encode("utf-8")
        self._seed_source()
        self._seed_target()
        self.manifest = kg_transfer.export_graph(
            self.source, 7, self.source_upload, self.package)

    @staticmethod
    def _database():
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(database.SCHEMA)
        return con

    def tearDown(self):
        self.source.close()
        self.target.close()
        self.temp.cleanup()

    def _seed_source(self):
        con = self.source
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role) "
            "VALUES(?,?,?,?,'teacher')",
            [(2, "teacher", "teacher-secret", "源老师"),
             (3, "other", "other-secret", "协作老师"),
             (4, "missing", "missing-secret", "目标不存在")])
        con.execute("INSERT INTO knowledge_graphs(id,subject,name,description,"
                    "grade_level,created_by) VALUES(7,'化学','必修一','完整迁移','高一',2)")
        path = os.path.join(self.source_upload, "kg", "7")
        os.makedirs(path)
        with open(os.path.join(path, "book.txt"), "wb") as fh:
            fh.write(self.content)
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,mime,size,sha256,"
            "page_count,char_count,status,error,uploaded_by) "
            "VALUES(11,7,'教材.txt','kg/7/book.txt','text/plain',?,?,1,?,"
            "'完成','部分片段失败：模型超时',2)",
            (len(self.content), hashlib.sha256(self.content).hexdigest(),
             len(self.content.decode("utf-8"))))
        con.execute(
            "INSERT INTO kg_chunks(id,graph_id,material_id,page_no,chunk_no,content,"
            "status,extraction_json) VALUES(21,7,11,1,0,?,'success',?)",
            (self.content.decode("utf-8"),
             json.dumps({"concepts": [{"name": "氧化还原"}], "relations": []},
                        ensure_ascii=False)))
        con.execute(
            "INSERT INTO kg_chunks(id,graph_id,material_id,page_no,chunk_no,content,"
            "status,error,extraction_json) VALUES(22,7,11,1,1,'第二块','failed',"
            "'模型超时','{\"concepts\":[],\"relations\":[]}')")
        con.executemany(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized,definition,source_materials) "
            "VALUES(?,7,?,?,?,',11,')",
            [(31, "氧化还原", "氧化还原", "电子转移"),
             (32, "电子守恒", "电子守恒", "得失电子相等")])
        con.execute("INSERT INTO kg_edges(id,graph_id,src_id,dst_id,relation,evidence,"
                    "source_materials) VALUES(41,7,31,32,'prerequisite','教材原文',',11,')")
        snapshot_nodes = [dict(row) for row in con.execute(
            "SELECT * FROM kg_nodes WHERE graph_id=7 ORDER BY id")]
        snapshot_nodes.append({
            "id": 99, "name": "历史概念", "normalized": "历史概念", "category": "",
            "definition": "已从当前图删除", "aliases": "", "source_materials": ",11,",
            "confidence": 0.5, "created_at": None, "updated_at": None})
        snapshot_edges = [dict(row) for row in con.execute(
            "SELECT * FROM kg_edges WHERE graph_id=7")]
        snapshot_edges.append({
            "id": 999, "src_id": 99, "dst_id": 31, "relation": "related",
            "evidence": "历史关系", "source_materials": ",11,", "confidence": 0.5,
            "created_at": None})
        con.execute(
            "INSERT INTO kg_audits(id,graph_id,status,model,suggestions,rejected,snapshot,"
            "operation_id,created_by) VALUES(51,7,'已应用','model-x',?,?,?,777,2)",
            (json.dumps([{"action": "merge_nodes", "node_ids": [31, 32],
                          "keep_id": 31, "reason": "同义"}], ensure_ascii=False),
             json.dumps([{"action": "delete_edge", "edge_id": 999,
                          "reason": "历史拒绝项"}], ensure_ascii=False),
             json.dumps({"nodes": snapshot_nodes, "edges": snapshot_edges},
                        ensure_ascii=False)))
        con.executemany("INSERT INTO kg_members(graph_id,user_id,role) VALUES(7,?,?)",
                        [(3, "edit"), (4, "manage")])
        con.commit()

    def _seed_target(self):
        con = self.target
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role) "
            "VALUES(?,?,?,?,'teacher')",
            [(20, "owner", "owner-secret", "新所有者"),
             (30, "other", "target-secret", "同名协作老师")])
        con.execute("INSERT INTO knowledge_graphs(id,subject,name,created_by) "
                    "VALUES(100,'数学','既有图谱',20)")
        con.execute("INSERT INTO kg_materials(id,graph_id,filename,stored_name,size,"
                    "sha256,uploaded_by) VALUES(500,100,'old.txt','kg/100/old.txt',0,?,20)",
                    ("0" * 64,))
        con.execute("INSERT INTO kg_chunks(id,graph_id,material_id,content) "
                    "VALUES(550,100,500,'旧文本')")
        con.executemany("INSERT INTO kg_nodes(id,graph_id,name,normalized) VALUES(?,100,?,?)",
                        [(600, "既有A", "既有A"), (601, "既有B", "既有B")])
        con.execute("INSERT INTO kg_edges(id,graph_id,src_id,dst_id,relation) "
                    "VALUES(650,100,600,601,'related')")
        con.execute("INSERT INTO kg_audits(id,graph_id,status,created_by) "
                    "VALUES(700,100,'无需调整',20)")
        con.execute("INSERT INTO courses(id,name,teacher_id) VALUES(900,'目标课程',20)")
        con.commit()

    def test_roundtrip_remaps_all_ids_users_and_course(self):
        with zipfile.ZipFile(self.package) as archive:
            self.assertEqual(set(archive.namelist()),
                             {"manifest.json", "data.json", "metadata.json",
                              "README.md", "files/11/教材.txt"})
            raw = archive.read("data.json")
            self.assertNotIn(b"teacher-secret", raw)
            data = json.loads(raw)
            self.assertEqual(data["users"]["2"], "teacher")
            self.assertEqual(data["graph"]["name"], "")
            metadata = json.loads(archive.read("metadata.json"))
            readme = archive.read("README.md").decode("utf-8")
        self.assertEqual(metadata["format"], "gradelogy-kg")
        self.assertEqual(metadata["version"], 2)
        self.assertTrue(metadata["graph_name_omitted"])
        self.assertNotIn("name", metadata["graph"])
        self.assertEqual(metadata["counts"]["kg_nodes"], 2)
        self.assertIn("prerequisite", readme)
        self.assertIn("metadata.json", readme)
        self.assertNotIn("必修一", readme)
        self.assertNotIn("必修一", json.dumps(metadata, ensure_ascii=False))
        self.assertEqual(self.manifest["counts"]["kg_nodes"], 2)
        inspected = kg_transfer.inspect_graph(self.source, 7)
        self.assertEqual(inspected["counts"]["chunks"], 2)
        self.assertEqual(len(inspected["recent_errors"]), 2)

        result = kg_transfer.import_graph(
            self.target, self.package, self.target_upload, 20, course_id=900,
            name="迁移后的图谱")
        gid = result["graph_id"]
        self.assertNotEqual(gid, 7)
        graph = self.target.execute("SELECT * FROM knowledge_graphs WHERE id=?", (gid,)).fetchone()
        self.assertEqual(graph["created_by"], 20)
        self.assertEqual(graph["name"], "迁移后的图谱")
        self.assertIsNotNone(self.target.execute(
            "SELECT 1 FROM course_knowledge_graphs WHERE course_id=900 AND graph_id=?",
            (gid,)).fetchone())
        material = self.target.execute(
            "SELECT * FROM kg_materials WHERE graph_id=?", (gid,)).fetchone()
        self.assertNotEqual(material["id"], 11)
        self.assertEqual(material["uploaded_by"], 20)
        self.assertTrue(material["stored_name"].startswith(f"kg/{gid}/"))
        with open(os.path.join(self.target_upload, material["stored_name"]), "rb") as fh:
            self.assertEqual(fh.read(), self.content)
        nodes = {row["name"]: row for row in self.target.execute(
            "SELECT * FROM kg_nodes WHERE graph_id=?", (gid,))}
        self.assertEqual(set(nodes), {"氧化还原", "电子守恒"})
        for node in nodes.values():
            self.assertEqual(node["source_materials"], f",{material['id']},")
        edge = self.target.execute("SELECT * FROM kg_edges WHERE graph_id=?", (gid,)).fetchone()
        self.assertNotEqual(edge["id"], 41)
        self.assertEqual(edge["src_id"], nodes["氧化还原"]["id"])
        self.assertEqual(edge["dst_id"], nodes["电子守恒"]["id"])
        self.assertEqual(edge["source_materials"], f",{material['id']},")
        audit = self.target.execute("SELECT * FROM kg_audits WHERE graph_id=?", (gid,)).fetchone()
        self.assertIsNone(audit["operation_id"])
        self.assertEqual(audit["created_by"], 20)
        suggestion = json.loads(audit["suggestions"])[0]
        self.assertEqual(suggestion["keep_id"], nodes["氧化还原"]["id"])
        self.assertEqual(suggestion["node_ids"],
                         [nodes["氧化还原"]["id"], nodes["电子守恒"]["id"]])
        rejected = json.loads(audit["rejected"])[0]
        self.assertNotEqual(rejected["edge_id"], 999)
        snapshot = json.loads(audit["snapshot"])
        snapshot_nodes = {row["name"]: row for row in snapshot["nodes"]}
        self.assertEqual(snapshot_nodes["氧化还原"]["id"], nodes["氧化还原"]["id"])
        self.assertNotIn(snapshot_nodes["历史概念"]["id"],
                         {row["id"] for row in nodes.values()})
        snapshot_edges = {row["evidence"]: row for row in snapshot["edges"]}
        self.assertEqual(snapshot_edges["教材原文"]["id"], edge["id"])
        self.assertNotEqual(snapshot_edges["历史关系"]["id"], edge["id"])
        members = self.target.execute(
            "SELECT user_id,role FROM kg_members WHERE graph_id=?", (gid,)).fetchall()
        self.assertEqual([(row["user_id"], row["role"]) for row in members], [(30, "edit")])
        self.assertEqual(self.target.execute(
            "SELECT COUNT(*) FROM ai_operations").fetchone()[0], 0)
        self.assertFalse(self.target.in_transaction)

        duplicate = kg_transfer.import_graph(
            self.target, self.package, self.target_upload, 20, name="迁移后的图谱副本")
        self.assertNotEqual(duplicate["graph_id"], gid)
        self.assertEqual(self.target.execute(
            "SELECT COUNT(*) FROM knowledge_graphs").fetchone()[0], 3)

    def test_graph_only_export_omits_materials_and_imports(self):
        package = os.path.join(self.temp.name, "graph-only.zip")
        manifest = kg_transfer.export_graph(
            self.source, 7, self.source_upload, package, scope="graph")
        self.assertEqual(manifest["scope"], "graph")
        self.assertEqual(manifest["materials"], [])
        self.assertEqual(manifest["counts"]["kg_materials"], 0)
        self.assertEqual(manifest["counts"]["kg_chunks"], 0)
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            data = json.loads(archive.read("data.json"))
            metadata = json.loads(archive.read("metadata.json"))
            readme = archive.read("README.md").decode("utf-8")
        self.assertEqual(names, {"manifest.json", "data.json", "metadata.json", "README.md"})
        for key in ("materials", "chunks", "audits", "members"):
            self.assertEqual(data[key], [])
        self.assertEqual(data["users"], {})
        self.assertEqual(data["graph"]["name"], "")
        self.assertEqual(len(data["nodes"]), 2)
        self.assertEqual(len(data["edges"]), 1)
        for row in data["nodes"] + data["edges"]:
            self.assertEqual(row["source_materials"], "")
        self.assertEqual(metadata["scope"], "graph")
        self.assertEqual(metadata["document_type"], "knowledge_graph_only_package")
        self.assertIn("仅知识图谱", readme)
        self.assertIn("无法恢复资料内容", readme)

        result = kg_transfer.import_graph(
            self.target, package, self.target_upload, 20, name="仅图谱导入")
        gid = result["graph_id"]
        self.assertEqual(result["counts"], {
            "materials": 0, "chunks": 0, "nodes": 2,
            "edges": 1, "audits": 0, "members": 0})
        self.assertEqual(self.target.execute(
            "SELECT COUNT(*) FROM kg_materials WHERE graph_id=?", (gid,)).fetchone()[0], 0)
        nodes = {row["name"]: row for row in self.target.execute(
            "SELECT * FROM kg_nodes WHERE graph_id=?", (gid,))}
        self.assertEqual(set(nodes), {"氧化还原", "电子守恒"})
        for node in nodes.values():
            self.assertEqual(node["source_materials"], "")
        edge = self.target.execute(
            "SELECT * FROM kg_edges WHERE graph_id=?", (gid,)).fetchone()
        self.assertEqual(edge["src_id"], nodes["氧化还原"]["id"])
        self.assertEqual(edge["dst_id"], nodes["电子守恒"]["id"])
        self.assertEqual(edge["source_materials"], "")
        self.assertEqual(edge["evidence"], "教材原文")

    def test_cli_status_export_and_import(self):
        source_db = os.path.join(self.temp.name, "source.db")
        target_db = os.path.join(self.temp.name, "target.db")
        for connection, path in ((self.source, source_db), (self.target, target_db)):
            with contextlib.closing(sqlite3.connect(path)) as disk:
                connection.backup(disk)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = kg_cli.main([
                "--db", source_db, "--storage", self.source_upload, "status", "7"])
        self.assertEqual(code, 0)
        self.assertIn("图谱 #7：必修一", stdout.getvalue())
        self.assertIn("节点 2；关系 1；材料 1；文本块 2", stdout.getvalue())
        self.assertIn("模型超时", stdout.getvalue())

        cli_package = os.path.join(self.temp.name, "cli-graph.zip")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = kg_cli.main([
                "--db", source_db, "--storage", self.source_upload,
                "export", "7", "--output", cli_package])
        self.assertEqual(code, 0)
        self.assertIn("导出成功", stdout.getvalue())
        self.assertIn("材料 1 份；节点 2 个；关系 1 条", stdout.getvalue())
        self.assertTrue(zipfile.is_zipfile(cli_package))

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = kg_cli.main([
                "--db", target_db, "--storage", self.target_upload,
                "import", cli_package, "--owner", "owner", "--name", "命令行导入图谱",
                "--course-id", "900"])
        self.assertEqual(code, 0)
        self.assertIn("导入成功：新图谱 #", stdout.getvalue())
        self.assertIn("已绑定课程 #900", stdout.getvalue())

        with contextlib.closing(sqlite3.connect(target_db)) as imported:
            imported.row_factory = sqlite3.Row
            graph = imported.execute(
                "SELECT * FROM knowledge_graphs WHERE name='命令行导入图谱'").fetchone()
            self.assertIsNotNone(graph)
            self.assertEqual(graph["created_by"], 20)
            self.assertIsNotNone(imported.execute(
                "SELECT 1 FROM course_knowledge_graphs WHERE course_id=900 AND graph_id=?",
                (graph["id"],)).fetchone())
            material = imported.execute(
                "SELECT stored_name FROM kg_materials WHERE graph_id=?",
                (graph["id"],)).fetchone()
        with open(os.path.join(self.target_upload, material["stored_name"]), "rb") as fh:
            self.assertEqual(fh.read(), self.content)

    def test_malicious_archives_are_rejected_before_writing(self):
        with zipfile.ZipFile(self.package) as source:
            manifest = source.read("manifest.json")
            data = source.read("data.json")
        for case in ("traversal", "duplicate", "symlink"):
            with self.subTest(case=case):
                path = os.path.join(self.temp.name, f"{case}.zip")
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("manifest.json", manifest)
                    if case == "duplicate":
                        with self.assertWarns(UserWarning):
                            archive.writestr("manifest.json", manifest)
                    archive.writestr("data.json", data)
                    if case == "traversal":
                        archive.writestr("../escape.txt", "bad")
                    elif case == "symlink":
                        info = zipfile.ZipInfo("files/11/link")
                        info.external_attr = 0o120777 << 16
                        archive.writestr(info, "target")
                with self.assertRaises(kg_transfer.KGTransferError):
                    kg_transfer.import_graph(
                        self.target, path, self.target_upload, 20, name="恶意包测试")
                self.assertEqual(self.target.execute(
                    "SELECT COUNT(*) FROM knowledge_graphs").fetchone()[0], 1)
        self.assertFalse(os.path.exists(os.path.join(self.temp.name, "escape.txt")))

    def test_checksum_failure_rolls_back_rows_and_files(self):
        with zipfile.ZipFile(self.package) as source:
            manifest_text = source.read("manifest.json")
            data_text = source.read("data.json")
            manifest = json.loads(manifest_text)
            material_path = manifest["materials"][0]["archive_path"]
        bad_package = os.path.join(self.temp.name, "bad-checksum.zip")
        with zipfile.ZipFile(bad_package, "w") as archive:
            archive.writestr("manifest.json", manifest_text)
            archive.writestr("data.json", data_text)
            archive.writestr(material_path, b"X" * len(self.content))
        with self.assertRaisesRegex(kg_transfer.KGTransferError, "SHA256"):
            kg_transfer.import_graph(
                self.target, bad_package, self.target_upload, 20, name="校验失败测试")
        self.assertEqual(self.target.execute(
            "SELECT COUNT(*) FROM knowledge_graphs").fetchone()[0], 1)
        self.assertEqual(self.target.execute(
            "SELECT COUNT(*) FROM kg_nodes WHERE graph_id<>100").fetchone()[0], 0)
        kg_dir = os.path.join(self.target_upload, "kg")
        self.assertEqual(os.listdir(kg_dir) if os.path.isdir(kg_dir) else [], [])
        self.assertFalse(self.target.in_transaction)


class KGArchiveTest(unittest.TestCase):
    """资料归档：移动文件、保留图谱、批量/还原、备份排除、导出兼容。"""

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = KGRouteTest.login(2)
        con = self._db()
        for table in _TABLES:
            con.execute(f"DELETE FROM {table}")
        con.executemany(
            "INSERT INTO users(id,username,password_hash,display_name,role,active,"
            "must_change_password) VALUES(?,?, 'x',?,?,1,0)",
            [(2, "teacher", "老师", "teacher")])
        con.commit()
        con.close()
        shutil.rmtree(os.path.join(app_module.UPLOAD_DIR, "kg"), ignore_errors=True)
        shutil.rmtree(os.path.join(app_module.cfg.ARCHIVE_DIR, "kg"), ignore_errors=True)

    def tearDown(self):
        con = self._db()
        for table in _TABLES:
            con.execute(f"DELETE FROM {table}")
        con.commit()
        con.close()
        shutil.rmtree(os.path.join(app_module.UPLOAD_DIR, "kg"), ignore_errors=True)
        shutil.rmtree(os.path.join(app_module.cfg.ARCHIVE_DIR, "kg"), ignore_errors=True)

    @staticmethod
    def _db():
        con = sqlite3.connect(app_module.DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    def _post(self, endpoint, data=None, **values):
        payload = {"_csrf": "test-token"}
        payload.update(data or {})
        with app_module.app.test_request_context():
            url = app_module.url_for(endpoint, **values)
        return self.client.post(url, data=payload)

    def _create_graph(self):
        self._post("knowledge_graph_create", {"subject": "高中化学", "name": "归档测试图"})
        con = self._db()
        graph = con.execute("SELECT * FROM knowledge_graphs ORDER BY id DESC").fetchone()
        con.close()
        return graph

    def _seed_material(self, graph_id, mid, filename, status="完成", content=b"data"):
        abs_dir = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph_id))
        os.makedirs(abs_dir, exist_ok=True)
        with open(os.path.join(abs_dir, filename), "wb") as fh:
            fh.write(content)
        sha = hashlib.sha256(content).hexdigest()
        con = self._db()
        con.execute(
            "INSERT INTO kg_materials(id,graph_id,filename,stored_name,size,sha256,"
            "status,uploaded_by) VALUES(?,?,?,?,?,?,?,2)",
            (mid, graph_id, filename, f"kg/{graph_id}/{filename}",
             len(content), sha, status))
        con.execute(
            "INSERT INTO kg_chunks(id,graph_id,material_id,content,status)"
            " VALUES(?,?,?,?,'success')", (mid * 10, graph_id, mid, "chunk"))
        con.execute(
            "INSERT INTO kg_nodes(id,graph_id,name,normalized,source_materials)"
            " VALUES(?,?,?,?,?)", (mid * 100, graph_id, f"概念{mid}", f"概念{mid}", f",{mid},"))
        con.execute(
            "INSERT INTO kg_edges(id,graph_id,src_id,dst_id,relation,source_materials)"
            " VALUES(?,?,?,?,?,?)",
            (mid * 1000, graph_id, mid * 100, mid * 100, "related", f",{mid},"))
        con.commit()
        con.close()

    def test_archive_moves_file_and_keeps_chunks_nodes_edges(self):
        graph = self._create_graph()
        self._seed_material(graph["id"], 11, "book.txt", content=b"oxidation")
        self._post("knowledge_graph_material_archive", mid=11)
        con = self._db()
        row = con.execute("SELECT archived_at FROM kg_materials WHERE id=11").fetchone()
        self.assertTrue(row["archived_at"])
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM kg_chunks WHERE material_id=11").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0], 1)
        con.close()
        upload_path = os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]), "book.txt")
        archive_path = os.path.join(
            app_module.cfg.ARCHIVE_DIR, "kg", str(graph["id"]), "book.txt")
        self.assertFalse(os.path.exists(upload_path))
        self.assertTrue(os.path.isfile(archive_path))

    def test_archive_rejects_building_status(self):
        graph = self._create_graph()
        self._seed_material(graph["id"], 12, "building.txt", status="建图中")
        self._post("knowledge_graph_material_archive", mid=12)
        con = self._db()
        row = con.execute("SELECT archived_at FROM kg_materials WHERE id=12").fetchone()
        con.close()
        self.assertFalse(row["archived_at"])
        self.assertTrue(os.path.isfile(
            os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]), "building.txt")))

    def test_bulk_archive_and_restore(self):
        graph = self._create_graph()
        self._seed_material(graph["id"], 21, "a.txt", content=b"a")
        self._seed_material(graph["id"], 22, "b.txt", content=b"b")
        self._post("knowledge_graph_materials_bulk_archive",
                   {"material_ids": ["21", "22"]}, gid=graph["id"])
        con = self._db()
        rows = con.execute(
            "SELECT id,archived_at FROM kg_materials WHERE id IN (21,22) ORDER BY id").fetchall()
        self.assertTrue(all(r["archived_at"] for r in rows))
        con.close()
        for name in ("a.txt", "b.txt"):
            self.assertFalse(os.path.exists(
                os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]), name)))
            self.assertTrue(os.path.isfile(
                os.path.join(app_module.cfg.ARCHIVE_DIR, "kg", str(graph["id"]), name)))
        self._post("knowledge_graph_materials_bulk_restore",
                   {"material_ids": ["21", "22"]}, gid=graph["id"])
        con = self._db()
        rows = con.execute(
            "SELECT archived_at FROM kg_materials WHERE id IN (21,22)").fetchall()
        self.assertTrue(all(not r["archived_at"] for r in rows))
        con.close()
        for name in ("a.txt", "b.txt"):
            self.assertTrue(os.path.isfile(
                os.path.join(app_module.UPLOAD_DIR, "kg", str(graph["id"]), name)))

    def test_archived_file_excluded_from_backup_zip(self):
        graph = self._create_graph()
        self._seed_material(graph["id"], 31, "archived.txt", content=b"x")
        self._seed_material(graph["id"], 32, "kept.txt", content=b"y")
        self._post("knowledge_graph_material_archive", mid=31)
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        try:
            app_module._build_backup_zip(handle.name)
            with zipfile.ZipFile(handle.name) as archive:
                names = archive.namelist()
            self.assertIn(f"uploads/kg/{graph['id']}/kept.txt", names)
            self.assertNotIn(f"uploads/kg/{graph['id']}/archived.txt", names)
            self.assertFalse(any(n.startswith("archives/") for n in names))
            con = self._db()
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM kg_materials WHERE archived_at<>''").fetchone()[0], 1)
            con.close()
        finally:
            os.remove(handle.name)

    def test_export_reads_archived_file_and_errors_when_missing(self):
        tmp = tempfile.mkdtemp()
        try:
            upload = os.path.join(tmp, "uploads")
            archive = os.path.join(tmp, "archives")
            content = b"archived content"
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.executescript(database.SCHEMA)
            con.execute(
                "INSERT INTO knowledge_graphs(id,subject,name,created_by) VALUES(7,'化学','x',2)")
            archive_dir = os.path.join(archive, "kg", "7")
            os.makedirs(archive_dir)
            with open(os.path.join(archive_dir, "book.txt"), "wb") as fh:
                fh.write(content)
            sha = hashlib.sha256(content).hexdigest()
            con.execute(
                "INSERT INTO kg_materials(id,graph_id,filename,stored_name,size,sha256,"
                "status,archived_at) VALUES(11,7,'教材.txt','kg/7/book.txt',?,?, '完成','2026-01-01')",
                (len(content), sha))
            con.commit()
            package = os.path.join(tmp, "g.zip")
            kg_transfer.export_graph(con, 7, upload, package, archive_dir=archive)
            self.assertTrue(os.path.isfile(package))
            with zipfile.ZipFile(package) as z:
                self.assertTrue(any(n.startswith("files/11/") for n in z.namelist()))
            os.remove(os.path.join(archive_dir, "book.txt"))
            with self.assertRaises(kg_transfer.KGTransferError) as ctx:
                kg_transfer.export_graph(con, 7, upload, os.path.join(tmp, "g2.zip"),
                                         archive_dir=archive)
            self.assertIn("已归档且归档文件缺失", str(ctx.exception))
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
