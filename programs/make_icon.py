# -*- coding: utf-8 -*-
"""Generate assets/app.ico - a simple PDF-style application icon."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")   # assets/ 位于项目根目录
os.makedirs(ASSETS, exist_ok=True)

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded red background.
radius = 48
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=(211, 47, 39, 255))

# White document page with a folded corner.
x0, y0, x1, y1 = 62, 40, 194, 216
fold = 34
d.polygon([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1),
           (x0, y1)], fill=(255, 255, 255, 255))
d.polygon([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)],
          fill=(224, 224, 224, 255))
d.line([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)],
       fill=(189, 189, 189, 255), width=2)

# "PDF" label on the page.
try:
    font = ImageFont.truetype("C:\\Windows\\Fonts\\arialbd.ttf", 40)
except Exception:
    font = ImageFont.load_default()
text = "PDF"
tb = d.textbbox((0, 0), text, font=font)
tw, th = tb[2] - tb[0], tb[3] - tb[1]
d.text(((x0 + x1) / 2 - tw / 2, (y0 + y1) / 2 - th / 2 + 18),
       text, font=font, fill=(211, 47, 39, 255))

ico = os.path.join(ASSETS, "app.ico")
img.save(ico, format="ICO",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                (128, 128), (256, 256)])
print(ico, os.path.getsize(ico))
