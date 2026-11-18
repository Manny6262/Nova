"""Nova's read, write, execution, network, and computer-control tools.

The application gates tools by capability and approval policy. Direct calls to
this module do not ask for approval. File handlers use allowed-root and credential
path checks, but run_python/run_command execute with the host user's permissions:
they are NOT an operating-system sandbox. Full-control/bypass mode is powerful
and should only be used for trusted tasks. Network handlers have separate checks.

Each handler returns a dict: {ok, text, preview}. `text` is what Nova sees;
`preview` is a one-line summary the UI shows on the tool chip.
"""

import os
import re
import sys
import fnmatch
import platform
import shutil
import subprocess
import ctypes
import shlex
import sqlite3
import time
import json
from contextlib import contextmanager
from datetime import datetime

import media_engine

# ---------------------------------------------------------------- sandbox

_APP_DIR = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))

# Nova may read inside any of these roots. Home covers Manuel's projects; the app
# dir lets her read her own source (which usually lives outside the home folder).
ALLOWED_ROOTS = [
    os.path.realpath(os.path.expanduser("~")),               # C:\Users\manue
    _APP_DIR,                                                # C:\NovaChat
]

# a human-friendly name for the sandbox, used in error messages
SANDBOX = os.path.realpath(os.path.expanduser("~"))

# case-insensitive substrings that mark a path as off-limits even inside SANDBOX
DENY = [
    os.sep + "appdata" + os.sep,
    os.sep + "application data" + os.sep,
    os.sep + "local settings" + os.sep,
    "ntuser",
    os.sep + "cookies",
    os.sep + ".ssh",
    os.sep + ".aws",
    os.sep + ".azure",
    os.sep + ".gnupg",
    os.sep + ".openclaw" + os.sep,
    "id_rsa",
    ".pem",
    ".ppk",
]

MAX_FILE_BYTES = 256 * 1024      # biggest file Nova will read whole
MAX_ENTRIES    = 400             # cap on directory listings
MAX_MATCHES    = 120             # cap on search results
MAX_TOOL_TEXT  = 8000            # cap on text handed back to the model
MAX_WRITE_BYTES = 1024 * 1024    # biggest payload a single write may create

# ---- code execution (always gated behind user approval in app.py) ----
WORKSPACE = os.path.join(_APP_DIR, "workspace")   # code runs here by default
EXEC_TIMEOUT = 30                                 # seconds before a run is killed


class ToolError(Exception):
    """Raised for anything the user/model did wrong (bad path, denied, etc.)."""


def _denied(path):
    low = (path + os.sep).lower()
    return any(bad in low for bad in DENY)


DRIVE_REMOVABLE = 2

# declare Win32 signatures so ctypes doesn't truncate pointers on 64-bit (which
# corrupts memory and hard-crashes the process). Must be set before any call.
try:
    from ctypes import wintypes as _wt
    _k32 = ctypes.windll.kernel32
    _k32.GetLogicalDrives.restype = ctypes.c_uint
    _k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    _k32.GetDriveTypeW.restype = ctypes.c_uint
    _k32.GetVolumeInformationW.argtypes = [
        _wt.LPCWSTR, _wt.LPWSTR, _wt.DWORD,
        ctypes.POINTER(_wt.DWORD), ctypes.POINTER(_wt.DWORD), ctypes.POINTER(_wt.DWORD),
        _wt.LPWSTR, _wt.DWORD,
    ]
    _k32.GetVolumeInformationW.restype = _wt.BOOL
except Exception:
    _k32 = None


def _is_removable(drive_root):
    """True if a drive letter (e.g. 'E:\\') is a removable/USB drive."""
    try:
        return _k32 is not None and _k32.GetDriveTypeW(drive_root) == DRIVE_REMOVABLE
    except Exception:
        return False


def _resolve(path, must="any"):
    if not path or not str(path).strip():
        raise ToolError("No path was given.")

    expanded = os.path.expanduser(os.path.expandvars(str(path).strip()))
    real = os.path.realpath(expanded)
    low = real.lower()

    inside = any(
        low == root.lower() or low.startswith(root.lower() + os.sep)
        for root in ALLOWED_ROOTS
    )
    # also allow anywhere on a plugged-in removable (USB) drive
    if not inside:
        drive = os.path.splitdrive(real)[0]        # e.g. "E:"
        if drive and _is_removable(drive + os.sep):
            inside = True

    if not inside:
        raise ToolError(f"Path is outside Nova's allowed area ({SANDBOX} or a USB drive).")

    if _denied(real):
        raise ToolError("That location is off-limits (system or credential data).")

    if must == "dir" and not os.path.isdir(real):
        raise ToolError(f"Not a directory: {real}")
    if must == "file" and not os.path.isfile(real):
        raise ToolError(f"Not a file: {real}")

    return real


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024


def _ok(text, preview):
    if len(text) > MAX_TOOL_TEXT:
        text = text[:MAX_TOOL_TEXT] + f"\n… [truncated at {MAX_TOOL_TEXT} chars]"
    return {"ok": True, "text": text, "preview": preview}


# ---------------------------------------------------------------- handlers

def list_directory(path="~"):
    real = _resolve(path, must="dir")

    dirs, files = [], []
    for name in sorted(os.listdir(real), key=str.lower):
        full = os.path.join(real, name)
        if _denied(full):
            continue
        try:
            if os.path.isdir(full):
                dirs.append(f"[dir]  {name}/")
            else:
                files.append(f"       {name}  ({_human(os.path.getsize(full))})")
        except OSError:
            continue
        if len(dirs) + len(files) >= MAX_ENTRIES:
            break

    body = "\n".join(dirs + files) or "(empty)"
    text = f"{real}\n{body}"
    return _ok(text, f"{len(dirs)} folders, {len(files)} files")


def read_file(path):
    real = _resolve(path, must="file")

    size = os.path.getsize(real)
    if size > MAX_FILE_BYTES:
        raise ToolError(f"File is {_human(size)}; Nova only reads files up to {_human(MAX_FILE_BYTES)}.")

    with open(real, "rb") as fh:
        raw = fh.read()

    if b"\x00" in raw[:4096]:
        raise ToolError("This looks like a binary file, not readable text.")

    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") + 1
    return _ok(text, f"{_human(size)}, {lines} lines")


def read_file_lines(path, start_line=1, end_line=200):
    """Read a bounded line range so large source files remain workable."""
    real = _resolve(path, must="file")
    try:
        start = max(1, int(start_line))
        end = int(end_line)
    except (TypeError, ValueError):
        raise ToolError("Line numbers must be integers.")
    if end < start:
        raise ToolError("end_line must be greater than or equal to start_line.")
    end = min(end, start + 399)
    selected = []
    with open(real, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if number > end:
                break
            if number >= start:
                selected.append(f"{number:>6}  {line.rstrip()}")
    if not selected:
        raise ToolError(f"No lines {start}-{end} exist in {real}.")
    return _ok("\n".join(selected), f"lines {start}-{start + len(selected) - 1}")


def search_files(directory="~", pattern="*"):
    real = _resolve(directory, must="dir")

    matches = []
    for root, subdirs, files in os.walk(real):
        subdirs[:] = [d for d in subdirs if not _denied(os.path.join(root, d))]
        for name in files:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                matches.append(os.path.join(root, name))
                if len(matches) >= MAX_MATCHES:
                    break
        if len(matches) >= MAX_MATCHES:
            break

    body = "\n".join(matches) if matches else f"No files matching '{pattern}' under {real}"
    note = f" (showing first {MAX_MATCHES})" if len(matches) >= MAX_MATCHES else ""
    return _ok(body + note, f"{len(matches)} match(es)")


def search_in_files(directory, query, pattern="*"):
    if not query or not query.strip():
        raise ToolError("No search text was given.")
    real = _resolve(directory, must="dir")
    needle = query.lower()

    hits = []
    for root, subdirs, files in os.walk(real):
        subdirs[:] = [d for d in subdirs if not _denied(os.path.join(root, d))]
        for name in files:
            if not fnmatch.fnmatch(name.lower(), pattern.lower()):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if needle in line.lower():
                            hits.append(f"{full}:{i}: {line.strip()[:200]}")
                            if len(hits) >= MAX_MATCHES:
                                break
            except OSError:
                continue
            if len(hits) >= MAX_MATCHES:
                break
        if len(hits) >= MAX_MATCHES:
            break

    body = "\n".join(hits) if hits else f"No lines containing '{query}' under {real}"
    return _ok(body, f"{len(hits)} line(s)")


def file_info(path):
    real = _resolve(path)
    st = os.stat(real)
    kind = "directory" if os.path.isdir(real) else "file"
    modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"Path:     {real}\n"
        f"Type:     {kind}\n"
        f"Size:     {_human(st.st_size)}\n"
        f"Modified: {modified}"
    )
    return _ok(text, f"{kind}, {_human(st.st_size)}")


