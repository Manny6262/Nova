"""Live GPT OAuth image-input exam through Nova."""

import base64
import io
import json

import requests
from PIL import Image, ImageDraw, ImageFont


image = Image.new("RGB", (900, 300), "white")
draw = ImageDraw.Draw(image)
font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 84)
draw.text((45, 90), "NOVA CODE 7429", fill="black", font=font)
buffer = io.BytesIO()
image.save(buffer, "PNG")
encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

response = requests.post(
    "http://127.0.0.1:5000/chat",
    json={
        "message": "Read the exact four-digit code in the attached image. Reply with the digits.",
        "images": [encoded],
        "provider": "codex",
        "caps": {"write": False, "exec": False, "web": False, "control": False},
        "approval_mode": "bypass",
    },
    stream=True,
    timeout=(10, 180),
)
response.raise_for_status()
events = [json.loads(line) for line in response.iter_lines(decode_unicode=True) if line]
answer = "".join(event.get("token", "") for event in events)
errors = [event["error"] for event in events if event.get("error")]
result = {"pass": "7429" in answer and not errors, "answer": answer, "errors": errors}
print(json.dumps(result, indent=2))
assert result["pass"], result
