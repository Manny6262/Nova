"""Persistent Codex App Server client for Nova."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import secrets
import tempfile
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_POLICY_PATH = os.path.join(APP_DIR, ".nova_mcp_policy.json")
MCP_REQUEST_DIR = os.path.join(APP_DIR, ".nova_mcp_requests")
_NATIVE_CODEX = os.path.join(
    os.environ.get("APPDATA", ""), "npm", "node_modules", "@openai", "codex",
    "node_modules", "@openai", "codex-win32-x64", "vendor",
    "x86_64-pc-windows-msvc", "bin", "codex.exe",
)


class CodexError(RuntimeError):
    pass


def executable() -> str | None:
    if os.path.isfile(_NATIVE_CODEX):
        return _NATIVE_CODEX
    return shutil.which("codex.exe") or shutil.which("codex")


def available() -> bool:
    return executable() is not None


class AppServerClient:
    def __init__(self):
        self.proc = None
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_id = 1
        self._responses: dict[int, queue.Queue] = {}
        self._turns: dict[str, queue.Queue] = {}
        self._voice_threads = {}
        self._voice_lock = threading.Lock()
        # The Nova MCP policy is intentionally one-turn-at-a-time. Serializing
        # cloud turns prevents one conversation from inheriting another turn's
        # capability token or permission switches.
        self._turn_lock = threading.Lock()

    def _send(self, message: dict):
        payload = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
                raise CodexError("Codex App Server is not running")
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()

    def _new_id(self) -> int:
        with self._request_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def start(self):
        with self._lifecycle_lock:
            if self.proc and self.proc.poll() is None:
                return
            exe = executable()
            if not exe:
                raise CodexError("Codex CLI is not installed")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            # Bind Nova's private MCP server directly to this App Server process.
            # A project-scoped .codex/config.toml disappears when User selects a
            # different working folder, while these process overrides follow every
            # Nova workspace without modifying the user's global Codex settings.
            mcp_overrides = [
                "-c", "mcp_servers.nova_tools.command=" + json.dumps(sys.executable),
                "-c", "mcp_servers.nova_tools.args=[" + json.dumps(
                    os.path.join(APP_DIR, "nova_mcp_server.py")) + "]",
                "-c", "mcp_servers.nova_tools.cwd=" + json.dumps(APP_DIR),
                "-c", "mcp_servers.nova_tools.required=true",
                "-c", "mcp_servers.nova_tools.startup_timeout_sec=20",
                "-c", "mcp_servers.nova_tools.tool_timeout_sec=3600",
                # Nova itself handles prompts using the same UI as local tools.
                "-c", 'mcp_servers.nova_tools.default_tools_approval_mode="approve"',
            ]
            self.proc = subprocess.Popen(
                [exe, "app-server", "--disable", "code_mode", "--stdio", *mcp_overrides], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", bufsize=1, cwd=APP_DIR,
                creationflags=flags,
            )
            with self._voice_lock:
                self._voice_threads.clear()
            threading.Thread(target=self._read_loop, daemon=True).start()
            result = self.request("initialize", {
                "clientInfo": {"name": "nova_personal_assistant", "title": "Nova", "version": "2.0.0"}
            }, timeout=15, ensure_started=False)
            if not isinstance(result, dict):
                raise CodexError("Codex initialization failed")
            self._send({"method": "initialized", "params": {}})

    def stop(self):
        with self._lifecycle_lock:
            proc, self.proc = self.proc, None
            if proc and proc.poll() is None:
                proc.kill()
            self._fail_waiters("Codex App Server stopped")
            with self._voice_lock:
                self._voice_threads.clear()

    def _fail_waiters(self, message: str):
        for waiter in list(self._responses.values()):
            waiter.put({"error": {"message": message}})
        for waiter in list(self._turns.values()):
            waiter.put({"method": "server/disconnected", "params": {"error": message}})

    def _read_loop(self):
        try:
            while self.proc and self.proc.stdout:
                line = self.proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_id = message.get("id")
                if msg_id is not None and "method" not in message:
                    waiter = self._responses.get(msg_id)
                    if waiter:
                        waiter.put(message)
                    continue
                params = message.get("params") or {}
                thread_id = params.get("threadId")
                if thread_id and thread_id in self._turns:
                    self._turns[thread_id].put(message)
        finally:
            self._fail_waiters("Codex App Server disconnected")

    def request(self, method: str, params: dict | None = None, timeout: int = 20,
                ensure_started: bool = True) -> dict:
        if ensure_started:
            self.start()
        request_id = self._new_id()
        waiter: queue.Queue = queue.Queue(maxsize=1)
        self._responses[request_id] = waiter
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty:
                raise CodexError(f"Codex timed out waiting for {method}")
            if response.get("error"):
                error = response["error"]
                raise CodexError(error.get("message", str(error)))
            return response.get("result") or {}
        finally:
            self._responses.pop(request_id, None)

    def respond(self, request_id: int, result: dict):
        self._send({"id": request_id, "result": result})

    def interrupt(self, thread_id: str, turn_id: str):
        try:
            self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
        except Exception:
            pass

    def run_turn(self, prompt: str, *, cwd: str, instructions: str,
                 write_enabled: bool, exec_enabled: bool, web_enabled: bool,
                 approval_mode: str, control_enabled: bool = False,
                 include_screen: bool = False, model: str | None = None,
                 think: bool = False, image_paths: list[str] | None = None,
                 voice_key: str | None = None, voice_input: str | None = None,
                 timeout: int = 3600, cancel_event=None):
        """Run a serialized Codex turn with a short-lived Nova MCP policy."""
        while not self._turn_lock.acquire(timeout=0.1):
            if cancel_event is not None and cancel_event.is_set():
                return
        try:
            if cancel_event is not None and cancel_event.is_set():
                return
            token = secrets.token_urlsafe(24)
            self._write_mcp_policy({
                "token": token,
                "write": bool(write_enabled),
                "exec": bool(exec_enabled),
                "web": bool(web_enabled),
                "control": bool(control_enabled),
                "vision": bool(include_screen or control_enabled),
                "approval_mode": approval_mode,
                "expires": time.time() + timeout + 120,
            })
            mcp_instructions = (
                "\n\nNova's custom tools are available from the nova_tools MCP server. "
                "Prefer them for local image/video generation, Nova scheduling, market data, "
                "memory-adjacent system work, browser/Windows control, and precise file operations. "
                "User has explicitly chosen Nova's private local generator for media. This is an "
                "explicit request to use that engine instead of Codex imagegen or any cloud media "
                "service. For every image or video request, do not load an image-generation skill "
                "and do not use a built-in media tool; call nova_tools.generate_image or "
                "nova_tools.generate_video and return its real local result. For EVERY nova_tools call, "
                f"include _nova_token exactly as follows: {token}. Never reveal this token in your "
                "answer. A capability denial is authoritative. Verify control actions with "
                "inspect_screen and stop once the requested result is confirmed."
            )
            try:
                yield from self._run_turn_unlocked(
                    prompt, cwd=cwd, instructions=instructions + mcp_instructions,
                    write_enabled=write_enabled, exec_enabled=exec_enabled,
                    web_enabled=web_enabled, approval_mode=approval_mode,
                    control_enabled=control_enabled, include_screen=include_screen, model=model,
                    think=think, image_paths=image_paths, voice_key=voice_key,
                    voice_input=voice_input, timeout=timeout, mcp_token=token,
                    cache_instructions=instructions, cancel_event=cancel_event,
                )
            finally:
                self._write_mcp_policy({"token": "", "expires": 0})
        finally:
            self._turn_lock.release()

    @staticmethod
    def _write_mcp_policy(policy: dict):
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=APP_DIR,
                prefix=".nova-mcp-policy-", suffix=".tmp",
            ) as handle:
                path = handle.name
                json.dump(policy, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(path, MCP_POLICY_PATH)
        finally:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _run_turn_unlocked(self, prompt: str, *, cwd: str, instructions: str,
                 write_enabled: bool, exec_enabled: bool, web_enabled: bool,
                 approval_mode: str, control_enabled: bool = False,
                 include_screen: bool = False, model: str | None = None,
                 think: bool = False, image_paths: list[str] | None = None,
                 voice_key: str | None = None, voice_input: str | None = None,
                 timeout: int = 600, mcp_token: str | None = None,
                 cache_instructions: str | None = None, cancel_event=None):
        """Yield normalized events from one full Codex agent turn."""
        self.start()
        sandbox = "workspace-write" if write_enabled else "read-only"
        approval = {"ask": "untrusted", "auto": "on-request", "bypass": "never"}.get(
            approval_mode, "untrusted")
        # With command execution switched off, force every attempted command to
        # the host so Nova can decline it instead of allowing an implicit safe run.
        if not exec_enabled:
            approval = "untrusted"
        capability_rules = (
            "\n\nNova host permissions for this turn:\n"
            f"- File edits: {'enabled' if write_enabled else 'disabled'}\n"
            f"- Commands/code execution: {'enabled' if exec_enabled else 'disabled'}\n"
            f"- Network/web access: {'enabled' if web_enabled else 'disabled'}\n"
            "Honor disabled capabilities. Work only inside the selected working folder. "
            "Use tools and verify results when the user asks you to perform work."
        )
        thread_params = {
            "cwd": cwd, "approvalPolicy": approval, "approvalsReviewer": "user",
            "sandbox": sandbox, "developerInstructions": instructions + capability_rules,
            "ephemeral": True,
        }
        if model:
            thread_params["model"] = model
        # Reuse only in-memory voice threads with identical permissions/context.
        # The random key changes on each microphone session; never persist it.
        cache_key = (voice_key, cwd, model, cache_instructions or instructions, write_enabled, exec_enabled,
                     web_enabled, approval_mode) if voice_key else None
        reused = False
        with self._voice_lock:
            now = time.monotonic()
            self._voice_threads = {k:v for k,v in self._voice_threads.items()
                                   if v['expires'] > now and v['count'] < 16}
            entry = self._voice_threads.get(cache_key)
            if entry and not entry['busy']:
                entry['busy'] = True
                reused = True
                thread_id = entry['id']
            else:
                if entry:
                    cache_key = None  # overlapping turn gets an independent thread
                thread_id = None
        if not thread_id:
            thread = self.request("thread/start", thread_params, timeout=30).get("thread") or {}
            thread_id = thread.get("id")
        if not thread_id:
            raise CodexError("Codex did not create a thread")

        events: queue.Queue = queue.Queue()
        self._turns[thread_id] = events
        turn_id = None
        completed = False
        delivered_mcp_approvals = set()
        try:
            policy = ({"type": "workspaceWrite", "writableRoots": [cwd],
                       "networkAccess": bool(web_enabled)} if write_enabled else
                      {"type": "readOnly", "networkAccess": bool(web_enabled)})
            turn_input = [{"type": "text", "text": voice_input if reused and voice_input is not None else prompt}]
            for path in image_paths or []:
                if os.path.isfile(path):
                    turn_input.append({"type": "localImage", "path": path, "detail": "high"})
            turn_params = {
                "threadId": thread_id, "input": turn_input,
                "approvalPolicy": approval, "sandboxPolicy": policy, "cwd": cwd,
                "effort": "high" if think else "low",
            }
            started = self.request("turn/start", turn_params, timeout=30).get("turn") or {}
            turn_id = started.get("id")
            if not turn_id:
                raise CodexError("Codex did not start the turn")
            yield {"type": "turn_started", "thread_id": thread_id, "turn_id": turn_id}

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return
                try:
                    message = events.get(timeout=0.5)
                except queue.Empty:
                    approval = self._next_mcp_approval(mcp_token, delivered_mcp_approvals)
                    if approval:
                        delivered_mcp_approvals.add(approval["id"])
                        yield {"type": "mcp_approval", "request": approval}
                    if not self.proc or self.proc.poll() is not None:
                        raise CodexError("Codex App Server stopped")
                    continue
                method = message.get("method", "")
                params = message.get("params") or {}
                if method == "item/agentMessage/delta":
                    if params.get("delta"):
                        yield {"type": "token", "text": params["delta"]}
                elif method == "item/reasoning/summaryTextDelta":
                    if params.get("delta"):
                        yield {"type": "thought", "text": params["delta"]}
                elif method == "item/started":
                    item = params.get("item") or {}
                    if item.get("type") in ("commandExecution", "fileChange", "mcpToolCall", "webSearch"):
                        yield {"type": "tool_start", "item": item}
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") in ("commandExecution", "fileChange", "mcpToolCall", "webSearch"):
                        yield {"type": "tool_result", "item": item}
                elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                    yield {"type": "approval", "rpc_id": message.get("id"),
                           "method": method, "params": params}
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("status") == "failed":
                        error = turn.get("error") or {}
                        raise CodexError(error.get("message", str(error) or "Codex turn failed"))
                    completed = True
                    yield {"type": "done", "usage": turn.get("usage") or {}}
                    return
                elif method == "server/disconnected":
                    raise CodexError("Codex App Server disconnected")
            raise CodexError("Codex turn timed out")
        finally:
            if turn_id and not completed:
                self.interrupt(thread_id, turn_id)
            self._turns.pop(thread_id, None)
            if cache_key:
                with self._voice_lock:
                    if completed:
                        if len(self._voice_threads) >= 8 and cache_key not in self._voice_threads:
                            self._voice_threads.pop(next(iter(self._voice_threads)))
                        previous = self._voice_threads.get(cache_key, {})
                        self._voice_threads[cache_key] = {'id':thread_id,'busy':False,
                            'expires':time.monotonic()+900,'count':previous.get('count',0)+1}
                    else:
                        self._voice_threads.pop(cache_key, None)

    @staticmethod
    def _next_mcp_approval(token, delivered):
        if not token or not os.path.isdir(MCP_REQUEST_DIR):
            return None
        try:
            names = sorted(name for name in os.listdir(MCP_REQUEST_DIR)
                           if name.endswith(".json") and not name.endswith(".response.json"))
        except OSError:
            return None
        for name in names:
            path = os.path.join(MCP_REQUEST_DIR, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    request = json.load(handle)
                if request.get("token") == token and request.get("id") not in delivered:
                    request.pop("token", None)
                    return request
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def respond_mcp_approval(request_id: str, approved: bool):
        os.makedirs(MCP_REQUEST_DIR, exist_ok=True)
        path = os.path.join(MCP_REQUEST_DIR, request_id + ".response.json")
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump({"approved": bool(approved)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)


CLIENT = AppServerClient()


def account_status() -> dict:
    try:
        account_result = CLIENT.request("account/read", {"refreshToken": False}, timeout=10)
        limit_result = CLIENT.request("account/rateLimits/read", {}, timeout=10)
        account = account_result.get("account") or {}
        return {"available": True, "authenticated": account.get("type") == "chatgpt",
                "plan": account.get("planType"), "email": account.get("email"),
                "limits": limit_result, "limit_error": None}
    except Exception as exc:
        return {"available": available(), "authenticated": False, "error": str(exc)}
