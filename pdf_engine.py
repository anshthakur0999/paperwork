"""In-memory PDF operations for the local PDF workbench.

There are deliberately no filesystem or network operations in this module.
Coordinates, page dimensions and margins are PDF points (72 points = 1 inch).
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import secrets
import warnings
import zipfile
from contextlib import contextmanager
from pathlib import PurePath
from typing import Callable, Iterator

import pymupdf as fitz
from PIL import Image, ImageOps, UnidentifiedImageError


class PDFError(ValueError):
    """A safe, actionable validation or document-processing error."""


MAX_FILES = 100
HOSTED = os.environ.get("PAPERWORK_MODE") == "hosted"
MAX_INPUT_BYTES = int(os.environ.get("PAPERWORK_MAX_UPLOAD_MB", "25" if HOSTED else "200")) * 1024 * 1024
MAX_OUTPUT_BYTES = int(os.environ.get("PAPERWORK_MAX_OUTPUT_MB", "64" if HOSTED else "512")) * 1024 * 1024
MAX_PAGES = 3000
MAX_IMAGE_PIXELS = 40_000_000
MAX_RENDER_PIXELS = 6_000_000 if HOSTED else 24_000_000
MAX_TOTAL_RENDER_PIXELS = 36_000_000 if HOSTED else 180_000_000
MAX_RENDER_PAGES = 500
THUMBNAIL_EDGE = 220
MAX_TEXT_PAGES = 500
MAX_TEXT_BYTES = 10 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 128 * 1024 * 1024
PDF_MEDIA = "application/pdf"
ZIP_MEDIA = "application/zip"
TOOLS = frozenset({
    "images-to-pdf", "pdf-to-images", "merge", "split", "extract", "reorder",
    "delete", "rotate", "compress", "watermark", "page-numbers", "protect",
    "unlock", "extract-text", "extract-images", "crop", "metadata", "flatten",
})


def _stem(filename: str) -> str:
    # Both slash styles are untrusted upload names, regardless of host OS.
    leaf = re.split(r"[/\\]", str(filename))[-1]
    stem = PurePath(leaf).stem
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-")[:80]
    if not stem or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", stem):
        return "document"
    return stem


def _text(options: dict, key: str, default: str = "", *, limit: int = 2000) -> str:
    value = options.get(key, default)
    if not isinstance(value, str):
        raise PDFError(f"{key.replace('_', ' ').capitalize()} must be text.")
    if len(value) > limit or "\x00" in value:
        raise PDFError(f"{key.replace('_', ' ').capitalize()} is too long or invalid.")
    return value


def _number(options: dict, key: str, default: float, minimum: float,
            maximum: float, *, integer: bool = False) -> float | int:
    value = options.get(key, default)
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PDFError(f"{key.replace('_', ' ').capitalize()} must be a number.") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise PDFError(f"{key.replace('_', ' ').capitalize()} must be between {minimum:g} and {maximum:g}.")
    if integer and result != int(result):
        raise PDFError(f"{key.replace('_', ' ').capitalize()} must be a whole number.")
    return int(result) if integer else result


def _choice(options: dict, key: str, default: str, values: tuple[str, ...]) -> str:
    value = _text(options, key, default).strip().lower()
    if value not in values:
        raise PDFError(f"Choose a valid {key.replace('_', ' ')}: {', '.join(values)}.")
    return value


def _boolean(options: dict, key: str, default: bool) -> bool:
    value = options.get(key, default)
    if isinstance(value, bool):
        return value
    if value in ("true", "1", 1):
        return True
    if value in ("false", "0", 0):
        return False
    raise PDFError(f"{key.replace('_', ' ').capitalize()} must be true or false.")


def parse_pages(value: str, count: int, *, duplicates: bool = False) -> list[int]:
    """Parse one-based ordered ranges into zero-based indexes."""
    if not isinstance(value, str):
        raise PDFError("Page selection must be text such as 1,3-5.")
    if len(value) > 20_000:
        raise PDFError("Page selection is too long.")
    value = value.strip().lower()
    if value in ("", "all"):
        return list(range(count))
    result: list[int] = []
    seen: set[int] = set()
    for part in value.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise PDFError("Use page numbers and ranges such as 1,3-5.")
        if len(match[1]) > 10 or (match[2] and len(match[2]) > 10):
            raise PDFError(f"Page numbers must be between 1 and {count}.")
        first = int(match[1])
        last = int(match[2] or match[1])
        if first < 1 or last > count:
            raise PDFError(f"Page numbers must be between 1 and {count}.")
        if last < first:
            raise PDFError("Ranges must run forwards. Use 5,4,3 to reverse page order.")
        for page in range(first - 1, last):
            if page in seen and not duplicates:
                raise PDFError("A page was selected more than once. Repeated pages are allowed only when reordering.")
            result.append(page)
            seen.add(page)
            if len(result) > MAX_PAGES:
                raise PDFError(f"The result cannot contain more than {MAX_PAGES} pages.")
    if not result:
        raise PDFError("Select at least one page.")
    return result


@contextmanager
def _open_pdf(data: bytes, password: str = "", *, allow_locked: bool = False) -> Iterator[fitz.Document]:
    if not isinstance(data, bytes) or not data:
        raise PDFError("The PDF is empty.")
    if len(data) > MAX_INPUT_BYTES:
        raise PDFError(f"This PDF is too large. The limit is {MAX_INPUT_BYTES // (1024 * 1024)} MB.")
    document = None
    try:
        try:
            document = fitz.open(stream=data, filetype="pdf")
        except (ValueError, RuntimeError) as exc:
            raise PDFError("This file could not be read as a PDF. It may be damaged or use an unsupported format.") from exc
        if not document.is_pdf:
            raise PDFError("Please choose a PDF file.")
        if document.needs_pass:
            if password:
                if not document.authenticate(password):
                    raise PDFError("The PDF password is incorrect.")
            elif not allow_locked:
                raise PDFError("This PDF is password protected. Enter its password first.")
        # Encrypted object streams can hide the page tree until authentication.
        # Do not force MuPDF to parse that inaccessible tree for a locked preview.
        if allow_locked and document.is_encrypted:
            yield document
            return
        if document.page_count == 0:
            raise PDFError("This PDF contains no pages.")
        if document.page_count > MAX_PAGES:
            raise PDFError(f"PDFs may contain up to {MAX_PAGES} pages in this tool.")
        yield document
    finally:
        if document is not None:
            document.close()


def inspect_pdf(data: bytes, password: str = "") -> dict:
    """Inspect pages and fields, returning a small locked-PDF response if needed."""
    if not isinstance(password, str) or len(password) > 1000:
        raise PDFError("Enter a valid PDF password.")
    try:
        with _open_pdf(data, password, allow_locked=True) as document:
            locked = bool(document.is_encrypted)
            encrypted = locked or bool(document.needs_pass or (document.metadata or {}).get("encryption"))
            result = {
                "page_count": 0 if locked else document.page_count,
                "metadata": {} if locked else dict(document.metadata or {}),
                "pages": [],
                "encrypted": encrypted,
                "password_required": locked,
                "forms": 0,
            }
            if not locked:
                result["pages"] = [
                    {"number": index + 1, "width": round(page.rect.width, 2),
                     "height": round(page.rect.height, 2), "rotation": page.rotation}
                    for index, page in enumerate(document)
                ]
                result["forms"] = sum(1 for page in document for _ in (page.widgets() or []))
            return result
    except PDFError:
        raise
    except (ValueError, RuntimeError, TypeError) as exc:
        raise PDFError("This PDF could not be inspected. It may be damaged.") from exc


def _render_pixels(page: fitz.Page, scale: float) -> int:
    width, height = page.rect.width, page.rect.height
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise PDFError("A page has invalid dimensions.")
    return math.ceil(width * scale) * math.ceil(height * scale)


def preview_pdf(data: bytes, password: str = "", page: int = 1) -> bytes:
    try:
        with _open_pdf(data, password) as document:
            if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= document.page_count:
                raise PDFError(f"Page must be between 1 and {document.page_count}.")
            source = document[page - 1]
            dimension = max(source.rect.width, source.rect.height)
            if not math.isfinite(dimension) or dimension <= 0:
                raise PDFError("This page has invalid dimensions.")
            scale = min(1.0, 360 / dimension)
            _render_pixels(source, scale)
            return source.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False,
                                     colorspace=fitz.csRGB).tobytes("png")
    except PDFError:
        raise
    except (ValueError, RuntimeError, TypeError) as exc:
        raise PDFError("This page could not be previewed. It may be damaged.") from exc


def thumbnail_records(data: bytes, password: str = "", *,
                      cancelled: Callable[[], bool] | None = None) -> Iterator[dict]:
    """Render thumbnails lazily from one opened PDF, without retaining images.

    The caller must consume and close this generator in one worker while holding
    the engine lock. Cancellation is cooperative between individual page renders.
    """
    if not isinstance(password, str) or len(password) > 1000:
        raise PDFError("Enter a valid PDF password.")
    should_stop = cancelled or (lambda: False)
    if should_stop():
        return
    encoded_bytes = 0
    try:
        with _open_pdf(data, password) as document:
            for index in range(document.page_count):
                if should_stop():
                    return
                try:
                    page = document[index]
                    dimension = max(page.rect.width, page.rect.height)
                    if not math.isfinite(dimension) or dimension <= 0:
                        raise PDFError("This page has invalid dimensions.")
                    scale = min(1.0, THUMBNAIL_EDGE / dimension)
                    if _render_pixels(page, scale) > THUMBNAIL_EDGE ** 2:
                        raise PDFError("This thumbnail would use too much memory.")
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale),
                                            alpha=False, colorspace=fitz.csRGB)
                    encoded = base64.b64encode(pixmap.tobytes("jpeg", jpg_quality=70)).decode("ascii")
                    record = {"page": index + 1, "image": "data:image/jpeg;base64," + encoded,
                              "width": pixmap.width, "height": pixmap.height}
                    del pixmap
                except (PDFError, ValueError, RuntimeError, TypeError, OverflowError):
                    # One damaged page should not prevent reordering the others.
                    yield {"page": index + 1, "error": "This page could not be previewed."}
                    continue
                encoded_bytes += len(encoded)
                if encoded_bytes > MAX_THUMBNAIL_BYTES:
                    raise PDFError("The thumbnail output is too large. Use the page-number order for this PDF.")
                if should_stop():
                    return
                yield record
            if not should_stop():
                yield {"complete": True, "page_count": document.page_count}
    except PDFError:
        raise
    except (ValueError, RuntimeError, TypeError, OverflowError) as exc:
        raise PDFError("This PDF could not be previewed. It may be damaged.") from exc


def _save(document: fitz.Document, *, keep_encryption: bool = True, **kwargs) -> bytes:
    data = document.tobytes(garbage=4, deflate=True, deflate_images=True,
                            deflate_fonts=True, use_objstms=1,
                            encryption=fitz.PDF_ENCRYPT_KEEP if keep_encryption else fitz.PDF_ENCRYPT_NONE,
                            **kwargs)
    if len(data) > MAX_OUTPUT_BYTES:
        raise PDFError("The result is too large. Process fewer pages or files at a time.")
    return data


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    if sum(len(data) for _, data in entries) > MAX_OUTPUT_BYTES:
        raise PDFError("The result is too large. Process fewer pages or files at a time.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for filename, data in entries:
            archive.writestr(filename, data)
    return buffer.getvalue()


def _base_summary(tool: str, files: list[tuple[str, bytes]]) -> dict:
    return {"tool": tool, "input_bytes": sum(len(data) for _, data in files), "warnings": []}


def _finish(data: bytes, filename: str, media: str, summary: dict) -> tuple[bytes, str, str, dict]:
    if len(data) > MAX_OUTPUT_BYTES:
        raise PDFError("The result is too large. Process fewer pages or files at a time.")
    summary["output_bytes"] = len(data)
    return data, filename, media, summary


def _signed_warning(document: fitz.Document, summary: dict) -> None:
    if document.get_sigflags() > 0:
        summary["warnings"].append("Saving a modified PDF invalidates existing digital signatures.")


def _new_output_warning(document: fitz.Document, summary: dict) -> None:
    if document.needs_pass or (document.metadata or {}).get("encryption"):
        warning = "New combined or split PDFs are not password protected. Use Protect PDF on the result if needed."
        if warning not in summary["warnings"]:
            summary["warnings"].append(warning)


def _set_copied_metadata(target: fitz.Document, source: fitz.Document) -> None:
    metadata = {key: value for key, value in (source.metadata or {}).items()
                if key in {"title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate", "trapped"}}
    target.set_metadata(metadata)


def _images_to_pdf(files: list[tuple[str, bytes]], options: dict, summary: dict):
    page_size = _choice(options, "page_size", "auto", ("auto", "a4", "letter"))
    orientation = _choice(options, "orientation", "portrait", ("portrait", "landscape"))
    margin = _number(options, "margin", 24, 0, 144)
    with fitz.open() as output:
        for filename, data in files:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as original:
                        if original.format not in {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF"}:
                            raise PDFError(f"{_stem(filename)} is not a supported image (PNG, JPEG, WebP, BMP, TIFF or GIF).")
                        frame_count = getattr(original, "n_frames", 1)
                        if frame_count + output.page_count > MAX_PAGES:
                            raise PDFError(f"The result cannot contain more than {MAX_PAGES} pages.")
                        for frame_index in range(frame_count):
                            original.seek(frame_index)
                            if original.width * original.height > MAX_IMAGE_PIXELS:
                                raise PDFError("An image exceeds the 40-megapixel limit. Resize it first.")
                            image = ImageOps.exif_transpose(original.copy()).convert("RGBA")
                            white = Image.new("RGB", image.size, "white")
                            white.paste(image, mask=image.getchannel("A"))
                            if page_size == "auto":
                                # Natural dimensions at 96 dpi; keep excessively large sheets usable.
                                width, height = white.width * 0.75, white.height * 0.75
                                shrink = min(1.0, 2000 / max(width, height))
                                width, height = width * shrink + margin * 2, height * shrink + margin * 2
                            else:
                                width, height = fitz.paper_size(page_size)
                                if orientation == "landscape":
                                    width, height = height, width
                            if margin * 2 >= min(width, height):
                                raise PDFError("Margins leave no room for the image. Reduce the margin.")
                            page = output.new_page(width=width, height=height)
                            image_bytes = io.BytesIO()
                            # JPEG sources remain JPEG to avoid large lossless transcodes.
                            if original.format == "JPEG":
                                white.save(image_bytes, format="JPEG", quality=95, optimize=True)
                            else:
                                white.save(image_bytes, format="PNG", optimize=True)
                            page.insert_image(fitz.Rect(margin, margin, width - margin, height - margin),
                                              stream=image_bytes.getvalue(), keep_proportion=True)
            except PDFError:
                raise
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning) as exc:
                raise PDFError(f"The image {_stem(filename)} could not be read. It may be damaged or too large.") from exc
        summary.update(pages=output.page_count, count=output.page_count)
        return _finish(_save(output, keep_encryption=False), "images.pdf", PDF_MEDIA, summary)


def _pdf_to_images(document: fitz.Document, stem: str, options: dict, summary: dict):
    indexes = parse_pages(_text(options, "pages", "all", limit=20_000), document.page_count)
    format_ = _choice(options, "format", "png", ("png", "jpg", "jpeg"))
    extension = "jpg" if format_ == "jpeg" else format_
    dpi = _number(options, "dpi", 144, 36, 300, integer=True)
    scale = dpi / 72
    if len(indexes) > MAX_RENDER_PAGES:
        raise PDFError(f"Convert up to {MAX_RENDER_PAGES} pages at a time.")
    pixel_counts = [_render_pixels(document[index], scale) for index in indexes]
    if any(count > MAX_RENDER_PIXELS for count in pixel_counts):
        raise PDFError("A rendered page would exceed 24 megapixels. Lower the DPI.")
    if sum(pixel_counts) > MAX_TOTAL_RENDER_PIXELS:
        raise PDFError("The selected pages would use too much memory. Select fewer pages or lower the DPI.")
    entries = []
    total_bytes = 0
    for index in indexes:
        page = document[index]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
        pixmap.set_dpi(dpi, dpi)
        data = pixmap.tobytes("png" if extension == "png" else "jpeg", jpg_quality=90)
        total_bytes += len(data)
        if total_bytes > MAX_OUTPUT_BYTES:
            raise PDFError("The images are too large. Select fewer pages or lower the DPI.")
        entries.append((f"{stem}-page-{index + 1:04d}.{extension}", data))
    summary.update(pages=len(indexes), count=len(entries), dpi=dpi)
    if len(entries) == 1:
        name, data = entries[0]
        return _finish(data, name, "image/png" if extension == "png" else "image/jpeg", summary)
    return _finish(_zip(entries), f"{stem}-images.zip", ZIP_MEDIA, summary)


def _split(document: fitz.Document, stem: str, options: dict, summary: dict):
    mode = _choice(options, "mode", "each", ("each", "chunks", "ranges"))
    if mode == "ranges":
        ranges = _text(options, "ranges", limit=20_000)
        if not ranges.strip():
            raise PDFError("Enter groups of pages separated by semicolons, such as 1-3;4-6.")
        groups = [parse_pages(group, document.page_count) for group in ranges.split(";")]
        if any(not group.strip() for group in ranges.split(";")):
            raise PDFError("Each split range must contain page numbers.")
    else:
        size = 1 if mode == "each" else _number(options, "chunk_size", 1, 1, MAX_PAGES, integer=True)
        groups = [list(range(first, min(first + size, document.page_count)))
                  for first in range(0, document.page_count, size)]
    if len(groups) > MAX_PAGES or sum(len(group) for group in groups) > MAX_PAGES:
        raise PDFError(f"A split operation may output up to {MAX_PAGES} pages in total.")
    _new_output_warning(document, summary)
    summary["warnings"].append("Split outputs preserve page content and form fields; document-level bookmarks, attachments and layers may not be retained.")
    entries = []
    total = 0
    for number, indexes in enumerate(groups, 1):
        with fitz.open() as output:
            for position, index in enumerate(indexes):
                output.insert_pdf(document, from_page=index, to_page=index,
                                  final=position == len(indexes) - 1, widgets=True)
            _set_copied_metadata(output, document)
            data = _save(output, keep_encryption=False)
            total += len(data)
            if total > MAX_OUTPUT_BYTES:
                raise PDFError("The split output is too large. Use fewer groups.")
            entries.append((f"{stem}-part-{number:03d}.pdf", data))
    summary.update(pages=sum(len(group) for group in groups), count=len(entries))
    return _finish(_zip(entries), f"{stem}-split.zip", ZIP_MEDIA, summary)


def _merge(files: list[tuple[str, bytes]], password: str, summary: dict):
    if len(files) < 2:
        raise PDFError("Choose at least two PDFs to merge.")
    with fitz.open() as output:
        table_of_contents = []
        for filename, data in files:
            with _open_pdf(data, password) as document:
                if output.page_count + document.page_count > MAX_PAGES:
                    raise PDFError(f"The merged PDF cannot contain more than {MAX_PAGES} pages.")
                _signed_warning(document, summary)
                _new_output_warning(document, summary)
                offset = output.page_count
                output.insert_pdf(document, widgets=True, join_duplicates=False)
                table_of_contents.append([1, _stem(filename), offset + 1])
                for level, title, page in document.get_toc():
                    if page > 0:
                        table_of_contents.append([level + 1, title, offset + page])
                if offset == 0:
                    _set_copied_metadata(output, document)
        output.set_toc(table_of_contents)
        summary["warnings"].append("Form fields are kept where supported; duplicate field names are renamed. Document-level attachments, layers and named destinations may not be retained.")
        summary.update(pages=output.page_count, count=1)
        return _finish(_save(output, keep_encryption=False), "merged.pdf", PDF_MEDIA, summary)


def _color(options: dict) -> tuple[float, float, float]:
    value = _text(options, "color", "#64748b").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise PDFError("Choose a color in #RRGGBB format.")
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _insert_label(page: fitz.Page, text: str, *, size: float, position: str,
                  margin: float, opacity: float = 1, color=(0.2, 0.2, 0.2), center_vertical: bool = False):
    if any(ord(char) > 255 for char in text):
        raise PDFError("Watermark and page-number text currently supports Latin characters. Use English or Latin text.")
    # Position in the displayed crop rectangle, then map back to the page's
    # unrotated coordinates. Retaining rotation preserves widget appearances,
    # crop boxes and the original page content exactly.
    width, height = page.rect.width, page.rect.height
    available = width - margin * 2
    if available <= 2 or height - margin * 2 <= size * 1.2:
        raise PDFError("This page is too small for the label. Reduce the font size or margin.")
    text_width = fitz.get_text_length(text, fontname="helv", fontsize=size)
    if text_width > available:
        size = max(3.0, size * available / text_width)
        text_width = fitz.get_text_length(text, fontname="helv", fontsize=size)
    x = margin if position == "left" else width - margin - text_width if position == "right" else (width - text_width) / 2
    if center_vertical:
        y = height / 2 + size * 0.35
    elif position == "top":
        y = margin + size
    else:
        y = height - margin
    baseline = fitz.Point(x, y) * page.derotation_matrix
    page.insert_text(baseline, text, fontsize=size, fontname="helv", color=color,
                     fill_opacity=opacity, overlay=True, rotate=page.rotation)


def _extract_images(document: fitz.Document, stem: str, indexes: list[int], summary: dict):
    entries = []
    seen = set()
    total = 0
    skipped = 0
    for index in indexes:
        for item in document[index].get_images(full=True):
            xref, mask = item[:2]
            if not xref or xref in seen:
                continue
            seen.add(xref)
            image = document.extract_image(xref)
            if not image:
                skipped += 1
                continue
            if image["width"] * image["height"] > MAX_IMAGE_PIXELS:
                raise PDFError("An embedded image exceeds the 40-megapixel limit.")
            if mask:
                try:
                    base = fitz.Pixmap(document, xref)
                    alpha = fitz.Pixmap(document, mask)
                    if base.alpha:
                        base = fitz.Pixmap(base, 0)
                    if base.colorspace and base.colorspace.n > 3:
                        base = fitz.Pixmap(fitz.csRGB, base)
                    combined = fitz.Pixmap(base, alpha)
                    data, extension = combined.tobytes("png"), "png"
                except (ValueError, RuntimeError):
                    data, extension = image["image"], image["ext"]
                    skipped += 1
            else:
                data, extension = image["image"], image["ext"]
            total += len(data)
            if total > MAX_OUTPUT_BYTES:
                raise PDFError("The extracted images are too large. Select fewer pages.")
            entries.append((f"{stem}-image-{len(entries) + 1:03d}.{extension}", data))
    if not entries:
        raise PDFError("No embedded images were found in the selected pages. Use PDF to images to render complete pages.")
    if skipped:
        summary["warnings"].append("Some image masks or damaged image objects could not be fully extracted.")
    summary["warnings"].append("Extracts unique embedded images; vector artwork and inline images may require rendering the full page.")
    summary.update(pages=len(indexes), count=len(entries))
    return _finish(_zip(entries), f"{stem}-extracted-images.zip", ZIP_MEDIA, summary)


def _clean_extracted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00ad", "")
    for ligature, letters in (("\ufb00", "ff"), ("\ufb01", "fi"), ("\ufb02", "fl"), ("\ufb03", "ffi"), ("\ufb04", "ffl")):
        text = text.replace(ligature, letters)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def _extract_text(document: fitz.Document, stem: str, indexes: list[int], options: dict, summary: dict):
    format_ = _choice(options, "format", "txt", ("txt", "md", "json"))
    layout = _choice(options, "text_layout", "readable", ("readable", "simple"))
    if format_ == "md":
        layout = "readable"
    if len(indexes) > MAX_TEXT_PAGES:
        raise PDFError("Extract up to 500 pages at a time. Choose fewer pages in Pages to include.")
    converter = None
    if layout == "readable":
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        import pymupdf4llm
        converter = pymupdf4llm.to_markdown if format_ == "md" else pymupdf4llm.to_text
    records = []
    empty_pages = []
    fallback_pages = []
    total_bytes = 0
    for index in indexes:
        page = document[index]
        # OCR stays disabled: this tool reads only the existing text layer.
        native = page.get_text("text", sort=True)
        text = native
        if native.strip() and converter is not None:
            try:
                converted = converter(document, pages=[index], use_ocr=False, show_progress=False,
                                      write_images=False, embed_images=False)
                if isinstance(converted, str) and converted.strip():
                    text = converted
                else:
                    fallback_pages.append(index + 1)
            except (ValueError, RuntimeError, OSError, TypeError):
                fallback_pages.append(index + 1)
        text = _clean_extracted_text(text)
        if not text:
            empty_pages.append(index + 1)
        total_bytes += len(text.encode("utf-8"))
        if total_bytes > MAX_TEXT_BYTES:
            raise PDFError("The extracted text exceeds 10 MB. Select fewer pages.")
        records.append({"page": index + 1, "text": text})
    if len(empty_pages) == len(indexes):
        raise PDFError("No selectable text was found in these pages. Scanned PDFs are not supported by Extract text yet.")
    if empty_pages:
        page_list = ", ".join(map(str, empty_pages[:20])) + ("…" if len(empty_pages) > 20 else "")
        summary["warnings"].append(f"No selectable text on pages {page_list}. Those pages may be scanned or blank.")
    if fallback_pages:
        summary["warnings"].append(f"{len(fallback_pages)} pages used simpler formatting because their layout could not be resolved.")
    if format_ == "json":
        output = json.dumps({"pages": records}, ensure_ascii=False, indent=2).encode("utf-8")
        mime = "application/json"
    elif format_ == "md":
        output = "\n\n".join(f"## Page {item['page']}\n\n{item['text']}" for item in records).encode("utf-8")
        mime = "text/markdown; charset=utf-8"
    else:
        output = "\n\n".join(f"--- Page {item['page']} ---\n{item['text']}" for item in records).encode("utf-8")
        mime = "text/plain; charset=utf-8"
    if len(output) > MAX_TEXT_BYTES:
        raise PDFError("The extracted text exceeds 10 MB. Select fewer pages.")
    summary.update(pages=len(records), count=1, characters=sum(len(item["text"]) for item in records),
                   empty_pages=empty_pages, text_layout=layout)
    return _finish(output, f"{stem}-text.{format_}", mime, summary)


def operate(tool: str, files: list[tuple[str, bytes]], options: dict) -> tuple[bytes, str, str, dict]:
    """Perform one operation and return (bytes, safe filename, MIME type, summary)."""
    if tool not in TOOLS:
        raise PDFError("Choose a supported PDF tool.")
    if not isinstance(options, dict):
        raise PDFError("Tool options must be an object.")
    if not isinstance(files, list) or not files:
        raise PDFError("Choose at least one file.")
    if len(files) > MAX_FILES:
        raise PDFError(f"Choose up to {MAX_FILES} files at a time.")
    for item in files:
        if not isinstance(item, (tuple, list)) or len(item) != 2 or not isinstance(item[0], str) or not isinstance(item[1], bytes):
            raise PDFError("A file upload is invalid.")
        if not item[1]:
            raise PDFError("An uploaded file is empty.")
    if sum(len(data) for _, data in files) > MAX_INPUT_BYTES:
        raise PDFError(f"The total upload limit is {MAX_INPUT_BYTES // (1024 * 1024)} MB.")
    if tool not in {"merge", "images-to-pdf"} and len(files) != 1:
        raise PDFError("Choose one PDF for this tool.")
    password = _text(options, "password", limit=1000)
    summary = _base_summary(tool, files)
    try:
        if tool == "images-to-pdf":
            return _images_to_pdf(files, options, summary)
        if tool == "merge":
            return _merge(files, password, summary)
        filename, data = files[0]
        stem = _stem(filename)
        with _open_pdf(data, password) as document:
            if tool == "pdf-to-images":
                return _pdf_to_images(document, stem, options, summary)
            if tool == "split":
                _signed_warning(document, summary)
                return _split(document, stem, options, summary)
            indexes = parse_pages(_text(options, "pages", "all", limit=20_000), document.page_count, duplicates=tool == "reorder")
            if tool == "extract-images":
                return _extract_images(document, stem, indexes, summary)
            if tool == "extract-text":
                return _extract_text(document, stem, indexes, options, summary)
            _signed_warning(document, summary)
            keep_encryption = True
            if tool in {"extract", "reorder"}:
                document.select(indexes)
                summary["warnings"].append("Page selection updates bookmarks where supported; links to removed pages may be lost.")
            elif tool == "delete":
                if len(indexes) >= document.page_count:
                    raise PDFError("You cannot delete every page. Leave at least one page in the PDF.")
                document.delete_pages(indexes)
            elif tool == "rotate":
                angle = _number(options, "angle", 90, 90, 270, integer=True)
                if angle not in (90, 180, 270):
                    raise PDFError("Choose a rotation of 90, 180 or 270 degrees.")
                for index in indexes:
                    page = document[index]
                    page.set_rotation((page.rotation + angle) % 360)
            elif tool == "compress":
                level = _choice(options, "level", "balanced", ("lossless", "balanced", "strong"))
                if level != "lossless":
                    if not hasattr(document, "rewrite_images"):
                        raise PDFError("Image compression requires a newer PyMuPDF. Update dependencies or choose lossless compression.")
                    seen_images = set()
                    for page in document:
                        for image in page.get_images(full=True):
                            if image[0] in seen_images:
                                continue
                            seen_images.add(image[0])
                            if image[2] * image[3] > MAX_IMAGE_PIXELS:
                                raise PDFError("An embedded image exceeds the 40-megapixel limit. Choose lossless compression instead.")
                    threshold, target, quality = (225, 150, 78) if level == "balanced" else (144, 96, 55)
                    document.rewrite_images(dpi_threshold=threshold, dpi_target=target, quality=quality,
                                            lossy=True, lossless=True, bitonal=False, color=True, gray=True)
                    summary["warnings"].append("Embedded images are downsampled and recompressed. Selectable text and vector artwork remain intact; image quality may decrease.")
                output = _save(document)
                if len(output) >= len(data):
                    output = data
                    summary["warnings"].append("This PDF is already compact at the chosen setting. The original bytes are returned because recompression did not reduce its size.")
                summary.update(pages=document.page_count, count=1, level=level,
                               saved_bytes=len(data) - len(output),
                               saved_percent=round((1 - len(output) / len(data)) * 100, 1))
                return _finish(output, f"{stem}-compressed.pdf", PDF_MEDIA, summary)
            elif tool == "watermark":
                text = _text(options, "text", "CONFIDENTIAL", limit=300).strip()
                if not text or "\n" in text or "\r" in text:
                    raise PDFError("Enter a single line of watermark text.")
                position = _choice(options, "position", "center", ("center", "top", "bottom"))
                size = _number(options, "font_size", 42, 5, 160)
                opacity = _number(options, "opacity", 0.22, 0.01, 1)
                color = _color(options)
                for index in indexes:
                    _insert_label(document[index], text, size=size, position=position,
                                  margin=24, opacity=opacity, color=color, center_vertical=position == "center")
            elif tool == "page-numbers":
                start = _number(options, "start", 1, 0, 1_000_000, integer=True)
                position = _choice(options, "position", "center", ("left", "center", "right"))
                size = _number(options, "font_size", 11, 5, 72)
                margin = _number(options, "margin", 24, 5, 144)
                prefix = _text(options, "prefix", limit=100)
                suffix = _text(options, "suffix", limit=100)
                for sequence, index in enumerate(indexes):
                    text = f"{prefix}{start + sequence}{suffix}"
                    if "\n" in text or "\r" in text:
                        raise PDFError("Page-number text must fit on a single line.")
                    _insert_label(document[index], text, size=size, position=position, margin=margin)
            elif tool == "protect":
                user_password = _text(options, "user_password", limit=1000)
                owner_password = _text(options, "owner_password", limit=1000) or secrets.token_urlsafe(24)
                if not user_password:
                    raise PDFError("Enter a password to open the protected PDF.")
                if len(user_password.encode("utf-8")) > 40 or len(owner_password.encode("utf-8")) > 40:
                    raise PDFError("PDF passwords must be at most 40 UTF-8 bytes.")
                permissions = fitz.PDF_PERM_ACCESSIBILITY
                if _boolean(options, "allow_print", True):
                    permissions |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
                if _boolean(options, "allow_copy", False):
                    permissions |= fitz.PDF_PERM_COPY
                if _boolean(options, "allow_modify", False):
                    permissions |= fitz.PDF_PERM_MODIFY | fitz.PDF_PERM_ANNOTATE | fitz.PDF_PERM_FORM | fitz.PDF_PERM_ASSEMBLE
                output = document.tobytes(garbage=4, deflate=True, use_objstms=1,
                                          encryption=fitz.PDF_ENCRYPT_AES_256,
                                          owner_pw=owner_password, user_pw=user_password, permissions=permissions)
                summary["warnings"].append("The opening password encrypts the file. Copy, print and editing restrictions depend on the PDF viewer and are not a guarantee against copying.")
                summary.update(pages=document.page_count, count=1, encrypted=True)
                return _finish(output, f"{stem}-protected.pdf", PDF_MEDIA, summary)
            elif tool == "unlock":
                keep_encryption = False
                summary["encrypted"] = False
            elif tool == "crop":
                margins = [_number(options, key, 0, 0, 2000) for key in ("left", "top", "right", "bottom")]
                left, top, right, bottom = margins
                for index in indexes:
                    page = document[index]
                    box = page.cropbox
                    cropped = fitz.Rect(box.x0 + left, box.y0 + top, box.x1 - right, box.y1 - bottom)
                    if cropped.width < 10 or cropped.height < 10:
                        raise PDFError(f"Crop margins leave too little space on page {index + 1}. Reduce the margins.")
                    page.set_cropbox(cropped)
                summary["warnings"].append("Cropping hides the margins without deleting their content. It is not redaction. Margins follow the unrotated page coordinates.")
            elif tool == "metadata":
                metadata = {key: value for key, value in (document.metadata or {}).items()
                            if key in {"title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate", "trapped"}}
                for key in ("title", "author", "subject", "keywords", "creator"):
                    if key in options:
                        metadata[key] = _text(options, key, limit=2000)
                document.set_metadata(metadata)
                summary["warnings"].append("Edits the standard PDF metadata. Separate XMP metadata and embedded content are not scrubbed.")
            elif tool == "flatten":
                if not hasattr(document, "bake"):
                    raise PDFError("Flattening requires a newer PyMuPDF. Update the app dependencies.")
                document.bake(annots=True, widgets=True)
                summary["warnings"].append("Forms and annotation appearances become permanent page content. Editable fields, comments and annotation attachments are removed.")
            summary.update(pages=document.page_count, count=1, affected_pages=len(indexes))
            return _finish(_save(document, keep_encryption=keep_encryption), f"{stem}-{tool}.pdf", PDF_MEDIA, summary)
    except PDFError:
        raise
    except (ValueError, RuntimeError, TypeError, OSError, OverflowError) as exc:
        # Avoid returning raw parser details or upload contents in the API error.
        raise PDFError("This operation could not be completed. Check the PDF, password and selected options, then try again.") from exc
