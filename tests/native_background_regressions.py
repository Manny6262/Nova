import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from flask import Flask
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import native_background as b


class Background(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        b.ROOT=Path(self.temp.name)
        b.DB_PATH=str(b.ROOT/'nova.db')
        b._started=True
        with closing(sqlite3.connect(b.DB_PATH)) as c,c:
            c.execute('CREATE TABLE inbox(id INTEGER PRIMARY KEY, read_at REAL)')
            c.execute('INSERT INTO inbox VALUES(1,NULL)')
        b.init()
        app=Flask(__name__);app.register_blueprint(b.bp)
        self.client=app.test_client()
    def tearDown(self): self.temp.cleanup()
    def test_defaults_validation_and_no_backlog_notifications(self):
        self.assertFalse(self.client.get('/native-background').json['notifications'])
        for body in ([],{}, {'enabled':1},{'notifications':'true'},{'command':'evil'}):
            self.assertEqual(self.client.post('/native-background',json=body).status_code,400)
        self.assertEqual(self.client.post('/native-background',json={'notifications':True}).status_code,200)
        with patch.object(b,'send_toast') as toast:
            b.deliver();toast.assert_not_called()
        self.assertEqual(b.settings()['notify_after_id'],1)
        self.client.post('/native-background',json={'notifications':False})
        (b.ROOT/'background-service.json').write_text(json.dumps({'notifications':'yes'}))
        self.assertFalse(b.settings()['notifications'])
    def test_durable_delivery_receipts_and_failure_fallback(self):
        self.client.post('/native-background',json={'notifications':True})
        with closing(sqlite3.connect(b.DB_PATH)) as c,c:
            c.execute('INSERT INTO inbox VALUES(2,NULL)')
        with patch.object(b,'send_toast') as toast:
            b.deliver();b.deliver();self.assertEqual(toast.call_count,1)
        with closing(sqlite3.connect(b.DB_PATH)) as c,c:
            c.execute('INSERT INTO inbox VALUES(3,NULL)')
        with patch.object(b,'send_toast',side_effect=RuntimeError('Windows notifications unavailable')) as toast:
            b.deliver();b.deliver();self.assertEqual(toast.call_count,1)
        self.assertEqual(self.client.get('/native-background').json['last_notification']['status'],'failed')
        with closing(sqlite3.connect(b.DB_PATH)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM inbox WHERE read_at IS NULL').fetchone()[0],3)
    def test_optional_login_writes_only_own_startup_entry(self):
        import winreg
        (b.ROOT/'background_service.py').write_text('# fixture')
        class Key:
            def __enter__(self): return self
            def __exit__(self,*args): pass
        with patch.object(winreg,'CreateKey',return_value=Key()),patch.object(winreg,'SetValueEx') as write,patch.object(winreg,'DeleteValue') as remove:
            self.assertEqual(self.client.post('/native-background',json={'launch_at_login':True}).status_code,200)
            self.assertEqual(write.call_args.args[1],'NovaNativeBackground')
            self.assertIn(str(b.ROOT/'background_service.py'),write.call_args.args[-1])
            self.assertEqual(self.client.post('/native-background',json={'launch_at_login':False}).status_code,200)
            self.assertEqual(remove.call_args.args[1],'NovaNativeBackground')

if __name__=='__main__': unittest.main(verbosity=2)
