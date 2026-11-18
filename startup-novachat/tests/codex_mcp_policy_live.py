"""Live OAuth checks for Nova MCP capability denial and allowed mutation."""

import json
import os
import time

import requests


URL = "http://127.0.0.1:5000/chat"
TARGET = r"C:\NovaChat\workspace\codex_mcp_policy_exam.txt"


def turn(message, caps):
    response = requests.post(
        URL,
        json={"message": message, "provider": "codex", "caps": caps,
              "approval_mode": "bypass"},
        stream=True, timeout=(10, 180),
    )
    response.raise_for_status()
    return [json.loads(line) for line in response.iter_lines(decode_unicode=True) if line]


try:
    if os.path.exists(TARGET):
        os.unlink(TARGET)
    denied = turn(
        "Call the Nova MCP write_file tool for C:\\NovaChat\\workspace\\codex_mcp_policy_exam.txt "
        "with content BLOCKED. Use that exact MCP tool and no shell or native file tool. Report its result.",
        {"write": False, "exec": False, "web": False, "control": False},
    )
    denied_tools = [e["tool_start"]["name"] for e in denied if "tool_start" in e]
    assert any("write_file" in name for name in denied_tools), denied
    assert not os.path.exists(TARGET), "disabled GPT write unexpectedly created the file"

    allowed = turn(
        "Use Nova MCP write_file to create C:\\NovaChat\\workspace\\codex_mcp_policy_exam.txt "
        "with exactly GPT_SHARED_TOOLS_OK. Then use Nova MCP read_file to verify it. Do not use shell, "
        "Python, or native Codex file tools.",
        {"write": True, "exec": False, "web": False, "control": False},
    )
    allowed_tools = [e["tool_start"]["name"] for e in allowed if "tool_start" in e]
    assert any("write_file" in name for name in allowed_tools), allowed
    assert any("read_file" in name for name in allowed_tools), allowed
    assert open(TARGET, encoding="utf-8").read() == "GPT_SHARED_TOOLS_OK"
    print(json.dumps({
        "pass": True,
        "denied_tools": denied_tools,
        "allowed_tools": allowed_tools,
        "verified_content": "GPT_SHARED_TOOLS_OK",
    }, indent=2))
finally:
    if os.path.exists(TARGET):
        os.unlink(TARGET)
