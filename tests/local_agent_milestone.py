"""Isolated durable-task and local-only boundary checks for Nova Native."""
import json
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import jarvis_core as core


with tempfile.TemporaryDirectory() as folder:
    core.DB_PATH = str(Path(folder) / "nova.db")
    core._started = True  # No background threads or real model calls.
    with closing(sqlite3.connect(core.DB_PATH)) as conn:
        conn.executescript("""
            CREATE TABLE sessions(id INTEGER PRIMARY KEY,title TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE scheduled_tasks(id INTEGER PRIMARY KEY,name TEXT,prompt TEXT,
                schedule_type TEXT,schedule_value TEXT,session_id INTEGER,enabled INTEGER,
                last_run REAL,next_run REAL,created_at REAL);
            INSERT INTO sessions(id,title,created_at,updated_at) VALUES(1,'fixture',1,1);
        """)
        conn.commit()
    core.init()
    app = Flask(__name__)
    app.register_blueprint(core.bp)
    client = app.test_client()

    assert client.post("/jobs", json={"prompt": "test", "provider": "codex"}).status_code == 400
    assert client.post("/jobs", json={"prompt": "test", "approval_mode": "bypass"}).status_code == 400
    created = client.post("/jobs", json={"name": "Local fixture", "prompt": "Summarize fixture",
                                         "workspace": folder}).json
    jid = created["id"]
    detail = client.get(f"/jobs/{jid}").json
    assert detail["provider"] == "local" and detail["approval_mode"] == "ask"
    assert client.post(f"/jobs/{jid}/pause").json["status"] == "paused"
    assert client.post(f"/jobs/{jid}/resume").status_code == 200
    assert client.post(f"/jobs/{jid}/cancel").json["status"] == "cancelled"
    assert client.post(f"/jobs/{jid}/retry").status_code == 200

    # A scheduled occurrence is committed with the job and cannot be claimed twice.
    due = time.time() - 10
    with core.db() as conn:
        conn.execute("""INSERT INTO scheduled_tasks(id,name,prompt,schedule_type,schedule_value,
            session_id,enabled,next_run,created_at) VALUES(7,'Read-only brief','Summarize news',
            'daily','09:00',1,1,?,?)""", (due, due))
        task = dict(conn.execute("SELECT * FROM scheduled_tasks WHERE id=7").fetchone())
    scheduled_id = core.enqueue_scheduled_task(task, time.time() + 86400)
    assert scheduled_id and core.enqueue_scheduled_task(task, time.time() + 86400) is None
    with core.db() as conn:
        row = conn.execute("SELECT source,provider,schedule_key,capabilities FROM jobs WHERE id=?",
                           (scheduled_id,)).fetchone()
        assert row["source"] == "schedule" and row["provider"] == "local"
        assert row["schedule_key"] and not any(json.loads(row["capabilities"])[k]
                                               for k in ("write", "exec", "control"))

        # A crash records interruption and leaves the checkpoint for reviewed retry.
        checkpoint = json.dumps({"partial_result": "Found one source"})
        conn.execute("UPDATE jobs SET status='running',checkpoint=? WHERE id=?", (checkpoint, scheduled_id))
    core.init()
    interrupted = client.get(f"/jobs/{scheduled_id}").json
    assert interrupted["status"] == "interrupted" and interrupted["checkpoint"] == checkpoint
    assert client.post(f"/jobs/{scheduled_id}/retry").status_code == 200
    inbox = client.get("/inbox").json
    assert any(item["kind"] == "interrupted" and item["ref_id"] == scheduled_id for item in inbox)
    assert client.post(f"/inbox/{inbox[0]['id']}/read").status_code == 200

    # Capabilities that could change the machine cannot be silently replayed.
    changing = client.post("/jobs", json={"prompt": "fixture", "workspace": folder,
                                         "caps": {"write": True}}).json["id"]
    assert client.post(f"/jobs/{changing}/pause").status_code == 200
    assert client.post(f"/jobs/{changing}/resume").status_code == 409

    # Exercise the actual worker stream without invoking a model or a real tool.
    seen_payloads = []
    original_get, original_post = core.requests.get, core.requests.post

    class FakeResponse:
        ok = True

        def __init__(self, lines=()):
            self.lines = lines

        def raise_for_status(self):
            pass

        def iter_lines(self, **_kwargs):
            yield from self.lines

        def close(self):
            pass

    def fake_get(*_args, **_kwargs):
        return FakeResponse()

    def fake_post(url, **kwargs):
        if url.endswith("/chat/stop"):
            return FakeResponse()
        assert url.endswith("/chat")
        seen_payloads.append(kwargs["json"])
        if kwargs["json"]["turn_id"] == scheduled_id:
            return FakeResponse([json.dumps({"token": "Verified result"}).encode(),
                                 json.dumps({"done": True}).encode()])
        if kwargs["json"]["turn_id"] == pause_id:
            def lines():
                yield json.dumps({"token": "Partial result"}).encode()
                assert client.post(f"/jobs/{pause_id}/pause").status_code == 200
                yield json.dumps({"done": True}).encode()
            return FakeResponse(lines())
        raise AssertionError("Unexpected job")

    core.requests.get, core.requests.post = fake_get, fake_post
    try:
        core._run_job(scheduled_id)
        completed = client.get(f"/jobs/{scheduled_id}").json
        assert completed["status"] == "completed" and completed["result"] == "Verified result"
        assert seen_payloads[0]["provider"] == "local" and seen_payloads[0]["approval_mode"] == "ask"
        assert "Found one source" in seen_payloads[0]["message"]

        pause_id = client.post("/jobs", json={"prompt": "Read fixture", "workspace": folder}).json["id"]
        core._run_job(pause_id)
        paused = client.get(f"/jobs/{pause_id}").json
        assert paused["status"] == "paused" and "Partial result" in paused["checkpoint"]
        assert client.post(f"/jobs/{pause_id}/resume").status_code == 200
    finally:
        core.requests.get, core.requests.post = original_get, original_post

    queued_id = client.post("/jobs", json={"prompt": "Read-only queued fixture",
                                            "workspace": folder}).json["id"]
    assert client.post("/background-control", json={"enabled": False}).json["enabled"] is False
    assert client.get(f"/jobs/{queued_id}").json["status"] == "paused"
    assert client.post("/jobs", json={"prompt": "blocked"}).status_code == 400
    assert client.post(f"/jobs/{queued_id}/resume").status_code == 409
    assert client.post("/background-control", json={"enabled": True}).json["enabled"] is True
    assert client.post(f"/jobs/{queued_id}/resume").status_code == 200

    print("PASS local routing, durable schedule claim, worker checkpoint, pause/resume, global stop, restart, inbox, replay guard")
