"""Isolated task acceptance tests with real scoped file effects and Python checks."""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import json
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from flask import Flask

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
import jarvis_core as core
import fix_tasks as f
import memory_store
REAL_PROPOSE=f.propose


class FixTasks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'project'; self.root.mkdir()
        self.dbpath=Path(self.temp.name)/'data/nova.db'; self.dbpath.parent.mkdir()
        core.DB_PATH=f.DB_PATH=str(self.dbpath); core._started=f._started=True
        with closing(sqlite3.connect(self.dbpath)) as c,c:
            c.execute('CREATE TABLE sessions(id INTEGER PRIMARY KEY,title TEXT,created_at REAL,updated_at REAL)')
        core.init()
        self.project=memory_store.save_project(f.DB_PATH,{'name':'Task project','goal':'Correct calculation'})['id']
        (self.root/'example.py').write_text('def add(a, b):\n    return a - b\n',encoding='utf-8')
        (self.root/'check.py').write_text('from example import add\nassert add(2, 3) == 5\nprint("Behavior test passed")\n',encoding='utf-8')
        app=Flask(__name__); app.register_blueprint(f.bp); app.register_blueprint(core.bp)
        self.client=app.test_client(); self.proposals=0
        def propose(row,snapshot,failures):
            self.proposals+=1
            return {'summary':'Correct addition.','plan':['Replace subtraction','Check behavior'],
                    'edits':[{'file':'example.py','old':'return a - b','new':'return a + b'}]}
        self.mock=patch.object(f,'propose',side_effect=propose); self.mock.start()

    def tearDown(self):
        self.mock.stop(); self.temp.cleanup()

    def draft(self,**kwargs):
        body={'name':'Correct addition','goal':'Fix addition','criteria':'add(2,3)==5','root':str(self.root),
              'files':['example.py'],'references':['check.py'],'test_file':'check.py','project_id':self.project,'model':'qwen3.5:35b'}
        body.update(kwargs); r=self.client.post('/fix-tasks',json=body)
        self.assertEqual(r.status_code,201,r.json); return r.json['id']

    def get(self,tid):
        response=self.client.get('/fix-tasks/'+tid); self.assertEqual(response.status_code,200,response.json)
        return response.json

    def act(self,tid,action,body=None):
        return self.client.post('/fix-tasks/'+tid+'/'+action,json={} if body is None else body)

    def prepare(self,**kwargs):
        tid=self.draft(**kwargs)
        self.assertEqual(self.act(tid,'approve',{'approved':True}).status_code,200)
        f.tick(); return tid

    def apply(self,tid,token=None):
        return self.act(tid,'apply',{'approved':True,'review_token':token or self.get(tid)['review_token']})

    def test_draft_never_reads_or_runs_and_requires_literal_approval(self):
        tid=self.draft(); f.tick()
        self.assertEqual(self.get(tid)['captures'],[]); self.assertEqual(self.proposals,0)
        self.assertEqual(self.act(tid,'approve',{'approved':1}).status_code,409)
        self.assertEqual(self.act(tid,'resume').status_code,409)

    def test_complete_workflow_checks_diff_apply_and_exact_rollback(self):
        old=(self.root/'example.py').read_bytes(); tid=self.prepare(); item=self.get(tid)
        self.assertEqual(item['state'],'ready',item)
        self.assertEqual((self.root/'example.py').read_bytes(),old)
        self.assertIn('+    return a + b',item['changes'][0]['diff'])
        self.assertTrue(all(c['status']=='passed' for c in item['checks']))
        self.assertIn('Behavior test passed',item['checks'][-1]['output'])
        result=self.apply(tid); self.assertEqual(result.status_code,200,result.json)
        self.assertIn('return a + b',(self.root/'example.py').read_text())
        self.assertEqual((Path(result.json['rollback'])/'example.py').read_bytes(),old)
        self.assertEqual(self.apply(tid).status_code,409)

    def test_failed_behavior_blocks_apply(self):
        (self.root/'check.py').write_text('raise AssertionError("purposeful failure")')
        tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'needs_changes')
        self.assertEqual(self.apply(tid).status_code,409)

    def test_source_conflict_blocks_apply(self):
        tid=self.prepare(); (self.root/'example.py').write_text('User changed this')
        self.assertEqual(self.apply(tid).status_code,409)
        self.assertEqual((self.root/'example.py').read_text(),'User changed this')

    def test_stale_revision_token_cannot_apply(self):
        tid=self.prepare(); token=self.get(tid)['review_token']
        self.assertEqual(self.act(tid,'revise',{'feedback':'Keep behavior correct, explain the fix.'}).status_code,200)
        f.tick(); self.assertEqual(self.get(tid)['revision'],2)
        self.assertEqual(self.apply(tid,token).status_code,409)

    def test_cancel_and_reject_terminal(self):
        tid=self.draft(); self.assertEqual(self.act(tid,'cancel').status_code,200)
        self.assertEqual(self.act(tid,'approve',{'approved':True}).status_code,409)
        tid=self.prepare(); self.assertEqual(self.act(tid,'reject').status_code,200)
        self.assertEqual(self.apply(tid).status_code,409)

    def test_capture_checkpoint_survives_restart_and_uses_original(self):
        tid=self.draft(); self.act(tid,'approve',{'approved':True})
        with f.db() as c: c.execute("UPDATE fix_tasks SET state='running' WHERE id=?",(tid,))
        scope=self.get(tid)['scope']; snapshot=f.capture(scope,tid)
        f.save(tid,'Captured',checkpoint={'snapshot':snapshot})
        (self.root/'example.py').write_text('changed after capture')
        f.init_storage(); self.assertEqual(self.get(tid)['state'],'interrupted')
        self.assertEqual(self.act(tid,'resume').status_code,200); f.tick()
        self.assertEqual(self.get(tid)['state'],'ready')
        self.assertEqual(self.apply(tid).status_code,409)

    def test_no_test_replay_after_uncertain_interruption(self):
        tid=self.draft(); self.act(tid,'approve',{'approved':True})
        with f.db() as c:
            c.execute("UPDATE fix_tasks SET state='running',checkpoint=? WHERE id=?",(json.dumps({'test_started':True}),tid))
        f.init_storage()
        self.assertEqual(self.act(tid,'resume').status_code,409)
        self.assertEqual(self.act(tid,'resume',{'retry_checks':True}).status_code,200)

    def test_global_stop_and_late_result_blocked(self):
        tid=self.draft(); self.act(tid,'approve',{'approved':True})
        def delayed(row,snapshot,failures):
            self.client.post('/background-control',json={'enabled':False})
            return {'summary':'late','plan':[],'edits':[{'file':'example.py','old':'return a - b','new':'return a + b'}]}
        with patch.object(f,'propose',side_effect=delayed): f.tick()
        self.assertEqual(self.get(tid)['state'],'paused')
        self.assertEqual(self.get(tid)['changes'],[])
        self.client.post('/background-control',json={'enabled':True}); f.tick()
        self.assertEqual(self.get(tid)['state'],'paused')

    def test_model_cannot_edit_references_or_escape(self):
        for name in ('check.py','../outside.py','C:/outside.py'):
            with patch.object(f,'propose',return_value={'summary':'bad','plan':[],
                       'edits':[{'file':name,'old':'x','new':'y'}]}):
                tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'failed')
        self.assertEqual((self.root/'check.py').read_text().splitlines()[0],'from example import add')

    def test_invalid_and_private_scopes(self):
        for change in ({'files':['../x.py']},{'files':['example.py','EXAMPLE.py']},{'files':['example.py\\x']},
                       {'files':['example.py:stream']},{'files':['.env.json']},{'files':['data.db']},
                       {'test_file':'example.py'},{'model':'qwen:cloud'},{'root':'\\\\host\\share'},
                       {'provider':'oauth'}):
            body={'name':'x','goal':'x','criteria':'x','root':str(self.root),'files':['example.py'],
                  'references':[],'model':'qwen3.5:35b',**change}
            self.assertEqual(self.client.post('/fix-tasks',json=body).status_code,400,change)

    def test_invalid_model_replacement_retried_bounded(self):
        with patch.object(f,'propose',return_value={'summary':'bad','plan':[],
                   'edits':[{'file':'example.py','old':'does not exist','new':'x'}]}) as mock:
            tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'failed'); self.assertEqual(mock.call_count,3)

    def test_test_cannot_change_candidate_under_checked_revision(self):
        (self.root/'check.py').write_text('from pathlib import Path\nPath("example.py").write_text("tampered")\n')
        tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'failed')
        self.assertIn('working copy changed',self.get(tid)['error']); self.assertEqual(self.apply(tid).status_code,409)

    def test_removed_project_fails_closed(self):
        tid=self.draft()
        with f.db() as c: c.execute("UPDATE projects SET state='paused' WHERE id=?",(self.project,))
        self.assertEqual(self.act(tid,'approve',{'approved':True}).status_code,409)

    def test_new_file_creation_is_approved_and_rollback_records_absence(self):
        with patch.object(f,'propose',return_value={'summary':'new','plan':[],
                   'edits':[{'file':'new.json','old':'','new':'{"ready":true}'}]}):
            tid=self.prepare(files=['new.json'],references=[],test_file='')
        self.assertFalse((self.root/'new.json').exists())
        self.assertEqual(self.apply(tid).status_code,200)
        self.assertEqual(json.loads((self.root/'new.json').read_text()),{'ready':True})

    def test_failed_syntax_and_approved_rules_block_review(self):
        with patch.object(f,'propose',return_value={'summary':'bad','plan':[],
                   'edits':[{'file':'example.py','old':'return a - b','new':'return ('}]}):
            tid=self.prepare(test_file=''); self.assertEqual(self.get(tid)['state'],'needs_changes')
        tid=self.prepare(test_file='',rules=[{'file':'example.py','contains':'required_marker','absent':''}])
        self.assertEqual(self.get(tid)['state'],'needs_changes'); self.assertEqual(self.apply(tid).status_code,409)

    def test_partial_apply_recovery_needs_approval_and_reconciles_hashes(self):
        tid=self.prepare(); token=self.get(tid)['review_token']; real=f.os.replace
        def crash_after_replace(a,b):
            real(a,b); raise OSError('Simulated interruption after first replacement')
        with patch.object(f.os,'replace',side_effect=crash_after_replace):
            self.assertEqual(self.apply(tid).status_code,409)
        self.assertEqual(self.get(tid)['state'],'apply_interrupted'); f.init_storage()
        self.assertEqual(self.apply(tid,token).status_code,200)
        self.assertEqual(self.get(tid)['state'],'applied')

    def test_windows_newlines_and_bom_are_preserved(self):
        (self.root/'example.py').write_bytes(b'\xef\xbb\xbfdef add(a, b):\r\n    return a - b\r\n')
        with patch.object(f,'propose',return_value={'summary':'fix','plan':[],
                   'edits':[{'file':'example.py','old':'def add(a, b):\n    return a - b\n','new':'def add(a, b):\n    return a + b\n'}]}):
            tid=self.prepare()
        self.assertEqual(self.apply(tid).status_code,200)
        self.assertEqual((self.root/'example.py').read_bytes(),b'\xef\xbb\xbfdef add(a, b):\r\n    return a + b\r\n')

    def test_frozen_archive_dependency_can_be_checked_but_never_edited(self):
        with zipfile.ZipFile(self.root/'baseline.zip','w') as z: z.writestr('README.txt','frozen')
        (self.root/'check.py').write_text('import zipfile\nassert zipfile.ZipFile("baseline.zip").read("README.txt")==b"frozen"\n')
        tid=self.prepare(references=['check.py','baseline.zip'])
        self.assertEqual(self.get(tid)['state'],'ready')
        body={'name':'x','goal':'x','criteria':'x','root':str(self.root),'files':['baseline.zip'],'model':'qwen3.5:35b'}
        self.assertEqual(self.client.post('/fix-tasks',json=body).status_code,400)

    def test_private_install_and_windows_device_paths_are_rejected(self):
        for value in ('nul.py','CON.json','_nova_review.json','dir/_nova_check.py'):
            body={'name':'x','goal':'x','criteria':'x','root':str(self.root),'files':[value],'model':'qwen3.5:35b'}
            self.assertEqual(self.client.post('/fix-tasks',json=body).status_code,400,value)
        body={'name':'x','goal':'x','criteria':'x','root':str(self.dbpath.parent),'files':['test.py'],'model':'qwen3.5:35b'}
        self.assertEqual(self.client.post('/fix-tasks',json=body).status_code,400)

    def test_oversized_or_non_text_files_stop_before_inference(self):
        (self.root/'example.py').write_bytes(b'x'*(f.MAX_BYTES+1))
        tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'failed'); self.assertEqual(self.proposals,0)

    def test_project_progress_distinguishes_candidate_from_applied(self):
        tid=self.prepare()
        context=memory_store.context_text(f.DB_PATH,'calculation',self.project)
        self.assertIn('candidate_not_applied',context); self.assertIn('originals unchanged',context)

    def test_local_model_action_requests_or_incomplete_stream_fail(self):
        from unittest.mock import MagicMock
        tid=self.draft(); self.act(tid,'approve',{'approved':True})
        with f.db() as c: c.execute("UPDATE fix_tasks SET state='running' WHERE id=?",(tid,))
        with f.db() as c: row=dict(c.execute('SELECT * FROM fix_tasks WHERE id=?',(tid,)).fetchone())
        snapshot=f.capture(self.get(tid)['scope'],tid)
        for lines in ([{'message':{'tool_calls':[{}]},'done':True}],[{'message':{'content':'{}'}}]):
            session=MagicMock(); session.get.return_value.json.return_value={'models':[{'name':'qwen3.5:35b','size':20}]}
            response=session.post.return_value.__enter__.return_value
            response.iter_lines.return_value=[json.dumps(x).encode() for x in lines]
            with patch.object(f.requests,'Session',return_value=session):
                with self.assertRaises(ValueError): REAL_PROPOSE(row,snapshot,[])

    def test_remote_alias_is_rejected_even_if_named_like_local(self):
        from unittest.mock import MagicMock
        tid=self.draft(); self.act(tid,'approve',{'approved':True})
        with f.db() as c: c.execute("UPDATE fix_tasks SET state='running' WHERE id=?",(tid,))
        with f.db() as c: row=dict(c.execute('SELECT * FROM fix_tasks WHERE id=?',(tid,)).fetchone())
        session=MagicMock(); session.get.return_value.json.return_value={'models':[{'name':'qwen3.5:35b','size':20,'remote_host':'https://remote'}]}
        with patch.object(f.requests,'Session',return_value=session):
            with self.assertRaises(ValueError): REAL_PROPOSE(row,{},[])
        session.post.assert_not_called()

    def test_guard_blocks_outside_writes_reads_processes_and_network(self):
        outside=Path(self.temp.name)/'private.txt'; outside.write_text('private')
        scripts=[f'from pathlib import Path\nPath({str(outside)!r}).write_text("bad")',
                 f'from pathlib import Path\nPath({str(outside)!r}).read_text()',
                 'import os\nos.system("echo should-not-run")',
                 'import socket\nsocket.socket()', 'import ctypes\nctypes.CDLL(None)']
        for script in scripts:
            (self.root/'check.py').write_text(script)
            tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'needs_changes',script)
            self.assertEqual(self.apply(tid).status_code,409)
            self.assertEqual(outside.read_text(),'private')

    def test_early_successful_exit_does_not_count_as_a_finished_check(self):
        (self.root/'check.py').write_text('import sys\nsys.exit(0)')
        tid=self.prepare(); self.assertEqual(self.get(tid)['state'],'needs_changes')

    def test_registered_native_mode_rejects_arbitrary_checkers(self):
        body={'name':'x','goal':'x','criteria':'x','root':str(self.root),'files':['example.py'],
              'references':['check.py'],'test_file':'check.py','test_mode':'native_build','model':'qwen3.5:35b'}
        self.assertEqual(self.client.post('/fix-tasks',json=body).status_code,400)

    def test_revisions_keep_previous_candidate_and_failed_check_context(self):
        tid=self.prepare()
        self.act(tid,'revise',{'feedback':'Explain clearly'})
        with f.db() as c: cp=json.loads(c.execute('SELECT checkpoint FROM fix_tasks WHERE id=?',(tid,)).fetchone()[0])
        self.assertIn('return a + b',cp['previous_candidate']['example.py'])
        self.assertTrue(cp['previous_checks'])


if __name__=='__main__': unittest.main(verbosity=2)
