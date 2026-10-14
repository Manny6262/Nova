"""Isolated regression checks; no live data, providers, or background services."""
import ast
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, closing
from pathlib import Path

from flask import Flask, Blueprint, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
from chat_control import ChatTurn, TurnRegistry, TurnCancelled


class NativeRegressions(unittest.TestCase):
    def test_full_python_modules_compile(self):
        for name in ['app.py','jarvis_core.py','codex_bridge.py','chat_control.py']:
            path=ROOT/'engine'/name
            compile(path.read_text(encoding='utf-8'),str(path),'exec')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db_path = Path(self.folder.name) / 'qa.db'
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript('''
                CREATE TABLE sessions(id INTEGER PRIMARY KEY);
                CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id INTEGER,role TEXT,content TEXT,created_at REAL);
                CREATE TABLE activity(id INTEGER PRIMARY KEY, ts REAL);
                INSERT INTO sessions VALUES(1);
                INSERT INTO sessions VALUES(2);
                INSERT INTO messages VALUES(1,2,'user','QA history',946684800.125);
                INSERT INTO activity VALUES(1,1);
                INSERT INTO activity VALUES(2,2);
            ''')
            db.commit()
        @contextmanager
        def db():
            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.row_factory = sqlite3.Row
                yield connection
        self.app = Flask(__name__)
        bp = Blueprint('regressions', __name__)
        self.registry = TurnRegistry()
        import re
        scope = dict(app=self.app, bp=bp, db=db, jsonify=jsonify, request=request,
                     CHAT_TURNS=self.registry, re=re)
        # Use the actual route functions without importing app.py, which starts
        # voice/database/background services as part of normal app startup.
        for filename, names in [('app.py', {'get_session','stop_chat','validate_json_request'}),
                                ('jarvis_core.py', {'activity'})]:
            tree=ast.parse((ROOT/'engine'/filename).read_text(encoding='utf-8'))
            selected=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
            assert len(selected)==len(names)
            exec(compile(ast.Module(body=selected,type_ignores=[]),filename,'exec'),scope)
        self.app.register_blueprint(bp)
        self.client=self.app.test_client()

    def tearDown(self):
        self.folder.cleanup()

    def test_missing_and_empty_histories_differ(self):
        self.assertEqual(self.client.get('/sessions/999').status_code,404)
        empty=self.client.get('/sessions/1')
        self.assertEqual(empty.status_code,200)
        self.assertEqual(empty.json,[])

    def test_original_timestamp_is_returned(self):
        message=self.client.get('/sessions/2').json[0]
        self.assertEqual(message['created_at'],946684800.125)
        self.assertEqual(message['content'],'QA history')

    def test_activity_limit_validation_and_bounds(self):
        for limit in ['abc','1.5','']:
            response=self.client.get('/activity?limit='+limit)
            self.assertEqual(response.status_code,400)
            self.assertEqual(response.content_type,'application/json')
            self.assertIn('error',response.json)
        self.assertEqual(len(self.client.get('/activity?limit=0').json),1)
        self.assertEqual(len(self.client.get('/activity?limit=9999').json),2)

    def test_stop_is_scoped_and_idempotent_while_active(self):
        first='a'*32;second='b'*32
        turn=self.registry.start(first);other=self.registry.start(second)
        self.assertIsNone(self.registry.start(first))
        self.assertEqual(self.client.post('/chat/stop',json={'turn_id':first}).status_code,200)
        self.assertTrue(turn.cancelled.is_set())
        self.assertFalse(other.cancelled.is_set())
        self.assertEqual(self.client.post('/chat/stop',json={'turn_id':first}).status_code,200)
        self.registry.finish(first)
        self.assertEqual(self.client.post('/chat/stop',json={'turn_id':first}).status_code,404)
        self.assertEqual(self.client.post('/chat/stop',json={'turn_id':[]} ).status_code,400)

    def test_cancel_releases_approval_wait_and_closes_model_response(self):
        turn=ChatTurn();closed=threading.Event()
        class Response:
            def close(self): closed.set()
        turn.attach(Response())
        turn.cancel()
        self.assertTrue(closed.wait(1))
        start=time.monotonic()
        with self.assertRaises(TurnCancelled): turn.wait_for_approval(threading.Event(),30)
        self.assertLess(time.monotonic()-start,0.5)

    def test_approval_arriving_after_cancel_is_never_accepted(self):
        turn=ChatTurn();approved=threading.Event();approved.set();turn.cancel()
        with self.assertRaises(TurnCancelled): turn.wait_for_approval(approved,30)

    def test_invalid_json_is_rejected(self):
        self.assertEqual(self.client.post('/chat/stop',json=[]).status_code,400)


if __name__=='__main__': unittest.main(verbosity=2)
