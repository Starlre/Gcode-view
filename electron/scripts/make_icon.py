"""Render the simple layer motif at Windows icon sizes (build time only)."""
from pathlib import Path
from PIL import Image, ImageDraw

image = Image.new("RGBA", (1024, 1024))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, 1023, 1023), radius=240, fill="#15764e")
for points in (((54, 88), (128, 51), (202, 88), (128, 125), (54, 88)),
               ((54, 123), (128, 160), (202, 123)),
               ((54, 158), (128, 195), (202, 158))):
    scaled = [(x * 4, y * 4) for x, y in points]
    draw.line(scaled, fill="white", width=44, joint="curve")
    for x, y in scaled:
        draw.ellipse((x - 22, y - 22, x + 22, y + 22), fill="white")
target = Path(__file__).resolve().parents[1] / "assets"
target.mkdir(exist_ok=True)
image.resize((256, 256), Image.Resampling.LANCZOS).save(target / "studio.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
