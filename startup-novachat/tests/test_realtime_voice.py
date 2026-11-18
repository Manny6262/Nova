import json
import logging
import socket
import sys
import time
import unittest

import vosk
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from realtime_voice import VoiceStreamServer


class FakeRecognizer:
    def __init__(self, _model, rate):
        assert rate == 16000

    def SetWords(self, _enabled):
        pass

    def AcceptWaveform(self, data):
        assert isinstance(data, bytes)
        return False

    def PartialResult(self):
        return json.dumps({'partial': 'hello nova'})

    def FinalResult(self):
        return json.dumps({'text': 'hello nova', 'result': [
            {'word': 'hello', 'conf': .94}, {'word': 'nova', 'conf': .90}]})


class EndpointingRecognizer(FakeRecognizer):
    """Simulate Vosk finalizing one phrase before Nova ends the full turn."""
    def __init__(self, model, rate):
        super().__init__(model, rate)
        self.accepted = False

    def AcceptWaveform(self, data):
        self.accepted = True
        return True

    def Result(self):
        return json.dumps({'text': 'hello', 'result': [{'word': 'hello', 'conf': .96}]})

    def FinalResult(self):
        return json.dumps({'text': 'nova', 'result': [{'word': 'nova', 'conf': .94}]})


class VoiceStreamTests(unittest.TestCase):
    def setUp(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        self.original = vosk.KaldiRecognizer
        self.ws_logger = logging.getLogger('websockets.server')
        self.ws_logger_was_disabled = self.ws_logger.disabled
        self.ws_logger.disabled = True  # expected rejected-origin test must not look like a crash
        vosk.KaldiRecognizer = FakeRecognizer
        self.server = VoiceStreamServer(lambda: object(), port=port, origin_port=5000)
        self.server.start()
        self.assertTrue(self.server.running, self.server.error)

    def tearDown(self):
        self.server.stop()
        vosk.KaldiRecognizer = self.original
        self.ws_logger.disabled = self.ws_logger_was_disabled

    def test_streams_partial_and_confident_final(self):
        config = self.server.config()
        with connect(config['url'], origin='http://127.0.0.1:5000', proxy=None) as ws:
            ws.send(json.dumps({'type': 'start', 'id': 'one'}))
            ws.send(b'\0\0' * 1600)
            partial = json.loads(ws.recv(timeout=2))
            self.assertEqual((partial['type'], partial['text']), ('partial', 'hello nova'))
            ws.send(json.dumps({'type': 'final', 'id': 'one'}))
            final = json.loads(ws.recv(timeout=2))
            self.assertEqual(final['text'], 'hello nova')
            self.assertEqual(final['words'], 2)
            self.assertGreater(final['confidence'], .9)

    def test_rejects_foreign_web_origin(self):
        with self.assertRaises(InvalidStatus):
            connect(self.server.config()['url'], origin='https://evil.example', proxy=None)

    def test_rejects_wrong_token(self):
        url = self.server.config()['url'].split('token=')[0] + 'token=wrong'
        with connect(url, origin='http://127.0.0.1:5000', proxy=None) as ws:
            with self.assertRaises(Exception):
                ws.recv(timeout=1)

    def test_keeps_words_from_internal_vosk_endpoint(self):
        vosk.KaldiRecognizer = EndpointingRecognizer
        with connect(self.server.config()['url'], origin='http://127.0.0.1:5000', proxy=None) as ws:
            ws.send(json.dumps({'type': 'start', 'id': 'endpoint'}))
            ws.send(b'\0\0' * 1600)
            endpoint = json.loads(ws.recv(timeout=2))
            self.assertEqual((endpoint['type'], endpoint['text'], endpoint['words']),
                             ('endpoint', 'hello', 1))
            partial = json.loads(ws.recv(timeout=2))
            self.assertEqual((partial['type'], partial['text']), ('partial', 'hello'))
            ws.send(json.dumps({'type': 'final', 'id': 'endpoint'}))
            final = json.loads(ws.recv(timeout=2))
            self.assertEqual(final['text'], 'hello nova')
            self.assertEqual(final['words'], 2)
            self.assertGreater(final['confidence'], .94)


if __name__ == '__main__':
    unittest.main()
