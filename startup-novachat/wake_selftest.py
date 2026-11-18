"""Synthetic pass/fail check for Nova's strict two-stage wake recognizer."""
import subprocess

import numpy as np
import requests

import wake_service as wake


def synthesized_pcm(text):
    response = requests.post("http://127.0.0.1:5000/speak", json={"text": text}, timeout=45)
    response.raise_for_status()
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le",
         "-ac", "1", "-ar", "16000", "pipe:1"],
        input=response.content, capture_output=True, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    padding = np.zeros(8000, dtype=np.int16).tobytes()
    return padding + decoded.stdout + padding


def main():
    vosk_model, whisper_model, recognizer_type = wake.load_models()
    cases = [("Hey Nova", True), ("Good morning Manuel", False)]
    for phrase, expected in cases:
        accepted, reason = wake.verify_candidate(
            vosk_model, whisper_model, recognizer_type, synthesized_pcm(phrase),
        )
        print(f"{phrase!r}: {accepted} ({reason})")
        if accepted != expected:
            raise SystemExit(1)
    silence = np.zeros(32000, dtype=np.int16).tobytes()
    accepted, reason = wake.verify_candidate(vosk_model, whisper_model, recognizer_type, silence)
    print(f"silence: {accepted} ({reason})")
    if accepted:
        raise SystemExit(1)
    print("strict wake self-test passed")


if __name__ == "__main__":
    main()
