"""Loopback-only streaming speech recognition for Nova's low-latency voice mode.

The browser sends 16 kHz mono PCM while the user talks. Vosk produces partial
captions and a confidence-scored final candidate. The browser may use a strong
candidate immediately; weak candidates still go through Whisper. This service
never approves actions and never writes audio to disk.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit


class VoiceStreamServer:
    def __init__(self, model_getter, host="127.0.0.1", port=5011, origin_port=5000):
        self.model_getter = model_getter
        self.host = host
        self.port = int(port)
        self.origin = f"http://127.0.0.1:{int(origin_port)}"
        self.token = secrets.token_urlsafe(32)
        self._thread = None
        self._server = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.error = ""

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self.error)

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.error = ""
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="nova-live-stt", daemon=True)
            self._thread.start()
        self._ready.wait(timeout=1.0)

    def _run(self):
        try:
            from websockets.sync.server import serve
            with serve(self._handle, self.host, self.port, origins=[self.origin],
                       compression=None, max_size=128 * 1024, max_queue=8,
                       server_header="Nova-Voice") as server:
                self._server = server
                self._ready.set()
                server.serve_forever()
        except Exception as exc:
            self.error = str(exc)[:300]
            self._ready.set()
        finally:
            self._server = None

    def config(self):
        return {
            "available": self.running,
            "url": f"ws://{self.host}:{self.port}/voice?token={self.token}",
            "error": self.error,
        }

    def _handle(self, connection):
        path = connection.request.path
        supplied = parse_qs(urlsplit(path).query).get("token", [""])[0]
        if not secrets.compare_digest(supplied, self.token):
            connection.close(code=1008, reason="Unauthorized")
            return

        from vosk import KaldiRecognizer
        recognizer = None
        segment_id = None
        audio_bytes = 0
        last_partial = ""
        segment_started = 0.0
        collected_text = []
        collected_words = []

        for message in connection:
            if isinstance(message, str):
                try:
                    command = json.loads(message)
                except (TypeError, ValueError):
                    continue
                kind = command.get("type")
                if kind == "start":
                    segment_id = str(command.get("id", ""))[:64]
                    recognizer = KaldiRecognizer(self.model_getter(), 16000)
                    recognizer.SetWords(True)
                    audio_bytes = 0
                    last_partial = ""
                    segment_started = time.monotonic()
                    collected_text = []
                    collected_words = []
                elif kind == "final" and recognizer and command.get("id") == segment_id:
                    self._send_final(connection, recognizer, segment_id, audio_bytes,
                                     collected_text, collected_words)
                    recognizer = None
                    segment_id = None
                elif kind == "cancel":
                    recognizer = None
                    segment_id = None
                continue

            if not recognizer or not segment_id:
                continue
            # 35 seconds of mono int16 at 16 kHz. Also cap wall time so a broken
            # browser cannot retain recognizer state indefinitely.
            if audio_bytes + len(message) > 1_120_000 or time.monotonic() - segment_started > 40:
                connection.send(json.dumps({"type": "error", "id": segment_id,
                                            "error": "Speech segment exceeded its limit."}))
                recognizer = None
                segment_id = None
                continue
            audio_bytes += len(message)
            complete = recognizer.AcceptWaveform(message)
            payload = json.loads(recognizer.Result() if complete else recognizer.PartialResult())
            if complete:
                if payload.get("text"):
                    collected_text.append(payload["text"].strip())
                collected_words.extend(payload.get("result") or [])
                endpoint_text = " ".join(collected_text).strip()
                if endpoint_text:
                    connection.send(json.dumps({
                        "type": "endpoint", "id": segment_id,
                        "text": endpoint_text[:500], "words": len(collected_words),
                    }))
            live_tail = (payload.get("partial") or "").strip()
            partial = " ".join(collected_text + ([live_tail] if live_tail else [])).strip()
            if partial and partial != last_partial:
                last_partial = partial
                connection.send(json.dumps({"type": "partial", "id": segment_id,
                                            "text": partial[:500]}))

    @staticmethod
    def _send_final(connection, recognizer, segment_id, audio_bytes, collected_text, collected_words):
        result = json.loads(recognizer.FinalResult() or "{}")
        words = collected_words + (result.get("result") or [])
        text = " ".join(collected_text + [(result.get("text") or "").strip()]).strip()
        confidences = [float(word.get("conf", 0.0)) for word in words]
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        connection.send(json.dumps({
            "type": "final", "id": segment_id,
            "text": text[:2000],
            "confidence": round(confidence, 4),
            "words": len(words),
            "audio_ms": round(audio_bytes / 32),
        }))

    def stop(self):
        server = self._server
        if server:
            server.shutdown()
