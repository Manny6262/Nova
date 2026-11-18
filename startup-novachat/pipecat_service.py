"""Nova's isolated Pipecat audio worker; LLM/tools stay in Nova's task host.

PCM -> Silero -> Vosk/Smart Turn -> Nova chat adapter -> sentence frames ->
existing Kokoro -> output. No raw audio or transcript logging. Loopback only.
"""
import asyncio
import base64
import ctypes
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import secrets
import time
import wave

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.frames.frames import Frame, InputAudioRawFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.frame_processor import FrameProcessor
from vosk import Model, KaldiRecognizer, SetLogLevel

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:" + os.environ.get("NOVA_PARENT_PORT", "5000")
TOKEN = os.environ.get("NOVA_PIPECAT_TOKEN", "")
logger.remove()
logger.add(__import__("sys").stderr, level="WARNING")
SetLogLevel(-1)
model = None
active = 0
last_active = time.monotonic()


def wav_bytes(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(pcm)
    return buf.getvalue()


@dataclass
class SpeechRequest(TextFrame):
    request_id: str = ""


@dataclass
class FinishTurn(Frame):
    pass


class AudioInput(FrameProcessor):
    def __init__(self, send):
        super().__init__()
        self.send = send
        self.vad = SileroVADAnalyzer(sample_rate=16000, params=VADParams(
            confidence=.75, start_secs=.16, stop_secs=.32, min_volume=.35))
        self.vad.set_sample_rate(16000)
        self.turn = LocalSmartTurnAnalyzerV3(sample_rate=16000)
        self.turn.set_sample_rate(16000)
        self.pre = deque(maxlen=14)
        self.recognizer = None
        self.audio = bytearray()
        self.texts = []
        self.words = []
        self.partial = ""
        self.silent = 0
        self.checked = False
        self.sequence = 0
        self.pause = .45

    def recognize(self, data):
        complete = self.recognizer.AcceptWaveform(data)
        result = json.loads(self.recognizer.Result() if complete else self.recognizer.PartialResult())
        if complete:
            self.texts.append(result.get("text", ""))
            self.words.extend(result.get("result", []))
        return " ".join(self.texts + [result.get("partial", "")]).strip()

    async def finish(self):
        if self.recognizer is None:
            return
        final = json.loads(self.recognizer.FinalResult())
        text = " ".join(self.texts + [final.get("text", "")]).strip()
        words = self.words + final.get("result", [])
        confidence = sum(w.get("conf", 0) for w in words) / max(1, len(words))
        audio = wav_bytes(bytes(self.audio))
        self.recognizer = None
        self.audio.clear()
        self.pre.clear()
        self.turn.clear()
        await self.send({"type": "utterance", "id": self.sequence, "text": text,
            "confidence": confidence, "words": len(words),
            "wav": base64.b64encode(audio).decode(), "source": "PIPECAT"})

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, FinishTurn):
            await self.finish()
            return
        if not isinstance(frame, InputAudioRawFrame):
            await self.push_frame(frame, direction)
            return
        data = frame.audio
        state = await self.vad.analyze_audio(data)
        speech = state == VADState.SPEAKING
        turn_state = self.turn.append_audio(data, speech)
        if self.recognizer is None:
            self.pre.append(data)
            if not speech:
                return
            self.sequence += 1
            self.recognizer = KaldiRecognizer(model, 16000)
            self.recognizer.SetWords(True)
            self.audio = bytearray(b"".join(self.pre))
            self.texts, self.words, self.partial = [], [], ""
            self.recognize(bytes(self.audio))
            self.silent, self.checked = 0, False
            await self.send({"type": "speech_start", "id": self.sequence})
        else:
            self.audio.extend(data)
            partial = self.recognize(data)
            if partial and partial != self.partial:
                self.partial = partial
                await self.send({"type": "partial", "text": partial[:2000]})
        if speech:
            self.silent, self.checked = 0, False
        else:
            self.silent += len(data) / 32000
        if self.silent >= self.pause and not self.checked:
            self.checked = True
            result, _ = await self.turn.analyze_end_of_turn()
            if result == EndOfTurnState.COMPLETE:
                await self.finish()
        if self.recognizer is not None and (self.silent >= max(.95, self.pause + .5)
                or turn_state == EndOfTurnState.COMPLETE or len(self.audio) >= 960000):
            await self.finish()

    async def cleanup(self):
        await self.vad.cleanup()
        await self.turn.cleanup()
        await super().cleanup()


