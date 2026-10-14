"""Native service settings and optional local Windows inbox notifications."""
from flask import Blueprint, jsonify, request
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing

bp = Blueprint('native_background',__name__)
ROOT = Path(os.environ.get('NOVA_DATA_DIR',Path(__file__).resolve().parent.parent))
DB_PATH = str(Path(__file__).with_name('nova.db'))
_started = False


def settings():
    result = {'enabled':True,'notifications':False,'launch_at_login':False,'notify_after_id':0}
    try:
        saved = json.loads((ROOT/'background-service.json').read_text(encoding='utf-8'))
        result.update({k:v for k,v in saved.items() if k in ('enabled','notifications','launch_at_login') and type(v) is bool})
        if type(saved.get('notify_after_id')) is int and saved['notify_after_id'] >= 0:
            result['notify_after_id'] = saved['notify_after_id']
    except (OSError,ValueError,AttributeError):
        pass
    return result


@bp.route('/native-background',methods=['GET','POST'])
def config():
    saved = settings()
    if request.method == 'POST':
        body = request.get_json(silent=True)
        if not isinstance(body,dict) or not body or set(body)-{'enabled','notifications','launch_at_login'} or any(type(v) is not bool for v in body.values()):
            return jsonify(error='Use boolean background-service settings.'),400
        if body.get('notifications') is True and not saved['notifications']:
            with closing(sqlite3.connect(DB_PATH)) as c, c:
                saved['notify_after_id'] = c.execute('SELECT coalesce(max(id),0) FROM inbox').fetchone()[0]
        if 'launch_at_login' in body:
            if os.name != 'nt':
                return jsonify(error='Windows startup is unavailable on this computer.'),400
            import winreg
            service = ROOT/'background_service.py'
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            if body['launch_at_login'] and (not service.is_file() or not pythonw.is_file()):
                return jsonify(error='The private service or Python runtime is missing.'),409
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
                if body['launch_at_login']:
                    winreg.SetValueEx(key,'NovaNativeBackground',0,winreg.REG_SZ,subprocess.list2cmdline([str(pythonw),str(service)]))
                else:
                    try:
                        winreg.DeleteValue(key,'NovaNativeBackground')
                    except FileNotFoundError:
                        pass
        saved.update(body)
        temporary = ROOT/'background-service.config.tmp'
        temporary.write_text(json.dumps(saved),encoding='utf-8')
        temporary.replace(ROOT/'background-service.json')
        if body.get('enabled') is True and (ROOT/'background_service.py').is_file():
            subprocess.Popen([sys.executable,str(ROOT/'background_service.py')],cwd=ROOT,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    state = {}
    try:
        state = json.loads((ROOT/'background-service-state.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        pass
    last_notice = None
    try:
        with closing(sqlite3.connect(DB_PATH,timeout=15)) as c:
            row = c.execute('SELECT status,detail FROM notification_receipts ORDER BY attempted_at DESC LIMIT 1').fetchone()
            if row:
                last_notice = {'status':row[0],'detail':row[1]}
    except sqlite3.OperationalError:
        pass
    return jsonify(**saved,service=state,last_notification=last_notice)


def init():
    global _started
    with closing(sqlite3.connect(DB_PATH,timeout=15)) as c, c:
        c.execute('CREATE TABLE IF NOT EXISTS notification_receipts(inbox_id INTEGER PRIMARY KEY,status TEXT NOT NULL,detail TEXT,attempted_at REAL NOT NULL)')
    if not _started and os.environ.get('NOVA_DATA_DIR'):
        _started = True
        threading.Thread(target=_loop,name='nova-native-notifications',daemon=True).start()


def send_toast():
    # Fixed, privacy-preserving text. Never interpolate model output or paths
    # into a shell script, and never use this path for outbound messages.
    script = r'''
    $ErrorActionPreference = 'Stop'
    New-Item -Path 'HKCU:\Software\Classes\AppUserModelId\Nova.Native' -Force | Out-Null
    New-ItemProperty -Path 'HKCU:\Software\Classes\AppUserModelId\Nova.Native' -Name DisplayName -Value 'Nova Native' -PropertyType String -Force | Out-Null
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>Nova has an update for you</text><text>Open Nova Native → Tasks to review results or a decision.</text></binding></visual></toast>')
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Nova.Native').Show($toast)
    '''
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
        capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-500:])


def deliver():
    if not settings()['notifications'] or os.name != 'nt':
        return
    with closing(sqlite3.connect(DB_PATH,timeout=15)) as c, c:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute('SELECT id FROM inbox WHERE read_at IS NULL AND id>? AND id NOT IN (SELECT inbox_id FROM notification_receipts) ORDER BY id LIMIT 20',
                         (settings()['notify_after_id'],)).fetchall()
        if not rows:
            return
        # At-most-one submission per notice. OS acceptance does not prove the
        # user saw it. The durable inbox remains authoritative after a crash.
        c.executemany("INSERT INTO notification_receipts(inbox_id,status,attempted_at) VALUES(?,'submitted',?)",[(r[0],time.time()) for r in rows])
    status, detail = 'submitted','Requested Windows notification; delivery depends on Windows notification settings.'
    try:
        send_toast()
    except Exception as exc:
        status,detail = 'failed',str(exc)[:500]
    with closing(sqlite3.connect(DB_PATH,timeout=15)) as c, c:
        c.executemany('UPDATE notification_receipts SET status=?,detail=? WHERE inbox_id=?',[(status,detail,r[0]) for r in rows])


def _loop():
    while True:
        try:
            deliver()
        except Exception:
            pass
        time.sleep(10)
