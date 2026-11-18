"""Live API audit. Only synthetic records are created; all are removed by ID."""

import base64
import io
import json
import time
import uuid
import requests
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

BASE = "http://127.0.0.1:5000"
http = requests.Session()
http.trust_env = False
failures = []
checks = 0


def check(name, condition):
    global checks
    checks += 1
    print(("PASS " if condition else "FAIL ") + name, flush=True)
    if not condition:
        failures.append(name)


for endpoint in [
    "health",
    "voice_status",
    "models",
    "workspace",
    "sessions",
    "facts",
    "jobs",
    "projects",
    "monitors",
    "event-rules",
    "activity",
    "diagnostics",
    "wake-service",
    "scheduled",
    "cloud_status",
]:
    result = http.get(BASE + "/" + endpoint, timeout=25)
    check(
        "GET " + endpoint,
        result.status_code == 200
        and result.headers.get("Content-Type", "").startswith("application/json"),
    )

original = http.get(BASE + "/models").json()
installed = {m["name"] for m in original["models"]}
tags = {m["name"] for m in http.get("http://127.0.0.1:11434/api/tags").json()["models"]}
check("dropdown includes every downloaded model", installed == tags)
try:
    result = http.post(BASE + "/models/select", json={"provider": "codex"}, timeout=10)
    check(
        "switch to cloud",
        result.status_code == 200 and result.json()["provider"] == "codex",
    )
    result = http.post(
        BASE + "/models/select",
        json={"provider": "local", "name": original["active"]},
        timeout=10,
    )
    check(
        "switch to installed local model",
        result.status_code == 200 and result.json()["active"] == original["active"],
    )
    result = http.post(
        BASE + "/models/select",
        json={"provider": "local", "name": "nova-audit-nonexistent"},
        timeout=10,
    )
    check("reject missing model", result.status_code == 400)
finally:
    http.post(
        BASE + "/models/select",
        json={"provider": original["provider"], "name": original["active"]},
        timeout=10,
    ).raise_for_status()

for endpoint, payload in [
    ("confirm", {"token": [], "approved": True}),
    ("confirm", []),
    (
        "scheduled",
        {
            "name": "AUDIT",
            "prompt": "test",
            "schedule_type": "daily",
            "schedule_value": "25:99",
        },
    ),
    ("speak", {"text": None}),
    ("chat", {"message": None}),
    ("models/select", {"provider": None}),
    ("facts", {"text": None}),
    ("jobs/nova-audit-missing/approval", {"approved": "false"}),
    (
        "monitors",
        {"kind": "path", "target": "C:/NovaChat", "interval_seconds": "invalid"},
    ),
]:
    result = http.post(BASE + "/" + endpoint, json=payload, timeout=10)
    check("bad input " + endpoint + " " + str(payload), result.status_code == 400)

before = {s["id"] for s in http.get(BASE + "/sessions").json()}
result = http.post(BASE + "/chat", json={"message": ""}, timeout=10)
after = {s["id"] for s in http.get(BASE + "/sessions").json()}
check(
    "empty chat rejected without creating history",
    result.status_code == 400 and before == after,
)
fact_id = None
try:
    result = http.post(
        BASE + "/facts", json={"text": "Synthetic audit " + uuid.uuid4().hex}, timeout=5
    )
    result.raise_for_status()
    fact_id = result.json()["id"]
    check(
        "memory create/read",
        any(f["id"] == fact_id for f in http.get(BASE + "/facts").json()),
    )
finally:
    if fact_id is not None:
        http.delete(BASE + f"/facts/{fact_id}").raise_for_status()

writer = PdfWriter()
page = writer.add_blank_page(width=200, height=200)
font = DictionaryObject(
    {
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }
)
page[NameObject("/Resources")] = DictionaryObject(
    {
        NameObject("/Font"): DictionaryObject(
            {NameObject("/F1"): writer._add_object(font)}
        )
    }
)
content = DecodedStreamObject()
content.set_data(b"BT /F1 12 Tf 10 100 Td (NOVA AUDIT PDF) Tj ET")
page[NameObject("/Contents")] = writer._add_object(content)
buf = io.BytesIO()
writer.write(buf)
result = http.post(
    BASE + "/extract",
    json={"name": "audit.pdf", "data": base64.b64encode(buf.getvalue()).decode()},
    timeout=10,
)
check(
    "PDF attachment extraction",
    result.status_code == 200 and "NOVA AUDIT PDF" in result.json().get("text", ""),
)
result = http.post(
    BASE + "/extract", json={"name": "bad.pdf", "data": "not-a-pdf"}, timeout=10
)
check("malformed PDF rejected", result.status_code == 400)

for provider, model in [("local", "qwen2.5:3b"), ("codex", "")]:
    sid = http.post(BASE + "/sessions", json={}).json()["id"]
    try:
        started = time.monotonic()
        answer = ""
        actual = provider
        error = None
        with http.post(
            BASE + "/chat",
            json={
                "session_id": sid,
                "message": "Synthetic audit. Do not use tools. Reply only: READY.",
                "provider": provider,
                "model": model,
                "caps": {"write": False, "exec": False, "web": False, "control": False},
                "approval_mode": "ask",
            },
            stream=True,
            timeout=(5, 90),
        ) as response:
            response.raise_for_status()
            done = False
            for raw in response.iter_lines():
                if raw:
                    event = json.loads(raw)
                    actual = event.get("provider", actual)
                    answer += event.get("token", "")
                    error = event.get("error", error)
                    done = done or event.get("done", False)
            check(
                provider + " real model reply",
                done and bool(answer.strip()) and not error and actual == provider,
            )
            print(
                json.dumps(
                    {
                        "provider": actual,
                        "seconds": round(time.monotonic() - started, 2),
                        "error": error,
                    }
                ),
                flush=True,
            )
    finally:
        http.delete(BASE + f"/sessions/{sid}").raise_for_status()

print(json.dumps({"checks": checks, "failures": failures}), flush=True)
raise SystemExit(bool(failures))
