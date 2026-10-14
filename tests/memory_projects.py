"""Acceptance checks for user-managed local memory/project continuity.

Actual route/storage code; isolated legacy DB, no live tools or model inference.
"""
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from flask import Flask, jsonify, request

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))
import jarvis_core as core
import memory_store as store


class MemoryProjects(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "nova.db")
        core.DB_PATH = self.path
        core._started = True  # The fixture never runs real workers.
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE sessions(id INTEGER PRIMARY KEY,title TEXT,created_at REAL,updated_at REAL);
                CREATE TABLE facts(id INTEGER PRIMARY KEY,text TEXT,source TEXT,created_at REAL);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id INTEGER,role TEXT,content TEXT,created_at REAL);
                INSERT INTO sessions VALUES(1,'Old chat',10,10);
                INSERT INTO facts VALUES(1,'Legacy favorite is blue','chat',11);
                INSERT INTO messages VALUES(1,1,'user','Original user conversation',10);
            """)
            conn.commit()
        core.init()
        self.app = Flask(__name__)
        self.app.register_blueprint(core.bp)
        self.scope = dict(app=self.app, request=request, jsonify=jsonify, DB_PATH=self.path,
                          memory_store=store, jarvis_core=core, NOVA_PERSONA="You are Nova.")
        names = {"all_facts", "add_fact", "system_message", "get_facts", "post_fact",
                 "save_fact_request", "update_fact", "delete_fact", "restore_fact"}
        tree = ast.parse((ENGINE / "app.py").read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        assert len(nodes) == len(names)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), self.scope)
        self.client = self.app.test_client()

    def tearDown(self):
        self.folder.cleanup()

    def project(self, name, **values):
        response = self.client.post("/projects", json={"name": name, **values})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["id"]

    def memory(self, text, **values):
        response = self.client.post("/facts", json={"text": text, **values})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["id"]

    def test_legacy_migration_preserves_data_and_is_repeatable(self):
        store.init_storage(self.path)
        core.init()
        old = self.client.get("/facts").json[0]
        self.assertEqual((old["id"], old["text"], old["source"], old["created_at"], old["updated_at"]),
                         (1, "Legacy favorite is blue", "chat", 11, 11))
        with store.connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT content FROM messages").fetchone()[0], "Original user conversation")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_user_can_correct_remove_restore_and_recall_after_new_process(self):
        fid = self.memory("I prefer concise replies", kind="preference", pinned=True)
        self.assertEqual(self.client.patch(f"/facts/{fid}", json={"text": "I prefer detailed replies"}).status_code, 200)
        prompt = self.scope["system_message"]("How do I like replies?")["content"]
        self.assertIn("detailed replies", prompt)
        self.assertNotIn("concise replies", prompt)
        self.assertEqual(self.client.delete(f"/facts/{fid}").status_code, 200)
        self.assertNotIn("detailed replies", store.context_text(self.path, "replies"))
        removed = self.client.get("/facts?include_removed=1").json
        self.assertTrue(next(row for row in removed if row["id"] == fid)["deleted_at"])
        self.assertEqual(self.client.post(f"/facts/{fid}/restore").status_code, 200)
        code = ("import sys;sys.path.insert(0,sys.argv[1]);import memory_store as m;"
                "m.init_storage(sys.argv[2]);print(m.context_text(sys.argv[2],'replies'))")
        result = subprocess.run([sys.executable, "-c", code, str(ENGINE), self.path], capture_output=True, text=True, check=True)
        self.assertIn("detailed replies", result.stdout)

    def test_project_scoping_link_unlink_and_removed_context(self):
        first = self.project("Orchid", goal="Build a local garden planner", decisions="Use SQLite", next_step="Draft planting calendar")
        second = self.project("Harbor", goal="Plan the harbor trip")
        self.memory("Use metric planting distances", project_id=first)
        self.memory("Harbor secret test preference", project_id=second)
        global_id = self.memory("I like practical explanations")
        self.assertEqual(self.client.post("/sessions/1/project", json={"project_id": first}).status_code, 200)
        context = store.select_context(self.path, "planting", first)
        text = json.dumps(context)
        self.assertIn("planting calendar", text)
        self.assertIn("metric planting", text)
        self.assertIn("practical explanations", text)
        self.assertNotIn("Harbor", text)
        self.assertNotIn("metric planting", store.context_text(self.path, "planting"))
        self.assertEqual(self.client.post("/sessions/1/project", json={"project_id": None}).status_code, 200)
        self.assertIsNone(self.client.get("/sessions/1/project").json["project"])
        self.client.post("/sessions/1/project", json={"project_id": first})
        self.assertEqual(self.client.delete(f"/projects/{first}").status_code, 200)
        self.assertIsNone(self.client.get("/sessions/1/project").json["project"])
        self.assertNotIn("metric planting", json.dumps(self.client.get("/facts").json))
        self.assertEqual(self.client.post(f"/projects/{first}/restore").status_code, 200)
        self.assertEqual(self.client.get("/sessions/1/project").json["project"]["id"], first)

    def test_chat_memory_keeps_origin_and_deduplicates_only_within_scope(self):
        pid = self.project("Orchid")
        fid = self.scope["add_fact"]("Use green labels", "chat", project_id=pid, source_session_id=1)
        self.assertEqual(fid, self.scope["add_fact"]("use green labels", "chat", project_id=pid, source_session_id=1))
        other = self.memory("Use green labels")
        self.assertNotEqual(fid, other)
        row = next(row for row in self.client.get("/facts").json if row["id"] == fid)
        self.assertEqual((row["source"], row["source_session_id"], row["project_id"]), ("chat", 1, pid))

    def test_relevance_budget_does_not_hide_related_facts_behind_many_preferences(self):
        for index in range(30):
            self.memory(f"Preference {index}: use readable prose", pinned=True)
        self.memory("Orchid's calibration code is 47", kind="fact", pinned=False)
        for index in range(20):
            self.memory(f"Recent unrelated item {index}", kind="fact", pinned=False)
        context = store.select_context(self.path, "What is Orchid calibration code?", max_memories=8, max_chars=1600)
        self.assertLessEqual(len(context["memories"]), 8)
        self.assertLessEqual(sum(len(row["text"]) for row in context["memories"]), 1600)
        self.assertIn("calibration code is 47", json.dumps(context))
        self.assertIn("do not grant tool access", store.context_text(self.path, "calibration"))

    def test_invalid_updates_and_links_are_rejected_without_changing_records(self):
        pid = self.project("Valid")
        fid = self.memory("Valid memory")
        for payload in ({"text": ""}, {"text": "x" * 4001}, {"pinned": "true"}, {"kind": "unknown"},
                        {"project_id": True}, {"project_id": 999}):
            self.assertIn(self.client.patch(f"/facts/{fid}", json=payload).status_code, (400, 404))
        for payload in ({"name": ""}, {"state": "unknown"}, {"files": "bad"}, {"files": ["first\rsecond"]},
                        {"path": str(Path(self.folder.name) / "missing")}, {"goal": 3}):
            self.assertEqual(self.client.patch(f"/projects/{pid}", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/sessions/999/project", json={"project_id": pid}).status_code, 404)
        self.assertEqual(self.client.post("/sessions/1/project", json={}).status_code, 400)
        self.assertEqual(self.client.get(f"/projects/{pid}").json["name"], "Valid")
        self.assertEqual(self.client.get("/facts").json[0]["text"], "Valid memory")
        self.assertEqual(self.client.post("/projects", json={"name": "Valid"}).status_code, 400)

    def test_project_job_context_does_not_grant_changing_capabilities(self):
        pid = self.project("Orchid", next_step="Draft a plan")
        response = self.client.post("/jobs", json={"prompt": "Draft the next step", "project_id": pid,
                                                   "caps": {"web": False, "write": False, "exec": False, "control": False}})
        self.assertEqual(response.status_code, 200)
        job = self.client.get("/jobs/" + response.json["id"]).json
        self.assertEqual(job["project_id"], pid)
        self.assertFalse(any(job["capabilities"].values()))
        self.assertEqual(job["approval_mode"], "ask")
        self.assertEqual(self.client.post("/jobs", json={"prompt": "Draft", "project_id": 999}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
