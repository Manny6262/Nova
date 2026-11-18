"""Durable Jarvis services for Nova: projects, background work and event awareness."""
from flask import Blueprint, jsonify, request
import ctypes
import json
import os
import platform
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid

import requests


bp = Blueprint("jarvis", __name__)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "nova.db")
PORT = int(os.environ.get("PORT", 5000))
WORKER_COUNT = 2

_started = False
_job_queue = queue.Queue()
_stop = {}
_stop_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, name, declaration):
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def init():
    """Migrate storage, recover safe work, then start the worker/event services."""
    global _started
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          path TEXT NOT NULL, description TEXT DEFAULT '', created_at REAL NOT NULL, active INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
          status TEXT NOT NULL, progress INTEGER DEFAULT 0, session_id INTEGER, project_id INTEGER,
          result TEXT DEFAULT '', error TEXT DEFAULT '', created_at REAL NOT NULL, started_at REAL, finished_at REAL);
        CREATE TABLE IF NOT EXISTS job_events(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          ts REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, detail TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS monitors(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          target TEXT NOT NULL, interval_seconds INTEGER DEFAULT 60, enabled INTEGER DEFAULT 1,
          last_status TEXT, last_detail TEXT, last_checked REAL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS event_rules(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          kind TEXT NOT NULL, target TEXT DEFAULT '', enabled INTEGER DEFAULT 0, action_prompt TEXT DEFAULT '',
          cooldown_seconds INTEGER DEFAULT 300, last_value TEXT, last_triggered REAL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
          title TEXT NOT NULL, detail TEXT DEFAULT '', status TEXT DEFAULT 'info', ref_id TEXT);
        CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, ts);
        """)
        for name, declaration in (
            ("workspace", "TEXT DEFAULT ''"), ("provider", "TEXT DEFAULT 'codex'"),
            ("model", "TEXT DEFAULT ''"), ("capabilities", "TEXT DEFAULT '{}'"),
            ("approval_mode", "TEXT DEFAULT 'ask'"), ("stage", "TEXT DEFAULT 'Queued'"),
            ("pending_token", "TEXT"), ("attempts", "INTEGER DEFAULT 0"),
            ("source", "TEXT DEFAULT 'manual'"), ("event_rule_id", "INTEGER"),
        ):
            _add_column(c, "jobs", name, declaration)

        # Never automatically repeat an in-flight command after a crash.
        c.execute("""UPDATE jobs SET status='interrupted', stage='Interrupted by restart',
                     error='Nova restarted while this task was active. Review it, then retry.',
                     pending_token=NULL, finished_at=?
                     WHERE status IN ('running','waiting_approval')""", (time.time(),))
        if not c.execute("SELECT 1 FROM event_rules WHERE kind='usb' LIMIT 1").fetchone():
            c.execute("""INSERT INTO event_rules(name,kind,target,enabled,action_prompt,cooldown_seconds,created_at)
                         VALUES('USB drives','usb','',0,'',5,?)""", (time.time(),))
        queued = [row[0] for row in c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at")]

    if _started:
        return
    _started = True
    for index in range(WORKER_COUNT):
        threading.Thread(target=_worker_loop, name=f"nova-worker-{index + 1}", daemon=True).start()
    threading.Thread(target=_monitor_loop, name="nova-monitor", daemon=True).start()
    threading.Thread(target=_event_loop, name="nova-event-awareness", daemon=True).start()
    for jid in queued:
        _job_queue.put(jid)


def event(kind, title, detail="", status="info", ref_id=None):
    try:
        with db() as c:
            c.execute("INSERT INTO activity(ts,kind,title,detail,status,ref_id) VALUES(?,?,?,?,?,?)",
                      (time.time(), kind, title, str(detail)[:5000], status,
                       str(ref_id) if ref_id is not None else None))
    except Exception:
        pass


def _job_event(jid, kind, message, detail=""):
    with db() as c:
        c.execute("INSERT INTO job_events(job_id,ts,kind,message,detail) VALUES(?,?,?,?,?)",
                  (jid, time.time(), kind, str(message)[:500], str(detail)[:5000]))


def _create_job(body, source="manual", event_rule_id=None):
    prompt = str(body.get("prompt", "")).strip()
    name = str(body.get("name") or prompt[:60] or "Background task").strip()
    if not prompt:
        raise ValueError("A task instruction is required.")
    workspace = os.path.abspath(str(body.get("workspace") or APP_DIR))
    if not os.path.isdir(workspace):
        raise ValueError("Choose an existing working folder.")
    provider = body.get("provider", "codex")
    if provider not in ("codex", "local"):
        provider = "codex"
    approval = body.get("approval_mode", "ask")
    if approval not in ("ask", "auto", "bypass"):
        approval = "ask"
    caps = body.get("caps") or {"write": False, "exec": False, "web": True, "control": False}
    caps = {key: bool(caps.get(key)) for key in ("write", "exec", "web", "control")}
    jid = uuid.uuid4().hex
    with db() as c:
        c.execute("""INSERT INTO jobs(id,name,prompt,status,progress,project_id,workspace,provider,model,
                     capabilities,approval_mode,stage,created_at,source,event_rule_id)
                     VALUES(?,?,?,'queued',0,?,?,?,?,?,?,'Queued',?,?,?)""",
                  (jid, name, prompt, body.get("project_id"), workspace, provider,
                   str(body.get("model") or ""), json.dumps(caps), approval,
                   time.time(), source, event_rule_id))
    _job_event(jid, "queued", "Task queued", workspace)
    event("job", name, "Queued for a background worker", "queued", jid)
    _job_queue.put(jid)
    return jid


@bp.get("/command-center/summary")
def summary():
    with db() as c:
        counts = {key: c.execute(sql).fetchone()[0] for key, sql in {
            "projects": "SELECT count(*) FROM projects",
            "running": "SELECT count(*) FROM jobs WHERE status IN ('running','waiting_approval')",
            "queued": "SELECT count(*) FROM jobs WHERE status='queued'",
            "monitors": "SELECT count(*) FROM monitors WHERE enabled=1",
            "events": "SELECT count(*) FROM event_rules WHERE enabled=1",
        }.items()}
    counts["workers"] = WORKER_COUNT
    return jsonify(counts)


@bp.get("/activity")
def activity():
    limit = min(500, max(1, int(request.args.get("limit", 150))))
    with db() as c:
        rows = c.execute("SELECT * FROM activity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([dict(row) for row in rows])


@bp.route("/projects", methods=["GET", "POST"])
def projects():
    if request.method == "GET":
        with db() as c:
            rows = c.execute("SELECT * FROM projects ORDER BY active DESC,name").fetchall()
        return jsonify([dict(row) for row in rows])
    body = request.json or {}
    name = str(body.get("name", "")).strip()
    path = os.path.abspath(str(body.get("path", "")).strip())
    if not name or not os.path.isdir(path):
        return jsonify(error="Name and an existing folder are required."), 400
    with db() as c:
        cur = c.execute("INSERT INTO projects(name,path,description,created_at) VALUES(?,?,?,?)",
                        (name, path, str(body.get("description", ""))[:1000], time.time()))
    event("project", "Project added", f"{name} · {path}", "ok", cur.lastrowid)
    return jsonify(id=cur.lastrowid, name=name, path=path)


@bp.delete("/projects/<int:pid>")
def project_delete(pid):
    with db() as c:
        c.execute("DELETE FROM projects WHERE id=? AND active=0", (pid,))
    event("project", "Project removed", str(pid), "warning", pid)
    return jsonify(ok=True)


@bp.route("/jobs", methods=["GET", "POST"])
def jobs():
    if request.method == "GET":
        with db() as c:
            rows = c.execute("""SELECT j.*,
                (SELECT message FROM job_events e WHERE e.job_id=j.id ORDER BY e.id DESC LIMIT 1) AS last_event
                FROM jobs j ORDER BY j.created_at DESC LIMIT 100""").fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["capabilities"] = json.loads(item.get("capabilities") or "{}")
            item.pop("pending_token", None)
            output.append(item)
        return jsonify(output)
    try:
        jid = _create_job(request.json or {})
        return jsonify(id=jid, status="queued")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@bp.get("/jobs/<jid>")
def job_detail(jid):
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        events = c.execute("SELECT ts,kind,message,detail FROM job_events WHERE job_id=? ORDER BY id", (jid,)).fetchall()
    if not row:
        return jsonify(error="Task not found."), 404
    item = dict(row)
    item["capabilities"] = json.loads(item.get("capabilities") or "{}")
    item["awaiting_approval"] = bool(item.pop("pending_token", None))
    item["events"] = [dict(evt) for evt in events]
    return jsonify(item)


def _signal_confirmation(token, approved):
    if not token:
        return False
    try:
        response = requests.post(f"http://127.0.0.1:{PORT}/confirm",
                                 json={"token": token, "approved": bool(approved)}, timeout=3)
        return response.ok
    except Exception:
        return False


@bp.post("/jobs/<jid>/approval")
def job_approval(jid):
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or type(body.get("approved")) is not bool:
        return jsonify(error="A boolean approval is required."), 400
    approved = body["approved"]
    with db() as c:
        row = c.execute("SELECT pending_token,status FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row or row["status"] != "waiting_approval" or not row["pending_token"]:
        return jsonify(error="This task is not waiting for approval."), 409
    if not _signal_confirmation(row["pending_token"], approved):
        return jsonify(error="The approval expired or the worker is unavailable."), 409
    with db() as c:
        c.execute("UPDATE jobs SET status='running',stage=?,pending_token=NULL WHERE id=?",
                  ("Approval granted" if approved else "Action declined", jid))
    _job_event(jid, "approval", "Approved" if approved else "Declined")
    return jsonify(ok=True)


@bp.post("/jobs/<jid>/cancel")
def cancel_job(jid):
    with db() as c:
        row = c.execute("SELECT status,pending_token FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return jsonify(error="Task not found."), 404
        if row["status"] == "queued":
            c.execute("UPDATE jobs SET status='cancelled',stage='Cancelled',finished_at=? WHERE id=?",
                      (time.time(), jid))
    if row["pending_token"]:
        _signal_confirmation(row["pending_token"], False)
    with _stop_lock:
        if jid in _stop:
            _stop[jid].set()
    _job_event(jid, "cancel", "Cancellation requested")
    return jsonify(ok=True)


@bp.post("/jobs/<jid>/retry")
def retry_job(jid):
    with db() as c:
        row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return jsonify(error="Task not found."), 404
        if row["status"] not in ("failed", "cancelled", "interrupted"):
            return jsonify(error="Only failed, cancelled, or interrupted tasks can be retried."), 409
        c.execute("""UPDATE jobs SET status='queued',progress=0,stage='Queued for retry',result='',error='',
                     pending_token=NULL,started_at=NULL,finished_at=NULL WHERE id=?""", (jid,))
    _job_event(jid, "retry", "Task queued for retry")
    _job_queue.put(jid)
    return jsonify(ok=True)


def _wait_for_server(stop):
    deadline = time.time() + 30
    while time.time() < deadline and not stop.is_set():
        try:
            if requests.get(f"http://127.0.0.1:{PORT}/", timeout=1).ok:
                return True
        except Exception:
            pass
        stop.wait(0.5)
    return False


def _worker_loop():
    while True:
        jid = _job_queue.get()
        try:
            _run_job(jid)
        finally:
            _job_queue.task_done()


def _run_job(jid):
    stop = threading.Event()
    with _stop_lock:
        _stop[jid] = stop
    try:
        with db() as c:
            claimed = c.execute("""UPDATE jobs SET status='running',progress=3,stage='Worker claimed task',
                                 started_at=?,finished_at=NULL,attempts=coalesce(attempts,0)+1
                                 WHERE id=? AND status='queued'""", (time.time(), jid)).rowcount
            if not claimed:
                return
            job = dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
            if not job.get("session_id"):
                now = time.time()
                cur = c.execute("INSERT INTO sessions(title,created_at,updated_at) VALUES(?,?,?)",
                                ("⚙ " + job["name"][:70], now, now))
                job["session_id"] = cur.lastrowid
                c.execute("UPDATE jobs SET session_id=? WHERE id=?", (job["session_id"], jid))
        _job_event(jid, "start", "Background worker started", job.get("workspace") or APP_DIR)
        event("job", job["name"], "Background worker started", "running", jid)
        if not _wait_for_server(stop):
            raise RuntimeError("Nova's local command service did not become ready.")

        payload = {
            "message": job["prompt"], "session_id": job["session_id"],
            "provider": job.get("provider") or "codex", "model": job.get("model") or "",
            "caps": json.loads(job.get("capabilities") or "{}"),
            "approval_mode": job.get("approval_mode") or "ask",
            "think": False, "voice_mode": False,
        }
        response = requests.post(f"http://127.0.0.1:{PORT}/chat", json=payload,
                                 stream=True, timeout=(10, 1800))
        response.raise_for_status()
        result = ""
        progress = 8
        for raw in response.iter_lines(decode_unicode=True, chunk_size=1):
            if stop.is_set():
                response.close()
                raise InterruptedError("Cancelled by Manuel")
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            stage = None
            if item.get("provider_label"):
                stage = "Connected to " + item["provider_label"]
                _job_event(jid, "provider", stage)
            if item.get("thought"):
                stage = "Reasoning"
            if item.get("tool_start"):
                tool = item["tool_start"]
                stage = "Using " + str(tool.get("name", "tool")).replace("_", " ")
                _job_event(jid, "tool_start", stage, json.dumps(tool.get("args") or {})[:3000])
                progress = min(82, progress + 8)
            if item.get("tool_result"):
                tool = item["tool_result"]
                stage = ("Finished " if tool.get("ok") else "Problem with ") + str(tool.get("name", "tool")).replace("_", " ")
                _job_event(jid, "tool_result", stage, tool.get("detail") or tool.get("preview") or "")
                progress = min(88, progress + 6)
            if item.get("confirm_request"):
                confirm = item["confirm_request"]
                stage = "Waiting for your approval: " + str(confirm.get("summary") or confirm.get("name") or "action")
                with db() as c:
                    c.execute("UPDATE jobs SET status='waiting_approval',stage=?,pending_token=? WHERE id=?",
                              (stage, confirm.get("token"), jid))
                _job_event(jid, "approval", stage, confirm.get("detail") or "")
                event("job", job["name"], stage, "warning", jid)
                continue
            token = item.get("token")
            if token:
                result += token
                progress = min(94, progress + 1)
                stage = "Writing result"
            if item.get("error"):
                raise RuntimeError(str(item["error"]))
            if stage and not item.get("confirm_request"):
                with db() as c:
                    c.execute("UPDATE jobs SET status='running',progress=?,stage=?,pending_token=NULL WHERE id=?",
                              (progress, stage, jid))

        if stop.is_set():
            raise InterruptedError("Cancelled by Manuel")
        with db() as c:
            c.execute("""UPDATE jobs SET status='completed',progress=100,stage='Completed',result=?,error='',
                         pending_token=NULL,finished_at=? WHERE id=?""", (result[-30000:], time.time(), jid))
        _job_event(jid, "complete", "Task completed", result[-5000:])
        event("job", job["name"], result[-500:] or "Completed", "ok", jid)
    except InterruptedError as exc:
        with db() as c:
            c.execute("""UPDATE jobs SET status='cancelled',stage='Cancelled',error=?,pending_token=NULL,
                         finished_at=? WHERE id=?""", (str(exc), time.time(), jid))
        _job_event(jid, "cancelled", "Task cancelled", str(exc))
        event("job", "Background task cancelled", str(exc), "warning", jid)
    except Exception as exc:
        with db() as c:
            c.execute("""UPDATE jobs SET status='failed',stage='Failed',error=?,pending_token=NULL,
                         finished_at=? WHERE id=?""", (str(exc)[:3000], time.time(), jid))
        _job_event(jid, "failed", "Task failed", str(exc))
        event("job", "Background task failed", str(exc), "error", jid)
    finally:
        with _stop_lock:
            _stop.pop(jid, None)


@bp.route("/monitors", methods=["GET", "POST"])
def monitors():
    if request.method == "GET":
        with db() as c:
            rows = c.execute("SELECT * FROM monitors ORDER BY created_at DESC").fetchall()
        return jsonify([dict(row) for row in rows])
    body = request.json or {}
    kind = body.get("kind")
    target = str(body.get("target", "")).strip()
    name = str(body.get("name") or target).strip()
    if kind not in ("url", "path", "process") or not target:
        return jsonify(error="Choose URL, path, or process and provide a target."), 400
    try:
        interval = max(15, int(body.get("interval_seconds", 60)))
        if interval > 31536000:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        return jsonify(error="Monitor interval must be a number of seconds up to one year."), 400
    with db() as c:
        cur = c.execute("INSERT INTO monitors(name,kind,target,interval_seconds,created_at) VALUES(?,?,?,?,?)",
                        (name, kind, target, interval, time.time()))
    event("monitor", "Monitor added", f"{kind}: {target}", "ok", cur.lastrowid)
    return jsonify(id=cur.lastrowid)


@bp.post("/monitors/<int:mid>/toggle")
def monitor_toggle(mid):
    with db() as c:
        c.execute("UPDATE monitors SET enabled=1-enabled WHERE id=?", (mid,))
    return jsonify(ok=True)


@bp.delete("/monitors/<int:mid>")
def monitor_delete(mid):
    with db() as c:
        c.execute("DELETE FROM monitors WHERE id=?", (mid,))
    return jsonify(ok=True)


def _process_running(name):
    output = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"], capture_output=True,
                            text=True, timeout=8,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout
    return name.lower() in output.lower()


def _check_monitor(item):
    if item["kind"] == "url":
        response = requests.get(item["target"], timeout=8)
        return ("up" if response.ok else "down"), f"HTTP {response.status_code}"
    if item["kind"] == "path":
        exists = os.path.exists(item["target"])
        return ("up" if exists else "down"), ("available" if exists else "missing")
    found = _process_running(item["target"])
    return ("up" if found else "down"), ("running" if found else "not running")


def _monitor_loop():
    time.sleep(5)
    while True:
        try:
            now = time.time()
            with db() as c:
                rows = c.execute("""SELECT * FROM monitors WHERE enabled=1 AND
                    (last_checked IS NULL OR last_checked+interval_seconds<=?)""", (now,)).fetchall()
            for row in rows:
                item = dict(row)
                try:
                    status, detail = _check_monitor(item)
                except Exception as exc:
                    status, detail = "down", str(exc)
                with db() as c:
                    c.execute("UPDATE monitors SET last_status=?,last_detail=?,last_checked=? WHERE id=?",
                              (status, detail, now, item["id"]))
                if status != item.get("last_status"):
                    event("monitor", item["name"], detail, "ok" if status == "up" else "error", item["id"])
        except Exception:
            pass
        time.sleep(5)


def _removable_drives():
    if os.name != "nt":
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drives = []
    for index in range(26):
        if mask & (1 << index):
            root = f"{chr(65 + index)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:
                drives.append(root)
    return drives


def _event_value(rule):
    kind, target = rule["kind"], rule["target"]
    if kind == "usb":
        return json.dumps(_removable_drives())
    if kind == "process":
        return "running" if _process_running(target) else "stopped"
    if kind == "folder":
        if not os.path.isdir(target):
            return "missing"
        # Use a stable directory fingerprint. If the watched folder is NovaChat,
        # ignore Nova's own database/audit churn so recording an event cannot
        # trigger the same rule again in a feedback loop.
        internal = {
            os.path.normcase(DB_PATH),
            os.path.normcase(DB_PATH + "-wal"),
            os.path.normcase(DB_PATH + "-shm"),
            os.path.normcase(os.path.join(APP_DIR, "nova_audit.jsonl")),
        }
        entries = []
        for name in sorted(os.listdir(target)):
            path = os.path.join(target, name)
            if not os.path.isfile(path) or os.path.normcase(os.path.abspath(path)) in internal:
                continue
            try:
                stat = os.stat(path)
                entries.append((name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return json.dumps(entries, separators=(",", ":"))
    raise ValueError("Unsupported event type")


@bp.route("/event-rules", methods=["GET", "POST"])
def event_rules():
    if request.method == "GET":
        with db() as c:
            rows = c.execute("SELECT * FROM event_rules ORDER BY id").fetchall()
        return jsonify([dict(row) for row in rows])
    body = request.json or {}
    kind = body.get("kind")
    target = str(body.get("target", "")).strip()
    if kind not in ("usb", "process", "folder"):
        return jsonify(error="Choose USB, application process, or folder activity."), 400
    if kind != "usb" and not target:
        return jsonify(error="This event type needs a target."), 400
    if kind == "folder" and not os.path.isdir(target):
        return jsonify(error="Choose an existing folder."), 400
    name = str(body.get("name") or f"{kind}: {target}").strip()
    action = str(body.get("action_prompt") or "").strip()
    with db() as c:
        cur = c.execute("""INSERT INTO event_rules(name,kind,target,enabled,action_prompt,cooldown_seconds,created_at)
                         VALUES(?,?,?,1,?,?,?)""",
                        (name, kind, target, action, max(5, int(body.get("cooldown_seconds", 300))), time.time()))
    event("awareness", "Event awareness enabled", name, "ok", cur.lastrowid)
    return jsonify(id=cur.lastrowid)


@bp.post("/event-rules/<int:rid>/toggle")
def event_rule_toggle(rid):
    with db() as c:
        c.execute("UPDATE event_rules SET enabled=1-enabled,last_value=NULL WHERE id=?", (rid,))
    return jsonify(ok=True)


@bp.delete("/event-rules/<int:rid>")
def event_rule_delete(rid):
    with db() as c:
        row = c.execute("SELECT kind FROM event_rules WHERE id=?", (rid,)).fetchone()
        if row and row["kind"] == "usb":
            return jsonify(error="The built-in USB rule can be paused but not removed."), 409
        c.execute("DELETE FROM event_rules WHERE id=?", (rid,))
    return jsonify(ok=True)


def _describe_event(rule, old_value, new_value):
    if rule["kind"] == "usb":
        old, new = set(json.loads(old_value or "[]")), set(json.loads(new_value or "[]"))
        added, removed = sorted(new - old), sorted(old - new)
        if added:
            return "USB connected: " + ", ".join(added)
        if removed:
            return "USB removed: " + ", ".join(removed)
    if rule["kind"] == "process":
        return f"{rule['target']} is now {new_value}"
    return f"Folder activity detected in {rule['target']}"


def _event_loop():
    time.sleep(6)
    while True:
        try:
            with db() as c:
                rules = c.execute("SELECT * FROM event_rules WHERE enabled=1").fetchall()
            for row in rules:
                rule = dict(row)
                try:
                    value = _event_value(rule)
                except Exception as exc:
                    event("awareness", rule["name"], str(exc), "error", rule["id"])
                    continue
                old = rule.get("last_value")
                with db() as c:
                    c.execute("UPDATE event_rules SET last_value=? WHERE id=?", (value, rule["id"]))
                if old is None or old == value:
                    continue
                detail = _describe_event(rule, old, value)
                now = time.time()
                event("awareness", rule["name"], detail, "ok", rule["id"])
                if rule.get("action_prompt") and now - (rule.get("last_triggered") or 0) >= rule["cooldown_seconds"]:
                    prompt = rule["action_prompt"] + "\n\nTrigger context: " + detail
                    try:
                        _create_job({"name": f"Event: {rule['name']}", "prompt": prompt,
                                     "workspace": APP_DIR, "provider": "codex",
                                     "caps": {"web": True, "write": False, "exec": False, "control": False},
                                     "approval_mode": "ask"}, source="event", event_rule_id=rule["id"])
                        with db() as c:
                            c.execute("UPDATE event_rules SET last_triggered=? WHERE id=?", (now, rule["id"]))
                    except Exception as exc:
                        event("awareness", "Event action failed to queue", str(exc), "error", rule["id"])
        except Exception:
            pass
        time.sleep(3)


@bp.get("/diagnostics")
def diagnostics():
    checks = []
    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    try:
        response = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        add("Ollama", response.ok, f"HTTP {response.status_code}")
    except Exception as exc:
        add("Ollama", False, str(exc))
    add("Background workers", _started, f"{WORKER_COUNT} durable workers")
    add("Event awareness", _started, "Explicit rules only; USB awareness is off by default")
    add("Database", os.path.isfile(DB_PATH),
        f"{os.path.getsize(DB_PATH)//1024} KB" if os.path.isfile(DB_PATH) else "missing")
    add("Kokoro voice", os.path.isfile(os.path.join(APP_DIR, "kokoro", "kokoro-v1.0.onnx")), "natural voice model")
    add("Whisper", True, "loads on first transcription")
    add("Disk space", shutil.disk_usage(APP_DIR).free > 2 * 1024**3,
        f"{shutil.disk_usage(APP_DIR).free/1024**3:.1f} GB free")
    add("Command service", shutil.which("codex") is not None,
        "Codex CLI available" if shutil.which("codex") else "Codex CLI not found")
    add("Platform", True, f"{platform.system()} {platform.release()} · Python {platform.python_version()}")
    return jsonify(checks)
