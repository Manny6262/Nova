"""Runs Nova as a native desktop window instead of a browser tab.

Flask serves the app on localhost as usual; pywebview just points its own
window at that address using the OS web renderer (Edge WebView2 on Windows),
so there's no bundled Chromium and no browser chrome around the UI.
"""

import os
import threading
import ctypes
import sys
import traceback
import time
import socket
from ctypes import wintypes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STARTUP_LOG = os.path.join(APP_DIR, "desktop-startup.log")


def log_startup(message):
    """Persist desktop-host failures because pythonw has no visible console."""
    try:
        from datetime import datetime
        with open(STARTUP_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass


def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    log_startup(
        "fatal startup error:\n"
        + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    )
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


# Install this before importing the desktop and server dependencies so failures
# there are visible even though the launcher intentionally uses pythonw.exe.
if __name__ == "__main__":
    sys.excepthook = log_unhandled_exception
    log_startup("launch requested")

# Let the embedded Edge WebView2 use the microphone and play a reply after the
# asynchronous transcription/model round. Without the autoplay policy WebView2
# can accept the microphone, render Nova's text, then reject Audio.play() because
# the original Voice button gesture is no longer considered active.
_webview_args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").split()
for _required_arg in (
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
):
    if _required_arg not in _webview_args:
        _webview_args.append(_required_arg)
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(_webview_args)

PORT = int(os.environ.get("PORT", 5000))
_instance_mutex = None


def claim_single_instance():
    """Prevent two Nova desktops from fighting over the same local server/port."""
    global _instance_mutex
    # Preserve the Win32 last-error value from CreateMutexW. Accessing it via a
    # later windll call is racy and can misclassify the first launch as a second.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    _instance_mutex = kernel.CreateMutexW(None, False, "Local\\NovaDesktopSingleton")
    create_error = ctypes.get_last_error()
    if not _instance_mutex:
        raise ctypes.WinError()
    return create_error != 183


def show_existing_window():
    """Make a second desktop-icon click restore the existing Nova window."""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        hwnd = None
        # A second click can arrive while the first host is still loading.
        for _ in range(80):
            hwnd = user32.FindWindowW(None, "Nova")
            if hwnd:
                break
            time.sleep(.1)
        if not hwnd:
            log_startup("existing Nova window not found")
            return False
        # ShowWindowAsync alone can be undone by WinForms while it is still
        # applying the process's inherited minimized state. Restore once on this
        # thread as well, then ask Windows to foreground the finished window.
        user32.ShowWindow(hwnd, 9)
        user32.ShowWindowAsync(hwnd, 9)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < 700 or rect.bottom - rect.top < 600:
            x, y = centered(1160, 880)
            user32.SetWindowPos(hwnd, 0, x, y, 1160, 880, 0x0040)
        user32.SetForegroundWindow(hwnd)
        log_startup("window restore requested")
        return True
    except Exception:
        return False


class DesktopApi:
    def choose_folder(self):
        """Open the native Windows folder picker for Nova's working directory."""
        try:
            import webview
            window = webview.windows[0]
            dialog_type = getattr(webview, "FOLDER_DIALOG", None)
            if dialog_type is None and hasattr(webview, "FileDialog"):
                dialog_type = webview.FileDialog.FOLDER
            selected = window.create_file_dialog(dialog_type)
            return selected[0] if selected else None
        except Exception:
            return None


def run_flask():
    try:
        from app import app, FastRequestHandler
        app.run(port=PORT, debug=False, use_reloader=False, threaded=True,
                request_handler=FastRequestHandler)
    except BaseException:
        log_startup("Flask server thread failed:\n" + traceback.format_exc())
        raise


def wait_for_server(timeout=20.0):
    """Do not display a desktop shell whose local application server is dead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=.25):
                return True
        except OSError:
            time.sleep(.1)
    return False


def centered(width, height):
    """Top-left corner that centers the window on the primary monitor, so Nova
    never opens off-screen (a stale saved position used to push it out of view)."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        return max(0, (sw - width) // 2), max(0, (sh - height) // 2)
    except Exception:
        return 100, 60


def main():
    if not claim_single_instance():
        show_existing_window()
        return
    # The singleton check must precede app imports: importing app starts workers.
    # A repeated desktop click should only restore the existing window.
    import webview
    import app as app_module
    server_thread = threading.Thread(target=run_flask, daemon=True)
    server_thread.start()
    if not wait_for_server():
        raise RuntimeError(f"Nova's local server did not start on port {PORT}")
    threading.Thread(target=app_module.warm_voice_models, daemon=True).start()

    w, h = 1160, 880
    x, y = centered(w, h)
    start_in_voice = "--voice" in sys.argv
    window = webview.create_window(
        "Nova",
        f"http://127.0.0.1:{PORT}/?voice=1" if start_in_voice else f"http://127.0.0.1:{PORT}",
        width=w,
        height=h,
        x=x,
        y=y,
        min_size=(700, 600),
        background_color="#04070f",
        js_api=DesktopApi(),
    )
    # Let the dashboard's power button close Edge WebView2 before Python exits.
    # Abruptly killing the parent can leave a stale msedge breakpoint dialog.
    app_module.SHUTDOWN_CALLBACK = window.destroy

    def reveal_desktop():
        # pywebview now owns the native window, so its restore call also updates
        # WinForms' internal state. This prevents it from re-minimizing a window
        # that was restored only through Win32 during startup.
        window.restore()
        window.show()
        show_existing_window()

    try:
        webview.start(func=reveal_desktop)
    finally:
        # pywebview can finish after its window closes while a helper thread from
        # Flask/Codex keeps Python alive. That headless process still owns both the
        # port and Nova's single-instance mutex, making later desktop clicks no-op.
        # Once the only desktop window is gone, the whole desktop host must end.
        os._exit(0)


if __name__ == "__main__":
    main()
