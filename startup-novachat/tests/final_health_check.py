"""Final read-only health, configuration, and storage verification."""

import json
import sqlite3

import requests


session = requests.Session()
session.trust_env = False
models = session.get("http://127.0.0.1:5000/models", timeout=10).json()
diagnostics = session.get("http://127.0.0.1:5000/diagnostics", timeout=30).json()
with sqlite3.connect(r"C:\NovaChat\nova.db") as connection:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    synthetic = connection.execute(
        "SELECT count(*) FROM activity WHERE title LIKE 'NOVA_AUDIT_%'"
    ).fetchone()[0]

print(json.dumps({
    "provider": models.get("provider"),
    "active_model": models.get("active"),
    "downloaded_models": [item.get("name") for item in models.get("models", [])],
    "diagnostics": diagnostics,
    "database_integrity": integrity,
    "foreign_key_errors": foreign_keys,
    "synthetic_activity_left": synthetic,
}, indent=2))

assert models.get("provider") == "local"
assert models.get("active") == "qwen3.5:35b"
assert models.get("active") in {item.get("name") for item in models.get("models", [])}
assert integrity == "ok"
assert foreign_keys == []
assert synthetic == 0
