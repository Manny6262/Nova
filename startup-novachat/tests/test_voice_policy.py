import unittest
from pathlib import Path
from voice_policy import needs_desktop, compact_history


class VoicePolicyTests(unittest.TestCase):
    def test_verified_voice_prompt_never_claims_it_cannot_hear(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn("transcribed, so you did hear him", source)
        self.assertIn("Never say that you cannot hear", source)

    def test_chat_does_not_request_desktop(self):
        for text in [
            "how are you",
            "tell me a joke",
            "help me debug Python",
            "why is the sky blue",
        ]:
            self.assertFalse(needs_desktop(text), text)

    def test_computer_requests_are_routed(self):
        for text in [
            "open YouTube",
            "click that button",
            "what is on my screen",
            "launch the browser",
        ]:
            self.assertTrue(needs_desktop(text), text)

    def test_history_is_bounded_and_not_mutated(self):
        history = [
            {"role": "user", "content": "a" * 10000},
            {"role": "assistant", "content": "last"},
        ]
        result = compact_history(history, 3000)
        self.assertLessEqual(sum(len(m["content"]) for m in result), 3000)
        self.assertEqual(result[-1]["content"], "last")
        self.assertEqual(len(history[0]["content"]), 10000)