def _total_ram_bytes():
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys, stat.ullAvailPhys
    except Exception:
        pass
    return None, None


def system_info():
    total_ram, avail_ram = _total_ram_bytes()
    disk = shutil.disk_usage(os.path.splitdrive(SANDBOX)[0] + os.sep)

    lines = [
        f"OS:        {platform.platform()}",
        f"Machine:   {platform.machine()}",
        f"Processor: {platform.processor() or 'unknown'}",
        f"CPU cores: {os.cpu_count()}",
        f"Python:    {platform.python_version()}",
    ]
    if total_ram:
        lines.append(f"RAM:       {_human(avail_ram)} free of {_human(total_ram)}")
    lines.append(f"Disk (C:): {_human(disk.free)} free of {_human(disk.total)}")

    return _ok("\n".join(lines), f"{os.cpu_count()} cores, {_human(total_ram) if total_ram else '?'} RAM")


def current_time():
    now = datetime.now()
    text = now.strftime("%A, %B %d, %Y  %H:%M:%S")
    return _ok(text, now.strftime("%H:%M"))


def _volume_label(root):
    if _k32 is None:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        _k32.GetVolumeInformationW(root, buf, 256, None, None, None, None, 0)
        return buf.value
    except Exception:
        return ""


def list_drives():
    """List drives currently attached, flagging removable/USB ones Nova can write to."""
    import string
    if _k32 is None:
        raise ToolError("Drive enumeration isn't available on this system.")
    kinds = {2: "USB / removable", 3: "fixed disk", 4: "network", 5: "CD/DVD", 6: "RAM disk"}
    bitmask = _k32.GetLogicalDrives()

    lines = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not (bitmask & (1 << i)):
            continue
        root = f"{letter}:\\"
        kind = kinds.get(_k32.GetDriveTypeW(root), "unknown")
        label = _volume_label(root)
        try:
            u = shutil.disk_usage(root)
            space = f"{_human(u.free)} free of {_human(u.total)}"
        except OSError:
            space = "not ready"
        lines.append(f"{root}  [{kind}]" + (f'  "{label}"' if label else "") + f"  — {space}")

    return _ok("Drives attached:\n" + "\n".join(lines), f"{len(lines)} drives")


# ---- write tools (gated behind a user confirmation in app.py) ----

def write_file(path, content=""):
    real = _resolve(path)
    parent = os.path.dirname(real)
    if not os.path.isdir(parent):
        raise ToolError(f"Parent folder doesn't exist: {parent}")
    if os.path.isdir(real):
        raise ToolError(f"That path is a folder, not a file: {real}")

    data = content or ""
    if len(data.encode("utf-8", "ignore")) > MAX_WRITE_BYTES:
        raise ToolError("Content is too large to write (limit 1 MB).")

    existed = os.path.isfile(real)
    with open(real, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)

    verb = "Overwrote" if existed else "Created"
    return _ok(f"{verb} {real} ({len(data)} chars).", f"{verb.lower()} {os.path.basename(real)}")


def append_to_file(path, content=""):
    real = _resolve(path, must="file")
    data = content or ""
    if len(data.encode("utf-8", "ignore")) > MAX_WRITE_BYTES:
        raise ToolError("Content is too large to append (limit 1 MB).")

    with open(real, "a", encoding="utf-8", newline="") as fh:
        fh.write(data)

    return _ok(f"Appended {len(data)} chars to {real}.", f"appended to {os.path.basename(real)}")


def create_directory(path):
    real = _resolve(path)
    if os.path.isfile(real):
        raise ToolError(f"A file already exists at that path: {real}")
    if os.path.isdir(real):
        return _ok(f"Folder already exists: {real}", "already exists")

    os.makedirs(real, exist_ok=True)
    return _ok(f"Created folder {real}.", f"created {os.path.basename(real)}/")


def replace_in_file(path, old_text, new_text, expected_replacements=1):
    """Replace an exact string atomically, rejecting stale or ambiguous edits."""
    real = _resolve(path, must="file")
    if not isinstance(old_text, str) or not old_text:
        raise ToolError("old_text must be a non-empty exact string.")
    if not isinstance(new_text, str):
        raise ToolError("new_text must be text.")
    try:
        expected = int(expected_replacements)
    except (TypeError, ValueError):
        raise ToolError("expected_replacements must be an integer.")
    if not 1 <= expected <= 100:
        raise ToolError("expected_replacements must be between 1 and 100.")
    with open(real, "r", encoding="utf-8", errors="strict") as handle:
        original = handle.read()
    actual = original.count(old_text)
    if actual != expected:
        raise ToolError(
            f"Edit not applied: expected {expected} exact match(es), found {actual}. "
            "Read the current file and retry with a unique exact string."
        )
    updated = original.replace(old_text, new_text)
    if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError("Edited file would exceed the 1 MB write limit.")
    temp = real + ".nova-edit.tmp"
    try:
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, real)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return _ok(f"Updated {real}: {actual} exact replacement(s).", f"edited {os.path.basename(real)}")


def move_path(source, destination):
    """Move or rename one file or directory inside Nova's allowed roots."""
    src = _resolve(source)
    dst = _resolve(destination)
    if not os.path.exists(src):
        raise ToolError(f"Source does not exist: {src}")
    if os.path.exists(dst):
        raise ToolError(f"Destination already exists: {dst}")
    parent = os.path.dirname(dst)
    if not os.path.isdir(parent):
        raise ToolError(f"Destination folder does not exist: {parent}")
    shutil.move(src, dst)
    return _ok(f"Moved {src} to {dst}.", f"moved to {dst}")


def delete_path(path):
    """Delete one file or one empty folder; recursive deletion stays shell-only."""
    real = _resolve(path)
    if os.path.isfile(real):
        os.unlink(real)
        return _ok(f"Deleted file {real}.", f"deleted {os.path.basename(real)}")
    if os.path.isdir(real):
        try:
            os.rmdir(real)
        except OSError:
            raise ToolError("Only empty folders can be deleted with this tool.")
        return _ok(f"Deleted empty folder {real}.", f"deleted {os.path.basename(real)}/")
    raise ToolError(f"Path does not exist: {real}")


@contextmanager
def _schedule_db():
    connection = sqlite3.connect(os.path.join(_APP_DIR, "nova.db"))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _next_scheduled_run(schedule_type, schedule_value, now=None):
    current = time.time() if now is None else now
    if schedule_type == "interval":
        try:
            minutes = int(schedule_value)
        except (TypeError, ValueError):
            raise ToolError("Interval must be a whole number of minutes.")
        if not 1 <= minutes <= 525600:
            raise ToolError("Interval must be between 1 and 525600 minutes.")
        return current + minutes * 60
    if schedule_type == "daily":
        try:
            hour, minute = (int(part) for part in str(schedule_value).split(":"))
        except (TypeError, ValueError):
            raise ToolError("Daily time must use HH:MM in local time.")
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ToolError("Daily time must use HH:MM from 00:00 to 23:59.")
        from datetime import timedelta
        base = datetime.fromtimestamp(current)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= current:
            target += timedelta(days=1)
        return target.timestamp()
    raise ToolError("schedule_type must be 'daily' or 'interval'.")


