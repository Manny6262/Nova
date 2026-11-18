"""Desktop-host settings required for real WebView2 voice playback."""

import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "desktop.py"


class DesktopVoiceTests(unittest.TestCase):
    def test_webview_allows_delayed_voice_playback(self):
        text = SOURCE.read_text(encoding="utf-8")
        ast.parse(text)
        self.assertIn("--use-fake-ui-for-media-stream", text)
        self.assertIn("--autoplay-policy=no-user-gesture-required", text)


if __name__ == "__main__":
    unittest.main()
