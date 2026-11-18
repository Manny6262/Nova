import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_engine
import tools


class MediaWorkflowTests(unittest.TestCase):
    def test_image_workflow_uses_official_z_image_layout(self):
        workflow = media_engine._image_workflow("a glass city", "text", 1024, 768, 8, 42)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "z_image_turbo_bf16.safetensors")
        self.assertEqual(workflow["2"]["inputs"]["type"], "lumina2")
        self.assertEqual(workflow["8"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["10"]["class_type"], "SaveImage")

    def test_video_workflow_uses_official_wan_layout(self):
        workflow = media_engine._video_workflow("a running fox", "", 512, 320, 33, 8, 30, 42)
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "wan2.1_t2v_1.3B_fp16.safetensors")
        self.assertEqual(workflow["2"]["inputs"]["type"], "wan")
        self.assertEqual(workflow["6"]["inputs"]["length"], 33)
        self.assertEqual(workflow["11"]["inputs"]["format"], "mp4")

    def test_generate_image_publishes_structured_media(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "generated.png"
            source.write_bytes(b"fake png")
            published = {
                "type": "image", "url": "/media/images/generated.png",
                "path": str(source), "mime": "image/png", "filename": "generated.png",
                "size_bytes": source.stat().st_size,
            }
            with patch.object(media_engine, "_require_models"), \
                 patch.object(media_engine, "_wait_for_output", return_value=("prompt", {"outputs": {}})), \
                 patch.object(media_engine, "_find_output", return_value=source), \
                 patch.object(media_engine, "_publish", return_value=published):
                result = tools.generate_image("a glass city", seed=42)
            self.assertTrue(result["ok"])
            self.assertEqual(result["media"]["type"], "image")
            self.assertEqual(result["media"]["url"], "/media/images/generated.png")

    def test_video_dimensions_and_frames_are_safely_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "generated.mp4"
            source.write_bytes(b"fake mp4")
            published = {
                "type": "video", "url": "/media/videos/generated.mp4",
                "path": str(source), "mime": "video/mp4", "filename": "generated.mp4",
                "size_bytes": source.stat().st_size,
            }
            captured = {}

            def fake_wait(workflow, timeout):
                captured["workflow"] = workflow
                return "prompt", {"outputs": {}}

            with patch.object(media_engine, "_require_models"), \
                 patch.object(media_engine, "_wait_for_output", side_effect=fake_wait), \
                 patch.object(media_engine, "_find_output", return_value=source), \
                 patch.object(media_engine, "_publish", return_value=published):
                result = media_engine.generate_video("a running fox", width=519, height=327, frames=32, seed=7)
            self.assertEqual(result["width"], 512)
            self.assertEqual(result["height"], 320)
            self.assertEqual(result["frames"], 29)
            self.assertEqual(captured["workflow"]["11"]["class_type"], "SaveVideo")


if __name__ == "__main__":
    unittest.main()
