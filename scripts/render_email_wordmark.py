"""Render static/email/wordmark.png — the header wordmark for HTML emails.

Mail clients can't use the web header's CSS (skewed bars, Impact, gradient text),
and Impact is missing on servers and phones, so the wordmark ships as an image.
Dev-only: needs resvg-py, Pillow and an Impact font (macOS has one).

    python scripts/render_email_wordmark.py [path/to/Impact.ttf]
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import resvg_py
from PIL import Image

SCALE = 3  # crisp on high-DPI screens; the email shows it at 1/3 size
FONT = sys.argv[1] if len(sys.argv) > 1 else "/System/Library/Fonts/Supplemental/Impact.ttf"
OUT = Path(__file__).resolve().parent.parent / "static" / "email" / "wordmark.png"

# Same geometry and colours as .proxmox-mark / .brand-title in static/style.css.
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="32">
  <defs>
    <linearGradient id="metal" x1="0" y1="0" x2="1" y2="0.3">
      <stop offset="0" stop-color="#e8ecf2"/>
      <stop offset="0.35" stop-color="#c7ced8"/>
      <stop offset="0.7" stop-color="#8899aa"/>
      <stop offset="1" stop-color="#c7ced8"/>
    </linearGradient>
  </defs>
  <g transform="translate(5 5) skewX(-18)">
    <rect width="5" height="22" rx="2.5" fill="#ef7b2d"/>
    <rect x="8" width="5" height="22" rx="2.5" fill="#c7ced8"/>
  </g>
  <text x="22" y="24.5" font-family="Impact" font-size="20.8" letter-spacing="-0.4" fill="url(#metal)">BACKUP SENTINEL</text>
</svg>"""


def main() -> None:
    png = resvg_py.svg_to_bytes(svg_string=SVG, zoom=SCALE, font_files=[FONT], skip_system_fonts=True)
    image = Image.open(io.BytesIO(bytes(png)))
    image = image.crop(image.getbbox())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, optimize=True)
    print(f"{OUT} {image.width}x{image.height}")


if __name__ == "__main__":
    main()