class SpeechOutput(FrameProcessor):
    """Keep synthesis in its own tasks so listening never waits for Kokoro."""
    def __init__(self, send, http):
        super().__init__()
        self.send, self.http = send, http
        self.pending = {}

    async def synthesize(self, frame):
        try:
            started = time.monotonic()
            response = await self.http.post(BASE + "/speak", json={"text": frame.text}, timeout=35)
            response.raise_for_status()
            await self.send({"type": "audio", "id": frame.request_id,
                "wav": base64.b64encode(response.content).decode(),
                "ms": round((time.monotonic() - started) * 1000)})
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.send({"type": "audio_error", "id": frame.request_id})
        finally:
            self.pending.pop(frame.request_id, None)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, SpeechRequest):
            if len(self.pending) >= 2 or frame.request_id in self.pending:
                await self.send({"type": "audio_error", "id": frame.request_id})
            else:
                self.pending[frame.request_id] = asyncio.create_task(self.synthesize(frame))
        else:
            await self.push_frame(frame, direction)

    async def cleanup(self):
        pending = list(self.pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await super().cleanup()


async def watch_parent():
    misses = 0
    parent_handle = None
    if os.name == "nt" and os.environ.get("NOVA_PARENT_PID"):
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        parent_handle = kernel.OpenProcess(0x100000, False, int(os.environ["NOVA_PARENT_PID"]))
        if not parent_handle:
            os._exit(0)
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            while True:
                await asyncio.sleep(1)
                # Watch this exact Nova process, not just the port: a quick
                # desktop restart must not leave an old authenticated helper.
                if parent_handle and kernel.WaitForSingleObject(parent_handle, 0) == 0:
                    os._exit(0)
                try:
                    response = await http.get(BASE + "/health", timeout=2)
                    misses = 0 if response.is_success else misses + 1
                except httpx.HTTPError:
                    misses += 1
                if misses >= 5 or (not active and time.monotonic() - last_active > 120):
                    os._exit(0)
    finally:
        if parent_handle:
            kernel.CloseHandle(parent_handle)


@asynccontextmanager
async def lifespan(app):
    global model
    if not TOKEN:
        raise RuntimeError("Launch through Nova: missing session credential")
    model = await asyncio.to_thread(Model, str(ROOT / "vosk-model-small-en-us-0.15"))
    watcher = asyncio.create_task(watch_parent())
    yield
    watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health(request: Request):
    if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + TOKEN):
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    return {"available": model is not None, "engine": "Pipecat 1.8.1", "sessions": active,
            "vad": "Silero", "turn_detection": "Smart Turn v3.2", "error": ""}


@app.websocket("/voice")
async def voice(ws: WebSocket):
    global active, last_active
    if ws.headers.get("origin") != BASE or not secrets.compare_digest(ws.query_params.get("token", ""), TOKEN):
        await ws.close(code=1008)
        return
    if active:
        await ws.close(code=1013)
        return
    active += 1
    await ws.accept()
    send_lock = asyncio.Lock()

    async def send(message):
        async with send_lock:
            await ws.send_json(message)

    worker = runner_task = None
    async with httpx.AsyncClient(trust_env=False) as http:
        try:
            source = AudioInput(send)
            speaker = SpeechOutput(send, http)
            worker = PipelineWorker(Pipeline([source, speaker]),
                params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
                enable_rtvi=False, enable_turn_tracking=False, idle_timeout_secs=None)
            runner = WorkerRunner(handle_sigint=False)
            runner_task = asyncio.create_task(runner.run(worker))
            await send({"type": "ready"})
            pending = bytearray()
            async with asyncio.timeout(15 * 60):
                while True:
                    message = await asyncio.wait_for(ws.receive(), timeout=30)
                    if message["type"] == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        data = message["bytes"]
                        if len(data) > 16384 or len(data) % 2:
                            await ws.close(code=1009)
                            break
                        pending.extend(data)
                        while len(pending) >= 1024:
                            chunk = bytes(pending[:1024])
                            del pending[:1024]
                            await worker.queue_frame(InputAudioRawFrame(chunk, 16000, 1))
                    elif message.get("text"):
                        command = json.loads(message["text"])
                        kind = command.get("type")
                        if kind == "speak":
                            await worker.queue_frame(SpeechRequest(str(command.get("text", ""))[:1200],
                                request_id=str(command.get("id", ""))[:64]))
                        elif kind == "cancel_speech":
                            task = speaker.pending.get(str(command.get("id", "")))
                            if task:
                                task.cancel()
                        elif kind == "finish":
                            await worker.queue_frame(FinishTurn())
                        elif kind == "settings":
                            source.pause = max(.35, min(1.4, float(command.get("pause", .65))))
        except (WebSocketDisconnect, TimeoutError):
            pass
        except Exception as exc:
            logger.warning("Voice session stopped: {}", type(exc).__name__)
            with suppress(Exception):
                await send({"type": "error", "error": "Voice engine stopped. Using classic voice."})
        finally:
            if worker:
                await worker.cancel()
            if runner_task:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(runner_task, timeout=5)
            active -= 1
            last_active = time.monotonic()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("NOVA_PIPECAT_PORT", "5012")),
                log_level="warning", access_log=False, ws_max_size=65536, ws_max_queue=8)
