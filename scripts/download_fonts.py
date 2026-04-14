"""
Downloads the NotoSansHebrew-Regular.ttf font required for PDF generation.

Run once before starting the bot:
    python scripts/download_fonts.py
"""

import os
import sys

import requests

# scripts/ is one level below the project root
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(_PROJECT_ROOT, "fonts")
FONT_PATH = os.path.join(FONTS_DIR, "NotoSansHebrew-Regular.ttf")

# Fallback list — tried in order until one succeeds
FONT_URLS = [
    # OpenMapTiles mirror (reliable, contains static NotoSansHebrew)
    "https://raw.githubusercontent.com/openmaptiles/fonts/master/noto-sans/NotoSansHebrew-Regular.ttf",
    # notofonts official GitHub (variable font, also accepted by ReportLab)
    "https://github.com/notofonts/hebrew/raw/main/fonts/NotoSansHebrew/variable/NotoSansHebrew%5Bwdth%2Cwght%5D.ttf",
    # Google Fonts — Alef Hebrew (simple fallback)
    "https://github.com/google/fonts/raw/main/ofl/alef/Alef-Regular.ttf",
]


def download_font() -> None:
    if os.path.exists(FONT_PATH):
        print(f"Font already present: {FONT_PATH}")
        return

    os.makedirs(FONTS_DIR, exist_ok=True)
    print("Downloading Hebrew font…")

    for url in FONT_URLS:
        print(f"  Trying: {url}")
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            # Sanity check: valid TTF files start with specific magic bytes
            if len(response.content) < 1000:
                print("  Skipping — response too small, likely not a valid font.")
                continue
        except requests.RequestException as exc:
            print(f"  Failed: {exc}")
            continue

        with open(FONT_PATH, "wb") as f:
            f.write(response.content)
        print(f"Font saved to: {FONT_PATH}")
        return

    print("ERROR: Could not download font from any source.", file=sys.stderr)
    print("Manually download a Hebrew TTF font and save it to:", file=sys.stderr)
    print(f"  {FONT_PATH}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    download_font()
