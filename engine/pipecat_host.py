"""Lazy, isolated Pipecat service lifecycle. No microphone is opened here."""
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "5000")) + 12
URL = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
bp = Blueprint("pipecat_voice", __name__)
_lock = threading.Lock()
_process = None


def status():
    if _process is None or _process.poll() is not None:
        return {"available": False, "error": "Voice engine is stopped."}
    try:
        response = requests.get(URL + "/health", headers={"Authorization": "Bearer " + TOKEN}, timeout=1)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return {"available": False, "error": "Voice engine is starting."}


@bp.route("/voice/pipecat", methods=["GET", "POST"])
def config():
    global _process
    origin = f"http://127.0.0.1:{os.environ.get('PORT', '5000')}"
    if request.remote_addr not in ("127.0.0.1", "::1") or request.headers.get("Origin", origin) != origin:
        return jsonify({"error": "Local Nova window required."}), 403
    if request.method == "POST":
        with _lock:
            if _process is None or _process.poll() is not None:
                python = ROOT / ".pipecat-venv/Scripts/python.exe"
                if not python.exists():
                    return jsonify({"available": False, "error": "Run setup-pipecat.ps1 first."}), 503
                env = dict(os.environ, NOVA_PIPECAT_TOKEN=TOKEN, NOVA_PIPECAT_PORT=str(PORT),
                           NOVA_PARENT_PID=str(os.getpid()),
                           NOVA_PARENT_PORT=os.environ.get("PORT", "5000"),
                           PIPECAT_SMART_TURN_LOG_DATA="false")
                with open(ROOT / "pipecat-service.log", "a", encoding="utf-8") as log:
                    _process = subprocess.Popen([str(python), str(ROOT / "pipecat_service.py")],
                        cwd=ROOT, env=env, stdout=log, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if status().get("available"):
                break
            if _process.poll() is not None:
                break
            time.sleep(.15)
    result = status()
    if result.get("available"):
        result["url"] = f"ws://127.0.0.1:{PORT}/voice?token={TOKEN}"
    return jsonify(result), 200 if result.get("available") or request.method == "GET" else 503
