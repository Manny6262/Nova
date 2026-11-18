"""Exercise the actual route in isolation, without starting Nova workers or opening its DB."""
import ast
import io
import re
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request
import numpy as np


def audio(seconds=1, amplitude=0):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(np.full(int(seconds * 16000), amplitude, dtype=np.int16).tobytes())
    stream.seek(0)
    return stream


class VoiceEndpointTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        source = Path(__file__).resolve().parents[1] / 'app.py'
        node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'transcribe')
        self.loaded = 0
        self.logprob = -0.2

        def get_model():
            self.loaded += 1
            return SimpleNamespace(transcribe=lambda *a, **kw: (
                [SimpleNamespace(text='Hello Nova', avg_logprob=self.logprob, no_speech_prob=0.1)], None))

        scope = dict(app=app, request=request, jsonify=jsonify, time=time, io=io, wave=wave,
                     re=re, get_whisper=get_model)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
        self.client = app.test_client()

    def post(self, seconds=1, amplitude=0):
        response = self.client.post('/transcribe', data={'audio': (audio(seconds, amplitude), 'test.wav')})
        response.request.environ['wsgi.input'].close()
        return response

    def test_silence_skips_model(self):
        self.assertEqual(self.post().json['text'], '')
        self.assertEqual(self.loaded, 0)

    def test_long_audio_rejected(self):
        self.assertEqual(self.post(36, 1000).status_code, 413)
        self.assertEqual(self.loaded, 0)

    def test_low_confidence_discarded(self):
        self.logprob = -1.5
        self.assertEqual(self.post(1, 1000).json['text'], '')

    def test_good_transcription_preserved(self):
        self.assertEqual(self.post(1, 1000).json['text'], 'Hello Nova')


if __name__ == '__main__':
    unittest.main()
