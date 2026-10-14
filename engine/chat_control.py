"""Cancellation for individual desktop chat turns; no global model shutdown."""
import threading
import time


class TurnCancelled(Exception):
    pass


class ChatTurn:
    def __init__(self):
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._response = None

    def check(self):
        if self.cancelled.is_set():
            raise TurnCancelled()

    def attach(self, response):
        with self._lock:
            self._response = response
        if self.cancelled.is_set():
            response.close()
            self.check()

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            response = self._response
        if response is not None:
            # Closing a requests stream may wait for a socket read. Keep Stop's
            # HTTP response prompt while that read unwinds in the background.
            threading.Thread(target=self._close, args=(response,), daemon=True).start()

    @staticmethod
    def _close(response):
        try:
            response.close()
        except Exception:
            pass

    def wait_for_approval(self, event, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            if event.wait(min(0.1, max(0, deadline - time.monotonic()))):
                self.check()
                return True
        self.check()
        return False


class TurnRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._turns = {}

    def start(self, turn_id):
        with self._lock:
            if turn_id in self._turns:
                return None
            turn = ChatTurn()
            self._turns[turn_id] = turn
            return turn

    def cancel(self, turn_id):
        with self._lock:
            turn = self._turns.get(turn_id)
        if turn is None:
            return False
        turn.cancel()
        return True

    def finish(self, turn_id):
        with self._lock:
            self._turns.pop(turn_id, None)
