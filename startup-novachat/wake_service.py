"""Strict, fully local background wake listener for Nova.

The microphone is released whenever Nova is open. While Nova is closed, short
speech candidates must pass two independent recognizers before anything launches:
Vosk constrained to "hey nova", then Faster-Whisper confirmation. Audio stays
in memory and is never written to disk or sent over the network.
"""
import array
import ctypes
import json
import math
import os
import socket
import subprocess
import sys
import time
import types
from collections import deque
from datetime import datetime

import sounddevice as sd

APP_DIR = os.path.dirname(os.path.abspath(__file__))
NOVA_PYTHON = r"C:\Python314\pythonw.exe"
NOVA_DESKTOP = os.path.join(APP_DIR, "desktop.py")
VOSK_MODEL_PATH = os.path.join(APP_DIR, "vosk-model-small-en-us-0.15")
LOG_PATH = os.path.join(APP_DIR, "wake_service.log")
PID_PATH = os.path.join(APP_DIR, "wake_service.pid")
STOP_PATH = os.path.join(APP_DIR, "wake_service.stop")
SAMPLE_RATE = 16000
BLOCK_SIZE = 800
PRE_ROLL_BLOCKS = 8
MIN_CANDIDATE_SECONDS = 0.45
MAX_CANDIDATE_SECONDS = 4.25
SILENCE_SECONDS = 0.55
VOSK_MIN_CONFIDENCE = 0.72
REJECT_COOLDOWN_SECONDS = 0.8
LAUNCH_COOLDOWN_SECONDS = 15.0
DIAGNOSTICS = os.environ.get("NOVA_WAKE_DIAGNOSTICS") == "1"


def log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def claim_single_instance():
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\NovaWakeServiceSingleton")
    return handle, ctypes.windll.kernel32.GetLastError() != 183


def stop_requested():
    return os.path.exists(STOP_PATH)


def nova_is_open():
    try:
        with socket.create_connection(("127.0.0.1", 5000), timeout=0.2):
            return True
    except OSError:
        return False


def launch_nova():
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen([NOVA_PYTHON, NOVA_DESKTOP, "--voice"], cwd=APP_DIR,
                     creationflags=flags, close_fds=True)
    log("two-stage wake confirmed; launching Nova in voice mode")


def load_models():
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    if not os.path.isdir(VOSK_MODEL_PATH):
        raise RuntimeError("Vosk wake model is missing")
    vosk_model = Model(VOSK_MODEL_PATH)
    if "av" not in sys.modules:
        sys.modules["av"] = types.ModuleType("av")
    from faster_whisper import WhisperModel
    whisper_model = WhisperModel("distil-small.en", device="cpu", compute_type="int8", cpu_threads=6)
    return vosk_model, whisper_model, KaldiRecognizer


def rms(chunk):
    samples = array.array("h", chunk)
    return math.sqrt(sum(sample * sample for sample in samples) / max(1, len(samples)))


def normalized(text):
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (text or "").lower()).split())


def begins_with_wake(text):
    clean = normalized(text)
    return clean == "hey nova" or clean.startswith("hey nova ")


def vosk_wake(vosk_model, recognizer_type, raw_audio):
    recognizer = recognizer_type(vosk_model, SAMPLE_RATE, json.dumps(["hey nova", "[unk]"]))
    recognizer.SetWords(True)
    recognizer.AcceptWaveform(raw_audio)
    result = json.loads(recognizer.FinalResult() or "{}")
    if not begins_with_wake(result.get("text", "")):
        return False, 0.0
    words = [word for word in result.get("result", []) if word.get("word") in ("hey", "nova")]
    confidence = sum(float(word.get("conf", 0.0)) for word in words[:2]) / 2 if len(words) >= 2 else 0.0
    return confidence >= VOSK_MIN_CONFIDENCE, confidence


