"""Real cloud voice with computer permissions armed, no requested actions."""

import json
import time
import uuid
import requests

base = "http://127.0.0.1:5000"
session = requests.Session()
session.trust_env = False
sid = session.post(base + "/sessions", json={}).json()["id"]
voice_session = str(uuid.uuid4())
try:
    for prompt in (
        "Hi Nova, how are you? Reply in one short sentence. Do not use tools.",
        "Tell me one short encouraging sentence. Do not use tools.",
    ):
        start, first, answer, provider, done = (
            time.perf_counter(),
            None,
            "",
            None,
            False,
        )
        with session.post(
            base + "/chat",
            json={
                "session_id": sid,
                "message": prompt,
                "voice_mode": True,
                "voice_version": 2,
                "voice_session": voice_session,
                "provider": "codex",
                "caps": {"write": False, "exec": False, "web": False, "control": True},
                "include_screen": True,
                "approval_mode": "ask",
            },
            stream=True,
            timeout=(5, 60),
        ) as response:
            response.raise_for_status()
            for raw in response.iter_lines(chunk_size=1):
                if not raw:
                    continue
                event = json.loads(raw)
                assert not event.get("error"), event
                assert not event.get("tool_start"), "Greeting must not need a tool"
                provider = event.get("provider", provider)
                if event.get("token"):
                    first = first or time.perf_counter() - start
                    answer += event["token"]
                done = done or event.get("done", False)
        assert done and answer and provider == "codex", (done, answer, provider)
        print(
            json.dumps(
                {
                    "provider": provider,
                    "first_text_seconds": round(first, 2),
                    "total_seconds": round(time.perf_counter() - start, 2),
                }
            ),
            flush=True,
        )
finally:
    session.delete(base + f"/sessions/{sid}").raise_for_status()
