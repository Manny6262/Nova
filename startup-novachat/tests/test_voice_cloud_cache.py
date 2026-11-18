import unittest
from unittest.mock import Mock
from codex_bridge import AppServerClient


class CloudVoiceCacheTests(unittest.TestCase):
    def setUp(self):
        self.client = AppServerClient()
        self.client.start = lambda: None
        self.client.interrupt = Mock()
        self.starts = 0
        self.prompts = []
        self.fail = False

        def request(method, params, **kwargs):
            if method == "thread/start":
                self.starts += 1
                return {"thread": {"id": str(self.starts)}}
            if method == "turn/start":
                self.prompts.append(params["input"][0]["text"])
                queue = self.client._turns[params["threadId"]]
                queue.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "turn": {
                                "status": "failed" if self.fail else "completed",
                                "error": {"message": "synthetic failure"},
                            }
                        },
                    }
                )
                return {"turn": {"id": "turn"}}
            raise AssertionError(method)

        self.client.request = request

    def run_turn(self, key="test-session", write=False):
        return list(
            self.client.run_turn(
                "FULL HISTORY",
                cwd="C:/NovaChat",
                instructions="test",
                write_enabled=write,
                exec_enabled=False,
                web_enabled=False,
                approval_mode="ask",
                voice_key=key,
                voice_input="NEW UTTERANCE",
            )
        )

    def test_reuses_completed_voice_thread_without_replaying_history(self):
        self.run_turn()
        self.run_turn()
        self.assertEqual(self.starts, 1)
        self.assertEqual(self.prompts, ["FULL HISTORY", "NEW UTTERANCE"])

    def test_permission_change_starts_new_thread(self):
        self.run_turn()
        self.run_turn(write=True)
        self.assertEqual(self.starts, 2)

    def test_new_session_never_reuses_old_conversation(self):
        self.run_turn()
        self.run_turn("another-session")
        self.assertEqual(self.starts, 2)

    def test_text_turns_are_unchanged(self):
        self.run_turn(None)
        self.run_turn(None)
        self.assertEqual(self.starts, 2)

    def test_failed_turn_invalidates_cache(self):
        self.run_turn()
        self.fail = True
        with self.assertRaises(Exception):
            self.run_turn()
        self.fail = False
        self.run_turn()
        self.assertEqual(self.starts, 2)

    def test_cache_bounded(self):
        for i in range(10):
            self.run_turn(str(i))
        self.assertEqual(len(self.client._voice_threads), 8)
