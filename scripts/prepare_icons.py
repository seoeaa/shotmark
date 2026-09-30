#!/usr/bin/env python3
"""Create committed icon sizes from the single generated original; no API calls."""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
NAME = "io.github.shotmark.Shotmark"


def main():
    with Image.open(ROOT / "assets/shotmark-generated-1024.png") as source:
        image = source.convert("RGBA")
        for size in SIZES:
            target = ROOT / f"assets/icons/hicolor/{size}x{size}/apps/{NAME}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            image.resize((size, size), Image.Resampling.LANCZOS).save(target, optimize=True)
    directories = [f"{size}x{size}/apps" for size in SIZES] + ["scalable/apps"]
    theme = (
        "[Icon Theme]\nName=Hicolor\nComment=Shotmark icons\nDirectories="
        + ",".join(directories)
        + "\n"
    )
    for size in SIZES:
        theme += f"\n[{size}x{size}/apps]\nSize={size}\nContext=Applications\nType=Fixed\n"
    theme += (
        "\n[scalable/apps]\nSize=16\nMinSize=8\nMaxSize=512\nContext=Applications\nType=Scalable\n"
    )
    (ROOT / "assets/icons/hicolor/index.theme").write_text(theme)


if __name__ == "__main__":
    main()
