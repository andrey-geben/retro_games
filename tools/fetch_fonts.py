"""Скачивает Google-шрифты (Press Start 2P, Handjet, VT323) и делает локальный fonts.css."""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

ROOT = Path(r"C:\Users\user\Documents\deepseek-harness\default-workspace\retro-web")
FONT_DIR = ROOT / "static" / "fonts"
FONT_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

FAMILIES = [
    ("Press Start 2P", "family=Press+Start+2P"),
    ("Handjet", "family=Handjet:wght@400;700"),
    ("VT323", "family=VT323"),
]

BLOCK_RE = re.compile(r"/\*\s*([\w\-\[\]]+)\s*\*/\s*@font-face\s*\{(.*?)\}", re.S)
FIELD_RE = re.compile(r"([\w-]+)\s*:\s*([^;]+);")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(request, timeout=60).read()


def main() -> None:
    rules: list[str] = []
    counter = 0

    for family, query in FAMILIES:
        css_url = f"https://fonts.googleapis.com/css2?{query}&display=swap"
        css = fetch(css_url).decode("utf-8")
        blocks = BLOCK_RE.findall(css)
        if not blocks:
            print("не нашёл блоков для", family)
            continue

        for subset, body in blocks:
            fields = dict(FIELD_RE.findall(body))
            src = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", body)
            if not src:
                continue
            counter += 1
            filename = f"f{counter:02d}.woff2"
            (FONT_DIR / filename).write_bytes(fetch(src.group(1)))
            rules.append(
                "@font-face {\n"
                f"  font-family: '{fields.get('font-family', family).strip(chr(39) + chr(34))}';\n"
                f"  font-style: {fields.get('font-style', 'normal')};\n"
                f"  font-weight: {fields.get('font-weight', '400')};\n"
                f"  font-display: swap;\n"
                f"  unicode-range: {fields.get('unicode-range', 'U+0-10FFFF')};\n"
                f"  src: url('{filename}') format('woff2');\n"
                "}\n"
            )
            print(f"{family:16} {subset:12} -> {filename}")

    (FONT_DIR / "fonts.css").write_text("\n".join(rules), encoding="utf-8")
    print("готово, файлов:", counter)


if __name__ == "__main__":
    main()
