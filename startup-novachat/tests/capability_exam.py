"""Live local-model capability exam with isolated fixtures and cleanup."""

import base64
import io
import json
import sqlite3
import time
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont


BASE = "http://127.0.0.1:5000"
MODEL = "qwen3.5:35b"
ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "nova.db"
http = requests.Session()
http.trust_env = False


def turn(session_id, message, *, write=False, web=False, images=None, think=False):
    result = {"answer": "", "tools": [], "results": [], "errors": [], "done": False}
    started = time.perf_counter()
    payload = {
        "session_id": session_id,
        "message": message,
        "provider": "local",
        "model": MODEL,
        "caps": {"write": write, "exec": False, "web": web, "control": False},
        "approval_mode": "bypass",
        "think": think,
        "images": images or [],
        "files": [],
    }
    with http.post(BASE + "/chat", json=payload, stream=True, timeout=(15, 360)) as response:
        response.raise_for_status()
        for raw in response.iter_lines(chunk_size=1):
            if not raw:
                continue
            event = json.loads(raw)
            result["answer"] += event.get("token", "")
            if isinstance(event.get("tool_start"), dict):
                result["tools"].append(event["tool_start"].get("name"))
            if isinstance(event.get("tool_result"), dict):
                result["results"].append(event["tool_result"])
            if event.get("error"):
                result["errors"].append(event["error"])
            result["done"] = result["done"] or event.get("done") is True
    result["seconds"] = round(time.perf_counter() - started, 2)
    assert result["done"] and not result["errors"], result
    return result


def new_session():
    response = http.post(BASE + "/sessions", json={}, timeout=10)
    response.raise_for_status()
    return response.json()["id"]


sessions = []
fixture = ROOT / "workspace" / f"capability-exam-{uuid.uuid4().hex}.txt"
memory = "Nova capability exam code is ORBIT-7391"
schedule_name = "NOVA_CAPABILITY_EXAM_" + uuid.uuid4().hex[:8]

try:
    sid = new_session(); sessions.append(sid)
    current = turn(sid, "Use the current_time tool and tell me the verified local date and time.")
    assert current["tools"] == ["current_time"], current
    print(f"PASS grounded current time ({current['seconds']}s)", flush=True)

    sid = new_session(); sessions.append(sid)
    fixture.write_text("project=Nova\nstatus=BEFORE\nowner=Manuel\n", encoding="utf-8")
    edited = turn(
        sid,
        f"Read {fixture}, change the exact text status=BEFORE to status=AFTER using "
        "the precise edit tool, then read the file again and report the verified result.",
        write=True,
    )
    assert "replace_in_file" in edited["tools"], edited
    assert edited["tools"].count("read_file") >= 2, edited
    assert "status=AFTER" in fixture.read_text(encoding="utf-8"), edited
    assert all(item.get("ok") for item in edited["results"]), edited
    print(f"PASS autonomous read-edit-verify ({edited['seconds']}s)", flush=True)

    sid = new_session(); sessions.append(sid)
    scheduled = turn(
        sid,
        f"Create a recurring scheduled task named {schedule_name}. Every 525600 minutes, "
        "its instruction is: Review Nova health and summarize any failures. Use the scheduling tool.",
        write=True,
    )
    assert "create_scheduled_task" in scheduled["tools"], scheduled
    with sqlite3.connect(DB) as connection:
        row = connection.execute(
            "SELECT id,session_id FROM scheduled_tasks WHERE name=?", (schedule_name,)
        ).fetchone()
    assert row, scheduled
    print(f"PASS conversational scheduling ({scheduled['seconds']}s)", flush=True)

    sid = new_session(); sessions.append(sid)
    image = Image.new("RGB", (1200, 400), "white")
    font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 72)
    ImageDraw.Draw(image).text((100, 120), "NOVA VISION CODE 7429", fill="black", font=font)
    output = io.BytesIO(); image.save(output, "PNG")
    vision = turn(
        sid,
        "Read the large code shown in the attached image. Reply with only the four digits.",
        images=[base64.b64encode(output.getvalue()).decode("ascii")],
    )
    assert "7429" in vision["answer"], vision
    print(f"PASS local vision/OCR ({vision['seconds']}s)", flush=True)

    sid = new_session(); sessions.append(sid)
    remembered = turn(sid, "Remember that " + memory + ".")
    recalled = turn(sid, "What is the Nova capability exam code I asked you to remember?")
    assert "ORBIT-7391" in recalled["answer"], {"remembered": remembered, "recalled": recalled}
    print(f"PASS durable memory recall ({recalled['seconds']}s)", flush=True)

    sid = new_session(); sessions.append(sid)
    web = turn(
        sid,
        "Search the web for the official Ollama documentation homepage. Tell me its URL and "
        "state that you verified it from search results.",
        web=True,
    )
    assert "web_search" in web["tools"], web
    assert any(item.get("ok") for item in web["results"]), web
    assert "ollama" in web["answer"].lower(), web
    print(f"PASS live web research ({web['seconds']}s)", flush=True)
finally:
    if fixture.exists():
        fixture.unlink()
    with sqlite3.connect(DB) as connection:
        task = connection.execute(
            "SELECT session_id FROM scheduled_tasks WHERE name=?", (schedule_name,)
        ).fetchone()
        connection.execute("DELETE FROM scheduled_tasks WHERE name=?", (schedule_name,))
        connection.execute("DELETE FROM facts WHERE lower(text)=lower(?)", (memory,))
        if task and task[0]:
            connection.execute("DELETE FROM sessions WHERE id=?", (task[0],))
    for session_id in sessions:
        http.delete(BASE + f"/sessions/{session_id}", timeout=10)

print("PASS capability exam cleanup", flush=True)
