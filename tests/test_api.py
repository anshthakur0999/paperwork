"""Loopback API behavior and browser request boundary checks."""

import asyncio
import base64
from io import BytesIO
import json
import tempfile
import threading
import zipfile

import pymupdf as fitz
from fastapi.testclient import TestClient
from PIL import Image
import pytest
from starlette.requests import ClientDisconnect, Request

import app as app_module
from app import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as active_client:
        yield active_client


@pytest.fixture
def pdf():
    with fitz.open() as document:
        for number in range(1, 4):
            page = document.new_page(width=300, height=400)
            page.insert_text((30, 50), f"Synthetic page {number}")
        return document.tobytes()


def upload(pdf, name="sample.pdf"):
    return (name, pdf, "application/pdf")


def test_health_and_local_interface(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    for path, content_types in [("/static/styles.css", {"text/css"}), ("/static/app.js", {"text/javascript", "application/javascript"})]:
        assert '<style id="app-styles">' in page.text if path.endswith("styles.css") else path in page.text
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset.headers["content-type"].split(";")[0] in content_types
        assert len(asset.content) > 100


def test_initial_page_is_styled_and_populated_without_javascript(client):
    import hashlib
    import re

    page = client.get("/")
    styles = re.search(r'<style id="app-styles">(.*?)</style>', page.text, re.S).group(1)
    style_hash = base64.b64encode(hashlib.sha256(styles.encode("utf-8")).digest()).decode("ascii")
    assert f"'sha256-{style_hash}'" in page.headers["content-security-policy"]
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    assert '<link rel="stylesheet"' not in page.text
    assert 'blocking="render"' not in page.text
    assert len(re.findall(r'class="tool-card" data-tool=', page.text)) == 15
    assert '<script id="tool-catalog" type="application/json">' in page.text
    assert page.headers["cache-control"] == "no-store"


def test_only_versioned_public_assets_are_cached(client):
    import re

    page = client.get("/")
    versioned_script = re.search(r'<script src="([^"]+)" defer>', page.text).group(1)
    assert client.get(versioned_script).headers["cache-control"] == "private, max-age=86400, immutable"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_inspect_and_preview(client, pdf):
    response = client.post("/api/inspect", files={"file": upload(pdf)})
    assert response.status_code == 200, response.text
    assert response.json()["page_count"] == 3
    preview = client.post("/api/preview", files={"file": upload(pdf)}, data={"page": "2"})
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("image/png")
    with Image.open(BytesIO(preview.content)) as rendered:
        assert rendered.width > 0 and rendered.height > 0


def test_process_streams_download_with_selected_pages(client, pdf):
    response = client.post("/api/process/extract", files=[("files", upload(pdf))], data={"options": json.dumps({"pages": "3,1"})})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert "attachment" in response.headers["content-disposition"]
    with fitz.open(stream=response.content, filetype="pdf") as document:
        assert len(document) == 2
        assert "page 3" in document[0].get_text()
        assert "page 1" in document[1].get_text()


def test_delete_multiple_pages_preserves_the_remaining_page_and_original(client, pdf):
    response = client.post("/api/process/delete", files=[("files", upload(pdf))], data={"options": json.dumps({"pages": "1,3"})})
    assert response.status_code == 200, response.text
    with fitz.open(stream=response.content, filetype="pdf") as result, fitz.open(stream=pdf, filetype="pdf") as original:
        assert len(result) == 1
        assert result[0].get_text() == original[1].get_text()
        assert len(original) == 3
    invalid = client.post("/api/process/delete", files=[("files", upload(pdf))], data={"options": json.dumps({"pages": "1-3"})})
    assert invalid.status_code == 400
    assert "at least one page" in invalid.json()["detail"]


def test_split_zip_download(client, pdf):
    response = client.post("/api/process/split", files=[("files", upload(pdf))], data={"options": json.dumps({"mode": "each"})})
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 3


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "127.0.0.1.evil.example", "192.168.1.20", "0.0.0.0"])
def test_rejects_non_loopback_hosts(client, host):
    response = client.get("/api/health", headers={"Host": host})
    assert response.status_code in {400, 403}


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "testserver"])
def test_accepts_loopback_hosts(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost.evil.example", "http://127.0.0.1.evil.example"])
@pytest.mark.parametrize("endpoint", ["/api/inspect", "/api/preview", "/api/thumbnails", "/api/process/extract"])
def test_rejects_cross_origin_api_posts(client, pdf, origin, endpoint):
    key = "files" if "/process/" in endpoint else "file"
    response = client.post(endpoint, files={key: upload(pdf)}, headers={"Origin": origin})
    assert response.status_code == 403
    assert "detail" in response.json()


@pytest.mark.parametrize("host, origin", [("127.0.0.1:8765", "http://127.0.0.1:8765"), ("localhost:8765", "http://localhost:8765"), ("testserver", "http://testserver")])
def test_accepts_same_origin_posts(client, pdf, host, origin):
    response = client.post("/api/inspect", files={"file": upload(pdf)}, headers={"Host": host, "Origin": origin})
    assert response.status_code == 200, response.text


def test_rejects_local_origin_on_a_different_port(client, pdf):
    response = client.post("/api/inspect", files={"file": upload(pdf)}, headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:9999"})
    assert response.status_code == 403


def test_fetch_metadata_cross_site_request_without_origin_is_rejected(client, pdf):
    response = client.post("/api/inspect", files={"file": upload(pdf)}, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


@pytest.mark.parametrize("options", ["not-json", "[]", '"string"', "123", "null"])
def test_invalid_options_json_returns_json_error(client, pdf, options):
    response = client.post("/api/process/extract", files=[("files", upload(pdf))], data={"options": options})
    assert response.status_code in {400, 422}
    assert "detail" in response.json()


@pytest.mark.parametrize("options", [{"pages": [1]}, {"pages": 2}, {"pages": "99"}])
def test_invalid_page_options_returns_json_error(client, pdf, options):
    response = client.post("/api/process/extract", files=[("files", upload(pdf))], data={"options": json.dumps(options)})
    assert response.status_code in {400, 422}
    assert "detail" in response.json()


def test_unknown_tool_and_missing_file_return_json_errors(client, pdf):
    unknown = client.post("/api/process/unknown", files=[("files", upload(pdf))])
    assert unknown.status_code in {400, 404}
    assert "detail" in unknown.json()
    missing = client.post("/api/inspect")
    assert missing.status_code in {400, 422}
    assert "detail" in missing.json()


def test_corrupt_upload_returns_friendly_error(client):
    response = client.post("/api/inspect", files={"file": upload(b"not a PDF")})
    assert response.status_code in {400, 422}
    assert "detail" in response.json()


def test_download_filename_handles_untrusted_upload_name(client, pdf):
    response = client.post("/api/process/extract", files=[("files", upload(pdf, "../../private document.pdf"))], data={"options": '{"pages":"1"}'})
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert "../" not in disposition and "..\\" not in disposition
    assert "\r" not in disposition and "\n" not in disposition


def test_processed_document_is_not_publicly_cacheable(client, pdf):
    response = client.post("/api/process/extract", files=[("files", upload(pdf))], data={"options": '{"pages":"1"}'})
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers.get("cache-control", "")


@pytest.mark.parametrize("origin", ["http://[", "http://[invalid]"])
def test_malformed_origin_is_rejected_without_server_exception(client, pdf, origin):
    response = client.post("/api/inspect", files={"file": upload(pdf)}, headers={"Origin": origin})
    assert response.status_code == 403
    assert "detail" in response.json()


def multipart_body(pdf, ending=True):
    boundary = "synthetic-boundary"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode() + pdf
    if ending:
        body += f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def test_chunked_multipart_without_content_length(client, pdf):
    body, headers = multipart_body(pdf)
    response = client.post("/api/inspect", content=(body[offset:offset + 97] for offset in range(0, len(body), 97)), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["page_count"] == 3


def test_truncated_multipart_is_rejected(client, pdf):
    body, headers = multipart_body(pdf, ending=False)
    response = client.post("/api/inspect", content=body, headers=headers)
    assert response.status_code == 400
    assert "detail" in response.json()


def test_rejects_oversized_content_length_before_reading(client, pdf):
    response = client.post("/api/inspect", files={"file": upload(pdf)}, headers={"Content-Length": str(app_module.MAX_REQUEST_BYTES + 1)})
    assert response.status_code == 400
    assert "limit" in response.json()["detail"].lower()


def test_sum_of_uploads_is_limited_even_without_content_length(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_DOCUMENT_BYTES", 1024)
    monkeypatch.setattr(app_module, "MAX_REQUEST_BYTES", 4096)
    body, headers = multipart_body(b"x" * 1025)
    response = client.post("/api/inspect", content=(body[offset:offset + 73] for offset in range(0, len(body), 73)), headers=headers)
    assert response.status_code == 400
    assert "limit" in response.json()["detail"].lower()


def test_multiple_upload_sizes_are_added(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_DOCUMENT_BYTES", 1024)
    monkeypatch.setattr(app_module, "MAX_REQUEST_BYTES", 4096)
    response = client.post("/api/process/merge", files=[("files", upload(b"x" * 600, "one.pdf")), ("files", upload(b"x" * 600, "two.pdf"))])
    assert response.status_code == 400
    assert "limit" in response.json()["detail"].lower()


def test_oversized_options_are_rejected(client, pdf):
    response = client.post("/api/process/extract", files=[("files", upload(pdf))], data={"options": "x" * 32769})
    assert response.status_code == 400
    assert "large" in response.json()["detail"].lower()


def test_file_count_is_limited(client):
    response = client.post("/api/process/merge", files=[("files", upload(b"x", f"part-{number}.pdf")) for number in range(101)])
    assert response.status_code == 400
    assert "100" in response.json()["detail"]


def test_empty_file_is_rejected(client):
    response = client.post("/api/inspect", files={"file": upload(b"")})
    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_duplicate_options_cannot_hide_invalid_json(client, pdf):
    response = client.post("/api/process/extract", files=[("files", upload(pdf)), ("options", (None, '{"pages":"1"}')), ("options", (None, "invalid"))])
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"].lower()


def test_document_operations_do_not_use_temporary_file_spooling(client, pdf, monkeypatch):
    def refuse_temporary_storage(*args, **kwargs):
        raise AssertionError("Document data must remain in memory")
    for name in ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, refuse_temporary_storage)
    response = client.post("/api/process/extract", files=[("files", upload(pdf))], data={"options": '{"pages":"2"}'})
    assert response.status_code == 200, response.text


def test_password_fields_work_and_are_not_returned_in_headers(client, pdf):
    secret = "synthetic-secret-54321"
    protected = client.post("/api/process/protect", files=[("files", upload(pdf))], data={"options": json.dumps({"user_password": secret})})
    assert protected.status_code == 200, protected.text
    assert secret not in str(dict(protected.headers))
    locked = client.post("/api/inspect", files={"file": upload(protected.content)})
    assert locked.status_code == 200 and locked.json()["password_required"]
    inspected = client.post("/api/inspect", files={"file": upload(protected.content)}, data={"password": secret})
    assert inspected.status_code == 200 and inspected.json()["page_count"] == 3
    incorrect = client.post("/api/inspect", files={"file": upload(protected.content)}, data={"password": "wrong-synthetic-secret"})
    assert incorrect.status_code == 400
    assert "wrong-synthetic-secret" not in incorrect.text


def test_thumbnails_stream_all_pages_and_complete(client, pdf):
    response = client.post("/api/thumbnails", files={"file": upload(pdf)})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert "no-store" in response.headers["cache-control"]
    records = [json.loads(line) for line in response.text.splitlines()]
    assert [record["page"] for record in records[:-1]] == [1, 2, 3]
    for record in records[:-1]:
        with Image.open(BytesIO(base64.b64decode(record["image"].split(",", 1)[1]))) as image:
            assert image.format == "JPEG" and max(image.size) <= 220
    assert records[-1] == {"complete": True, "page_count": 3}


def test_thumbnails_validate_password_before_streaming(client, pdf):
    protected = client.post("/api/process/protect", files=[("files", upload(pdf))], data={"options": '{"user_password":"reader-secret"}'})
    for password in ("", "wrong"):
        response = client.post("/api/thumbnails", files={"file": upload(protected.content)}, data={"password": password})
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/json")
        assert "detail" in response.json()
    response = client.post("/api/thumbnails", files={"file": upload(protected.content)}, data={"password": "reader-secret"})
    assert response.status_code == 200
    assert json.loads(response.text.splitlines()[-1])["complete"]


def test_thumbnails_reject_corrupt_or_multiple_files(client, pdf):
    corrupt = client.post("/api/thumbnails", files={"file": upload(b"not a PDF")})
    assert corrupt.status_code == 400 and "detail" in corrupt.json()
    multiple = client.post("/api/thumbnails", files=[("files", upload(pdf)), ("files", upload(pdf))])
    assert multiple.status_code == 400 and "detail" in multiple.json()


@pytest.mark.parametrize("disconnect_before_body", [False, True])
def test_closing_thumbnail_stream_cancels_worker_and_releases_engine_lock(pdf, monkeypatch, disconnect_before_body):
    closed = threading.Event()
    yielded = []
    def many_thumbnails(data, password="", *, cancelled):
        try:
            for page in range(1, 1001):
                if cancelled():
                    return
                yielded.append(page)
                yield {"page": page, "image": "data:image/jpeg;base64,synthetic"}
        finally:
            closed.set()
    monkeypatch.setattr(app_module, "thumbnail_records", many_thumbnails)
    body, headers = multipart_body(pdf)
    async def exercise_disconnect():
        consumed = False
        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # is_disconnected checks are nonblocking, so an idle receive must
            # wait instead of fabricating a browser disconnection.
            await asyncio.Event().wait()
        request = Request({"type": "http", "method": "POST", "path": "/api/thumbnails", "headers": [(b"content-type", headers["Content-Type"].encode())]}, receive)
        endpoint = next(route.endpoint for route in create_app().routes if getattr(route, "path", "") == "/api/thumbnails")
        response = await endpoint(request)
        if disconnect_before_body:
            async def closed_socket(message):
                raise OSError("Synthetic browser disconnect before headers")
            with pytest.raises((ClientDisconnect, OSError)):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, closed_socket)
        else:
            first = await anext(response.body_iterator)
            assert json.loads(first)["page"] == 1
            await response.body_iterator.aclose()
        assert await asyncio.to_thread(closed.wait, 2)
        assert len(yielded) < 1000
        assert await asyncio.to_thread(app_module.ENGINE_LOCK.acquire, True, 2)
        app_module.ENGINE_LOCK.release()
    asyncio.run(exercise_disconnect())
