"""Regression coverage for long local-model tool workflows."""

import ast
import json
from pathlib import Path
import threading
import time
import types
import unittest
import uuid
from unittest.mock import Mock

import requests as real_requests
from flask import Flask, Response, jsonify, request, stream_with_context


SOURCE = Path(__file__).resolve().parents[1] / "app.py"


def helper_scope():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted_functions = {
        "_tool_round_budget",
        "_compact_tool_screens",
        "_tool_call_key",
        "_is_recoverable_tool_parse_error",
        "_tool_limit_fallback",
        "_missing_media_markdown",
    }
    wanted_constants = {
        "MAX_TOOL_ROUNDS",
        "MAX_CONTROL_TOOL_ROUNDS",
        "MAX_IDENTICAL_TOOL_CALLS",
        "MAX_TOOL_PARSE_RETRIES",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted_constants
            for target in node.targets
        ):
            nodes.append(node)
    env = {"json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), env)
    return env


class FakeOllamaResponse:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=False):
        lines = [json.dumps(event) for event in self.events]
        return lines if decode_unicode else [line.encode() for line in lines]


class ToolLoopTests(unittest.TestCase):
    def test_generated_video_gets_persistent_playback_link(self):
        env = helper_scope()
        media = [{"type": "video", "url": "/media/videos/example.mp4"}]
        self.assertEqual(
            env["_missing_media_markdown"](media, "Generated successfully."),
            "\n\n[Play generated video](/media/videos/example.mp4)",
        )
        self.assertEqual(
            env["_missing_media_markdown"](media, "[Play](/media/videos/example.mp4)"),
            "",
        )

    def test_control_budget_handles_long_observe_act_work(self):
        env = helper_scope()
        self.assertGreaterEqual(env["_tool_round_budget"](True), 32)
        self.assertGreater(env["_tool_round_budget"](True), env["_tool_round_budget"](False))

    def test_old_screens_are_pruned_but_latest_and_text_remain(self):
        env = helper_scope()
        messages = [
            {"role": "tool", "content": "first result", "images": ["old"]},
            {"role": "assistant", "content": "next"},
            {"role": "tool", "content": "latest result", "images": ["new"]},
        ]
        env["_compact_tool_screens"](messages)
        self.assertNotIn("images", messages[0])
        self.assertEqual(messages[0]["content"], "first result")
        self.assertEqual(messages[2]["images"], ["new"])

    def test_old_verbose_tool_results_are_compacted(self):
        env = helper_scope()
        messages = [
            {"role": "tool", "content": "x" * 5000, "images": [str(index)]}
            for index in range(12)
        ]
        env["_compact_tool_screens"](messages)
        self.assertLess(len(messages[0]["content"]), 1100)
        self.assertEqual(messages[-1]["content"], "x" * 5000)
        self.assertNotIn("images", messages[0])
        self.assertEqual(messages[-1]["images"], ["11"])

    def test_exhaustion_path_retires_the_generic_failure(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("Nova kept calling tools without answering", source)
        self.assertIn('"tool_budget_exhausted": True', source)

    def test_identical_action_key_is_stable(self):
        env = helper_scope()
        first = env["_tool_call_key"]("click_screen", {"x": 10, "y": 20})
        reordered = env["_tool_call_key"]("click_screen", {"y": 20, "x": 10})
        self.assertEqual(first, reordered)

    def test_limit_fallback_reports_verified_counts(self):
        env = helper_scope()
        text = env["_tool_limit_fallback"](40, 37, "last action failed")
        self.assertIn("40 tool actions", text)
        self.assertIn("37 actions completed successfully", text)
        self.assertIn("last action failed", text)

    def test_ollama_tool_xml_errors_are_recoverable(self):
        env = helper_scope()
        self.assertTrue(env["_is_recoverable_tool_parse_error"](
            "XML syntax error on line 5: element <function> closed by </parameter>"
        ))
        self.assertTrue(env["_is_recoverable_tool_parse_error"](
            "tool call parse failed"
        ))
        self.assertFalse(env["_is_recoverable_tool_parse_error"](
            "model runner disconnected"
        ))

    def test_chat_completes_after_ten_sequential_control_actions(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        helper_names = {
            "_tool_round_budget",
            "_compact_tool_screens",
            "_tool_call_key",
            "_tool_limit_fallback",
            "_missing_media_markdown",
            "chat",
        }
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in helper_names:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in {
                    "MAX_TOOL_ROUNDS", "MAX_CONTROL_TOOL_ROUNDS",
                    "MAX_IDENTICAL_TOOL_CALLS",
                }
                for target in node.targets
            ):
                nodes.append(node)

        app = Flask(__name__)
        calls = []
        tool_runs = []

        def post(_url, json=None, **_kwargs):
            calls.append(json)
            action_round = len([payload for payload in calls if payload.get("tools")])
            if action_round <= 10:
                return FakeOllamaResponse([{
                    "message": {
                        "content": f"Working on item {action_round}. ",
                        "thinking": f"reasoning for item {action_round}",
                        "tool_calls": [{"function": {
                            "name": "click_screen",
                            "arguments": {"x": action_round * 10, "y": 100},
                        }}],
                    },
                    "done": True,
                }])
            return FakeOllamaResponse([{
                "message": {"content": "Completed all ten clicks."},
                "done": True,
                "eval_count": 5,
                "eval_duration": 1_000_000_000,
            }])

        fake_requests = Mock(wraps=real_requests)
        fake_requests.post.side_effect = post
        fake_requests.exceptions = real_requests.exceptions
        fake_tools = types.SimpleNamespace(
            schemas_for=lambda *_args: [{"type": "function"}],
            CONFIRM_TOOLS={"click_screen"},
            NETWORK_TOOLS=set(),
            CONTROL_TOOLS={"click_screen"},
            category=lambda _name: "control",
            describe=lambda _name, _args: {
                "ok": True, "summary": "click", "overwrite": False,
                "exec": False, "dangers": [], "require_type": False,
                "detail": "",
            },
            run=lambda name, args: (
                tool_runs.append((name, args))
                or {"ok": True, "preview": "clicked", "text": "clicked"}
            ),
        )
        saved = []
        env = {
            "app": app,
            "request": request,
            "jsonify": jsonify,
            "Response": Response,
            "stream_with_context": stream_with_context,
            "json": json,
            "requests": fake_requests,
            "tools": fake_tools,
            "voice_policy": types.SimpleNamespace(compact_history=lambda rows: rows),
            "threading": threading,
            "time": time,
            "uuid": uuid,
            "os": __import__("os"),
            "re": __import__("re"),
            "MAX_IMAGES": 6,
            "MAX_ATTACH_FILES": 8,
            "MAX_ATTACH_CHARS": 20_000,
            "HISTORY_TURNS": 40,
            "ACTIVE_MODEL": "nova:latest",
            "ACTIVE_PROVIDER": "local",
            "FAST_VOICE_MODEL": "qwen2.5:3b",
            "LOCAL_AGENT_CONTEXT": 65_536,
            "OLLAMA_URL": "http://127.0.0.1:11434/api/chat",
            "CONFIRM_TIMEOUT": 1,
            "PENDING_LOCK": threading.Lock(),
            "PENDING_CONFIRMS": {},
            "NOVA_PERSONA": "Nova",
            "ACTIVE_WORKSPACE": "C:/NovaChat",
            "CODEX_VOICE_MODEL": "test",
            "codex_bridge": Mock(),
            "ensure_session": lambda value: value or 1,
            "extract_fact": lambda _text: None,
            "add_fact": lambda *_args, **_kwargs: None,
            "save_message": lambda sid, role, content: saved.append((sid, role, content)),
            "system_message": lambda: {"role": "system", "content": "You are Nova."},
            "load_history": lambda _sid, _limit: [{"role": "user", "content": "Click ten items."}],
            "compact_voice_memory": lambda **_kwargs: [],
            "_installed_names": lambda: ["nova:latest", "qwen2.5:3b"],
            "_resolve_installed": lambda name, _names: name,
            "capture_screen": Mock(),
            "capture_screen_b64": Mock(return_value="screen"),
            "audit": Mock(),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), env)

        client = app.test_client()
        response = client.post("/chat", json={
            "message": "Click ten items.",
            "session_id": 1,
            "provider": "local",
            "model": "nova:latest",
            "caps": {"control": True, "write": False, "exec": False, "web": True},
            "approval_mode": "bypass",
            "images": [],
            "files": [],
        })
        events = [json.loads(line) for line in response.data.decode().splitlines()]

        self.assertFalse(any("error" in event for event in events), events)
        self.assertTrue(any(event.get("done") for event in events), events)
        self.assertIn("Completed all ten clicks.", "".join(event.get("token", "") for event in events))
        self.assertEqual(len(tool_runs), 10)
        self.assertEqual(len(calls), 11)
        self.assertEqual(calls[0]["options"]["num_ctx"], 65_536)
        self.assertTrue(calls[0]["think"])
        self.assertNotIn("temperature", calls[0]["options"])
        self.assertEqual(
            next(message for message in calls[1]["messages"]
                 if message.get("role") == "assistant")["thinking"],
            "reasoning for item 1",
        )
        self.assertIn((1, "assistant", "Completed all ten clicks."), saved)


if __name__ == "__main__":
    unittest.main()
