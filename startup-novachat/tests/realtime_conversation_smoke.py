"""Warm local conversation -> Kokoro validation. Synthetic text only; no tools or cloud."""
import json
import time
import requests

base = 'http://127.0.0.1:5000'
payload = {
    'message': 'Say one short friendly sentence telling me you are ready.',
    'voice_mode': True, 'realtime_voice': True,
    # These intentionally hostile values verify backend enforcement.
    'provider': 'codex', 'model': 'not-installed',
    'caps': {'write': True, 'exec': True, 'web': True, 'control': True},
}
started = time.perf_counter()
first = None
answer = ''
provider = None
with requests.post(base + '/chat', json=payload, stream=True, timeout=(5, 120)) as response:
    response.raise_for_status()
    for line in response.iter_lines(chunk_size=1):
        event = json.loads(line) if line else {}
        if event.get('provider'): provider = event['provider']
        if event.get('error'): raise RuntimeError(event['error'])
        piece = event.get('token', '')
        if piece and first is None: first = time.perf_counter()
        answer += piece
model_done = time.perf_counter()
assert provider != 'codex', provider
assert answer.strip() and '{"name"' not in answer and 'arguments' not in answer.lower(), answer
speech = requests.post(base + '/speak', json={'text': answer}, timeout=60)
speech.raise_for_status()
assert speech.content[:4] == b'RIFF'
finished = time.perf_counter()
print({'reply': answer.strip(), 'first_text_ms': round((first-started)*1000),
       'model_total_ms': round((model_done-started)*1000),
       'speech_ready_ms': round((finished-model_done)*1000),
       'model_to_audio_ms': round((finished-started)*1000)})
