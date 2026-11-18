"""Real local TTS -> approval recognition -> JS intent parser. Never calls /confirm."""
import json
from pathlib import Path
import subprocess

import requests

html = (Path(__file__).resolve().parents[1] / 'templates/index.html').read_text(encoding='utf-8')
start = html.index('function spokenApproval(')
parser = html[start:html.index('\n}', start) + 2]
for phrase, expected in [('Approve.', True), ('Deny.', False), ('Yeah, go for it.', True),
                         ('Sure, do it.', True), ("No, don't do that.", False)]:
    audio = requests.post('http://127.0.0.1:5000/speak', json={'text': phrase}, timeout=40)
    audio.raise_for_status()
    result = requests.post('http://127.0.0.1:5000/transcribe',
                           data={'barge': '1', 'approval': '1'},
                           files={'audio': ('test.wav', audio.content, 'audio/wav')}, timeout=40)
    result.raise_for_status()
    result = result.json()
    js = parser + '\nconsole.log(JSON.stringify(spokenApproval(' + json.dumps(result['text']) + ')));'
    decision = json.loads(subprocess.check_output(['node', '-e', js], text=True))
    print(json.dumps({'phrase': phrase, 'heard': result['text'], 'verified': result['speech_verified'], 'decision': decision}))
    assert result['speech_verified'] and decision is expected, result
