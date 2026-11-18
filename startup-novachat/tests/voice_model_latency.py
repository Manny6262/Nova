"""Opt-in tiny cloud turns through existing sign-in. No tools or user data."""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_bridge  # noqa: E402

client = codex_bridge.CLIENT
try:
    for model in ("gpt-5.3-codex-spark", "gpt-5.6-luna", "gpt-5.5"):
        started, first = time.perf_counter(), None
        try:
            for event in client.run_turn(
                "Hello, how are you? Reply in one short natural sentence. Do not use tools.",
                cwd=str(Path(__file__).resolve().parents[1]),
                instructions="You are Nova, a friendly personal assistant. Respond conversationally.",
                write_enabled=False,
                exec_enabled=False,
                web_enabled=False,
                approval_mode="ask",
                model=model,
                timeout=30,
            ):
                if event.get("type") == "token" and first is None:
                    first = round(time.perf_counter() - started, 2)
            print(
                json.dumps(
                    {
                        "model": model,
                        "first": first,
                        "total": round(time.perf_counter() - started, 2),
                    }
                ),
                flush=True,
            )
        except Exception as error:
            print(json.dumps({"model": model, "error": str(error)[:200]}), flush=True)
finally:
    client.stop()
