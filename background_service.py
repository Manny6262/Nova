"""Windowless singleton supervisor for Nova Native's private engine only.

Never stops a foreign listener or restarts an unverified process. Existing
iMessage remains independent. A stopped PC cannot run local inference.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import requests

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:5100'
STATE = ROOT / 'background-service-state.json'
CONFIG = ROOT / 'background-service.json'
LOG = ROOT / 'background-service.log'


def write_state(status, detail, pid=None):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps({'status':status,'detail':detail,'pid':pid,'supervisor_pid':os.getpid(),'updated_at':time.time()}),encoding='utf-8')
    temp.replace(STATE)


def identity():
    try:
        reply = requests.get(URL+'/native-host',timeout=2)
        if not reply.ok:
            return 'foreign'
        item = reply.json()
        expected = os.path.normcase(str(ROOT / 'engine'))
        return 'ours' if item.get('native') is True and os.path.normcase(os.path.realpath(item.get('engine_path',''))) == expected else 'foreign'
    except requests.ConnectionError:
        return 'offline'
    except Exception:
        return 'foreign'  # Do not spawn into an occupied or ambiguous port.


def enabled():
    try:
        return json.loads(CONFIG.read_text(encoding='utf-8')).get('enabled',True) is True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def main():
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    token = hashlib.sha256(os.path.normcase(str(ROOT)).encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None,False,'Local\\NovaNative.Supervisor.'+token)
    if not handle:
        raise OSError('Could not create the Native supervisor lock.')
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return
    child = None
    child_started = 0
    failures = 0
    try:
        while True:
            if not enabled():
                write_state('disabled','Automatic engine recovery is off. Existing engine and iMessage are left available.')
                time.sleep(5)
                continue
            current = identity()
            if current == 'ours':
                write_state('running','Private engine available; approved work may continue while this PC is awake.',child.pid if child and child.poll() is None else None)
                failures = 0
            elif current == 'foreign':
                write_state('blocked','Port 5100 could not be verified as this installation. No process was stopped.')
            elif child and child.poll() is None:
                if time.monotonic()-child_started > 90:
                    # This is only the process we started, never a listener
                    # discovered on the port. Bound a stalled engine startup.
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    write_state('retrying','Private engine startup stalled; retrying after a delay.')
                else:
                    write_state('starting','Waiting for the private engine to finish starting.',child.pid)
            else:
                if failures:
                    time.sleep(min(30, 2 ** min(failures,5)))
                if LOG.exists() and LOG.stat().st_size > 5 * 1024**2:
                    LOG.replace(ROOT / 'background-service.previous.log')
                with LOG.open('a',encoding='utf-8') as log:
                    log.write('\nStarting private engine '+time.strftime('%Y-%m-%d %H:%M:%S')+'\n')
                    log.flush()
                    child = subprocess.Popen([sys.executable,str(ROOT/'backend_host.py'),'--port','5100'],
                        cwd=ROOT,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                    child_started = time.monotonic()
                failures += 1
                write_state('starting','Starting private engine after launch or interruption.',child.pid)
            time.sleep(5)
    finally:
        kernel.CloseHandle(handle)


if __name__ == '__main__':
    main()
