"""Check the actual running Windows bundle using synthetic input only."""
import hashlib
import argparse
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import httpx
import pymupdf
from PIL import Image


def main(port=8765):
    root = Path(__file__).resolve().parents[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
        assert client.get("/api/health").json()["app"] == "paperwork-local"
        page = client.get("/")
        assert page.status_code == 200
        assert '<style id="app-styles">' in page.text
        assert page.text.count('class="tool-card" data-tool=') == 15
        assert 'id="tool-catalog"' in page.text
        for asset in ("styles.css", "app.js", "tools.json"):
            response = client.get(f"/static/{asset}")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).digest() == hashlib.sha256((root / "static" / asset).read_bytes()).digest()

        def process(tool, content, options=None, name="sample.pdf"):
            response = client.post(f"/api/process/{tool}", files={"files": (name, content)}, data={"options": json.dumps(options or {})})
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            return response.content

        pdf = (root / "tmp/browser-qa/sample-three-pages.pdf").read_bytes()
        thumbnails = client.post("/api/thumbnails", files={"file": ("sample.pdf", pdf)})
        assert thumbnails.status_code == 200, thumbnails.text
        records = [json.loads(line) for line in thumbnails.text.splitlines()]
        assert [record["page"] for record in records[:-1]] == [1, 2, 3]
        assert all(record["image"].startswith("data:image/jpeg;base64,") for record in records[:-1])
        assert records[-1] == {"complete": True, "page_count": 3}
        reordered = process("reorder", pdf, {"pages": "3,1,2"})
        with pymupdf.open(stream=pdf, filetype="pdf") as original, pymupdf.open(stream=reordered, filetype="pdf") as result:
            assert [page.get_text() for page in result] == [original[index].get_text() for index in (2, 0, 1)]
        deleted = process("delete", pdf, {"pages": "1,3"})
        with pymupdf.open(stream=pdf, filetype="pdf") as original, pymupdf.open(stream=deleted, filetype="pdf") as result:
            assert len(result) == 1 and result[0].get_text() == original[1].get_text()
        png = (root / "tmp/browser-qa/sample-image.png").read_bytes()
        converted = process("images-to-pdf", png, {"page_size": "auto", "margin": 0}, "image.png")
        with pymupdf.open(stream=converted, filetype="pdf") as document:
            assert len(document) == 1 and document[0].rect == pymupdf.Rect(0, 0, 600, 450)
        rendered = process("pdf-to-images", pdf, {"pages": "2", "dpi": 72})
        with Image.open(BytesIO(rendered)) as image:
            assert image.size == (595, 842)
        split = process("split", pdf)
        with ZipFile(BytesIO(split)) as archive:
            assert len(archive.namelist()) == 3
        for text_format in ("txt", "md", "json"):
            extracted = process("extract-text", pdf, {"pages": "3,1", "format": text_format})
            text = extracted.decode("utf-8")
            assert text.strip()
            if text_format == "json":
                assert [item["page"] for item in json.loads(text)["pages"]] == [3,1]
            elif text_format == "md":
                assert text.index("## Page 3") < text.index("## Page 1")
            else:
                assert text.index("--- Page 3 ---") < text.index("--- Page 1 ---")
        compressed = process("compress", pdf, {"level": "lossless"})
        assert len(compressed) <= len(pdf)
        protected = process("protect", pdf, {"user_password": "Synthetic-test-42"})
        with pymupdf.open(stream=protected, filetype="pdf") as document:
            assert document.needs_pass and document.authenticate("Synthetic-test-42")
        unlocked = process("unlock", protected, {"password": "Synthetic-test-42"})
        with pymupdf.open(stream=unlocked, filetype="pdf") as document:
            assert not document.needs_pass and len(document) == 3
    print("Packaged app passed: bundled assets, streaming thumbnails, page ordering and deletion, conversion, splitting, TXT/Markdown/JSON text extraction, compression, AES protection and unlock.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify the running Paperwork Windows bundle.")
    parser.add_argument("--port",type=int,default=8765)
    main(parser.parse_args().port)
