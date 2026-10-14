"""Functional checks made from synthetic documents, never personal files."""

import base64
from contextlib import contextmanager
from io import BytesIO
import json
import random
import threading
import zipfile

import pymupdf as fitz
from PIL import Image
import pytest

import pdf_engine
from pdf_engine import PDFError, inspect_pdf, operate, preview_pdf, thumbnail_records


def make_pdf(labels=("apple", "banana", "cherry"), image=None):
    document = fitz.open()
    for number, label in enumerate(labels, 1):
        page = document.new_page(width=300, height=400)
        page.insert_text((30, 50), f"Page {number}: {label}", fontsize=16)
        if image is not None:
            page.insert_image(fitz.Rect(20, 90, 280, 370), stream=image)
    document.set_metadata({"title": "Synthetic test document", "author": "Tests"})
    result = document.tobytes()
    document.close()
    return result


def image_bytes(color="red", size=(160, 90), image_format="PNG"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def run(tool, content=None, options=None, files=None):
    inputs = files if files is not None else [("source.pdf", content or make_pdf())]
    result = operate(tool, inputs, options or {})
    assert isinstance(result, tuple) and len(result) == 4
    data, filename, mime, summary = result
    assert isinstance(data, bytes) and data
    assert isinstance(filename, str) and filename
    assert isinstance(mime, str) and mime
    assert summary
    return result


def texts(data, password=""):
    with fitz.open(stream=data, filetype="pdf") as document:
        if document.is_encrypted:
            assert document.authenticate(password)
        return [page.get_text() for page in document]


def parts(data):
    with zipfile.ZipFile(BytesIO(data)) as archive:
        filenames = archive.namelist()
        assert filenames and all(name.endswith(".pdf") for name in filenames)
        return [archive.read(name) for name in filenames]


@pytest.fixture
def sample_pdf():
    return make_pdf()


def test_error_type_and_inspection(sample_pdf):
    assert issubclass(PDFError, ValueError)
    info = inspect_pdf(sample_pdf)
    assert info["page_count"] == 3
    assert info["metadata"]["title"] == "Synthetic test document"
    assert info["encrypted"] is False
    assert info["forms"] == 0
    assert [page["number"] for page in info["pages"]] == [1, 2, 3]
    assert all(page["width"] == 300 and page["height"] == 400 for page in info["pages"])


def test_preview_is_png_and_uses_one_based_pages(sample_pdf):
    first = preview_pdf(sample_pdf, page=1)
    last = preview_pdf(sample_pdf, page=3)
    assert first != last
    with Image.open(BytesIO(first)) as image:
        assert image.format == "PNG"
        assert image.width <= 360 and image.height <= 360
    for invalid_page in (0, 4):
        with pytest.raises(PDFError):
            preview_pdf(sample_pdf, page=invalid_page)


@pytest.mark.parametrize("page_size, expected", [("auto", None), ("a4", (595, 842)), ("letter", (612, 792))])
def test_images_to_pdf(page_size, expected):
    data, filename, mime, _ = run("images-to-pdf", files=[("red.png", image_bytes()), ("blue.jpg", image_bytes("blue", image_format="JPEG"))], options={"page_size": page_size, "margin": 24})
    assert filename.endswith(".pdf") and mime == "application/pdf"
    with fitz.open(stream=data, filetype="pdf") as document:
        assert len(document) == 2
        assert all(len(page.get_images()) >= 1 for page in document)
        if expected:
            assert document[0].rect.width == pytest.approx(expected[0], abs=1)
            assert document[0].rect.height == pytest.approx(expected[1], abs=1)


@pytest.mark.parametrize("image_format", ["png", "jpg"])
def test_pdf_to_images_zip_and_selected_order(sample_pdf, image_format):
    data, _, _, _ = run("pdf-to-images", sample_pdf, {"pages": "3,1", "dpi": 72, "format": image_format})
    with zipfile.ZipFile(BytesIO(data)) as archive:
        assert len(archive.namelist()) == 2
        for name in archive.namelist():
            with Image.open(BytesIO(archive.read(name))) as rendered:
                assert rendered.size == (300, 400)
                assert rendered.format == ("PNG" if image_format == "png" else "JPEG")


def test_pdf_to_single_image_download(sample_pdf):
    data, filename, mime, _ = run("pdf-to-images", sample_pdf, {"pages": "2", "format": "png", "dpi": 72})
    assert filename.endswith(".png") and mime == "image/png"
    with Image.open(BytesIO(data)) as image:
        assert image.size == (300, 400)


def test_images_to_landscape_page_and_multi_frame_tiff():
    data, _, _, _ = run("images-to-pdf", files=[("image.png", image_bytes())], options={"page_size": "a4", "orientation": "landscape"})
    with fitz.open(stream=data, filetype="pdf") as document:
        assert document[0].rect.width > document[0].rect.height
    frames = BytesIO()
    Image.new("RGB", (80, 100), "red").save(frames, format="TIFF", save_all=True, append_images=[Image.new("RGB", (80, 100), "blue")])
    data, _, _, _ = run("images-to-pdf", files=[("two-frames.tiff", frames.getvalue())])
    assert len(texts(data)) == 2


def test_merge_follows_upload_order():
    data, _, _, _ = run("merge", files=[("second.pdf", make_pdf(("second",))), ("first.pdf", make_pdf(("first",)))])
    assert "second" in texts(data)[0]
    assert "first" in texts(data)[1]


@pytest.mark.parametrize("options, expected_labels", [
    ({"mode": "each"}, [["apple"], ["banana"], ["cherry"]]),
    ({"mode": "chunks", "chunk_size": 2}, [["apple", "banana"], ["cherry"]]),
    ({"mode": "ranges", "ranges": "3;1-2"}, [["cherry"], ["apple", "banana"]]),
])
def test_split_modes(sample_pdf, options, expected_labels):
    data, _, _, _ = run("split", sample_pdf, options)
    split_documents = parts(data)
    assert len(split_documents) == len(expected_labels)
    for document_data, expected in zip(split_documents, expected_labels):
        extracted = texts(document_data)
        assert len(extracted) == len(expected)
        assert all(label in text for label, text in zip(expected, extracted))


@pytest.mark.parametrize("tool, pages, expected", [
    ("extract", "3,1", ["cherry", "apple"]),
    ("reorder", "3,1,3,2", ["cherry", "apple", "cherry", "banana"]),
    ("delete", "2", ["apple", "cherry"]),
])
def test_page_operations_preserve_requested_order(sample_pdf, tool, pages, expected):
    data, _, _, _ = run(tool, sample_pdf, {"pages": pages})
    output = texts(data)
    assert len(output) == len(expected)
    assert all(label in text for label, text in zip(expected, output))


@pytest.mark.parametrize("pages", ["all", "1-3"])
def test_cannot_delete_every_page(sample_pdf, pages):
    with pytest.raises(PDFError):
        run("delete", sample_pdf, {"pages": pages})


@pytest.mark.parametrize("pages", ["0", "4", "-1", "two", "3-1", "1,1"])
def test_invalid_page_selection_is_a_friendly_error(sample_pdf, pages):
    with pytest.raises(PDFError):
        run("extract", sample_pdf, {"pages": pages})


def test_rotate_only_selected_pages(sample_pdf):
    data, _, _, _ = run("rotate", sample_pdf, {"pages": "2", "angle": 90})
    with fitz.open(stream=data, filetype="pdf") as document:
        assert [page.rotation for page in document] == [0, 90, 0]
    assert "banana" in texts(data)[1]


def test_lossless_compression_preserves_text(sample_pdf):
    data, _, _, _ = run("compress", sample_pdf, {"level": "lossless"})
    assert texts(data) == texts(sample_pdf)


def test_strong_compression_reduces_image_data_and_preserves_text():
    rng = random.Random(7)
    image = Image.frombytes("RGB", (1400, 1400), rng.randbytes(1400 * 1400 * 3))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    original = make_pdf(("searchable text survives",), image=buffer.getvalue())
    data, _, _, _ = run("compress", original, {"level": "strong"})
    assert len(data) < len(original) * 0.8
    assert "searchable text survives" in texts(data)[0]
    with fitz.open(stream=data, filetype="pdf") as document:
        assert document[0].get_images()


def test_watermark_and_page_number_text(sample_pdf):
    marked, _, _, _ = run("watermark", sample_pdf, {"pages": "1,3", "text": "PRIVATE COPY", "position": "top", "font_size": 18, "opacity": 0.4, "color": "#123456"})
    result = texts(marked)
    assert "PRIVATE COPY" in result[0] and "PRIVATE COPY" in result[2]
    assert "PRIVATE COPY" not in result[1]
    numbered, _, _, _ = run("page-numbers", sample_pdf, {"start": 7, "prefix": "Sheet ", "suffix": " end", "position": "right"})
    for number, text in enumerate(texts(numbered), 7):
        assert f"Sheet {number} end" in text


def test_protect_unlock_and_wrong_password(sample_pdf):
    protected, _, _, _ = run("protect", sample_pdf, {"user_password": "reader-secret", "owner_password": "owner-secret", "allow_print": True, "allow_copy": False, "allow_modify": False})
    with fitz.open(stream=protected, filetype="pdf") as document:
        assert document.is_encrypted
        assert document.authenticate("reader-secret")
        assert "AES" in document.metadata.get("encryption", "")
        assert document.permissions & fitz.PDF_PERM_PRINT
        assert not (document.permissions & fitz.PDF_PERM_COPY)
        assert not (document.permissions & fitz.PDF_PERM_MODIFY)
    info = inspect_pdf(protected)
    assert info["encrypted"] and info["password_required"]
    assert info["pages"] == []
    with pytest.raises(PDFError):
        inspect_pdf(protected, password="wrong")
    with pytest.raises(PDFError):
        run("unlock", protected, {"password": "wrong"})
    unlocked, _, _, _ = run("unlock", protected, {"password": "reader-secret"})
    assert texts(unlocked) == texts(sample_pdf)
    with fitz.open(stream=unlocked, filetype="pdf") as document:
        assert not document.is_encrypted


def test_protect_defaults_do_not_require_owner_password(sample_pdf):
    protected, _, _, _ = run("protect", sample_pdf, {"user_password": "reader-secret"})
    assert texts(protected, "reader-secret") == texts(sample_pdf)


def test_editing_encrypted_pdf_preserves_password(sample_pdf):
    protected, _, _, _ = run("protect", sample_pdf, {"user_password": "reader-secret", "owner_password": "owner-secret"})
    edited, _, _, _ = run("rotate", protected, {"password": "reader-secret", "pages": "2", "angle": 90})
    with fitz.open(stream=edited, filetype="pdf") as document:
        assert document.is_encrypted
        assert document.authenticate("reader-secret")
        assert document[1].rotation == 90


def test_split_protected_pdf_informs_about_unencrypted_outputs(sample_pdf):
    protected, _, _, _ = run("protect", sample_pdf, {"user_password": "reader-secret", "owner_password": "owner-secret"})
    output, _, _, summary = run("split", protected, {"password": "reader-secret", "mode": "each"})
    assert any("not password protected" in warning.lower() for warning in summary["warnings"])
    for part in parts(output):
        with fitz.open(stream=part, filetype="pdf") as document:
            assert not document.is_encrypted


@pytest.mark.parametrize("output_format", ["txt", "json"])
def test_extract_text(sample_pdf, output_format):
    data, filename, _, _ = run("extract-text", sample_pdf, {"format": output_format, "pages": "2"})
    decoded = data.decode("utf-8-sig")
    assert "banana" in decoded and "apple" not in decoded and "cherry" not in decoded
    assert filename.endswith(f".{output_format}")
    if output_format == "json":
        assert json.loads(decoded)


def test_extract_embedded_images():
    content = make_pdf(("image page",), image=image_bytes("green", (80, 100)))
    data, _, _, _ = run("extract-images", content)
    with zipfile.ZipFile(BytesIO(data)) as archive:
        assert len(archive.namelist()) == 1
        with Image.open(BytesIO(archive.read(archive.namelist()[0]))) as output:
            assert output.size == (80, 100)


def test_crop_changes_selected_page_dimensions(sample_pdf):
    data, _, _, _ = run("crop", sample_pdf, {"pages": "2", "left": 10, "right": 20, "top": 30, "bottom": 40})
    with fitz.open(stream=data, filetype="pdf") as document:
        assert document[1].rect.width == 270
        assert document[1].rect.height == 330
        assert document[0].rect.width == 300 and document[2].rect.height == 400


def test_metadata_updates(sample_pdf):
    expected = {"title": "Changed title", "author": "New author", "subject": "Local only", "keywords": "offline,private", "creator": "Local PDF"}
    data, _, _, _ = run("metadata", sample_pdf, expected)
    assert all(inspect_pdf(data)["metadata"][key] == value for key, value in expected.items())
    assert texts(data) == texts(sample_pdf)


def test_flatten_form_retains_visible_value():
    with fitz.open() as document:
        page = document.new_page(width=300, height=400)
        widget = fitz.Widget()
        widget.field_name = "Synthetic field"
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.field_value = "Completed field"
        widget.rect = fitz.Rect(30, 50, 260, 80)
        widget.text_fontsize = 14
        page.add_widget(widget)
        source = document.tobytes()
    assert inspect_pdf(source)["forms"] == 1
    data, _, _, _ = run("flatten", source)
    assert inspect_pdf(data)["forms"] == 0
    assert "Completed field" in texts(data)[0]


@pytest.mark.parametrize("tool, options", [("page-numbers", {"prefix": "Sheet ", "start": 7}), ("watermark", {"text": "PRIVATE COPY", "position": "top", "font_size": 18})])
def test_overlays_preserve_cropped_rotated_forms_through_flattening(tool, options):
    expected_values = ["Synthetic applicant", "Approval complete"]
    expected_crop = (30, 40, 550, 720)
    with fitz.open() as document:
        page = document.new_page(width=600, height=800)
        for number, value in enumerate(expected_values):
            widget = fitz.Widget()
            widget.field_name = f"Synthetic field {number}"
            widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
            widget.field_value = value
            widget.rect = fitz.Rect(60, 100 + number * 80, 360, 140 + number * 80)
            widget.text_fontsize = 14
            page.add_widget(widget)
        page.set_cropbox(fitz.Rect(expected_crop))
        page.set_rotation(90)
        original = document.tobytes()
    overlaid, _, _, _ = run(tool, original, options)
    with fitz.open(stream=overlaid, filetype="pdf") as document:
        page = document[0]
        assert page.rotation == 90
        assert (page.rect.width, page.rect.height) == (680, 520)
        assert tuple(page.cropbox) == expected_crop
        assert [widget.field_value for widget in page.widgets()] == expected_values
    flattened, _, _, _ = run("flatten", overlaid)
    with fitz.open(stream=flattened, filetype="pdf") as document:
        page = document[0]
        assert page.rotation == 90
        assert (page.rect.width, page.rect.height) == (680, 520)
        assert tuple(page.cropbox) == expected_crop
        assert list(page.widgets() or []) == []
        assert all(value in page.get_text() for value in expected_values)


@pytest.mark.parametrize("content", [b"", b"not a pdf", image_bytes()])
def test_corrupt_document_rejected(content):
    with pytest.raises(PDFError):
        inspect_pdf(content)


def test_invalid_tool_and_numeric_options_are_friendly_errors(sample_pdf):
    with pytest.raises(PDFError):
        operate("not-a-tool", [("source.pdf", sample_pdf)], {})
    for tool, options in [("rotate", {"angle": 45}), ("pdf-to-images", {"dpi": "bad"}), ("crop", {"left": 300}), ("watermark", {"opacity": 9})]:
        with pytest.raises(PDFError):
            operate(tool, [("source.pdf", sample_pdf)], options)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, [72], 1000000])
