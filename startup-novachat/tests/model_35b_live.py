"""Installation, capability, native tool-call, and Nova integration check for Qwen 3.5 35B."""

import json
import time

import requests


OLLAMA = "http://127.0.0.1:11434"
NOVA = "http://127.0.0.1:5000"
MODEL = "qwen3.5:35b"
http = requests.Session()
http.trust_env = False


def timed_chat(payload, timeout=300):
    started = time.monotonic()
    response = http.post(OLLAMA + "/api/chat", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json(), round(time.monotonic() - started, 2)


tags = http.get(OLLAMA + "/api/tags", timeout=20).json()["models"]
assert any(item["name"] == MODEL for item in tags), "model is absent from Ollama tags"
show = http.post(OLLAMA + "/api/show", json={"model": MODEL}, timeout=30).json()
capabilities = set(show.get("capabilities") or [])
assert {"completion", "tools", "thinking", "vision"}.issubset(capabilities), capabilities
print("PASS installed capabilities " + ",".join(sorted(capabilities)), flush=True)

reply, seconds = timed_chat({
    "model": MODEL,
    "messages": [{"role": "user", "content": "Reply with exactly NOVA35_OK"}],
    "think": False,
    "stream": False,
    "keep_alive": "30m",
    "options": {"num_predict": 40},
})
text = reply.get("message", {}).get("content", "").strip()
assert "NOVA35_OK" in text, repr(text)
print(f"PASS direct response {seconds}s {text!r}", flush=True)

tool_reply, seconds = timed_chat({
    "model": MODEL,
    "messages": [{"role": "user", "content": "Use the current_time tool now. Do not guess."}],
    "tools": [{
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "Return the current local date and time.",
            "parameters": {"type": "object", "properties": {}},
        },
    }],
    "think": False,
    "stream": False,
    "keep_alive": "30m",
})
calls = tool_reply.get("message", {}).get("tool_calls") or []
assert calls and calls[0].get("function", {}).get("name") == "current_time", calls
print(f"PASS native tool call {seconds}s current_time", flush=True)

sid = http.post(NOVA + "/sessions", json={}, timeout=10).json()["id"]
try:
    started = time.monotonic()
    answer = ""
    errors = []
    done = False
    with http.post(NOVA + "/chat", json={
        "session_id": sid,
        "message": "Model integration test. Do not use tools. Reply exactly NOVA_APP_OK.",
        "provider": "local",
        "model": MODEL,
        "caps": {"write": False, "exec": False, "web": False, "control": False},
        "approval_mode": "ask",
        "think": False,
    }, stream=True, timeout=(10, 300)) as response:
        response.raise_for_status()
        for raw in response.iter_lines():
            if not raw:
                continue
            event = json.loads(raw)
            answer += event.get("token", "")
            if event.get("error"):
                errors.append(event["error"])
            done = done or event.get("done") is True
    assert done and not errors and "NOVA_APP_OK" in answer, {"answer": answer, "errors": errors}
    print(f"PASS Nova chat integration {round(time.monotonic() - started, 2)}s {answer.strip()!r}", flush=True)
finally:
    http.delete(NOVA + f"/sessions/{sid}", timeout=10)

sid = http.post(NOVA + "/sessions", json={}, timeout=10).json()["id"]
try:
    answer = ""
    started_tools = []
    finished_tools = []
    errors = []
    done = False
    with http.post(NOVA + "/chat", json={
        "session_id": sid,
        "message": "Use the current_time tool, then tell me the verified local date and time.",
        "provider": "local",
        "model": MODEL,
        "caps": {"write": False, "exec": False, "web": False, "control": False},
        "approval_mode": "ask",
        "think": False,
    }, stream=True, timeout=(10, 300)) as response:
        response.raise_for_status()
        for raw in response.iter_lines():
            if not raw:
                continue
            event = json.loads(raw)
            answer += event.get("token", "")
            if isinstance(event.get("tool_start"), dict):
                started_tools.append(event["tool_start"].get("name"))
            if isinstance(event.get("tool_result"), dict):
                finished_tools.append(event["tool_result"])
            if event.get("error"):
                errors.append(event["error"])
            done = done or event.get("done") is True
    assert done and not errors, {"answer": answer, "errors": errors}
    assert started_tools == ["current_time"], started_tools
    assert finished_tools and finished_tools[0].get("ok") is True, finished_tools
    assert answer.strip(), "Nova did not provide a final answer after the tool"
    print("PASS Nova tool loop current_time -> verified final answer", flush=True)
finally:
    http.delete(NOVA + f"/sessions/{sid}", timeout=10)

selected = http.post(NOVA + "/models/select", json={
    "provider": "local", "name": MODEL,
}, timeout=20)
selected.raise_for_status()
assert selected.json().get("active") == MODEL, selected.text
print("PASS selected in Nova " + MODEL, flush=True)
