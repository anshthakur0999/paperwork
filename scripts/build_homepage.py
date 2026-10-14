"""Build the GitHub Pages homepage from Paperwork's shared tool catalog."""
import argparse
from html import escape
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output = Path(output)
    config = json.loads((ROOT / "homepage/config.json").read_text(encoding="utf-8"))
    for key in ("homepage_url", "app_url", "source_url"):
        url = urlsplit(config[key])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError(f"{key} must be a public HTTPS URL.")
    catalog = json.loads((ROOT / "static/tools.json").read_text(encoding="utf-8"))
    svg = re.search(r'<svg class="svg-library".*?</svg>', (ROOT / "static/index.html").read_text(encoding="utf-8"), re.S).group(0)
    cards = []
    for tool in catalog:
        safe = {key: escape(str(value), quote=True) for key, value in tool.items()}
        href = escape(config["app_url"].rstrip("/") + "/#" + tool["id"], quote=True)
        cards.append(f'<a class="tool-card" data-category="{safe["category"]}" data-tool="{safe["id"]}" href="{href}"><span class="tool-icon {safe["category"]}"><svg class="icon" aria-hidden="true"><use href="#i-{safe["icon"]}"/></svg></span><svg class="icon card-arrow" aria-hidden="true"><use href="#i-arrow"/></svg><h3>{safe["name"]}</h3><p>{safe["description"]}</p></a>')
    token = escape(config.get("google_verification", ""), quote=True)
    replacements = {
        "HOMEPAGE_URL": escape(config["homepage_url"], quote=True),
        "APP_URL": escape(config["app_url"], quote=True),
        "SOURCE_URL": escape(config["source_url"], quote=True),
        "VERIFICATION_TAG": f'<meta name="google-site-verification" content="{token}">' if token else "",
        "SVG_LIBRARY": svg,
        "TOOL_CARDS": "\n".join(cards),
    }
    page = (ROOT / "homepage/index.html").read_text(encoding="utf-8")
    for key, value in replacements.items():
        page = page.replace("{{" + key + "}}", value)
    if re.search(r"\{\{[A-Z_]+\}\}", page):
        raise ValueError("An unresolved homepage placeholder remains.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(page, encoding="utf-8")
    for name in ("homepage.css", "homepage.js"):
        shutil.copyfile(ROOT / "homepage" / name, output / name)
    shutil.copyfile(ROOT / "static/styles.css", output / "styles.css")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += f'  <url><loc>{escape(config["homepage_url"])}</loc></url>\n</urlset>\n'
    (output / "sitemap.xml").write_text(xml, encoding="utf-8")
    print(f"Built {len(catalog)} tool links in {output.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "output/github-pages"))
    build(parser.parse_args().output)
