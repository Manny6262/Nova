"""Real isolated Pipecat pipeline using synthetic audio, never a microphone/tool."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import wave

import httpx
import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:5000'


def pcm(wav):
    with wave.open(io.BytesIO(wav), 'rb') as reader:
        values = np.frombuffer(reader.readframes(reader.getnframes()), dtype=np.int16)
        rate = reader.getframerate()
    if rate != 16000:
        values = np.interp(np.linspace(0, len(values)-1, round(len(values)*16000/rate)),
                           np.arange(len(values)), values).astype(np.int16)
    return bytes(16000) + values.tobytes() + bytes(64000)


async def test(port, token):
    url = f'ws://127.0.0.1:{port}/voice?token={token}'
    async with httpx.AsyncClient(trust_env=False) as http:
        for _ in range(150):
            try:
                r = await http.get(f'http://127.0.0.1:{port}/health',
                                   headers={'Authorization':'Bearer '+token})
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)
        else:
            raise AssertionError('service failed to start')
        assert (await http.get(f'http://127.0.0.1:{port}/health')).status_code == 403
        for bad_url, origin in [(url, 'https://evil.example'),
                                (f'ws://127.0.0.1:{port}/voice?token=wrong', BASE)]:
            try:
                async with connect(bad_url, origin=origin, proxy=None):
                    raise AssertionError('unauthorized connection accepted')
            except InvalidStatus:
                pass
        print('PASS: unauthorized HTTP, wrong token, foreign origin rejected', flush=True)
        audio = await http.post(BASE+'/speak', json={'text':'Hello Nova, please check the project.'}, timeout=40)
        audio.raise_for_status()
        raw = pcm(audio.content)
        async with connect(url, origin=BASE, proxy=None, max_size=4_000_000) as ws:
            assert json.loads(await asyncio.wait_for(ws.recv(), 15))['type'] == 'ready'
            await ws.send(json.dumps({'type':'settings','pause':.4}))
            await ws.send(json.dumps({'type':'speak','id':'speech-test','text':'Ready when you are.'}))
            # Silence must never create a command, approval, or utterance.
            for _ in range(40):
                await ws.send(bytes(1024))
                await asyncio.sleep(.032)
            await ws.send(json.dumps({'type':'finish'}))
            async def stream():
                for offset in range(0, len(raw), 1024):
                    await ws.send(raw[offset:offset+1024])
                    await asyncio.sleep(.032)
            sender = asyncio.create_task(stream())
            utterance = spoken = None
            partials = 0
            async with asyncio.timeout(25):
                while utterance is None or spoken is None:
                    e = json.loads(await ws.recv())
                    if e['type'] == 'partial':
                        partials += 1
                    if e['type'] == 'utterance':
                        utterance = e
                    if e['type'] == 'audio':
                        spoken = e
                    assert e['type'] not in ('error', 'audio_error'), e.get('error')
            await sender
            assert utterance['words'] >= 4 and 'project' in utterance['text'], utterance['text']
            assert partials > 0
            assert base64.b64decode(spoken['wav'])[:4] == b'RIFF'
            print('PASS: actual Silero + Smart Turn + streaming Vosk; concurrent Kokoro output', flush=True)
            print({'text':utterance['text'], 'partials':partials, 'tts_ms':spoken['ms']}, flush=True)
        await asyncio.sleep(1)
        health = (await http.get(f'http://127.0.0.1:{port}/health',
                    headers={'Authorization':'Bearer '+token})).json()
        assert health['sessions'] == 0, health
        print('PASS: disconnect releases pipeline session', flush=True)


if __name__ == '__main__':
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port = probe.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = dict(os.environ, NOVA_PIPECAT_TOKEN=token, NOVA_PIPECAT_PORT=str(port))
    with open(ROOT/'pipecat-test.log','w',encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, str(ROOT/'pipecat_service.py')], env=env,
            cwd=ROOT, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            asyncio.run(test(port, token))
        finally:
            process.terminate()
            process.wait(timeout=10)
