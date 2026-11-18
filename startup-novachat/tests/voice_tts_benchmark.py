"""Offline Kokoro CPU benchmark. Existing assets only; no microphone/cloud/audio playback."""
import gc
import json
import statistics
import time
from pathlib import Path

import onnxruntime as ort
from kokoro_onnx import Kokoro

root = Path(__file__).resolve().parents[1]
phrases = ["I'm doing well. How are you?", "I can help you with that. What would you like to work on?"]
for threads in (0, 1, 2, 4, 8):
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
    session = ort.InferenceSession(str(root / 'kokoro/kokoro-v1.0.onnx'),
                                   sess_options=options, providers=['CPUExecutionProvider'])
    engine = Kokoro.from_session(session, str(root / 'kokoro/voices-v1.0.bin'))
    results = []
    for phrase in phrases:
        engine.create(phrase, voice='af_heart', speed=1.1, lang='en-us')
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            samples, rate = engine.create(phrase, voice='af_heart', speed=1.1, lang='en-us')
            timings.append(round((time.perf_counter()-started)*1000))
        results.append(dict(median_ms=statistics.median(timings), runs_ms=timings,
                            audio_seconds=round(len(samples)/rate, 2)))
    print(json.dumps(dict(threads=threads, results=results)), flush=True)
    del engine, session
    gc.collect()