def test_render_options_reject_non_finite_or_unreasonable_values(sample_pdf, value):
    with pytest.raises(PDFError):
        run("pdf-to-images", sample_pdf, {"dpi": value})


def test_embedded_image_extraction_deduplicates_shared_image_objects():
    source = make_pdf(image=image_bytes())
    output, _, _, _ = run("extract-images", source)
    with zipfile.ZipFile(BytesIO(output)) as archive:
        assert len(archive.namelist()) == 1


def test_thumbnails_open_pdf_once_and_emit_ordered_bounded_jpegs(sample_pdf, monkeypatch):
    opens = []
    original = pdf_engine._open_pdf
    @contextmanager
    def counted_open(*args, **kwargs):
        opens.append(1)
        with original(*args, **kwargs) as document:
            yield document
    monkeypatch.setattr(pdf_engine, "_open_pdf", counted_open)
    records = list(thumbnail_records(sample_pdf))
    assert len(opens) == 1
    assert [record["page"] for record in records[:-1]] == [1, 2, 3]
    assert records[-1] == {"complete": True, "page_count": 3}
    images = []
    for record in records[:-1]:
        assert record["image"].startswith("data:image/jpeg;base64,")
        image_data = base64.b64decode(record["image"].split(",", 1)[1])
        images.append(image_data)
        with Image.open(BytesIO(image_data)) as image:
            assert image.format == "JPEG"
            assert image.size == (record["width"], record["height"])
            assert max(image.size) <= 220
    assert len(set(images)) == 3