def list_scheduled_tasks():
    """List Nova's recurring background tasks."""
    with _schedule_db() as connection:
        rows = connection.execute(
            "SELECT id,name,prompt,schedule_type,schedule_value,enabled,last_run,next_run "
            "FROM scheduled_tasks ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    if not rows:
        return _ok("No scheduled tasks.", "0 scheduled tasks")
    lines = []
    for row in rows:
        state = "enabled" if row["enabled"] else "paused"
        next_run = datetime.fromtimestamp(row["next_run"]).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"#{row['id']} [{state}] {row['name']} — {row['schedule_type']} "
            f"{row['schedule_value']} — next {next_run}\n  {row['prompt'][:300]}"
        )
    return _ok("\n".join(lines), f"{len(rows)} scheduled task(s)")


def create_scheduled_task(name, instruction, schedule_type, schedule_value):
    """Create a recurring task and its conversation for results."""
    title = str(name or "").strip()
    prompt = str(instruction or "").strip()
    value = str(schedule_value or "").strip()
    if not title or not prompt:
        raise ToolError("A task name and instruction are required.")
    if len(title) > 160 or len(prompt) > 8000:
        raise ToolError("The task name or instruction is too long.")
    next_run = _next_scheduled_run(schedule_type, value)
    now = time.time()
    with _schedule_db() as connection:
        cursor = connection.execute(
            "INSERT INTO sessions (title,created_at,updated_at) VALUES (?,?,?)",
            ("⏰ " + title, now, now),
        )
        session_id = cursor.lastrowid
        cursor = connection.execute(
            "INSERT INTO scheduled_tasks "
            "(name,prompt,schedule_type,schedule_value,session_id,enabled,next_run,created_at) "
            "VALUES (?,?,?,?,?,1,?,?)",
            (title, prompt, schedule_type, value, session_id, next_run, now),
        )
        task_id = cursor.lastrowid
    when = datetime.fromtimestamp(next_run).strftime("%A, %B %d at %I:%M %p")
    return _ok(
        f"Created scheduled task #{task_id} '{title}'. Next run: {when}. Results go to session {session_id}.",
        f"scheduled {title}",
    )


def manage_scheduled_task(task_id, action):
    """Pause, resume, or delete an existing scheduled task."""
    try:
        identifier = int(task_id)
    except (TypeError, ValueError):
        raise ToolError("task_id must be an integer.")
    operation = str(action or "").strip().lower()
    if operation not in {"pause", "resume", "delete"}:
        raise ToolError("action must be pause, resume, or delete.")
    with _schedule_db() as connection:
        row = connection.execute(
            "SELECT id,name,schedule_type,schedule_value FROM scheduled_tasks WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row:
            raise ToolError(f"Scheduled task #{identifier} was not found.")
        if operation == "delete":
            connection.execute("DELETE FROM scheduled_tasks WHERE id=?", (identifier,))
        elif operation == "pause":
            connection.execute("UPDATE scheduled_tasks SET enabled=0 WHERE id=?", (identifier,))
        else:
            next_run = _next_scheduled_run(row["schedule_type"], row["schedule_value"])
            connection.execute(
                "UPDATE scheduled_tasks SET enabled=1,next_run=? WHERE id=?",
                (next_run, identifier),
            )
    return _ok(f"{operation.title()}d scheduled task #{identifier} '{row['name']}'.", operation)


# ---- code execution (both gated behind a user confirmation in app.py) ----

def _format_run(returncode, stdout, stderr):
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip())
    body = "\n".join(parts).strip() or "(no output)"
    if len(body) > MAX_TOOL_TEXT:
        body = body[:MAX_TOOL_TEXT] + f"\n… [truncated at {MAX_TOOL_TEXT} chars]"
    ok = returncode == 0
    return {"ok": ok, "text": body, "preview": "done" if ok else f"exit {returncode}"}


def run_python(code=""):
    if not code or not code.strip():
        raise ToolError("No code to run.")
    os.makedirs(WORKSPACE, exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=WORKSPACE, capture_output=True, text=True, timeout=EXEC_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"Code was killed after {EXEC_TIMEOUT}s (still running).")
    return _format_run(proc.returncode, proc.stdout or "", proc.stderr or "")


def run_command(command="", directory=None):
    if not command or not command.strip():
        raise ToolError("No command given.")
    cwd = _resolve(directory, must="dir") if directory else WORKSPACE
    os.makedirs(WORKSPACE, exist_ok=True)
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=EXEC_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command was killed after {EXEC_TIMEOUT}s (still running).")
    return _format_run(proc.returncode, proc.stdout or "", proc.stderr or "")


# ---- web access (read-only: search + fetch a page) ----

WEB_TIMEOUT = 10
WEB_MAX_BYTES = 2_000_000        # stop downloading a page past 2 MB
WEB_MAX_TEXT = 6000              # cap text handed to the model


