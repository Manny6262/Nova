r"""Local image and video generation for Nova through ComfyUI.

The engine and model weights live in C:\NovaMedia. Generated files are copied
into Nova's media folder so the desktop UI can display them safely.
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
MEDIA_ROOT = Path(r"C:\NovaMedia")
PORTABLE_ROOT = MEDIA_ROOT / "ComfyUI_windows_portable"
COMFY_ROOT = PORTABLE_ROOT / "ComfyUI"
PYTHON_EXE = PORTABLE_ROOT / "python_embeded" / "python.exe"
COMFY_MAIN = COMFY_ROOT / "main.py"
COMFY_OUTPUT = COMFY_ROOT / "output"
PUBLIC_ROOT = APP_DIR / "media"
LOG_ROOT = MEDIA_ROOT / "logs"
PID_FILE = MEDIA_ROOT / "comfyui.pid"
ENGINE_URL = "http://127.0.0.1:8188"

IMAGE_MODELS = {
    "diffusion": COMFY_ROOT / "models" / "diffusion_models" / "z_image_turbo_bf16.safetensors",
    "text_encoder": COMFY_ROOT / "models" / "text_encoders" / "qwen_3_4b.safetensors",
    "vae": COMFY_ROOT / "models" / "vae" / "ae.safetensors",
}
VIDEO_MODELS = {
    "diffusion": COMFY_ROOT / "models" / "diffusion_models" / "wan2.1_t2v_1.3B_fp16.safetensors",
    "text_encoder": COMFY_ROOT / "models" / "text_encoders" / "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": COMFY_ROOT / "models" / "vae" / "wan_2.1_vae.safetensors",
}
EXPECTED_MODEL_BYTES = {
    "z_image_turbo_bf16.safetensors": 12_309_866_400,
    "qwen_3_4b.safetensors": 8_044_982_048,
    "ae.safetensors": 335_304_388,
    "wan2.1_t2v_1.3B_fp16.safetensors": 2_838_303_560,
    "umt5_xxl_fp8_e4m3fn_scaled.safetensors": 6_735_906_897,
    "wan_2.1_vae.safetensors": 253_815_318,
}

_start_lock = threading.Lock()
_generation_lock = threading.Lock()


class MediaError(RuntimeError):
    pass


def _json_request(path: str, payload=None, timeout=15):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ENGINE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:3000]
        raise MediaError(f"ComfyUI returned HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise MediaError(f"ComfyUI is unavailable: {error}") from error


def _engine_health():
    try:
        return _json_request("/system_stats", timeout=4)
    except MediaError:
        return None


def _model_state(models):
    return {
        name: {
            "ready": path.is_file() and path.stat().st_size >= EXPECTED_MODEL_BYTES.get(path.name, 1_000_000),
            "path": str(path),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
        }
        for name, path in models.items()
    }


def status():
    health = _engine_health()
    device = ((health or {}).get("devices") or [{}])[0]
    return {
        "ready": bool(health),
        "engine_url": ENGINE_URL,
        "engine_version": ((health or {}).get("system") or {}).get("comfyui_version"),
        "device": device.get("name"),
        "image_models": _model_state(IMAGE_MODELS),
        "video_models": _model_state(VIDEO_MODELS),
        "generated_folder": str(PUBLIC_ROOT),
    }


def ensure_started(wait_seconds=180):
    if _engine_health():
        return status()
    with _start_lock:
        if _engine_health():
            return status()
        if not PYTHON_EXE.is_file() or not COMFY_MAIN.is_file():
            raise MediaError("The local ComfyUI engine is not installed in C:\\NovaMedia.")
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        out = open(LOG_ROOT / "comfyui.out.log", "a", encoding="utf-8")
        err = open(LOG_ROOT / "comfyui.err.log", "a", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(PYTHON_EXE), "-s", str(COMFY_MAIN), "--windows-standalone-build",
             "--enable-dynamic-vram", "--listen", "127.0.0.1", "--port", "8188",
             "--disable-auto-launch"],
            cwd=str(PORTABLE_ROOT), stdout=out, stderr=err, creationflags=creationflags,
        )
        PID_FILE.write_text(str(process.pid), encoding="ascii")
        deadline = time.monotonic() + max(10, int(wait_seconds))
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise MediaError(f"ComfyUI stopped during startup (exit code {process.returncode}).")
            if _engine_health():
                return status()
            time.sleep(2)
        raise MediaError("ComfyUI did not become ready within the startup timeout.")


def _require_models(models, label):
    missing = [
        path.name for path in models.values()
        if not path.is_file() or path.stat().st_size < EXPECTED_MODEL_BYTES.get(path.name, 1_000_000)
    ]
    if missing:
        raise MediaError(f"{label} model setup is incomplete. Missing: {', '.join(missing)}")


def _bounded_dimension(value, low, high, label):
    try:
        value = int(value)
    except (TypeError, ValueError) as error:
        raise MediaError(f"{label} must be a number.") from error
    value = max(low, min(high, value))
    return max(16, (value // 16) * 16)


def _seed(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = -1
    return random.SystemRandom().randrange(0, 2**63) if value < 0 else min(value, 2**63 - 1)


def _wait_for_output(workflow, timeout_seconds):
    ensure_started()
    client_id = uuid.uuid4().hex
    queued = _json_request("/prompt", {"prompt": workflow, "client_id": client_id}, timeout=30)
    prompt_id = queued.get("prompt_id")
    if not prompt_id:
        raise MediaError("ComfyUI did not accept the generation request: " + json.dumps(queued)[:2000])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        history = _json_request(f"/history/{prompt_id}", timeout=15)
        entry = history.get(prompt_id)
        if entry:
            status_info = entry.get("status") or {}
            if status_info.get("status_str") == "error":
                messages = status_info.get("messages") or []
                raise MediaError("Generation failed: " + json.dumps(messages, ensure_ascii=False)[-3000:])
            if entry.get("outputs"):
                return prompt_id, entry
        time.sleep(1)
    try:
        _json_request("/interrupt", {}, timeout=5)
    except MediaError:
        pass
    raise MediaError(f"Generation timed out after {timeout_seconds} seconds.")


def _find_output(entry, media_type):
    keys = ("images",) if media_type == "image" else ("videos", "gifs", "images")
    for output in (entry.get("outputs") or {}).values():
        for key in keys:
            for item in output.get(key) or []:
                filename = item.get("filename")
                if not filename:
                    continue
                subfolder = item.get("subfolder") or ""
                source = COMFY_OUTPUT / subfolder / filename
                if source.is_file():
                    return source
    raise MediaError(f"ComfyUI completed but did not report a saved {media_type} file.")


def _publish(source, media_type):
    folder = PUBLIC_ROOT / ("images" if media_type == "image" else "videos")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if target.exists():
        target = folder / f"{source.stem}_{uuid.uuid4().hex[:8]}{source.suffix}"
    shutil.copy2(source, target)
    mime = mimetypes.guess_type(target.name)[0] or ("image/png" if media_type == "image" else "video/mp4")
    relative = target.relative_to(PUBLIC_ROOT).as_posix()
    return {
        "type": media_type,
        "url": "/media/" + relative,
        "path": str(target),
        "mime": mime,
        "filename": target.name,
        "size_bytes": target.stat().st_size,
    }


def _image_workflow(prompt, negative_prompt, width, height, steps, seed):
    full_prompt = prompt.strip()
    if negative_prompt.strip():
        full_prompt += "\nAvoid these elements: " + negative_prompt.strip()
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": full_prompt, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "res_multistep", "scheduler": "simple", "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "Nova/images/Nova_Image"}},
    }


def _video_workflow(prompt, negative_prompt, width, height, frames, fps, steps, seed):
    negative = negative_prompt.strip() or (
        "overexposed, static, blurry details, subtitles, painting, still image, gray cast, "
        "worst quality, low quality, JPEG artifacts, ugly, deformed, disfigured, malformed limbs, "
        "fused fingers, cluttered background, extra legs, walking backwards"
    )
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.1_t2v_1.3B_fp16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt.strip(), "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": width, "height": height, "length": frames, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "seed": seed, "steps": steps, "cfg": 6.0, "sampler_name": "uni_pc", "scheduler": "simple", "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": float(fps), "codec": "h264"}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "Nova/videos/Nova_Video", "format": "mp4", "codec": "h264"}},
    }


def generate_image(prompt, negative_prompt="", width=1024, height=1024, steps=8, seed=-1):
    if not str(prompt or "").strip():
        raise MediaError("An image prompt is required.")
    _require_models(IMAGE_MODELS, "Image")
    width = _bounded_dimension(width, 512, 1536, "width")
    height = _bounded_dimension(height, 512, 1536, "height")
    steps = max(4, min(20, int(steps)))
    chosen_seed = _seed(seed)
    with _generation_lock:
        prompt_id, entry = _wait_for_output(
            _image_workflow(str(prompt), str(negative_prompt or ""), width, height, steps, chosen_seed), 1200
        )
    media = _publish(_find_output(entry, "image"), "image")
    return {"prompt_id": prompt_id, "seed": chosen_seed, "width": width, "height": height, "media": media}


def generate_video(prompt, negative_prompt="", width=512, height=320, frames=33, fps=8, steps=30, seed=-1):
    if not str(prompt or "").strip():
        raise MediaError("A video prompt is required.")
    _require_models(VIDEO_MODELS, "Video")
    width = _bounded_dimension(width, 320, 832, "width")
    height = _bounded_dimension(height, 240, 640, "height")
    frames = max(9, min(81, int(frames)))
    frames = ((frames - 1) // 4) * 4 + 1
    fps = max(4, min(30, int(fps)))
    steps = max(10, min(50, int(steps)))
    chosen_seed = _seed(seed)
    with _generation_lock:
        prompt_id, entry = _wait_for_output(
            _video_workflow(str(prompt), str(negative_prompt or ""), width, height, frames, fps, steps, chosen_seed), 3600
        )
    media = _publish(_find_output(entry, "video"), "video")
    return {"prompt_id": prompt_id, "seed": chosen_seed, "width": width, "height": height,
            "frames": frames, "fps": fps, "media": media}
