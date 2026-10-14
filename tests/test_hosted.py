"""Public deployment boundaries, resource limits, and privacy messaging."""
import json
import re
from xml.etree import ElementTree

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PAPERWORK_SOURCE_URL", "https://github.com/anshthakur0999/paperwork")
    with TestClient(create_app(hosted=True, allowed_hosts=("paperwork.example",)), base_url="https://paperwork.example") as active:
        yield active


@pytest.fixture
def pdf():
    with fitz.open() as document:
        for number in range(1, 4):
            page = document.new_page()
            page.insert_text((50, 50), f"Synthetic page {number}")
        return document.tobytes()


def test_requires_explicit_host(monkeypatch):
    monkeypatch.delenv("PAPERWORK_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)
    with pytest.raises(ValueError, match="hostname"):
        create_app(hosted=True)
    for host in ("*", "https://paperwork.example", "paperwork.example/path"):
        with pytest.raises(ValueError, match="hostnames"):
            create_app(hosted=True, allowed_hosts=(host,))


def test_render_hostname_and_limits(monkeypatch):
    monkeypatch.delenv("PAPERWORK_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "paperwork.example")
    monkeypatch.setenv("PAPERWORK_MAX_UPLOAD_MB", "3")
    application = create_app(hosted=True)
    assert application.state.upload_limit == 3 * 1024 * 1024
    with TestClient(application, base_url="https://paperwork.example") as client:
        assert client.get("/api/health").status_code == 200


def test_hosted_page_explains_uploads_and_browser_drafts(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Server processing" in page.text
    assert "Files are uploaded to the PDF server" in page.text
    assert "Saved drafts stay in this browser for 24 hours" in page.text
    assert "25 MB total" in page.text
    assert "Files are sent only to the server on this computer" not in page.text
    config = json.loads(re.search(r'<script id="app-config" type="application/json">(.*?)</script>', page.text).group(1))
    assert config == {"mode": "hosted", "max_upload_bytes": 25 * 1024 * 1024}
    assert 'href="https://github.com/anshthakur0999/paperwork"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/api/health").json()["app"] == "paperwork-hosted"


def test_hosted_shutdown_is_disabled(client):
    assert client.post("/api/shutdown").status_code == 404


def test_search_metadata_verification_and_canonical_are_in_the_initial_head(client):
    for path in ("/", "/index.html", "/static/index.html"):
        response = client.get(path)
        head = response.text.split("</head>")[0]
        assert "Free Online PDF Tools" in head
        assert '<meta name="description"' in head
        assert '<link rel="canonical" href="https://paperwork.example/">' in head
        assert '<meta name="google-site-verification" content="vwXE8kAhkuLpdhujuebl3A_YPzSea7Rawg5iIZIEblE">' in head
        assert "X-Robots-Tag" not in response.headers
    # Internal health probes must not change the published canonical hostname.
    assert 'href="https://paperwork.example/"' in client.get("/", headers={"Host": "localhost"}).text
    assert client.get("/api/health").headers["X-Robots-Tag"] == "noindex, nofollow"


def test_sitemap_contains_only_the_public_homepage_and_robots_allows_assets(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert "Allow: /\nDisallow: /api/" in robots.text
    assert "Sitemap: https://paperwork.example/sitemap.xml" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    urls = ElementTree.fromstring(sitemap.content).findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url")
    assert [url[0].text for url in urls] == ["https://paperwork.example/"]
    assert client.head("/robots.txt").status_code == 200
    assert client.head("/sitemap.xml").status_code == 200


def test_local_mode_has_no_public_sitemap_or_google_verification():
    with TestClient(create_app(hosted=False)) as client:
        page = client.get("/")
        assert "google-site-verification" not in page.text
        assert 'rel="canonical"' not in page.text
        assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
        assert client.get("/sitemap.xml").status_code == 404


def test_static_homepage_is_canonical_without_changing_the_document_workspace(monkeypatch):
    monkeypatch.setenv("PAPERWORK_HOMEPAGE_URL", "https://anshthakur0999.github.io/paperwork/")
    monkeypatch.setenv("PAPERWORK_SOURCE_URL", "https://github.com/anshthakur0999/paperwork")
    with TestClient(create_app(hosted=True, allowed_hosts=("paperwork.example",)), base_url="https://paperwork.example") as client:
        page = client.get("/")
        assert '<link rel="canonical" href="https://anshthakur0999.github.io/paperwork/">' in page.text
        assert '>Paperwork homepage</a>' in page.text
        assert 'id="file-input"' in page.text
        assert page.text.count('class="tool-card" data-tool=') == 15
        assert "google-site-verification" in page.text
        assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.com/", "https://user:secret@example.com/", "https://example.com/#fragment"])
def test_rejects_invalid_homepage_configuration(monkeypatch, url):
    monkeypatch.setenv("PAPERWORK_HOMEPAGE_URL", url)
    with pytest.raises(ValueError, match="public HTTPS URL"):
        create_app(hosted=True, allowed_hosts=("paperwork.example",))


@pytest.mark.parametrize("host", ["evil.example", "paperwork.example.evil.example", "paperwork.example@evil.example"])
def test_rejects_other_hosts(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "http://paperwork.example", "null", "https://paperwork.example/path"])
def test_rejects_other_origins(client, pdf, origin):
    response = client.post("/api/inspect", files={"file": ("sample.pdf", pdf)}, headers={"Origin": origin})
    assert response.status_code == 403


def test_same_origin_processing_preserves_selected_page(client, pdf):
    headers = {"Origin": "https://paperwork.example", "Sec-Fetch-Site": "same-origin"}
    response = client.post("/api/process/delete", files={"files": ("sample.pdf", pdf)}, data={"options": json.dumps({"pages": "1,3"})}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    with fitz.open(stream=response.content, filetype="pdf") as document:
        assert len(document) == 1
        assert "page 2" in document[0].get_text()
    assert client.post("/api/inspect", files={"file": ("sample.pdf", pdf)}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_upload_limit_applies_to_total_file_bytes(monkeypatch):
    monkeypatch.setenv("PAPERWORK_MAX_UPLOAD_MB", "1")
    with TestClient(create_app(hosted=True, allowed_hosts=("paperwork.example",)), base_url="https://paperwork.example") as client:
        response = client.post("/api/inspect", files={"file": ("oversized.pdf", b"x" * (1024 * 1024 + 1))})
        assert response.status_code == 400
        assert "1 MB limit" in response.json()["detail"]
