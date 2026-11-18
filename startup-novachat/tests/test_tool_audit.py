"""Harmless tool checks in an isolated temporary fixture, never user projects."""
import sys
import sqlite3
from contextlib import contextmanager
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools  # noqa: E402


class ToolAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nova-tool-audit-')
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        for name, value in [('WORKSPACE', self.root), ('ALLOWED_ROOTS', [self.root]),
                            ('DENY', [])]:
            patcher = patch.object(tools, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_write_append_read(self):
        target = str(Path(self.root) / 'fixture.txt')
        self.assertTrue(tools.run('write_file', {'path': target, 'content': 'audit-one'})['ok'])
        self.assertTrue(tools.run('append_to_file', {'path': target, 'content': '-two'})['ok'])
        result = tools.run('read_file', {'path': target})
        self.assertTrue(result['ok'], result)
        self.assertIn('audit-one-two', result['text'])

    def test_precise_edit_move_delete_and_line_range(self):
        original = Path(self.root) / 'source.txt'
        moved = Path(self.root) / 'renamed.txt'
        original.write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
        lines = tools.run('read_file_lines', {
            'path': str(original), 'start_line': 2, 'end_line': 3,
        })
        self.assertTrue(lines['ok'], lines)
        self.assertIn('beta', lines['text'])
        edit = tools.run('replace_in_file', {
            'path': str(original), 'old_text': 'beta', 'new_text': 'BETA',
            'expected_replacements': 1,
        })
        self.assertTrue(edit['ok'], edit)
        stale = tools.run('replace_in_file', {
            'path': str(original), 'old_text': 'beta', 'new_text': 'wrong',
            'expected_replacements': 1,
        })
        self.assertFalse(stale['ok'], stale)
        self.assertTrue(tools.run('move_path', {
            'source': str(original), 'destination': str(moved),
        })['ok'])
        self.assertTrue(tools.run('delete_path', {'path': str(moved)})['ok'])
        self.assertFalse(moved.exists())

    def test_conversational_schedule_management(self):
        database = str(Path(self.root) / 'schedule.db')
        connection = sqlite3.connect(database)
        try:
            connection.executescript('''
                CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, created_at REAL, updated_at REAL);
                CREATE TABLE scheduled_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT, prompt TEXT, schedule_type TEXT, schedule_value TEXT,
                    session_id INTEGER, enabled INTEGER, last_run REAL,
                    next_run REAL, created_at REAL);
            ''')
        finally:
            connection.close()
        @contextmanager
        def open_database():
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()
        with patch.object(tools, '_schedule_db', open_database):
            created = tools.run('create_scheduled_task', {
                'name': 'Morning brief', 'instruction': 'Summarize the market.',
                'schedule_type': 'daily', 'schedule_value': '08:30',
            })
            self.assertTrue(created['ok'], created)
            listed = tools.run('list_scheduled_tasks', {})
            self.assertTrue(listed['ok'], listed)
            self.assertIn('Morning brief', listed['text'])
            paused = tools.run('manage_scheduled_task', {'task_id': 1, 'action': 'pause'})
            self.assertTrue(paused['ok'], paused)
            with open_database() as connection:
                self.assertEqual(connection.execute(
                    'SELECT enabled FROM scheduled_tasks WHERE id=1').fetchone()[0], 0)

    def test_python_execution(self):
        result = tools.run('run_python', {'code': 'print("NOVA_AUDIT_OK")'})
        self.assertTrue(result['ok'], result)
        self.assertIn('NOVA_AUDIT_OK', result['text'])

    def test_command_execution(self):
        result = tools.run('run_command', {'command': 'echo NOVA_AUDIT_OK'})
        self.assertTrue(result['ok'], result)
        self.assertIn('NOVA_AUDIT_OK', result['text'])

    def test_failed_execution_reported(self):
        result = tools.run('run_python', {'code': 'raise SystemExit(7)'})
        self.assertFalse(result['ok'])
        self.assertIn('7', result['preview'])

    def test_file_escape_rejected(self):
        result = tools.run('read_file', {'path': str(Path(self.root).parent / 'outside.txt')})
        self.assertFalse(result['ok'])

    def test_disabled_capabilities_not_exposed(self):
        for entry in tools.schemas_for(write=False, execute=False, web=False, control=False):
            name = entry['function']['name']
            self.assertNotIn(tools.category(name), ('write', 'exec', 'control'))
            self.assertNotIn(name, tools.NETWORK_TOOLS)

    def test_computer_control_cannot_click_or_type_into_nova_itself(self):
        class User32:
            @staticmethod
            def GetForegroundWindow():
                return 1
            @staticmethod
            def GetWindowTextW(_handle, buffer, _length):
                buffer.value = 'Nova'
                return 4
        fake = types.SimpleNamespace(user32=User32())
        with patch.object(tools.ctypes, 'windll', fake):
            click = tools.run('click_screen', {'x': 10, 'y': 10})
            typed = tools.run('type_text', {'text': 'should not type'})
        self.assertFalse(click['ok'], click)
        self.assertFalse(typed['ok'], typed)
        self.assertIn("Nova's own window", click['text'])
        self.assertIn("Nova's own window", typed['text'])

    def test_unknown_and_malformed_tools(self):
        self.assertFalse(tools.run('not_a_tool', {})['ok'])
        self.assertFalse(tools.run('read_file', {'unexpected': True})['ok'])


if __name__ == '__main__':
    unittest.main()