def _check_url_safe(url):
    """Only http/https, and block internal/loopback/private addresses (SSRF guard)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else "https://" + url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("Only http/https URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise ToolError("That doesn't look like a valid URL.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ToolError(f"Couldn't resolve host: {host}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ToolError("That address is internal/private and off-limits.")

    return parsed.geturl()


def web_search(query, count=5):
    if not query or not query.strip():
        raise ToolError("No search query.")
    from ddgs import DDGS

    count = max(1, min(int(count or 5), 10))
    results = DDGS().text(query, max_results=count)
    if not results:
        return _ok(f"No results for: {query}", "0 results")

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n   {r.get('href', '')}\n   {r.get('body', '')[:220]}")
    return _ok("\n\n".join(lines), f"{len(results)} results")


def fetch_url(url):
    safe_url = _check_url_safe(url)
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(safe_url, headers={"User-Agent": "Mozilla/5.0 (NovaAssistant)"},
                        timeout=WEB_TIMEOUT, stream=True)
    resp.raise_for_status()

    ctype = resp.headers.get("content-type", "")
    raw = b""
    for chunk in resp.iter_content(8192):
        raw += chunk
        if len(raw) > WEB_MAX_BYTES:
            break

    if "html" in ctype or raw.lstrip()[:1] == b"<":
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "form"]):
            tag.decompose()
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        text = soup.get_text("\n", strip=True)
    else:
        title = ""
        text = raw.decode("utf-8", errors="replace")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > WEB_MAX_TEXT:
        text = text[:WEB_MAX_TEXT] + "\n… [truncated]"

    from urllib.parse import urlparse
    header = (title + "\n" if title else "") + safe_url
    return _ok(header + "\n\n" + text, f"{len(text)} chars from {urlparse(safe_url).hostname}")


# ---- market data (read-only, via Yahoo Finance / yfinance) ----

def _fmt_money(n):
    if n is None:
        return "n/a"
    n = float(n)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(n) >= div:
            return f"${n / div:.2f}{unit}"
    return f"${n:,.2f}"


def get_quote(symbol):
    if not symbol or not symbol.strip():
        raise ToolError("No symbol given.")
    import yfinance as yf

    sym = symbol.strip().upper()
    fi = yf.Ticker(sym).fast_info
    last = fi.get("lastPrice")
    if last is None:
        raise ToolError(f"No market data for '{sym}' — check the symbol.")

    prev = fi.get("previousClose")
    change = (last - prev) if prev else None
    pct = (change / prev * 100) if (change is not None and prev) else None

    lines = [
        f"{sym}   ${last:,.2f}" +
        (f"   ({'+' if change >= 0 else ''}{change:,.2f}, {pct:+.2f}%)" if change is not None else ""),
        f"Day range:  ${fi.get('dayLow', 0):,.2f} – ${fi.get('dayHigh', 0):,.2f}",
        f"Prev close: ${prev:,.2f}" if prev else "",
        f"52-week:    ${fi.get('yearLow', 0):,.2f} – ${fi.get('yearHigh', 0):,.2f}",
        f"Market cap: {_fmt_money(fi.get('marketCap'))}",
        f"Volume:     {int(fi.get('lastVolume', 0)):,}" if fi.get("lastVolume") else "",
    ]
    preview = f"{sym} ${last:,.2f}" + (f" {pct:+.1f}%" if pct is not None else "")
    return _ok("\n".join(l for l in lines if l), preview)


def get_history(symbol, period="1mo", interval="1d"):
    if not symbol or not symbol.strip():
        raise ToolError("No symbol given.")
    import yfinance as yf

    sym = symbol.strip().upper()
    h = yf.Ticker(sym).history(period=period, interval=interval)
    if h.empty:
        raise ToolError(f"No price history for '{sym}' (period={period}).")

    closes = h["Close"]
    start, end = float(closes.iloc[0]), float(closes.iloc[-1])
    pct = (end - start) / start * 100 if start else 0
    hi, lo = float(h["High"].max()), float(h["Low"].min())

    recent = closes.tail(8)
    pts = "\n".join(f"  {idx.strftime('%Y-%m-%d')}:  ${v:,.2f}" for idx, v in recent.items())
    text = (f"{sym} — {period} history ({interval})\n"
            f"Move:  ${start:,.2f} → ${end:,.2f}   ({pct:+.2f}%)\n"
            f"High ${hi:,.2f} / Low ${lo:,.2f}\n"
            f"Recent closes:\n{pts}")
    return _ok(text, f"{sym} {pct:+.1f}% / {period}")


def get_fundamentals(symbol):
    if not symbol or not symbol.strip():
        raise ToolError("No symbol given.")
    import yfinance as yf

    sym = symbol.strip().upper()
    info = yf.Ticker(sym).info
    if not info or not info.get("shortName"):
        raise ToolError(f"No fundamentals for '{sym}'.")

    lines = [
        f"{info.get('shortName', sym)} ({sym})",
        f"Sector:      {info.get('sector', 'n/a')}" +
        (f" · {info.get('industry')}" if info.get("industry") else ""),
        f"Market cap:  {_fmt_money(info.get('marketCap'))}",
        f"P/E (ttm):   {round(info['trailingPE'], 2) if info.get('trailingPE') else 'n/a'}",
        f"EPS (ttm):   {info.get('trailingEps', 'n/a')}",
        f"52-week:     ${info.get('fiftyTwoWeekLow', '?')} – ${info.get('fiftyTwoWeekHigh', '?')}",
    ]
    dy = info.get("dividendYield")
    if dy:
        lines.append(f"Dividend:    {dy if dy < 1 else dy}% yield")
    summary = info.get("longBusinessSummary", "")
    if summary:
        lines.append("\n" + summary[:400])
    return _ok("\n".join(lines), f"{sym} fundamentals")


def get_market_overview():
    import yfinance as yf

    indices = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones"), ("^VIX", "VIX")]
    lines = []
    for sym, name in indices:
        try:
            fi = yf.Ticker(sym).fast_info
            last, prev = fi.get("lastPrice"), fi.get("previousClose")
            pct = (last - prev) / prev * 100 if (last and prev) else None
            lines.append(f"{name:11} {last:,.2f}" + (f"   ({pct:+.2f}%)" if pct is not None else ""))
        except Exception:
            lines.append(f"{name:11} n/a")
    return _ok("Major indices:\n" + "\n".join(lines), "market overview")


# ---- desktop control (always approval-gated in app.py) ----

def open_application(path, arguments=""):
    """Launch one explicitly named application or document."""
    import subprocess
    target = os.path.expandvars(os.path.expanduser(path.strip()))
    if not target:
        raise ToolError("No application or document given.")
    args = [target] + (shlex.split(arguments, posix=False) if arguments else [])
    subprocess.Popen(args, cwd=os.path.dirname(target) if os.path.isfile(target) else None)
    return _ok(f"Opened {target}", os.path.basename(target) or target)


def open_webpage(url):
    """Open one http/https address in the user's default browser."""
    import webbrowser
    import time as _time
    from urllib.parse import urlparse

    target = (url or "").strip()
    if not target:
        raise ToolError("No web address given.")
    if "://" not in target:
        target = "https://" + target
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError("Only valid http/https web addresses can be opened.")
    if parsed.username or parsed.password:
        raise ToolError("Web addresses containing credentials are not allowed.")
    if len(target) > 4096:
        raise ToolError("That web address is too long.")
    try:
        import win32gui
        import win32process
        kernel32 = ctypes.windll.kernel32
        browser_classes = {"Chrome_WidgetWin_1", "MozillaWindowClass"}
        browser_processes = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"}
        def process_name(hwnd):
            process_id = win32process.GetWindowThreadProcessId(hwnd)[1]
            handle = kernel32.OpenProcess(0x1000, False, process_id)
            if not handle:
                return ""
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return os.path.basename(buffer.value).lower()
                return ""
            finally:
                kernel32.CloseHandle(handle)
        def is_browser_window(hwnd):
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) not in browser_classes:
                return False
            try:
                return process_name(hwnd) in browser_processes
            except Exception:
                return False
        before = {}
        def remember(hwnd, _extra):
            if is_browser_window(hwnd):
                before[hwnd] = win32gui.GetWindowText(hwnd)
        win32gui.EnumWindows(remember, None)
    except Exception:
        win32gui = None
        browser_classes = set()
        before = {}

    if not webbrowser.open(target, new=2, autoraise=True):
        raise ToolError("Windows could not open the default browser.")

    focused_title = ""
    if win32gui is not None:
        # webbrowser.open may create the right tab without foregrounding its
        # window. Detect a new/retitled browser window and explicitly raise it.
        candidate = None
        for _ in range(20):
            windows = []
            def collect(hwnd, _extra):
                if is_browser_window(hwnd):
                    windows.append((hwnd, win32gui.GetWindowText(hwnd)))
            win32gui.EnumWindows(collect, None)
            changed = [(hwnd, title) for hwnd, title in windows
                       if title and before.get(hwnd) != title]
            if changed:
                candidate, focused_title = changed[0]
                break
            if windows and _ == 19:
                # The target may already be the active tab, so its title and
                # handle do not change. It is still safe to foreground the real
                # browser process (ChatGPT/WebView windows are excluded above).
                candidate, focused_title = windows[0]
                break
            _time.sleep(0.15)
        if candidate:
            try:
                user32 = ctypes.windll.user32
                win32gui.ShowWindow(candidate, 9)
                win32gui.SetWindowPos(candidate, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
                win32gui.SetWindowPos(candidate, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
                foreground = user32.GetForegroundWindow()
                foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
                target_thread = user32.GetWindowThreadProcessId(candidate, None)
                current_thread = kernel32.GetCurrentThreadId()
                attached = []
                for source_thread in {current_thread, foreground_thread}:
                    if source_thread and source_thread != target_thread:
                        if user32.AttachThreadInput(source_thread, target_thread, True):
                            attached.append(source_thread)
                try:
                    # A synthetic Alt release allows a process handling the
                    # user's authorized action to cross Windows' focus lock.
                    user32.keybd_event(0x12, 0, 0, 0)
                    user32.keybd_event(0x12, 0, 0x0002, 0)
                    user32.BringWindowToTop(candidate)
                    user32.SetForegroundWindow(candidate)
                    user32.SetActiveWindow(candidate)
                finally:
                    for source_thread in attached:
                        user32.AttachThreadInput(source_thread, target_thread, False)
                _time.sleep(0.15)
                if user32.GetForegroundWindow() != candidate:
                    focused_title = ""
            except Exception:
                focused_title = ""
    detail = f"Opened {target} in the default browser."
    if focused_title:
        detail += f" Foreground browser window: {focused_title}."
    return _ok(detail, parsed.hostname or target)


def search_youtube(query):
    """Open YouTube search results for a safely encoded query."""
    from urllib.parse import urlencode

    search = (query or "").strip()
    if not search:
        raise ToolError("No YouTube search given.")
    if len(search) > 500:
        raise ToolError("The YouTube search is too long.")
    url = "https://www.youtube.com/results?" + urlencode({"search_query": search})
    result = open_webpage(url)
    result["preview"] = f"YouTube: {search[:80]}"
    result["text"] = f"Opened YouTube search results for: {search}"
    return result


def copy_file(source, destination):
    """Copy one file, including to a user-selected USB drive."""
    import shutil
    src = _resolve(source, must="file")
    dst = os.path.abspath(os.path.expanduser(destination))
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return _ok(f"Copied {src} to {dst}", f"copied to {dst}")


def type_text(text):
    """Type text into the currently focused Windows application."""
    import ctypes
    user32 = ctypes.windll.user32
    title = ctypes.create_unicode_buffer(512)
    foreground = user32.GetForegroundWindow()
    user32.GetWindowTextW(foreground, title, len(title))
    if title.value.strip().lower() == "nova":
        raise ToolError("Refusing to type into Nova's own window during computer control. Focus the requested application first.")
    for ch in text[:10000]:
        user32.VkKeyScanW(ord(ch))
        user32.SendMessageW(user32.GetForegroundWindow(), 0x0102, ord(ch), 0)
    return _ok(f"Typed {len(text[:10000])} characters.", "text entered")


def click_screen(x, y):
    """Move the pointer and left-click an exact screen coordinate."""
    import ctypes
    user32 = ctypes.windll.user32
    title = ctypes.create_unicode_buffer(512)
    foreground = user32.GetForegroundWindow()
    user32.GetWindowTextW(foreground, title, len(title))
    if title.value.strip().lower() == "nova":
        raise ToolError("Refusing to click Nova's own window during computer control. Navigate to and inspect the requested application first.")
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78) or user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(79) or user32.GetSystemMetrics(1)
    px, py = int(x), int(y)
    if not (0 <= px < width and 0 <= py < height):
        raise ToolError(f"Coordinate is outside the {width}x{height} screenshot.")
    user32.SetCursorPos(left + px, top + py)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return _ok(f"Clicked screen coordinate ({int(x)}, {int(y)}).", f"clicked {x},{y}")


def move_pointer(x, y):
    """Move the pointer without clicking."""
    user32 = ctypes.windll.user32
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78) or user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(79) or user32.GetSystemMetrics(1)
    px, py = int(x), int(y)
    if not (0 <= px < width and 0 <= py < height):
        raise ToolError(f"Coordinate is outside the {width}x{height} screenshot.")
    if not user32.SetCursorPos(left + px, top + py):
        raise ToolError("Windows could not move the pointer.")
    return _ok(f"Moved pointer to ({int(x)}, {int(y)}).", f"pointer at {x},{y}")


