import json
import os
import subprocess
import sys
import time

import nova_mcp_server
import tools


def _policy(token="test-token", **overrides):
    value = {
        "token": token, "write": False, "exec": False, "web": False,
        "control": False, "vision": False, "expires": time.time() + 60,
    }
    value.update(overrides)
    with open(nova_mcp_server.POLICY_PATH, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


def test_mcp_definitions_match_registry():
    definitions = tools.mcp_definitions()
    assert len(definitions) == len(tools.schemas())
    assert all("_nova_token" in item["inputSchema"]["required"] for item in definitions)
    assert next(item for item in definitions if item["name"] == "current_time")["annotations"]["readOnlyHint"]
    assert not next(item for item in definitions if item["name"] == "write_file")["annotations"]["readOnlyHint"]


def test_mcp_policy_allows_safe_tool_and_blocks_disabled_write(tmp_path):
    _policy()
    allowed = nova_mcp_server._call_tool("current_time", {"_nova_token": "test-token"})
    assert allowed["isError"] is False
    denied = nova_mcp_server._call_tool("write_file", {
        "_nova_token": "test-token", "path": str(tmp_path / "blocked.txt"), "content": "no",
    })
    assert denied["isError"] is True
    assert not (tmp_path / "blocked.txt").exists()


def test_stdio_initialize_list_and_call():
    _policy()
    process = subprocess.Popen(
        [sys.executable, os.path.join(nova_mcp_server.APP_DIR, "nova_mcp_server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        cwd=nova_mcp_server.APP_DIR,
    )
    try:
        messages = [
            {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                "protocolVersion":"2025-06-18","capabilities":{},
                "clientInfo":{"name":"test","version":"1"}}},
            {"jsonrpc":"2.0","method":"notifications/initialized","params":{}},
            {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}},
            {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
                "name":"current_time","arguments":{"_nova_token":"test-token"}}},
        ]
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        replies = [json.loads(process.stdout.readline()) for _ in range(3)]
        assert replies[0]["result"]["serverInfo"]["name"] == "nova-tools"
        assert len(replies[1]["result"]["tools"]) == len(tools.schemas())
        assert replies[2]["result"]["isError"] is False
    finally:
        process.kill()
        process.wait(timeout=5)
        try:
            os.unlink(nova_mcp_server.POLICY_PATH)
        except OSError:
            pass
