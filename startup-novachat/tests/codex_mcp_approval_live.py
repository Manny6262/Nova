"""Live GPT OAuth exam for Nova's existing confirmation UI/protocol."""

import json
import os

import requests


TARGET = r"C:\NovaChat\workspace\codex_mcp_approval_exam.txt"
session = requests.Session()
session.trust_env = False

try:
    if os.path.exists(TARGET):
        os.unlink(TARGET)
    response = session.post(
        "http://127.0.0.1:5000/chat",
        json={
            "message": (
                "Use Nova MCP write_file to create C:\\NovaChat\\workspace\\codex_mcp_approval_exam.txt "
                "with exactly GPT_APPROVAL_OK. Do not use shell or native file tools."
            ),
            "provider": "codex",
            "caps": {"write": True, "exec": False, "web": False, "control": False},
            "approval_mode": "ask",
        },
        stream=True, timeout=(10, 180),
    )
    response.raise_for_status()
    events = []
    approved = False
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        event = json.loads(line)
        events.append(event)
        confirmation = event.get("confirm_request")
        if confirmation:
            accepted = session.post(
                "http://127.0.0.1:5000/confirm",
                json={"token": confirmation["token"], "approved": True}, timeout=10,
            )
            accepted.raise_for_status()
            approved = True
    tools_used = [e["tool_start"]["name"] for e in events if "tool_start" in e]
    assert approved, events
    assert any("write_file" in name for name in tools_used), events
    assert open(TARGET, encoding="utf-8").read() == "GPT_APPROVAL_OK"
    print(json.dumps({"pass": True, "confirmation_received": approved,
                      "tools": tools_used, "verified_content": "GPT_APPROVAL_OK"}, indent=2))
finally:
    if os.path.exists(TARGET):
        os.unlink(TARGET)
