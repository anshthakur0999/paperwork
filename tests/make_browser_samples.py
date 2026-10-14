"""Generate non-sensitive samples for a manual end-to-end browser check."""
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw


def main():
    target = Path(__file__).resolve().parents[1] / "tmp" / "browser-qa"
    target.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for number, heading in enumerate(["Project overview", "Working notes", "Next steps"], 1):
            page = document.new_page(width=595, height=842)
            page.draw_rect(pymupdf.Rect(32, 32, 563, 116), color=None, fill=(0.94, 0.93, 0.99))
            page.insert_text((52, 79), heading, fontsize=23, color=(0.2, 0.18, 0.4))
            page.insert_text((52, 160), "Synthetic document for local app verification.", fontsize=13)
            page.insert_text((52, 195), f"Page {number} of 3 - no personal information.", fontsize=11)
        document.save(target / "sample-three-pages.pdf")
    image = Image.new("RGB", (800, 600), "#eeeafa")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 70, 730, 530), radius=32, fill="#ffffff")
    draw.text((140, 230), "Paperwork image conversion sample", fill="#4f428f", font_size=28)
    draw.text((140, 280), "Made on this computer", fill="#696477", font_size=20)
    image.save(target / "sample-image.png")
    print(target)


if __name__ == "__main__":
    main()
