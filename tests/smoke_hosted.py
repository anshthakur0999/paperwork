"""Verify a running hosted app with generated, non-sensitive documents."""
import argparse
from io import BytesIO
import json
from zipfile import ZipFile

import httpx
import pymupdf as fitz
from PIL import Image


def main(url):
    image_buffer = BytesIO()
    Image.new("RGB", (160, 120), "#9d8bc1").save(image_buffer, format="PNG")
    image = image_buffer.getvalue()
    with fitz.open() as document:
        for number in range(1, 4):
            page = document.new_page(width=300, height=400)
            page.insert_text((30, 50), f"Synthetic page {number}")
            page.insert_image(fitz.Rect(30, 100, 190, 220), stream=image)
        pdf = document.tobytes()

    with httpx.Client(base_url=url.rstrip("/"), timeout=60, headers={"Origin": url.rstrip("/"), "Sec-Fetch-Site": "same-origin"}) as client:
        health = client.get("/api/health")
        assert health.status_code == 200, health.text
        assert health.json()["app"] == "paperwork-hosted"
        page = client.get("/")
        assert "Files are uploaded to the PDF server" in page.text
        assert "25 MB total" in page.text
        assert client.post("/api/shutdown").status_code == 404
        assert client.post("/api/inspect", files={"file": ("synthetic.pdf", pdf)}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/inspect", files={"file": ("synthetic.pdf", pdf)}).json()["page_count"] == 3
        thumbs = client.post("/api/thumbnails", files={"file": ("synthetic.pdf", pdf)})
        assert thumbs.status_code == 200, thumbs.text
        records = [json.loads(line) for line in thumbs.text.splitlines()]
        assert [record["page"] for record in records[:-1]] == [1, 2, 3]
        assert all(record["image"].startswith("data:image/jpeg;base64,") for record in records[:-1])
        assert records[-1] == {"complete": True, "page_count": 3}

        def process(tool, options=None, content=pdf, name="synthetic.pdf", extra_files=()):
            response = client.post(f"/api/process/{tool}", files=[("files", (name, content)), *extra_files], data={"options": json.dumps(options or {})})
            assert response.status_code == 200, f"{tool}: {response.status_code} {response.text[:300]}"
            assert response.headers["cache-control"] == "no-store"
            print(f"Passed {tool}", flush=True)
            return response.content

        deleted = process("delete", {"pages": "1,3"})
        with fitz.open(stream=deleted, filetype="pdf") as document:
            assert len(document) == 1 and "page 2" in document[0].get_text()
        for tool in ("reorder", "extract"):
            with fitz.open(stream=process(tool, {"pages": "3,1"}), filetype="pdf") as document:
                assert len(document) == 2
                assert "page 3" in document[0].get_text() and "page 1" in document[1].get_text()
        with fitz.open(stream=process("merge", extra_files=[("files", ("second.pdf", pdf))]), filetype="pdf") as document:
            assert len(document) == 6
        with fitz.open(stream=process("rotate", {"pages": "2", "angle": 90}), filetype="pdf") as document:
            assert document[1].rotation == 90
        with fitz.open(stream=process("watermark", {"text": "SAMPLE"}), filetype="pdf") as document:
            assert "SAMPLE" in document[0].get_text()
        with fitz.open(stream=process("page-numbers", {"prefix": "Page "}), filetype="pdf") as document:
            assert "Page 1" in document[0].get_text()
        with fitz.open(stream=process("compress", {"level": "balanced"}), filetype="pdf") as document:
            assert len(document) == 3
        with ZipFile(BytesIO(process("split"))) as archive:
            assert len(archive.namelist()) == 3
        with ZipFile(BytesIO(process("extract-images"))) as archive:
            assert len(archive.namelist()) == 1
        with fitz.open(stream=process("images-to-pdf", content=image, name="synthetic.png"), filetype="pdf") as document:
            assert len(document) == 1
        with Image.open(BytesIO(process("pdf-to-images", {"pages": "1", "dpi": 72}))) as rendered:
            assert rendered.size == (300, 400)
        password = "Synthetic-test-42"
        protected = process("protect", {"user_password": password, "allow_print": False, "allow_copy": False, "allow_modify": False})
        with fitz.open(stream=protected, filetype="pdf") as document:
            assert document.needs_pass and document.authenticate(password)
            assert not document.permissions & (fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY | fitz.PDF_PERM_MODIFY)
        with fitz.open(stream=process("unlock", {"password": password}, content=protected), filetype="pdf") as document:
            assert not document.needs_pass and len(document) == 3
        for format_ in ("txt", "md", "json"):
            text = process("extract-text", {"pages": "3,1", "format": format_}).decode("utf-8")
            if format_ == "json":
                assert [page["page"] for page in json.loads(text)["pages"]] == [3, 1]
            else:
                assert text.index("Synthetic page 3") < text.index("Synthetic page 1")
        assert client.get("/api/health").json()["status"] == "ok"
    print("Hosted smoke check passed for all 15 tools.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    main(parser.parse_args().url)