def scroll_screen(clicks):
    """Scroll the active window; positive is up and negative is down."""
    amount = max(-20, min(20, int(clicks)))
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, amount * 120, 0)
    return _ok(f"Scrolled {'up' if amount > 0 else 'down'} {abs(amount)} notch(es).",
               f"scrolled {amount:+d}")


_KEYS = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "escape": 0x1B,
    "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "delete": 0x2E,
}
_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 13)})


def _vk(name):
    key = str(name or "").strip().lower()
    if key in _KEYS:
        return _KEYS[key]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    raise ToolError(f"Unsupported key: {name}")


def press_key(key, presses=1):
    """Press one named keyboard key a bounded number of times."""
    vk = _vk(key)
    count = max(1, min(20, int(presses)))
    user32 = ctypes.windll.user32
    for _ in range(count):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)
    return _ok(f"Pressed {key} {count} time(s).", f"pressed {key}")


def hotkey(keys):
    """Press a keyboard shortcut such as ['ctrl', 's']."""
    if not isinstance(keys, list) or not 2 <= len(keys) <= 4:
        raise ToolError("A hotkey requires a list of 2 to 4 keys.")
    codes = [_vk(key) for key in keys]
    user32 = ctypes.windll.user32
    for vk in codes:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(codes):
        user32.keybd_event(vk, 0, 0x0002, 0)
    label = "+".join(str(k) for k in keys)
    return _ok(f"Pressed hotkey {label}.", label)


def inspect_screen():
    """Request a fresh screenshot plus a text map of the foreground UI."""
    user32 = ctypes.windll.user32
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78) or user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(79) or user32.GetSystemMetrics(1)
    details = [
        "A fresh screenshot is attached to this tool result.",
        f"Virtual desktop size: {width}x{height}.",
        "Coordinates below use the same 0-based virtual-desktop space as click_screen.",
    ]
    try:
        from pywinauto import Desktop

        handle = user32.GetForegroundWindow()
        window = Desktop(backend="uia").window(handle=handle)
        title = window.window_text() or "untitled window"
        details.append(f"Foreground window: {title}")
        details.append("Visible UI elements (type, label, click center):")
        allowed = {
            "Button", "CheckBox", "ComboBox", "Document", "Edit", "Hyperlink",
            "ListItem", "MenuItem", "RadioButton", "TabItem", "Text", "TreeItem",
        }
        seen = set()
        count = 0
        for element in window.descendants():
            label = " ".join((element.window_text() or "").split())
            kind = element.element_info.control_type or "Element"
            if not label or kind not in allowed:
                continue
            rect = element.rectangle()
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            x = int((rect.left + rect.right) / 2 - left)
            y = int((rect.top + rect.bottom) / 2 - top)
            if not (0 <= x < width and 0 <= y < height):
                continue
            key = (kind, label, x, y)
            if key in seen:
                continue
            seen.add(key)
            details.append(f'- {kind}: "{label[:160]}" at ({x}, {y})')
            count += 1
            if count >= 80:
                details.append("- ... additional elements omitted")
                break
        if not count:
            details.append("- No labeled accessibility elements were exposed by this window.")
    except Exception as error:
        details.append(f"Accessibility map unavailable: {error}")
    return _ok(
        "\n".join(details),
        f"captured {width}x{height} with UI map",
    )


# ---------------------------------------------------------- media generation

def media_engine_status():
    """Report whether the private local media engine and its models are ready."""
    state = media_engine.status()
    image_ready = all(item["ready"] for item in state["image_models"].values())
    video_ready = all(item["ready"] for item in state["video_models"].values())
    device = state.get("device") or "not initialized"
    text = json.dumps(state, indent=2)
    return _ok(text, f"engine {'ready' if state['ready'] else 'stopped'}; image {image_ready}; video {video_ready}; {device}")


def generate_image(prompt="", negative_prompt="", width=1024, height=1024, steps=8, seed=-1):
    """Generate and publish one image through the local Z-Image Turbo workflow."""
    try:
        result = media_engine.generate_image(prompt, negative_prompt, width, height, steps, seed)
    except media_engine.MediaError as error:
        raise ToolError(str(error)) from error
    media = result["media"]
    response = _ok(
        f"Generated image successfully.\nPath: {media['path']}\nURL: {media['url']}\n"
        f"Size: {result['width']}x{result['height']}\nSeed: {result['seed']}",
        f"generated {result['width']}x{result['height']} image",
    )
    response["media"] = media
    return response


def generate_video(prompt="", negative_prompt="", width=512, height=320,
                   frames=33, fps=8, steps=30, seed=-1):
    """Generate and publish one MP4 video through the local Wan 2.1 workflow."""
    try:
        result = media_engine.generate_video(
            prompt, negative_prompt, width, height, frames, fps, steps, seed
        )
    except media_engine.MediaError as error:
        raise ToolError(str(error)) from error
    media = result["media"]
    duration = result["frames"] / result["fps"]
    response = _ok(
        f"Generated video successfully.\nPath: {media['path']}\nURL: {media['url']}\n"
        f"Size: {result['width']}x{result['height']}\nFrames: {result['frames']} at {result['fps']} fps "
        f"({duration:.1f} seconds)\nSeed: {result['seed']}",
        f"generated {duration:.1f}s MP4 video",
    )
    response["media"] = media
    return response


# ---------------------------------------------------------------- registry

