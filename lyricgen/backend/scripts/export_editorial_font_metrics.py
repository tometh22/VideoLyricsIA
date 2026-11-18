"""Regenerate the frontend's TTF measurements after changing catalogue fonts.

Run: python lyricgen/backend/scripts/export_editorial_font_metrics.py
"""
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from PIL import ImageFont
import lyric_looks as looks
import lyric_recipes as recipes


def export():
    result = {}
    for filename, _label, family, weight, *_ in recipes.FONTS.values():
        measure = looks._Measure(str(BACKEND / "fonts" / filename))
        font = ImageFont.truetype(measure.path, round(1000 * measure._em_per_fs))
        chars = {}
        for char in "".join(chr(i) for i in range(32, 256)) + "’–—…":
            bounds = font.getbbox(char)
            chars[char] = [round(font.getlength(char) / 1000, 4),
                           round(bounds[1] / 1000, 4), round(bounds[3] / 1000, 4)]
        result[f"{family}|{weight}"] = {"em": round(measure._em_per_fs, 5), "chars": chars}
    target = BACKEND.parent / "frontend/src/lib/editorialFontMetrics.json"
    target.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    export()
