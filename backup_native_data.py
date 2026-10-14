"""Snapshot Nova Native's personal data while the private engine is running."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from datetime import datetime
from zipfile import ZIP_DEFLATED, ZipFile


installed = Path(os.environ["LOCALAPPDATA"]) / "NovaNative"
engine = installed / "engine"
destination = Path(__file__).resolve().parent / "dist" / (
    "NovaNative-data-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
destination.parent.mkdir(parents=True, exist_ok=True)

with sqlite3.connect(f"file:{(engine / 'nova.db').as_posix()}?mode=ro", uri=True) as source:
    with sqlite3.connect(":memory:") as snapshot:
        source.backup(snapshot)
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        database = snapshot.serialize()

with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
    archive.writestr("engine/nova.db", database)
    for name in ("nova_settings.json", "nova_workspace.json", ".nova_mcp_policy.json"):
        path = engine / name
        if path.is_file():
            archive.write(path, "engine/" + name)
    for path in (engine / "media").rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(installed).as_posix())
    for name in ("notes.json", "last-session.txt"):
        path = installed / name
        if path.is_file():
            archive.write(path, name)

with ZipFile(destination) as archive:
    assert archive.testzip() is None
    assert "engine/nova.db" in archive.namelist()
print(f"{destination} ({destination.stat().st_size / 1024**2:.1f} MB)")