# name -> (handler, JSON schema in the OpenAI/Ollama function-tool format)
_SPEC = {
    "list_directory": (
        list_directory,
        "List the files and sub-folders inside a directory.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path, e.g. '~/robinhood_bot' or an absolute path."}
            },
            "required": ["path"],
        },
    ),
    "read_file": (
        read_file,
        "Read the full text contents of a single file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a text file."}
            },
            "required": ["path"],
        },
    ),
    "read_file_lines": (
        read_file_lines,
        "Read a numbered line range from a text file. Use this for large source files or precise edits.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a text file."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "start_line", "end_line"],
        },
    ),
    "search_files": (
        search_files,
        "Find files by name using a glob pattern (e.g. '*.py'), searching recursively.",
        {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Folder to search under."},
                "pattern": {"type": "string", "description": "Filename glob, e.g. '*.py' or 'app*'."},
            },
            "required": ["directory", "pattern"],
        },
    ),
    "search_in_files": (
        search_in_files,
        "Search file *contents* for a piece of text, returning matching lines with file and line number.",
        {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Folder to search under."},
                "query": {"type": "string", "description": "Text to look for inside files."},
                "pattern": {"type": "string", "description": "Optional filename glob to limit which files are searched, e.g. '*.py'."},
            },
            "required": ["directory", "query"],
        },
    ),
    "file_info": (
        file_info,
        "Get metadata about a file or folder: type, size, and last-modified time.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a file or folder."}
            },
            "required": ["path"],
        },
    ),
    "system_info": (
        system_info,
        "Report this computer's OS, CPU, RAM, and free disk space.",
        {"type": "object", "properties": {}},
    ),
    "current_time": (
        current_time,
        "Get the current local date and time.",
        {"type": "object", "properties": {}},
    ),
    "list_drives": (
        list_drives,
        "List the drives attached to this computer, including any plugged-in USB / removable "
        "drives (with their free space and label). Use this to find a USB before saving to it.",
        {"type": "object", "properties": {}},
    ),
    "write_file": (
        write_file,
        "Create a new text file or overwrite an existing one with the given content. Requires the user's approval.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file to write."},
                "content": {"type": "string", "description": "The full text to write into the file."},
            },
            "required": ["path", "content"],
        },
    ),
    "append_to_file": (
        append_to_file,
        "Append text to the end of an existing text file. Requires the user's approval.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the existing file to append to."},
                "content": {"type": "string", "description": "The text to add at the end of the file."},
            },
            "required": ["path", "content"],
        },
    ),
    "create_directory": (
        create_directory,
        "Create a new folder (and any missing parent folders). Requires the user's approval.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the folder to create."}
            },
            "required": ["path"],
        },
    ),
    "replace_in_file": (
        replace_in_file,
        "Precisely edit a text file by replacing an exact unique string. The edit is atomic and requires approval.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Exact current text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
                "expected_replacements": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["path", "old_text", "new_text"],
        },
    ),
    "move_path": (
        move_path,
        "Move or rename one file or folder without overwriting an existing destination. Requires approval.",
        {"type":"object","properties":{"source":{"type":"string"},"destination":{"type":"string"}},"required":["source","destination"]},
    ),
    "delete_path": (
        delete_path,
        "Delete one file or one empty folder. Never deletes a folder recursively. Requires explicit approval.",
        {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]},
    ),
    "list_scheduled_tasks": (
        list_scheduled_tasks,
        "List Nova's recurring scheduled tasks, including IDs, state, instructions, and next run time.",
        {"type":"object","properties":{}},
    ),
    "create_scheduled_task": (
        create_scheduled_task,
        "Create a recurring background task. Daily values use local HH:MM; interval values are whole minutes. Requires approval.",
        {
            "type":"object",
            "properties":{
                "name":{"type":"string"},
                "instruction":{"type":"string","description":"What Nova should do each time."},
                "schedule_type":{"type":"string","enum":["daily","interval"]},
                "schedule_value":{"type":"string","description":"HH:MM for daily or whole minutes for interval."},
            },
            "required":["name","instruction","schedule_type","schedule_value"],
        },
    ),
    "manage_scheduled_task": (
        manage_scheduled_task,
        "Pause, resume, or delete a scheduled task by ID. Requires approval.",
        {
            "type":"object",
            "properties":{
                "task_id":{"type":"integer"},
                "action":{"type":"string","enum":["pause","resume","delete"]},
            },
            "required":["task_id","action"],
        },
    ),
    "run_python": (
        run_python,
        "Execute Python code and return its output. Use this to actually run code you write "
        "(calculations, data processing, scripts). Requires the user's approval before it runs.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python source code to execute."}
            },
            "required": ["code"],
        },
    ),
    "run_command": (
        run_command,
        "Run a shell command and return its output (e.g. running a script, git, pip). "
        "Requires the user's approval before it runs.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run."},
                "directory": {"type": "string", "description": "Optional folder to run it in (defaults to Nova's workspace)."},
            },
            "required": ["command"],
        },
    ),
    "web_search": (
        web_search,
        "Search the web and return the top results (title, URL, snippet). Use for current "
        "events, news, prices, documentation, or anything you don't already know.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "count": {"type": "integer", "description": "How many results (1-10, default 5)."},
            },
            "required": ["query"],
        },
    ),
    "fetch_url": (
        fetch_url,
        "Fetch a web page and return its readable text. Use after web_search to read a "
        "specific public result. This read-only tool rejects localhost and private addresses; "
        "use open_webpage when the task is to navigate a browser.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http/https URL to read."}
            },
            "required": ["url"],
        },
    ),
    "get_quote": (
        get_quote,
        "Get the current price and key stats for a stock, ETF, index, or crypto ticker.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, e.g. AAPL, TSLA, SPY, BTC-USD."}
            },
            "required": ["symbol"],
        },
    ),
    "get_history": (
        get_history,
        "Get price history and the move over a period for a ticker.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol."},
                "period": {"type": "string", "description": "e.g. 5d, 1mo, 3mo, 1y, 5y (default 1mo)."},
                "interval": {"type": "string", "description": "e.g. 1d, 1h, 1wk (default 1d)."},
            },
            "required": ["symbol"],
        },
    ),
    "get_fundamentals": (
        get_fundamentals,
        "Get company fundamentals for a ticker: sector, market cap, P/E, EPS, 52-week range, summary.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker symbol."}
            },
            "required": ["symbol"],
        },
    ),
    "get_market_overview": (
        get_market_overview,
        "Get a snapshot of the major market indices (S&P 500, Nasdaq, Dow, VIX).",
        {"type": "object", "properties": {}},
    ),
    "open_application": (
        open_application, "Open an application or document. Requires approval.",
        {"type":"object","properties":{"path":{"type":"string"},"arguments":{"type":"string"}},"required":["path"]},
    ),
    "open_webpage": (
        open_webpage, "Navigate the default browser to an http/https page, including localhost and private pages. Use this for browser-control tasks, then inspect_screen before clicking. Requires computer-control approval.",
        {"type":"object","properties":{"url":{"type":"string","description":"The http/https address to open."}},"required":["url"]},
    ),
    "search_youtube": (
        search_youtube, "Open YouTube search results for the requested topic in the default browser. Requires computer-control approval.",
        {"type":"object","properties":{"query":{"type":"string","description":"What to search for on YouTube."}},"required":["query"]},
    ),
    "copy_file": (
        copy_file, "Copy a file to another folder or removable USB drive. Requires approval.",
        {"type":"object","properties":{"source":{"type":"string"},"destination":{"type":"string"}},"required":["source","destination"]},
    ),
    "type_text": (
        type_text, "Type text into the currently focused application. Requires approval.",
        {"type":"object","properties":{"text":{"type":"string"}},"required":["text"]},
    ),
    "click_screen": (
        click_screen, "Click an exact coordinate on the current screen. Requires approval and should follow screen inspection.",
        {"type":"object","properties":{"x":{"type":"integer"},"y":{"type":"integer"}},"required":["x","y"]},
    ),
    "move_pointer": (
        move_pointer, "Move the mouse pointer without clicking. Requires computer-control approval.",
        {"type":"object","properties":{"x":{"type":"integer"},"y":{"type":"integer"}},"required":["x","y"]},
    ),
    "scroll_screen": (
        scroll_screen, "Scroll the active window. Positive clicks scroll up; negative clicks scroll down. Requires computer-control approval.",
        {"type":"object","properties":{"clicks":{"type":"integer","minimum":-20,"maximum":20}},"required":["clicks"]},
    ),
    "press_key": (
        press_key, "Press a named key such as enter, escape, tab, down, or f5. Requires computer-control approval.",
        {"type":"object","properties":{"key":{"type":"string"},"presses":{"type":"integer","minimum":1,"maximum":20}},"required":["key"]},
    ),
    "hotkey": (
        hotkey, "Press a keyboard shortcut such as ctrl+s or alt+tab. Requires computer-control approval.",
        {"type":"object","properties":{"keys":{"type":"array","items":{"type":"string"},"minItems":2,"maxItems":4}},"required":["keys"]},
    ),
    "inspect_screen": (
        inspect_screen, "Capture and inspect a fresh screenshot of the desktop. Read-only; use after actions to verify the result.",
        {"type":"object","properties":{}},
    ),
    "media_engine_status": (
        media_engine_status,
        "Check Nova's private local image/video engine, GPU, model readiness, and output folder.",
        {"type": "object", "properties": {}},
    ),
    "generate_image": (
        generate_image,
        "Create an original image locally from a text prompt with Z-Image Turbo. Returns a displayable image and its saved path. Default 1024x1024 and 8 steps are recommended; use a fixed seed only when reproducibility matters.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed visual description including subject, setting, composition, lighting, and style."},
                "negative_prompt": {"type": "string", "description": "Optional elements or defects to avoid."},
                "width": {"type": "integer", "minimum": 512, "maximum": 1536, "default": 1024},
                "height": {"type": "integer", "minimum": 512, "maximum": 1536, "default": 1024},
                "steps": {"type": "integer", "minimum": 4, "maximum": 20, "default": 8},
                "seed": {"type": "integer", "description": "Use -1 for a random seed.", "default": -1},
            },
            "required": ["prompt"],
        },
    ),
    "generate_video": (
        generate_video,
        "Create a short original MP4 video locally from a text prompt with Wan 2.1. Returns a playable video and its saved path. Describe the subject, action, environment, lighting, and camera movement. The recommended quick default is 512x320, 33 frames, 8 fps.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed scene, motion, lighting, and camera-direction description."},
                "negative_prompt": {"type": "string", "description": "Optional elements or defects to avoid."},
                "width": {"type": "integer", "minimum": 320, "maximum": 832, "default": 512},
                "height": {"type": "integer", "minimum": 240, "maximum": 640, "default": 320},
                "frames": {"type": "integer", "minimum": 9, "maximum": 81, "default": 33},
                "fps": {"type": "integer", "minimum": 4, "maximum": 30, "default": 8},
                "steps": {"type": "integer", "minimum": 10, "maximum": 50, "default": 30},
                "seed": {"type": "integer", "description": "Use -1 for a random seed.", "default": -1},
            },
            "required": ["prompt"],
        },
    ),
}