def whisper_wake(whisper_model, raw_audio):
    import numpy as np
    waveform = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = whisper_model.transcribe(
        waveform, language="en", beam_size=3, best_of=3, vad_filter=True,
        initial_prompt="Hey Nova.", hotwords="Hey Nova", condition_on_previous_text=False,
    )
    return begins_with_wake(" ".join(segment.text for segment in segments).strip())


def verify_candidate(vosk_model, whisper_model, recognizer_type, raw_audio):
    duration = len(raw_audio) / 2 / SAMPLE_RATE
    if duration < MIN_CANDIDATE_SECONDS or duration > MAX_CANDIDATE_SECONDS + 0.6:
        return False, "duration"
    stage_one, confidence = vosk_wake(vosk_model, recognizer_type, raw_audio)
    if not stage_one:
        return False, f"vosk {confidence:.2f}"
    if not whisper_wake(whisper_model, raw_audio):
        return False, "whisper"
    return True, f"both ({confidence:.2f})"


def listen_until_wake(vosk_model, whisper_model, recognizer_type):
    pre_roll = deque(maxlen=PRE_ROLL_BLOCKS)
    recording = []
    started = False
    loud_blocks = quiet_blocks = 0
    noise_floor = 220.0
    peak_level = 0.0
    cooldown_until = 0.0
    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK_SIZE,
                           dtype="int16", channels=1, latency="low") as stream:
        log("microphone armed (strict two-stage mode)")
        while not nova_is_open() and not stop_requested():
            chunk, overflowed = stream.read(BLOCK_SIZE)
            chunk = bytes(chunk)
            level = rms(chunk)
            now = time.monotonic()
            if overflowed and DIAGNOSTICS:
                log("audio overflow")
            if not started:
                noise_floor = noise_floor * 0.985 + min(level, 1200.0) * 0.015
                threshold = max(380.0, min(900.0, noise_floor * 2.0))
                pre_roll.append(chunk)
                if now < cooldown_until:
                    continue
                loud_blocks = loud_blocks + 1 if level > threshold else 0
                if loud_blocks >= 2:
                    started = True
                    recording = list(pre_roll)
                    quiet_blocks = 0
                    peak_level = level
                continue
            recording.append(chunk)
            peak_level = max(peak_level, level)
            threshold = max(300.0, min(750.0, noise_floor * 1.45))
            quiet_blocks = quiet_blocks + 1 if level < threshold else 0
            duration = len(recording) * BLOCK_SIZE / SAMPLE_RATE
            if quiet_blocks * BLOCK_SIZE / SAMPLE_RATE < SILENCE_SECONDS and duration < MAX_CANDIDATE_SECONDS:
                continue
            raw_audio = b"".join(recording)
            strong_enough = peak_level >= max(650.0, noise_floor * 2.5)
            accepted, reason = (verify_candidate(vosk_model, whisper_model, recognizer_type, raw_audio)
                                if strong_enough else (False, "low energy"))
            recording, started = [], False
            loud_blocks = quiet_blocks = 0
            peak_level = 0.0
            pre_roll.clear()
            if accepted:
                log(f"wake accepted: {reason}")
                return True
            if DIAGNOSTICS:
                log(f"candidate rejected: {reason}")
            cooldown_until = time.monotonic() + REJECT_COOLDOWN_SECONDS
    return False


def main():
    mutex, first = claim_single_instance()
    if not first or stop_requested():
        return
    try:
        with open(PID_PATH, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        vosk_model, whisper_model, recognizer_type = load_models()
        log("wake service started; exact phrase is 'Hey Nova'")
        while not stop_requested():
            while nova_is_open() and not stop_requested():
                time.sleep(1.0)
            if stop_requested():
                break
            if listen_until_wake(vosk_model, whisper_model, recognizer_type):
                launch_nova()
                time.sleep(LAUNCH_COOLDOWN_SECONDS)
    except Exception as exc:
        log(f"wake service stopped with error: {exc}")
    finally:
        try:
            os.unlink(PID_PATH)
        except OSError:
            pass
        log("wake service stopped")


if __name__ == "__main__":
    main()
