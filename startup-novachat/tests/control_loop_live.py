"""End-to-end Nova browser-control audit against the private fixture page."""

import json
import time
import requests


BASE = "http://127.0.0.1:5000"
FIXTURE = "http://127.0.0.1:5123"
http = requests.Session()
http.trust_env = False


def main():
    for _ in range(30):
        try:
            if http.get(FIXTURE + "/status", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.2)
    else:
        raise RuntimeError("control fixture did not start")

    sid = http.post(BASE + "/sessions", json={}, timeout=10).json()["id"]
    events = []
    answer = ""
    started = time.monotonic()
    try:
        with http.post(
            BASE + "/chat",
            json={
                "session_id": sid,
                "message": (
                    "This is a controlled local browser test. Open " + FIXTURE +
                    "/ in the web browser. Inspect the screen, click the large green "
                    "test button exactly once, inspect again to verify the page says "
                    "CLICKED 1, then report the verified result. Use your tools to do "
                    "the task; do not merely describe what you would do."
                ),
                "provider": "local",
                "model": "qwen3.5:35b",
                "caps": {"write": False, "exec": False, "web": True, "control": True},
                "approval_mode": "bypass",
                "include_screen": True,
                "think": False,
            },
            stream=True,
            timeout=(10, 300),
        ) as response:
            response.raise_for_status()
            for raw in response.iter_lines():
                if not raw:
                    continue
                event = json.loads(raw)
                events.append(event)
                answer += event.get("token", "")

        status = http.get(FIXTURE + "/status", timeout=5).json()
        errors = [event.get("error") for event in events if event.get("error")]
        tools = [
            event["tool_start"].get("name")
            for event in events
            if isinstance(event.get("tool_start"), dict)
        ]
        result = {
            "pass": status.get("click_count") == 1 and not errors,
            "click_count": status.get("click_count"),
            "seconds": round(time.monotonic() - started, 2),
            "tool_events": tools,
            "errors": errors,
            "answer": answer[-1000:],
        }
        print(json.dumps(result, indent=2), flush=True)
        raise SystemExit(0 if result["pass"] else 1)
    finally:
        http.delete(BASE + f"/sessions/{sid}", timeout=10)
        try:
            http.post(FIXTURE + "/shutdown", timeout=2)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