# read-only network tools — auto-run like file reads, but gated by the web toggle
WEB_TOOLS = {"web_search", "fetch_url", "open_webpage", "search_youtube"}
MARKET_TOOLS = {"get_quote", "get_history", "get_fundamentals", "get_market_overview"}
NETWORK_TOOLS = WEB_TOOLS | MARKET_TOOLS
MEDIA_CREATE_TOOLS = {"generate_image", "generate_video"}

# tools that change the machine — app.py stops and asks the user before running these
CONTROL_TOOLS = {"open_application", "open_webpage", "search_youtube",
                 "type_text", "click_screen", "move_pointer",
                 "scroll_screen", "press_key", "hotkey"}
CONFIRM_TOOLS = {
    "write_file", "append_to_file", "create_directory", "replace_in_file",
    "move_path", "delete_path", "create_scheduled_task", "manage_scheduled_task",
    "run_python", "run_command", "copy_file",
} | CONTROL_TOOLS

# capability groups, so "safe mode" can keep each off until the user opts in
EXEC_TOOLS = {"run_python", "run_command"}
WRITE_TOOLS = CONFIRM_TOOLS - EXEC_TOOLS


def category(name):
    if name in CONTROL_TOOLS:
        return "control"
    if name in EXEC_TOOLS:
        return "exec"
    if name in WRITE_TOOLS:
        return "write"
    return "read"


# ---- danger scanner: flags risky patterns so the human doesn't rubber-stamp ----
# (regex, label, high?) — a "high" hit forces a type-to-confirm step in the UI
_DANGER_RULES = [
    (r"rm\s+-[rf]|rmdir\s+/s|\bdel\s+/[sfq]|\bformat\s|\bmkfs|shutil\.rmtree|os\.remove|os\.unlink|\.unlink\s*\(|Remove-Item\b[^\n]*-Recurse|rd\s+/s",
     "deletes files", True),
    (r"appdata|system32|\.ssh\b|\.aws\b|\bid_rsa\b|\.env\b|credentials|ntuser|\.gnupg",
     "touches sensitive locations", True),
    (r"\bpip\s+install|\bnpm\s+install|shutdown\b|\breg\s+delete|\breg\s+add|taskkill|\bsc\s+delete|netsh\b|Set-ItemProperty",
     "changes system settings", True),
    (r"requests\.|urllib|http\.client|httpx|socket\.|\bcurl\b|\bwget\b|Invoke-WebRequest|urlopen|smtplib|ftplib|webhook",
     "reaches the network", False),
    (r"subprocess|os\.system|Popen|\bpowershell\b|cmd\.exe|/bin/sh|\beval\s*\(|\bexec\s*\(|__import__|b64decode",
     "runs more code", False),
]


def scan_dangers(text):
    """Return {flags: [labels], require_type: bool} for a code/command string."""
    text = text or ""
    flags, high = [], False
    for pattern, label, is_high in _DANGER_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            flags.append(label)
            high = high or is_high
    return {"flags": flags, "require_type": high}


