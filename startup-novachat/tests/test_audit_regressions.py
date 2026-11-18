"""Exercise real route/function bodies without starting workers or modifying Nova DB."""

import ast
from pathlib import Path
import json
import threading
import time
import unittest
import tempfile
import os
from unittest.mock import Mock
from flask import Blueprint, Flask, request, jsonify


def scope(*names):
    source = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    env = dict(
        app=Flask(__name__),
        request=request,
        jsonify=jsonify,
        time=time,
        json=json,
        PENDING_LOCK=threading.Lock(),
        PENDING_CONFIRMS={},
        PORT=5000,
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), env)
    return env


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.env = scope("confirm")
        self.entry = {"approved": None, "event": threading.Event()}
        self.env["PENDING_CONFIRMS"]["test"] = self.entry
        self.client = self.env["app"].test_client()

    def test_false_string_not_approval(self):
        response = self.client.post(
            "/confirm", json={"token": "test", "approved": "false"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.entry["event"].is_set())

    def test_decision_cannot_be_overwritten(self):
        self.assertEqual(
            self.client.post(
                "/confirm", json={"token": "test", "approved": False}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/confirm", json={"token": "test", "approved": True}
            ).status_code,
            409,
        )
        self.assertIs(self.entry["approved"], False)

    def test_malformed_token(self):
        self.assertEqual(
            self.client.post(
                "/confirm", json={"token": [], "approved": True}
            ).status_code,
            400,
        )

    def test_boolean_approval(self):
        self.assertEqual(
            self.client.post(
                "/confirm", json={"token": "test", "approved": True}
            ).status_code,
            200,
        )
        self.assertIs(self.entry["approved"], True)


class ScheduleTests(unittest.TestCase):
    def test_invalid_times_rejected_before_database(self):
        env = scope("create_scheduled", "compute_next_run")
        env["db"] = Mock(side_effect=AssertionError("invalid schedule accessed DB"))
        client = env["app"].test_client()
        for kind, value in [
            ("daily", "25:99"),
            ("daily", "oops"),
            ("interval", "nan"),
            ("interval", "0"),
        ]:
            with self.subTest(value=value):
                response = client.post(
                    "/scheduled",
                    json={
                        "name": "test",
                        "prompt": "test",
                        "schedule_type": kind,
                        "schedule_value": value,
                    },
                )
                self.assertEqual(response.status_code, 400)

    def test_epoch_is_valid_start(self):
        env = scope("compute_next_run")
        self.assertEqual(env["compute_next_run"]("interval", "2", 0), 120)

    def run_response(self, events, http_error=False):
        env = scope("_run_scheduled")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_lines.return_value = [json.dumps(e).encode() for e in events]
        if http_error:
            response.raise_for_status.side_effect = RuntimeError("503")
        env["requests"] = Mock()
        env["requests"].post.return_value = response
        return env["_run_scheduled"]({"prompt": "test", "session_id": 123})

    def test_error_event_fails(self):
        self.assertFalse(self.run_response([{"error": "model failed"}]))

    def test_truncated_stream_fails(self):
        self.assertFalse(self.run_response([{"token": "unfinished"}]))

    def test_http_failure_fails(self):
        self.assertFalse(self.run_response([], True))

    def test_completed_stream_succeeds(self):
        self.assertTrue(self.run_response([{"token": "ready"}, {"done": True}]))


class SelectionTests(unittest.TestCase):
    def test_failed_persistence_preserves_memory(self):
        env = scope("commit_model_selection")
        env.update(
            ACTIVE_MODEL="old",
            ACTIVE_PROVIDER="local",
            _selection_lock=threading.Lock(),
            save_model_settings=Mock(side_effect=OSError("disk failure")),
        )
        with self.assertRaises(OSError):
            env["commit_model_selection"]("new", "codex")
        self.assertEqual(
            (env["ACTIVE_MODEL"], env["ACTIVE_PROVIDER"]), ("old", "local")
        )

    def test_atomic_save_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            env = scope("save_model_settings")
            target = Path(folder) / "settings.json"
            env.update(
                os=os,
                tempfile=tempfile,
                SETTINGS_PATH=str(target),
                ACTIVE_MODEL="old",
                ACTIVE_PROVIDER="local",
            )
            env["save_model_settings"]()
            self.assertEqual(json.loads(target.read_text())["model"], "old")
            fake_os = Mock(wraps=os)
            fake_os.replace.side_effect = OSError("locked")
            env["os"] = fake_os
            with self.assertRaises(OSError):
                env["save_model_settings"]("new", "codex")
            self.assertEqual(json.loads(target.read_text())["model"], "old")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


class TaskRouteTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "jarvis_core.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        nodes = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in ("job_approval", "monitors")
        ]
        app = Flask(__name__)
        bp = Blueprint("test_tasks", __name__)
        env = dict(
            bp=bp,
            request=request,
            jsonify=jsonify,
            db=Mock(side_effect=AssertionError("invalid input accessed DB")),
        )
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), env)
        app.register_blueprint(bp)
        self.client = app.test_client()

    def test_job_approval_requires_boolean(self):
        for value in ("false", "true", None, 1, []):
            with self.subTest(value=value):
                response = self.client.post(
                    "/jobs/test/approval", json={"approved": value}
                )
                self.assertEqual(response.status_code, 400)

    def test_invalid_monitor_interval(self):
        for value in ("invalid", None, 31536001):
            with self.subTest(value=value):
                response = self.client.post(
                    "/monitors",
                    json={
                        "kind": "path",
                        "target": "C:/NovaChat",
                        "interval_seconds": value,
                    },
                )
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
