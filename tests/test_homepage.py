"""The static homepage must work under a GitHub Pages project path."""
from html.parser import HTMLParser
import json
from pathlib import Path
from xml.etree import ElementTree

from scripts.build_homepage import build, ROOT


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = {}
        self.assets = []
        self.meta = {}
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("class") == "tool-card":
            self.cards[attrs["data-tool"]] = attrs["href"]
        if tag == "script":
            self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs["href"]
        if tag == "meta":
            self.meta[attrs.get("name")] = attrs.get("content")


def test_built_homepage_has_all_tool_links_and_only_local_assets(tmp_path):
    build(tmp_path)
    config = json.loads((ROOT / "homepage/config.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "static/tools.json").read_text(encoding="utf-8"))
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    parsed = Links()
    parsed.feed(page)
    assert parsed.cards == {tool["id"]: config["app_url"].rstrip("/") + "/#" + tool["id"] for tool in catalog}
    assert parsed.canonical == config["homepage_url"]
    assert parsed.meta["google-site-verification"] == config["google_verification"]
    assert all(asset.startswith("./") and (tmp_path / asset).is_file() for asset in parsed.assets)
    assert "file-input" not in page and "fetch(" not in (tmp_path / "homepage.js").read_text(encoding="utf-8")
    assert "{{" not in page
    sitemap = ElementTree.fromstring((tmp_path / "sitemap.xml").read_bytes())
    assert [url[0].text for url in sitemap] == [config["homepage_url"]]
    assert {path.name for path in tmp_path.iterdir()} == {"index.html", "homepage.js", "homepage.css", "styles.css", "sitemap.xml", ".nojekyll"}