def test_thumbnails_cancel_between_pages_and_close_document(sample_pdf, monkeypatch):
    stopped = threading.Event()
    closed = []
    original = pdf_engine._open_pdf
    @contextmanager
    def tracked_open(*args, **kwargs):
        try:
            with original(*args, **kwargs) as document:
                yield document
        finally:
            closed.append(True)
    monkeypatch.setattr(pdf_engine, "_open_pdf", tracked_open)
    records = thumbnail_records(sample_pdf, cancelled=stopped.is_set)
    assert next(records)["page"] == 1
    stopped.set()
    assert list(records) == []
    assert closed == [True]


def test_thumbnails_contain_page_errors_and_continue(sample_pdf, monkeypatch):
    original = fitz.Page.get_pixmap
    def damaged_second_page(page, *args, **kwargs):
        if page.number == 1:
            raise RuntimeError("Internal parser detail with document content")
        return original(page, *args, **kwargs)
    monkeypatch.setattr(fitz.Page, "get_pixmap", damaged_second_page)
    records = list(thumbnail_records(sample_pdf))
    assert records[0]["image"] and records[2]["image"]
    assert records[1] == {"page": 2, "error": "This page could not be previewed."}
    assert records[-1]["complete"]


def test_thumbnails_obey_encoded_output_limit(sample_pdf, monkeypatch):
    monkeypatch.setattr(pdf_engine, "MAX_THUMBNAIL_BYTES", 1)
    with pytest.raises(PDFError, match="too large"):
        list(thumbnail_records(sample_pdf))


def test_thumbnails_require_password_for_encrypted_pdf(sample_pdf):
    protected, _, _, _ = run("protect", sample_pdf, {"user_password": "reader-secret"})
    with pytest.raises(PDFError):
        list(thumbnail_records(protected))
    with pytest.raises(PDFError):
        list(thumbnail_records(protected, "wrong"))
    assert list(thumbnail_records(protected, "reader-secret"))[-1]["page_count"] == 3


def test_full_visual_reorder_accepts_page_list_longer_than_2000_characters():
    count = 620
    source = make_pdf(tuple(f"leaf-{number}" for number in range(1, count + 1)))
    selection = ",".join(str(number) for number in range(count, 0, -1))
    assert len(selection) > 2000
    reordered, _, _, _ = run("reorder", source, {"pages": selection})
    with fitz.open(stream=reordered, filetype="pdf") as document:
        assert len(document) == count
        assert f"leaf-{count}" in document[0].get_text()
        assert "leaf-1" in document[-1].get_text()
