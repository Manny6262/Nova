"""Model loading/readiness regression checks; no real model loads or selection writes."""
import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from model_status import local_model_status, prepare_local_model


class ModelPolishRegressions(unittest.TestCase):
    def response(self,data):
        result=Mock();result.json.return_value=data;return result

    def test_ready_means_present_in_running_models(self):
        with patch('model_status.requests.get',return_value=self.response({'models':[{'name':'qa:latest'}]})):
            self.assertTrue(local_model_status('qa')['ready'])
            status=local_model_status('other:latest')
            self.assertFalse(status['ready']);self.assertEqual(status['state'],'not_loaded')

    def test_service_failure_does_not_report_ready(self):
        with patch('model_status.requests.get',side_effect=requests.ConnectionError()):
            self.assertEqual(local_model_status('qa')['state'],'unavailable')

    def test_preparation_is_empty_and_requires_explicit_completion(self):
        with patch('model_status.requests.post',return_value=self.response({'done':True})) as post:
            self.assertTrue(prepare_local_model('qa:latest')['ready'])
            payload=post.call_args.kwargs['json']
            self.assertEqual(payload['prompt'],'');self.assertFalse(payload['stream'])
        for data in [{'done':False},{'error':'load failed'},{'done':True,'error':'failed'}]:
            with patch('model_status.requests.post',return_value=self.response(data)):
                with self.assertRaises(ValueError):prepare_local_model('qa:latest')

    def test_prepare_route_never_changes_current_selection(self):
        app=Flask(__name__)
        selected=('original:latest','local')
        scope=dict(app=app,jsonify=jsonify,request=request,requests=requests,
                   ACTIVE_MODEL=selected[0],ACTIVE_PROVIDER=selected[1],
                   _installed_names=lambda:['qa:latest'],
                   _resolve_installed=lambda name,names:name if name in names else None,
                   prepare_local_model=lambda name:{'ready':True,'state':'loaded'})
        tree=ast.parse((ROOT/'engine'/'app.py').read_text(encoding='utf-8'))
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='prepare_model']
        exec(compile(ast.Module(body=functions,type_ignores=[]),'app.py','exec'),scope)
        client=app.test_client()
        self.assertEqual(client.post('/models/prepare',json={'name':'qa:latest'}).status_code,200)
        self.assertEqual((scope['ACTIVE_MODEL'],scope['ACTIVE_PROVIDER']),selected)
        self.assertEqual(client.post('/models/prepare',json={'name':'missing'}).status_code,400)
        scope['prepare_local_model']=Mock(side_effect=requests.Timeout())
        self.assertEqual(client.post('/models/prepare',json={'name':'qa:latest'}).status_code,503)
        self.assertEqual((scope['ACTIVE_MODEL'],scope['ACTIVE_PROVIDER']),selected)

    def test_all_modified_python_compiles(self):
        for filename in ['app.py','model_status.py']:
            path=ROOT/'engine'/filename;compile(path.read_text(encoding='utf-8'),str(path),'exec')


if __name__=='__main__':unittest.main(verbosity=2)