def describe(name, arguments):
    """Dry-run inspection for the confirmation dialog. Never touches disk to write.

    Returns {ok: False, error} if the target is invalid (so no point asking), or
    {ok: True, summary, overwrite, detail} describing what approval would trigger.
    """
    args = arguments if isinstance(arguments, dict) else {}

    # code execution: show exactly what will run
    if name == "run_python":
        code = (args.get("code") or "").strip()
        if not code:
            return {"ok": False, "error": "No code to run."}
        d = scan_dangers(code)
        return {"ok": True, "summary": "Run Python code", "exec": True, "detail": code,
                "dangers": d["flags"], "require_type": d["require_type"]}

    if name == "run_command":
        command = (args.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "No command given."}
        where = args.get("directory")
        summary = "Run command:  " + command
        if where:
            summary += f"   (in {where})"
        d = scan_dangers(command)
        return {"ok": True, "summary": summary, "exec": True, "detail": command,
                "dangers": d["flags"], "require_type": d["require_type"]}

    if name == "open_application":
        target = (args.get("path") or "").strip()
        if not target:
            return {"ok": False, "error": "No application or document given."}
        detail = target + ((" " + args.get("arguments")) if args.get("arguments") else "")
        return {"ok": True, "summary": f"Open {target}", "exec": True, "detail": detail,
                "dangers": [], "require_type": False}

    if name == "open_webpage":
        from urllib.parse import urlparse
        target = (args.get("url") or "").strip()
        if "://" not in target:
            target = "https://" + target
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"ok": False, "error": "A valid http/https web address is required."}
        return {"ok": True, "summary": f"Open {parsed.hostname} in the default browser",
                "exec": True, "detail": target, "dangers": [], "require_type": False}

    if name == "search_youtube":
        query = (args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "A YouTube search is required."}
        return {"ok": True, "summary": f"Search YouTube for “{query[:120]}”",
                "exec": True, "detail": query[:500], "dangers": [], "require_type": False}

    if name == "type_text":
        text = (args.get("text") or "")[:10000]
        if not text:
            return {"ok": False, "error": "No text given."}
        return {"ok": True, "summary": f"Type {len(text)} characters into the focused window",
                "exec": True, "detail": text[:1500], "dangers": [], "require_type": False}

    if name == "click_screen":
        try: x, y = int(args.get("x")), int(args.get("y"))
        except (TypeError, ValueError): return {"ok": False, "error": "Valid x and y coordinates are required."}
        return {"ok": True, "summary": f"Click screen coordinate ({x}, {y})", "exec": True,
                "detail": f"x={x}, y={y}", "dangers": [], "require_type": False}

    if name == "move_pointer":
        try: x, y = int(args.get("x")), int(args.get("y"))
        except (TypeError, ValueError): return {"ok": False, "error": "Valid x and y coordinates are required."}
        return {"ok": True, "summary": f"Move pointer to ({x}, {y})", "exec": True,
                "detail": f"x={x}, y={y}", "dangers": [], "require_type": False}

    if name == "scroll_screen":
        try: clicks = max(-20, min(20, int(args.get("clicks"))))
        except (TypeError, ValueError): return {"ok": False, "error": "A valid scroll amount is required."}
        return {"ok": True, "summary": f"Scroll the active window {clicks:+d}", "exec": True,
                "detail": str(clicks), "dangers": [], "require_type": False}

    if name == "press_key":
        key = str(args.get("key") or "").strip()
        if not key: return {"ok": False, "error": "A key is required."}
        return {"ok": True, "summary": f"Press {key}", "exec": True,
                "detail": key, "dangers": [], "require_type": False}

    if name == "hotkey":
        keys = args.get("keys")
        if not isinstance(keys, list) or not 2 <= len(keys) <= 4:
            return {"ok": False, "error": "A hotkey requires 2 to 4 keys."}
        label = "+".join(str(k) for k in keys)
        return {"ok": True, "summary": f"Press hotkey {label}", "exec": True,
                "detail": label, "dangers": [], "require_type": False}

    if name == "copy_file":
        source, destination = (args.get("source") or "").strip(), (args.get("destination") or "").strip()
        if not source or not destination:
            return {"ok": False, "error": "Source and destination are required."}
        try: source = _resolve(source, must="file")
        except ToolError as err: return {"ok": False, "error": str(err)}
        return {"ok": True, "summary": f"Copy {source} to {destination}", "overwrite": os.path.exists(destination),
                "detail": f"{source}\n→ {destination}", "dangers": [], "require_type": False}

    if name == "replace_in_file":
        try:
            real = _resolve(args.get("path", ""), must="file")
            old_text = args.get("old_text")
            new_text = args.get("new_text")
            expected = int(args.get("expected_replacements", 1))
            if not isinstance(old_text, str) or not old_text:
                raise ToolError("old_text must be a non-empty exact string.")
            if not isinstance(new_text, str):
                raise ToolError("new_text must be text.")
            with open(real, "r", encoding="utf-8", errors="strict") as handle:
                actual = handle.read().count(old_text)
            if actual != expected:
                raise ToolError(f"Expected {expected} exact match(es), found {actual}.")
        except (ToolError, OSError, UnicodeError, TypeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
        detail = f"Replace:\n{old_text[:700]}\n\nWith:\n{new_text[:700]}"
        return {"ok": True, "summary": f"Edit {real} ({actual} replacement(s))",
                "overwrite": True, "detail": detail, "dangers": [], "require_type": False}

    if name == "move_path":
        try:
            source = _resolve(args.get("source", ""))
            destination = _resolve(args.get("destination", ""))
        except ToolError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "summary": f"Move {source} to {destination}",
                "overwrite": os.path.exists(destination), "detail": f"{source}\n→ {destination}",
                "dangers": [], "require_type": False}

    if name == "delete_path":
        try:
            real = _resolve(args.get("path", ""))
        except ToolError as error:
            return {"ok": False, "error": str(error)}
        if not os.path.exists(real):
            return {"ok": False, "error": f"Path does not exist: {real}"}
        return {"ok": True, "summary": f"Delete {real}", "overwrite": True,
                "detail": real, "dangers": ["deletes files"], "require_type": True}

    if name == "create_scheduled_task":
        title = str(args.get("name") or "").strip()
        instruction = str(args.get("instruction") or "").strip()
        schedule_type = str(args.get("schedule_type") or "").strip()
        schedule_value = str(args.get("schedule_value") or "").strip()
        try:
            next_run = _next_scheduled_run(schedule_type, schedule_value)
        except ToolError as error:
            return {"ok": False, "error": str(error)}
        if not title or not instruction:
            return {"ok": False, "error": "A task name and instruction are required."}
        when = datetime.fromtimestamp(next_run).strftime("%A, %B %d at %I:%M %p")
        return {"ok": True, "summary": f"Schedule '{title}' — next {when}",
                "overwrite": False, "detail": instruction[:1500], "dangers": [],
                "require_type": False}

    if name == "manage_scheduled_task":
        try:
            task_id = int(args.get("task_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "task_id must be an integer."}
        action = str(args.get("action") or "").strip().lower()
        if action not in {"pause", "resume", "delete"}:
            return {"ok": False, "error": "action must be pause, resume, or delete."}
        destructive = action == "delete"
        return {"ok": True, "summary": f"{action.title()} scheduled task #{task_id}",
                "overwrite": destructive, "detail": f"Task #{task_id}",
                "dangers": ["deletes a scheduled task"] if destructive else [],
                "require_type": destructive}

    try:
        real = _resolve(args.get("path", ""))
    except ToolError as err:
        return {"ok": False, "error": str(err)}

    if name == "create_directory":
        exists = os.path.isdir(real)
        return {
            "ok": True,
            "summary": f"Create folder  {real}",
            "overwrite": False,
            "detail": "(this folder already exists — nothing would change)" if exists else "",
        }

    content = args.get("content", "") or ""
    if len(content.encode("utf-8", "ignore")) > MAX_WRITE_BYTES:
        return {"ok": False, "error": "Content is too large to write (limit 1 MB)."}

    if name == "append_to_file":
        if not os.path.isfile(real):
            return {"ok": False, "error": f"File doesn't exist (use write_file to create it): {real}"}
        summary = f"Append {len(content)} chars to  {real}"
        overwrite = False
    else:  # write_file
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            return {"ok": False, "error": f"Parent folder doesn't exist: {parent}"}
        if os.path.isdir(real):
            return {"ok": False, "error": f"That path is a folder, not a file: {real}"}
        exists = os.path.isfile(real)
        summary = (f"Overwrite  {real}" if exists else f"Create  {real}") + f"  ({len(content)} chars)"
        overwrite = exists

    preview = content if len(content) <= 1500 else content[:1500] + "\n… [truncated]"
    return {"ok": True, "summary": summary, "overwrite": overwrite, "detail": preview}


def schemas():
    """Tool definitions to send to Ollama."""
    return [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}
        for name, (_, desc, params) in _SPEC.items()
    ]


def mcp_definitions():
    """Return MCP tool definitions backed by the same registry as Ollama.

    Every call carries a short-lived Nova capability token. The MCP server
    validates it against the current turn policy before invoking a handler, so
    GPT/Codex cannot bypass Nova's per-turn capability switches.
    """
    destructive = {"delete_path", "manage_scheduled_task"}
    definitions = []
    for name, (_, desc, params) in _SPEC.items():
        schema = json.loads(json.dumps(params))
        schema.setdefault("properties", {})["_nova_token"] = {
            "type": "string",
            "description": "Short-lived capability token supplied in Nova's turn instructions.",
        }
        schema.setdefault("required", []).append("_nova_token")
        read_only = name not in CONFIRM_TOOLS and name not in MEDIA_CREATE_TOOLS
        definitions.append({
            "name": name,
            "description": desc,
            "inputSchema": schema,
            "annotations": {
                "title": name.replace("_", " ").title(),
                "readOnlyHint": read_only,
                "destructiveHint": name in destructive,
                "idempotentHint": read_only,
                "openWorldHint": name in NETWORK_TOOLS or name in CONTROL_TOOLS,
            },
        })
    return definitions


def schemas_for(write=True, execute=True, web=True, control=False, vision=False):
    """Only the tools that are actually enabled — smaller prompt = faster first token."""
    out = []
    for name, (_, desc, params) in _SPEC.items():
        cat = category(name)
        if cat == "write" and not write:
            continue
        if cat == "exec" and not execute:
            continue
        if cat == "control" and not control:
            continue
        if name == "inspect_screen" and not vision:
            continue
        if name in NETWORK_TOOLS and not web:
            continue
        out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": params}})
    return out


def run(name, arguments):
    """Execute a tool by name. Never raises — errors come back as {ok: False}."""
    spec = _SPEC.get(name)
    if not spec:
        return {"ok": False, "text": f"Unknown tool: {name}", "preview": "unknown tool"}

    handler = spec[0]
    args = arguments if isinstance(arguments, dict) else {}

    try:
        return handler(**args)
    except ToolError as err:
        return {"ok": False, "text": f"Error: {err}", "preview": str(err)[:60]}
    except TypeError as err:
        return {"ok": False, "text": f"Bad arguments for {name}: {err}", "preview": "bad arguments"}
    except Exception as err:  # noqa: BLE001 - surface anything else as a clean tool error
        return {"ok": False, "text": f"{name} failed: {err}", "preview": str(err)[:60]}
