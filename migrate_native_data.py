"""Initialize Nova Native's private data without changing regular Nova."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: migrate_native_data.py ENGINE_DIR EXISTING_NOVA_DIR")
    engine = Path(sys.argv[1]).resolve()
    previous = Path(sys.argv[2]).resolve()
    if engine == previous:
        raise SystemExit("The native engine must be separate from regular Nova.")
    if not (engine / "app.py").is_file():
        raise SystemExit(f"Native engine files are missing: {engine}")

    workspace = engine / "workspace"
    media = engine / "media"
    workspace.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    workspace_settings = engine / "nova_workspace.json"
    if not workspace_settings.exists():
        workspace_settings.write_text(json.dumps({"path": str(workspace)}), encoding="utf-8")

    destination = engine / "nova.db"
    original = previous / "nova.db"
    if not destination.exists() and original.is_file():
        with sqlite3.connect(f"file:{original.as_posix()}?mode=ro", uri=True) as source:
            with sqlite3.connect(destination) as target:
                source.backup(target)
                tables = {row[0] for row in target.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "jobs" in tables:
                    target.execute(
                        """UPDATE jobs SET status='interrupted',
                           stage='Review after Nova Native migration',
                           error='Review before retrying in the separate native app.',
                           pending_token=NULL
                           WHERE status IN ('queued','running','waiting_approval')""")
                for table in ("scheduled_tasks", "monitors", "event_rules"):
                    if table in tables:
                        columns = {row[1] for row in target.execute(f"PRAGMA table_info({table})")}
                        if "enabled" in columns:
                            target.execute(f"UPDATE {table} SET enabled=0")
                target.commit()
        print("Copied conversation history and paused migrated automations.")
    elif destination.exists():
        print("Preserved existing Nova Native database.")
    else:
        print("Nova Native will create a fresh database on first launch.")

    original_media = previous / "media"
    if original_media.is_dir():
        for item in original_media.rglob("*"):
            if not item.is_file():
                continue
            relative = item.relative_to(original_media)
            output = media / relative
            if not output.exists():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, output)

    original_settings = previous / "nova_settings.json"
    native_settings = engine / "nova_settings.json"
    if original_settings.is_file() and not native_settings.exists():
        shutil.copy2(original_settings, native_settings)

    last_session = engine.parent / "last-session.txt"
    if not last_session.exists() and destination.is_file():
        with sqlite3.connect(destination) as connection:
            try:
                row = connection.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
            except sqlite3.DatabaseError:
                row = None
        if row:
            last_session.write_text(str(row[0]), encoding="utf-8")


if __name__ == "__main__":
    main()
