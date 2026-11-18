"""Live CRUD, persistence-worker, monitor, and event-awareness checks with cleanup."""

import os
import shutil
import sqlite3
import tempfile
import time
import uuid

import requests


BASE = "http://127.0.0.1:5000"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "nova.db")
http = requests.Session()
http.trust_env = False
tag = "NOVA_AUDIT_" + uuid.uuid4().hex[:10]
folder = tempfile.mkdtemp(prefix="nova-stateful-audit-", dir=os.path.dirname(__file__))
created = {"project": None, "monitor": None, "rule": None, "schedule": None,
           "schedule_session": None, "job": None, "job_session": None}


def check(label, condition):
    print(("PASS " if condition else "FAIL ") + label, flush=True)
    if not condition:
        raise AssertionError(label)


try:
    response = http.post(BASE + "/projects", json={
        "name": tag, "path": folder, "description": "Synthetic stateful audit"
    }, timeout=10)
    response.raise_for_status()
    created["project"] = response.json()["id"]
    check("project create/read", any(
        row["id"] == created["project"] for row in http.get(BASE + "/projects").json()
    ))

    response = http.post(BASE + "/monitors", json={
        "name": tag, "kind": "path", "target": folder, "interval_seconds": 15
    }, timeout=10)
    response.raise_for_status()
    created["monitor"] = response.json()["id"]
    deadline = time.time() + 15
    monitor = None
    while time.time() < deadline:
        monitor = next((row for row in http.get(BASE + "/monitors").json()
                        if row["id"] == created["monitor"]), None)
        if monitor and monitor.get("last_status"):
            break
        time.sleep(1)
    check("path monitor executes", monitor and monitor.get("last_status") == "up")
    http.post(BASE + f"/monitors/{created['monitor']}/toggle", timeout=10).raise_for_status()
    monitor = next(row for row in http.get(BASE + "/monitors").json()
                   if row["id"] == created["monitor"])
    check("monitor toggle persists", monitor["enabled"] == 0)

    response = http.post(BASE + "/event-rules", json={
        "name": tag, "kind": "folder", "target": folder,
        "action_prompt": "", "cooldown_seconds": 30,
    }, timeout=10)
    response.raise_for_status()
    created["rule"] = response.json()["id"]
    http.post(BASE + f"/event-rules/{created['rule']}/toggle", timeout=10).raise_for_status()
    rule = next(row for row in http.get(BASE + "/event-rules").json()
                if row["id"] == created["rule"])
    check("event rule create/toggle", rule["enabled"] == 0)

    response = http.post(BASE + "/scheduled", json={
        "name": tag, "prompt": "Reply only SCHEDULE_OK.",
        "schedule_type": "interval", "schedule_value": "525600",
    }, timeout=10)
    response.raise_for_status()
    created["schedule"] = response.json()["id"]
    created["schedule_session"] = response.json()["session_id"]
    http.post(BASE + f"/scheduled/{created['schedule']}/toggle", timeout=10).raise_for_status()
    scheduled = next(row for row in http.get(BASE + "/scheduled").json()
                     if row["id"] == created["schedule"])
    check("schedule create/toggle", scheduled["enabled"] == 0)

    response = http.post(BASE + "/jobs", json={
        "name": tag,
        "prompt": "Synthetic background worker audit. Do not use tools. Reply only JOB_OK.",
        "workspace": folder, "provider": "local", "model": "nova:latest",
        "caps": {"write": False, "exec": False, "web": False, "control": False},
        "approval_mode": "ask",
    }, timeout=10)
    response.raise_for_status()
    created["job"] = response.json()["id"]
    deadline = time.time() + 90
    job = None
    while time.time() < deadline:
        job = http.get(BASE + f"/jobs/{created['job']}", timeout=10).json()
        if job.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            break
        time.sleep(1)
    created["job_session"] = job.get("session_id") if job else None
    check("background worker completes", job and job.get("status") == "completed")
    print("BACKGROUND_RESULT " + repr((job.get("result") or "")[-500:]), flush=True)
    check("background worker returns model output", "JOB_OK" in (job.get("result") or ""))
finally:
    if created["schedule"] is not None:
        http.delete(BASE + f"/scheduled/{created['schedule']}", timeout=10)
    if created["schedule_session"] is not None:
        http.delete(BASE + f"/sessions/{created['schedule_session']}", timeout=10)
    if created["rule"] is not None:
        http.delete(BASE + f"/event-rules/{created['rule']}", timeout=10)
    if created["monitor"] is not None:
        http.delete(BASE + f"/monitors/{created['monitor']}", timeout=10)
    if created["project"] is not None:
        http.delete(BASE + f"/projects/{created['project']}", timeout=10)
    if created["job_session"] is not None:
        http.delete(BASE + f"/sessions/{created['job_session']}", timeout=10)
    with sqlite3.connect(DB_PATH) as conn:
        if created["job"]:
            conn.execute("DELETE FROM job_events WHERE job_id=?", (created["job"],))
            conn.execute("DELETE FROM jobs WHERE id=?", (created["job"],))
        conn.execute("DELETE FROM activity WHERE title=? OR ref_id=?", (tag, str(created["job"])))
    shutil.rmtree(folder, ignore_errors=True)

print("PASS stateful live audit cleanup", flush=True)
