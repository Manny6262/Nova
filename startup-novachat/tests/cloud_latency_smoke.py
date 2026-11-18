"""One read-only cloud chat through the real Nova endpoint, with synthetic text only."""
import json
import time
import requests

started = time.perf_counter()
first = None
provider = None
with requests.post('http://127.0.0.1:5000/chat', json={
    'message': 'This is a latency check. Do not use tools. Reply only: Hello, I am ready.',
    'voice_mode': True, 'think': False, 'provider': 'codex',
    'caps': {'write': False, 'exec': False, 'web': False, 'control': False},
    'approval_mode': 'ask',
}, stream=True, timeout=(5, 90)) as response:
    response.raise_for_status()
    for line in response.iter_lines(chunk_size=1):
        if not line:
            continue
        event = json.loads(line)
        if event.get('provider'):
            provider = event['provider']
        if event.get('error'):
            raise RuntimeError(event['error'])
        if event.get('token') and first is None:
            first = time.perf_counter() - started
assert provider == 'codex' and first is not None, (provider, first)
print({'provider': provider, 'first_text_seconds': round(first, 2), 'total_seconds': round(time.perf_counter()-started, 2)})
