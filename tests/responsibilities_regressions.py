"""Isolated responsibility acceptance tests: no real files, tools, or inference."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch
from flask import Flask

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import jarvis_core as core
import responsibilities as r
import memory_store
REAL_ANALYSE = r._analyse


class Responsibilities(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.file = self.folder/'plan.txt'
        self.file.write_text('Goal: plant garden. Next step: choose seeds.',encoding='utf-8')
        core.DB_PATH = r.DB_PATH = str(self.folder/'nova.db')
        core._started = r._started = True
        with closing(sqlite3.connect(r.DB_PATH)) as c, c:
            c.execute('CREATE TABLE sessions(id INTEGER PRIMARY KEY,title TEXT,created_at REAL,updated_at REAL)')
        core.init()
        self.project = memory_store.save_project(r.DB_PATH,{'name':'Garden','goal':'Grow food'})['id']
        app = Flask(__name__)
        app.register_blueprint(core.bp)
        app.register_blueprint(r.bp)
        self.client = app.test_client()
        self.calls = []
        def analysis(row,receipts,check):
            check()
            self.calls.append(receipts)
            return {'review':'Choose seeds next. Goal completion remains unverified.','metrics':{'eval_count':9},'advisory':True}
        self.mock = patch.object(r,'_analyse',side_effect=analysis)
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.temp.cleanup()

    def draft(self,**overrides):
        body = {'name':'Review garden','goal':'Keep garden moving','success_criteria':'Evidence of seed decisions',
                'files':[str(self.file)],'project_id':self.project,'interval_minutes':60,'model':'qwen3.5:35b'}
        body.update(overrides)
        response = self.client.post('/responsibilities',json=body)
        self.assertEqual(response.status_code,201,response.json)
        return response.json['id']

    def start(self,rid):
        response = self.client.post('/responsibilities/'+rid+'/approve',json={'approved':True})
        self.assertEqual(response.status_code,200,response.json)
        return response.json['run_id']

    def detail(self,rid):
        return self.client.get('/responsibilities/'+rid).json

    def test_approval_scope_and_validation(self):
        rid = self.draft()
        r.tick()
        self.assertEqual(self.detail(rid)['runs'],[])
        for action in ('run','resume'):
            self.assertEqual(self.client.post(f'/responsibilities/{rid}/{action}',json={}).status_code,409)
        self.assertEqual(self.client.post(f'/responsibilities/{rid}/approve',json={'approved':1}).status_code,400)
        self.assertEqual(self.client.post('/responsibilities',json=[]).status_code,400)
        for fields in ({'files':[str(self.folder)]},{'files':['relative.txt']},{'files':['https://example.com']},
                       {'files':['\\\\server\\secret.txt']},{'interval_minutes':True},{'interval_minutes':1},
                       {'model':'model:cloud'},{'provider':'codex'},{'caps':{'exec':True}}):
            body = {'name':'x','goal':'x','success_criteria':'x','files':[str(self.file)],'model':'qwen3.5:35b',**fields}
            self.assertEqual(self.client.post('/responsibilities',json=body).status_code,400,fields)
        self.assertEqual(self.file.read_text(),'Goal: plant garden. Next step: choose seeds.')

    def test_verified_steps_progress_and_unchanged_quiet(self):
        rid = self.draft()
        run = self.start(rid)
        r._run(run)
        detail = self.detail(rid)
        self.assertEqual(detail['state'],'active')
        first = detail['runs'][0]
        self.assertEqual(first['status'],'completed')
        self.assertTrue(all(s['status']=='verified' for s in first['steps']))
        receipt = json.loads(first['steps'][0]['receipt'])[0]
        self.assertEqual(receipt['bytes'],self.file.stat().st_size)
        self.assertEqual(len(receipt['sha256']),64)
        self.assertEqual(json.loads(first['steps'][2]['receipt'])['goal_completion'],'unverified')
        self.assertIn('Choose seeds',memory_store.context_text(r.DB_PATH,'garden',self.project))
        other = memory_store.save_project(r.DB_PATH,{'name':'Other'})['id']
        self.assertNotIn('Choose seeds',memory_store.context_text(r.DB_PATH,'garden',other))
        self.assertNotIn('Choose seeds',memory_store.context_text(r.DB_PATH,'garden'))
        r._run(run)  # A completed run can never republish.
        second = self.client.post(f'/responsibilities/{rid}/run',json={}).json['run_id']
        r._run(second)
        self.assertEqual(len(self.calls),1)
        self.assertIsNone(self.detail(rid)['runs'][0]['notification_id'])
        self.assertEqual(len(self.client.get('/inbox').json),1)
        self.file.write_text('Decided on basil seeds.',encoding='utf-8')
        third = self.client.post(f'/responsibilities/{rid}/run',json={}).json['run_id']
        r._run(third)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(len(self.client.get('/inbox').json),2)

    def test_restart_recovers_only_unfinished_step(self):
        rid = self.draft(); run = self.start(rid)
        def crash(row,receipts,check):
            raise KeyboardInterrupt('Simulated process death')
        with patch.object(r,'_analyse',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt): r._run(run)
        self.assertEqual(self.detail(rid)['runs'][0]['steps'][0]['status'],'verified')
        before = self.detail(rid)['runs'][0]['steps'][0]['receipt']
        r.init_storage()
        self.assertEqual(self.detail(rid)['runs'][0]['status'],'interrupted')
        self.file.write_text('New data after crash.',encoding='utf-8')
        with patch.object(r,'_capture',side_effect=AssertionError('Completed capture replayed')):
            r._run(run)
        after = self.detail(rid)['runs'][0]
        self.assertEqual(after['status'],'completed')
        self.assertEqual(after['steps'][0]['receipt'],before)
        self.assertIn('choose seeds',self.calls[0][0]['text_excerpt'])

    def test_pause_cancel_and_global_controls_block_late_effects(self):
        rid = self.draft(); run = self.start(rid)
        def pause(row,receipts,check):
            self.client.post(f'/responsibilities/{rid}/pause',json={})
            return {'review':'Must not publish','metrics':{}}
        with patch.object(r,'_analyse',side_effect=pause): r._run(run)
        self.assertEqual(self.detail(rid)['runs'][0]['status'],'paused')
        self.assertEqual(self.client.get('/inbox').json,[])
        resumed = self.client.post(f'/responsibilities/{rid}/resume',json={}).json['run_id']
        self.assertEqual(run,resumed)
        self.client.post('/background-control',json={'enabled':False})
        self.assertEqual(self.detail(rid)['state'],'paused')
        self.assertEqual(self.client.post(f'/responsibilities/{rid}/resume',json={}).status_code,409)
        self.client.post('/background-control',json={'enabled':True})
        r.tick()
        self.assertEqual(self.detail(rid)['runs'][0]['status'],'paused')
        self.client.post(f'/responsibilities/{rid}/resume',json={})
        self.client.post(f'/responsibilities/{rid}/cancel',json={})
        r._run(run)
        self.assertEqual(self.detail(rid)['state'],'cancelled')
        self.assertEqual(self.client.post(f'/responsibilities/{rid}/resume',json={}).status_code,409)
        self.assertEqual(self.client.get('/inbox').json,[])

    def test_failure_preserves_capture_and_requires_decision(self):
        rid = self.draft(); run = self.start(rid)
        with patch.object(r,'_analyse',side_effect=RuntimeError('Model offline')): r._run(run)
        failed = self.detail(rid)
        self.assertEqual(failed['state'],'paused')
        self.assertEqual(failed['runs'][0]['status'],'failed')
        self.assertEqual(failed['runs'][0]['steps'][0]['status'],'verified')
        self.assertEqual(self.client.get('/inbox').json[0]['kind'],'decision')
        self.assertEqual(self.client.post(f'/responsibilities/{rid}/resume',json={}).json['run_id'],run)
        r._run(run)
        self.assertEqual(self.detail(rid)['runs'][0]['status'],'completed')

    def test_removed_project_and_size_limit_fail_closed(self):
        rid = self.draft(); run = self.start(rid)
        with r.db() as c: c.execute('UPDATE projects SET deleted_at=? WHERE id=?',(time.time(),self.project))
        r._run(run)
        self.assertEqual(self.detail(rid)['state'],'paused')
        self.assertEqual(len(self.calls),0)
        rid2 = self.draft(project_id=None)
        run2 = self.start(rid2)
        with patch.object(r,'MAX_FILE_BYTES',1): r._run(run2)
        self.assertEqual(self.detail(rid2)['runs'][0]['status'],'failed')

    def test_concurrent_occurrence_and_due_restart_catchup(self):
        rid = self.draft(); run = self.start(rid)
        self.assertEqual(self.client.post(f'/responsibilities/{rid}/run',json={}).json['run_id'],run)
        with r.db() as c: c.execute('UPDATE responsibilities SET next_run=? WHERE id=?',(time.time()-864000,rid))
        r.tick(); r.tick()
        self.assertEqual(len(self.detail(rid)['runs']),1)
        self.assertGreater(self.detail(rid)['next_run'],time.time())

    def test_model_stream_requires_done_and_rejects_action_calls(self):
        rid = self.draft()
        row = self.detail(rid)
        class Reply:
            def __init__(self,items): self.items=items
            def raise_for_status(self): pass
            def json(self): return {'models':[{'name':'qwen3.5:35b','size':20000000000}]}
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def iter_lines(self,**_kwargs):
                for item in self.items: yield json.dumps(item).encode()
            def close(self): pass
        get = Reply([])
        with patch.object(r.requests,'get',return_value=get):
            for items in ([{'message':{'content':'Incomplete'}}],
                          [{'message':{'tool_calls':[{'name':'write_file'}]},'done':True}],
                          [{'done':True,'message':{'content':''}}]):
                with patch.object(r.requests,'post',return_value=Reply(items)):
                    with self.assertRaises((ValueError,RuntimeError)):
                        REAL_ANALYSE(row,[],lambda:None)
            with patch.object(r.requests,'post',return_value=Reply([{'message':{'content':'Review only'},'done':True,'eval_count':2}])) as post:
                result=REAL_ANALYSE(row,[],lambda:None)
                self.assertEqual(result['review'],'Review only')
                self.assertEqual(post.call_args.args[0],'http://127.0.0.1:11434/api/chat')
                self.assertNotIn('tools',post.call_args.kwargs['json'])

    def test_model_remote_alias_is_rejected_and_excerpt_budget_bounded(self):
        rid=self.draft()
        class Reply:
            def raise_for_status(self): pass
            def json(self): return {'models':[{'name':'qwen3.5:35b','remote_host':'https://cloud.invalid'}]}
        with patch.object(r.requests,'get',return_value=Reply()),patch.object(r.requests,'post') as post:
            with self.assertRaises(ValueError): REAL_ANALYSE(self.detail(rid),[],lambda:None)
            post.assert_not_called()
        self.file.write_text('x'*12000)
        files=[{'path':str(self.file),'target':str(self.file.resolve())}]*20
        receipts=r._capture(files,lambda:None)
        self.assertLessEqual(sum(len(item['text_excerpt']) for item in receipts),8000)


if __name__ == '__main__':
    unittest.main(verbosity=2)
