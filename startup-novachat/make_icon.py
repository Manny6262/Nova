"""Generates nova.ico — a glowing cyan/violet core matching the app's look."""

from PIL import Image, ImageDraw
import math

SIZE = 512
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

cx = cy = SIZE / 2

# dark rounded background
draw.rounded_rectangle([0, 0, SIZE, SIZE], radius=110, fill=(6, 10, 22, 255))

# soft outer glow, drawn as fading concentric circles
for r in range(int(SIZE * 0.42), int(SIZE * 0.18), -3):
    t = (r - SIZE * 0.18) / (SIZE * 0.42 - SIZE * 0.18)
    alpha = int(28 * (1 - t))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(56, 240, 255, alpha))

# outer cyan ring
r1 = SIZE * 0.30
draw.ellipse([cx - r1, cy - r1, cx + r1, cy + r1], outline=(56, 240, 255, 255), width=10)

# inner violet ring
r2 = SIZE * 0.20
draw.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], outline=(139, 92, 246, 255), width=8)

# bright core
r3 = SIZE * 0.10
draw.ellipse([cx - r3, cy - r3, cx + r3, cy + r3], fill=(160, 245, 255, 255))

out = "C:/NovaChat/nova.ico"
img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("saved", out)
