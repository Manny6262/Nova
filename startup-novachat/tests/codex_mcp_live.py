"""Live GPT OAuth exam proving Nova's custom MCP tools are available."""

import json
import time

import requests


session = requests.Session()
session.trust_env = False
started = time.monotonic()
response = session.post(
    "http://127.0.0.1:5000/chat",
    json={
        "message": (
            "Use the Nova MCP tool named list_scheduled_tasks now. Do not use shell, Python, "
            "or file reading. Then tell me how many scheduled tasks the tool returned."
        ),
        "provider": "codex",
        "caps": {"write": False, "exec": False, "web": False, "control": False},
        "approval_mode": "bypass",
    },
    stream=True,
    timeout=(10, 180),
)
response.raise_for_status()
events = [json.loads(line) for line in response.iter_lines(decode_unicode=True) if line]
tool_names = [event["tool_start"]["name"] for event in events if "tool_start" in event]
answer = "".join(event.get("token", "") for event in events)
providers = [event.get("provider") for event in events if event.get("provider")]
errors = [event.get("error") for event in events if event.get("error")]
result = {
    "pass": any("list_scheduled_tasks" in name for name in tool_names) and not errors,
    "seconds": round(time.monotonic() - started, 2),
    "providers": providers,
    "tools": tool_names,
    "errors": errors,
    "answer": answer,
}
print(json.dumps(result, indent=2))
assert result["pass"], result
assert "codex" in providers, result
