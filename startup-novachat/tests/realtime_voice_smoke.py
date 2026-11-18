"""Real Kokoro -> loopback live recognizer smoke test. No microphone, cloud, model, or tools."""
import io
import json
import time
import wave

import numpy as np
import requests
from websockets.sync.client import connect

base = 'http://127.0.0.1:5000'
phrase = 'Hello Nova, please tell me about the weather.'
audio = requests.post(base + '/speak', json={'text': phrase}, timeout=60)
audio.raise_for_status()
with wave.open(io.BytesIO(audio.content), 'rb') as wav:
    rate = wav.getframerate()
    pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    if wav.getnchannels() > 1:
        pcm = pcm.reshape(-1, wav.getnchannels()).mean(axis=1).astype(np.int16)
if rate != 16000:
    target = max(1, round(len(pcm) * 16000 / rate))
    pcm = np.interp(np.linspace(0, len(pcm)-1, target), np.arange(len(pcm)), pcm).astype(np.int16)

config = requests.get(base + '/voice/stream-config', timeout=5).json()
assert config['available'], config
started = time.perf_counter()
partials = []
with connect(config['url'], origin=base, proxy=None) as ws:
    ws.send(json.dumps({'type':'start', 'id':'smoke'}))
    raw = pcm.tobytes()
    for offset in range(0, len(raw), 3200):
        ws.send(raw[offset:offset+3200])
        time.sleep(.025)  # 4x realtime: lets recognition overlap incoming audio
        try:
            message = json.loads(ws.recv(timeout=.01))
            if message.get('type') == 'partial': partials.append(message['text'])
        except TimeoutError:
            pass
    final_sent = time.perf_counter()
    ws.send(json.dumps({'type':'final', 'id':'smoke'}))
    while True:
        result = json.loads(ws.recv(timeout=5))
        if result.get('type') == 'final': break
assert result['words'] >= 4 and result['confidence'] >= .70, result
assert 'nova' in result['text'].lower(), result
print({'text': result['text'], 'confidence': result['confidence'], 'words': result['words'],
       'partial_updates': len(partials), 'stream_test_ms': round((time.perf_counter()-started)*1000),
       'endpoint_tail_ms': round((time.perf_counter()-final_sent)*1000)})
