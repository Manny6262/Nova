"""Opt-in localhost smoke test. Synthesizes in memory; never opens the microphone."""
import io
import time
import requests

base = 'http://127.0.0.1:5000'
started = time.perf_counter()
response = requests.post(base + '/speak', json={'text': 'Please check the project and run the tests.'}, timeout=60)
response.raise_for_status()
assert response.content[:4] == b'RIFF', 'Expected WAV audio'
synth = time.perf_counter() - started
started = time.perf_counter()
transcript = requests.post(base + '/transcribe', files={'audio': ('test.wav', io.BytesIO(response.content), 'audio/wav')}, timeout=60)
transcript.raise_for_status()
body = transcript.json()
assert 'project' in body['text'].lower() and 'test' in body['text'].lower(), body
print({'synthesis_seconds': round(synth, 2), 'transcription_seconds': round(time.perf_counter()-started, 2), 'text': body['text']})
