"""STDIO MCP server exposing Nova's native tool registry to Codex OAuth turns."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import uuid

import tools


APP_DIR = os.path.dirname(os.path.abspath(__file__))
POLICY_PATH = os.path.join(APP_DIR, ".nova_mcp_policy.json")
REQUEST_DIR = os.path.join(APP_DIR, ".nova_mcp_requests")
PROTOCOL_VERSION = "2025-06-18"


def _reply(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _policy():
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if float(value.get("expires", 0)) < time.time():
            return {}
        return value
    except (OSError, ValueError, TypeError):
        return {}


def _authorize(name, arguments):
    policy = _policy()
    supplied = str(arguments.pop("_nova_token", ""))
    if not supplied or supplied != policy.get("token"):
        return "This Nova tool call has no valid capability token for the current turn."
    category = tools.category(name)
    if category == "write" and not policy.get("write"):
        return "File writing is turned off in Nova's safety settings."
    if category == "exec" and not policy.get("exec"):
        return "Command and code execution are turned off in Nova's safety settings."
    if category == "control" and not policy.get("control"):
        return "Computer control is turned off in Nova's safety settings."
    if name in tools.NETWORK_TOOLS and not policy.get("web"):
        return "Web and market access are turned off in Nova's safety settings."
    if name == "inspect_screen" and not policy.get("vision"):
        return "Screen inspection is turned off for this Nova turn."
    return None


def _request_approval(name, arguments, info, policy):
    os.makedirs(REQUEST_DIR, exist_ok=True)
    request_id = uuid.uuid4().hex
    request_path = os.path.join(REQUEST_DIR, request_id + ".json")
    response_path = os.path.join(REQUEST_DIR, request_id + ".response.json")
    request = {
        "id": request_id, "token": policy.get("token"), "name": name,
        "args": arguments, "info": info, "created": time.time(),
    }
    temp_path = request_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(request, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, request_path)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with open(response_path, "r", encoding="utf-8") as handle:
                    return bool(json.load(handle).get("approved"))
            except (OSError, ValueError):
                pass
            if _policy().get("token") != policy.get("token"):
                return False
            time.sleep(.1)
        return False
    finally:
        for path in (request_path, response_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _screen_content():
    """Return a fresh PNG after control actions so GPT can verify the screen."""
    try:
        from PIL import ImageGrab
        image = ImageGrab.grab(all_screens=True)
        if image.width > 1920:
            ratio = 1920 / image.width
            image = image.resize((1920, max(1, int(image.height * ratio))))
        output = io.BytesIO()
        image.save(output, "PNG", optimize=True)
        return {"type": "image", "data": base64.b64encode(output.getvalue()).decode("ascii"),
                "mimeType": "image/png"}
    except Exception:
        return None


def _call_tool(name, raw_arguments):
    arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
    denied = _authorize(name, arguments)
    if denied:
        result = {"ok": False, "text": denied, "preview": "capability denied"}
    else:
        policy = _policy()
        if name in tools.CONFIRM_TOOLS:
            info = tools.describe(name, arguments)
            if not info.get("ok"):
                result = {"ok": False, "text": "Error: " + info.get("error", "invalid action"),
                          "preview": "invalid action"}
            else:
                risky = bool(info.get("dangers")) or info.get("overwrite") or info.get("require_type")
                mode = policy.get("approval_mode", "ask")
                auto_run = mode == "bypass" or (mode == "auto" and not risky)
                if auto_run or _request_approval(name, arguments, info, policy):
                    result = tools.run(name, arguments)
                else:
                    result = {"ok": False, "text": "User declined or did not approve this action.",
                              "preview": "declined"}
        else:
            result = tools.run(name, arguments)
    content = [{"type": "text", "text": result.get("text", result.get("preview", ""))}]
    if result.get("ok") and (name == "inspect_screen" or name in tools.CONTROL_TOOLS):
        if name != "inspect_screen":
            inspected = tools.inspect_screen()
            if inspected.get("ok"):
                content[0]["text"] += "\n\n" + inspected.get("text", "")
        screenshot = _screen_content()
        if screenshot:
            content.append(screenshot)
    return {"content": content, "structuredContent": result, "isError": not bool(result.get("ok"))}


def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        _reply(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "nova-tools", "version": "1.0.0"},
            "instructions": (
                "Use these tools for Nova-specific memory, scheduling, market, browser, Windows, "
                "and file workflows. Always include the current _nova_token from Nova's developer "
                "instructions. Capability denials are authoritative. Inspect and verify after control actions."
            ),
        })
    elif method == "tools/list":
        _reply(request_id, {"tools": tools.mcp_definitions()})
    elif method == "tools/call":
        name = str(params.get("name") or "")
        if name not in {item["name"] for item in tools.mcp_definitions()}:
            _reply(request_id, error={"code": -32602, "message": "Unknown Nova tool: " + name})
        else:
            _reply(request_id, _call_tool(name, params.get("arguments") or {}))
    elif method in {"notifications/initialized", "notifications/cancelled"}:
        return
    elif request_id is not None:
        _reply(request_id, error={"code": -32601, "message": "Method not found: " + str(method)})


def main():
    # MCP's STDIO transport is one UTF-8 JSON-RPC message per line. Never write
    # logs to stdout because it is the protocol channel.
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                handle(message)
        except Exception as error:
            request_id = message.get("id") if isinstance(locals().get("message"), dict) else None
            if request_id is not None:
                _reply(request_id, error={"code": -32603, "message": str(error)[:500]})


if __name__ == "__main__":
    main()
