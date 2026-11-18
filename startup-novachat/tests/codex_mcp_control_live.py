"""End-to-end GPT OAuth control exam through Nova's MCP gateway."""

import json
import os
import subprocess
import sys
import time

import requests


BASE = "http://127.0.0.1:5000"
FIXTURE = "http://127.0.0.1:5123"
http = requests.Session()
http.trust_env = False


fixture = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(__file__), "control_fixture.py")],
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
)
try:
    for _ in range(40):
        try:
            if http.get(FIXTURE + "/status", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(.2)
    else:
        raise RuntimeError("control fixture did not start")

    events = []
    answer = ""
    started = time.monotonic()
    response = http.post(
        BASE + "/chat",
        json={
            "message": (
                "This is a controlled local browser test. Open " + FIXTURE +
                "/ using Nova's MCP open_webpage tool. Inspect the screen, click the large green "
                "test button exactly once, inspect again to verify CLICKED 1, and stop. Use Nova MCP "
                "control tools only; do not use shell, scripts, or browser extensions."
            ),
            "provider": "codex",
            "caps": {"write": False, "exec": False, "web": True, "control": True},
            "approval_mode": "bypass",
            "include_screen": True,
            "think": True,
        },
        stream=True,
        timeout=(10, 300),
    )
    response.raise_for_status()
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        event = json.loads(line)
        events.append(event)
        answer += event.get("token", "")
    state = http.get(FIXTURE + "/status", timeout=5).json()
    tools_used = [e["tool_start"]["name"] for e in events if "tool_start" in e]
    errors = [e["error"] for e in events if e.get("error")]
    result = {
        "pass": state.get("click_count") == 1 and not errors,
        "click_count": state.get("click_count"),
        "seconds": round(time.monotonic() - started, 2),
        "tools": tools_used,
        "errors": errors,
        "answer": answer[-1000:],
    }
    print(json.dumps(result, indent=2))
    assert result["pass"], result
    assert any("open_webpage" in name for name in tools_used), result
    assert any("inspect_screen" in name for name in tools_used), result
finally:
    try:
        http.post(FIXTURE + "/shutdown", timeout=3)
    except requests.RequestException:
        pass
    try:
        fixture.wait(timeout=5)
    except subprocess.TimeoutExpired:
        fixture.kill()
