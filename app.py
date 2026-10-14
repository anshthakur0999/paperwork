"""Paperwork's local and hosted interfaces. Server documents stay in memory."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from html import escape
import io
import json
import os
import queue
import re
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool

from pdf_engine import PDFError, inspect_pdf, operate, preview_pdf, thumbnail_records

VERSION = "1.0.0"
MAX_DOCUMENT_BYTES = 200 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_DOCUMENT_BYTES + 1024 * 1024
MAX_FILES = 100
ENGINE_LOCK = threading.Lock()  # MuPDF operations must not overlap between threads.
HOST_PATTERN = re.compile(r"^(127\.0\.0\.1|localhost|testserver)(:[0-9]{1,5})?$")
ASSET_DIR = Path(__file__).parent / "static"
CONTENT_SECURITY_POLICY = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
PUBLIC_ASSETS = {"/static/app.js": "app.js", "/static/styles.css": "styles.css", "/static/tools.json": "tools.json"}


def interface_response(*, hosted: bool = False, upload_limit: int = MAX_DOCUMENT_BYTES, source_url: str = "") -> HTMLResponse:
    """Include the complete first view and its CSS in the initial response."""
    template = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
    styles = "\n" + (ASSET_DIR / "styles.css").read_text(encoding="utf-8") + "\n"
    catalog = json.loads((ASSET_DIR / "tools.json").read_text(encoding="utf-8"))

    def symbol(name: str, extra_class: str = "") -> str:
        return f'<svg class="icon{extra_class}" aria-hidden="true"><use href="#i-{escape(name, quote=True)}"/></svg>'

    cards = "\n".join(
        f'<button class="tool-card" data-tool="{escape(tool["id"], quote=True)}">'
        f'<span class="tool-icon {escape(tool["category"], quote=True)}">{symbol(tool["icon"])}</span>'
        f'{symbol("arrow", " card-arrow")}<h3>{escape(tool["name"])}</h3>'
        f'<p>{escape(tool["description"])}</p></button>'
        for tool in catalog
    )
    catalog_json = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    config_json = json.dumps({"mode": "hosted" if hosted else "local", "max_upload_bytes": upload_limit}, separators=(",", ":"))
    script_version = hashlib.sha256((ASSET_DIR / "app.js").read_bytes()).hexdigest()[:12]
    page = template.replace('<link rel="stylesheet" href="/static/styles.css">', f'<style id="app-styles">{styles}</style>')
    page = page.replace('<div class="tool-grid" id="tool-grid"></div>', f'<div class="tool-grid" id="tool-grid">{cards}</div>')
    page = page.replace('<script src="/static/app.js" defer></script>', f'<script id="tool-catalog" type="application/json">{catalog_json}</script>\n  <script id="app-config" type="application/json">{config_json}</script>\n  <script src="/static/app.js?v={script_version}" defer></script>')
    if hosted:
        page = page.replace("Paperwork - Local PDF Studio", "Paperwork - PDF Studio")
        page = page.replace("Local processing", "Server processing")
        page = page.replace("Files are sent only to the server on this computer.", "Files are uploaded to the PDF server for processing in memory. Saved drafts stay in this browser for 24 hours.")
        page = page.replace("PDF files · up to 200 MB total", f"PDF files · up to {upload_limit // (1024 * 1024)} MB total")
    if source_url:
        footer = f'<footer class="source-footer"><a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">Source code on GitHub · AGPL-3.0</a></footer>'
        page = page.replace('<div class="sr-only" id="live-status"', footer + '\n  <div class="sr-only" id="live-status"')
    style_hash = base64.b64encode(hashlib.sha256(styles.encode("utf-8")).digest()).decode("ascii")
    policy = CONTENT_SECURITY_POLICY.replace("style-src 'self';", f"style-src 'self' 'sha256-{style_hash}';")
    return HTMLResponse(page, headers={"Content-Security-Policy": policy})


class InputError(ValueError):
    pass


class ThumbnailStreamingResponse(StreamingResponse):
    """Stop the worker even if sending headers fails before iteration starts."""

    def __init__(self, content, cleanup):
        super().__init__(content, media_type="application/x-ndjson")
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.cleanup()


async def read_form(request: Request) -> tuple[list[tuple[str, bytes]], dict[str, str]]:
    """Parse multipart directly into RAM, without UploadFile's disk spooling."""
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary or len(boundary) > 200:
        raise InputError("Choose a file and submit it as multipart form data.")
    app_state = getattr(request.scope.get("app"), "state", None)
    document_limit = getattr(app_state, "upload_limit", MAX_DOCUMENT_BYTES)
    request_limit = document_limit + 1024 * 1024 if hasattr(app_state, "upload_limit") else MAX_REQUEST_BYTES
    limit_error = f"The selected files exceed the {document_limit // (1024 * 1024)} MB limit."
    try:
        if int(request.headers.get("content-length", "0")) > request_limit:
            raise InputError(limit_error)
    except ValueError as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("Invalid request length.") from exc
    files: list[tuple[str, bytes]] = []
    fields: dict[str, str] = {}
    state: dict = {}
    size = 0
    document_size = 0
    complete = False

    def part_begin():
        if len(files) + len(fields) >= MAX_FILES + 20:
            raise InputError("Too many form fields.")
        state.clear()
        state.update(headers={}, header_name=bytearray(), header_value=bytearray(), data=io.BytesIO())

    def header_field(data, start, end):
        state["header_name"].extend(data[start:end])
        if len(state["header_name"]) > 1024:
            raise InputError("Invalid file header.")

    def header_value(data, start, end):
        state["header_value"].extend(data[start:end])
        if len(state["header_value"]) > 8192:
            raise InputError("Invalid file header.")

    def header_end():
        state["headers"][bytes(state["header_name"]).lower()] = bytes(state["header_value"])
        state["header_name"].clear()
        state["header_value"].clear()
        if len(state["headers"]) > 20:
            raise InputError("Invalid file headers.")

    def headers_finished():
        disposition, values = parse_options_header(state["headers"].get(b"content-disposition", b""))
        if disposition != b"form-data" or b"name" not in values:
            raise InputError("Invalid file submission.")
        state["name"] = values[b"name"].decode("utf-8", "replace")
        state["filename"] = values.get(b"filename")
        if state["filename"] is not None and len(files) >= MAX_FILES:
            raise InputError("Choose at most 100 files per operation.")

    def part_data(data, start, end):
        nonlocal document_size
        if state.get("filename") is not None:
            document_size += end - start
            if document_size > document_limit:
                raise InputError(limit_error)
        elif state["data"].tell() + end - start > 32768:
            raise InputError("The supplied options are too large.")
        state["data"].write(data[start:end])

    def part_end():
        data = state["data"].getvalue()
        if state.get("filename") is not None:
            if state["name"] not in ("file", "files"):
                raise InputError("Invalid file field.")
            if not data:
                raise InputError("One of the selected files is empty.")
            name = state["filename"].decode("utf-8", "replace")
            name = name.replace("\\", "/").rsplit("/", 1)[-1][:240]
            files.append((name or "document.pdf", data))
        else:
            if state["name"] in fields:
                raise InputError("Duplicate option field.")
            try:
                fields[state["name"]] = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InputError("Invalid option encoding.") from exc

    def ended():
        nonlocal complete
        complete = True

    callbacks = {"on_part_begin": part_begin, "on_header_field": header_field,
                 "on_header_value": header_value, "on_header_end": header_end,
                 "on_headers_finished": headers_finished, "on_part_data": part_data,
                 "on_part_end": part_end, "on_end": ended}
    parser = MultipartParser(boundary, callbacks)
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > request_limit:
                raise InputError(limit_error)
            parser.write(chunk)
        parser.finalize()
        if not complete:
            raise InputError("The file transfer was incomplete. Please try again.")
    except InputError:
        raise
    except Exception as exc:
        raise InputError("The file submission could not be read.") from exc
    return files, fields


def engine_call(function, *args):
    with ENGINE_LOCK:
        return function(*args)


def create_app(*, hosted: bool | None = None, allowed_hosts: tuple[str, ...] | None = None) -> FastAPI:
    if hosted is None:
        mode = os.environ.get("PAPERWORK_MODE", "local")
        if mode not in ("local", "hosted"):
            raise ValueError("PAPERWORK_MODE must be local or hosted.")
        hosted = mode == "hosted"
    if hosted and allowed_hosts is None:
        configured = os.environ.get("PAPERWORK_ALLOWED_HOSTS", os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""))
        allowed_hosts = tuple(host.strip().lower() for host in configured.split(",") if host.strip())
    if hosted and not allowed_hosts:
        raise ValueError("Set PAPERWORK_ALLOWED_HOSTS to your hosted hostname.")
    if hosted and any(not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host) for host in allowed_hosts):
        raise ValueError("PAPERWORK_ALLOWED_HOSTS must contain hostnames without schemes or wildcards.")
    app = FastAPI(title="Paperwork" if hosted else "Paperwork Local", docs_url=None, redoc_url=None, openapi_url=None)
    if hosted:
        upload_mb = int(os.environ.get("PAPERWORK_MAX_UPLOAD_MB", "25"))
        if not 1 <= upload_mb <= 200:
            raise ValueError("PAPERWORK_MAX_UPLOAD_MB must be between 1 and 200.")
        app.state.upload_limit = upload_mb * 1024 * 1024
    app.state.hosted = hosted

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        local_host = bool(HOST_PATTERN.fullmatch(host))
        hosted_host = bool(hosted and re.fullmatch(r"[a-z0-9.-]+(?::\d{1,5})?", host) and host.split(":")[0] in allowed_hosts)
        if not local_host and not hosted_host:
            return JSONResponse({"detail": "This hostname is not allowed." if hosted else "Only local connections are accepted."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                except ValueError:
                    return JSONResponse({"detail": "Invalid request origin."}, status_code=403)
                expected_scheme = request.url.scheme if hosted else "http"
                if parsed.scheme != expected_scheme or parsed.netloc.lower() != host or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                    return JSONResponse({"detail": "Only this app can submit documents."}, status_code=403)
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse({"detail": "Cross-site requests are blocked."}, status_code=403)
        response = await call_next(request)
        cache_control = "no-store"
        if request.method in ("GET", "HEAD") and request.url.path in PUBLIC_ASSETS:
            asset = ASSET_DIR / PUBLIC_ASSETS[request.url.path]
            version = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
            cache_control = "private, max-age=86400, immutable" if request.query_params.get("v") == version else "no-cache"
        response.headers.update({
            "Cache-Control": cache_control, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
        })
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response

    @app.api_route("/", methods=["GET", "HEAD"])
    @app.api_route("/index.html", methods=["GET", "HEAD"])
    @app.api_route("/static/index.html", methods=["GET", "HEAD"])
    async def interface():
        return interface_response(hosted=hosted, upload_limit=getattr(app.state, "upload_limit", MAX_DOCUMENT_BYTES), source_url=os.environ.get("PAPERWORK_SOURCE_URL", ""))

    @app.exception_handler(InputError)
    @app.exception_handler(PDFError)
    async def input_error(request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": "paperwork-hosted" if hosted else "paperwork-local", "version": VERSION}

    @app.post("/api/inspect")
    async def inspect(request: Request):
        files, fields = await read_form(request)
        if len(files) != 1:
            raise InputError("Choose one PDF to inspect.")
        return await run_in_threadpool(engine_call, inspect_pdf, files[0][1], fields.get("password", ""))

    @app.post("/api/preview")
    async def preview(request: Request):
        files, fields = await read_form(request)
        if len(files) != 1:
            raise InputError("Choose one PDF to preview.")
        try:
            page = int(fields.get("page", "1"))
        except ValueError as exc:
            raise InputError("Enter a valid page number.") from exc
        data = await run_in_threadpool(engine_call, preview_pdf, files[0][1], fields.get("password", ""), page)
        return Response(data, media_type="image/png")

    @app.post("/api/thumbnails")
    async def thumbnails(request: Request):
        files, fields = await read_form(request)
        if len(files) != 1:
            raise InputError("Choose one PDF to preview.")
        records: queue.Queue = queue.Queue(maxsize=2)
        cancelled = threading.Event()
        ready = threading.Event()
        initial_error: list[str] = []

        def offer(record):
            while not cancelled.is_set():
                try:
                    records.put(record, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def render():
            acquired = False
            generator = None
            try:
                # Waiting for another operation must also remain cancellable.
                while not cancelled.is_set():
                    if ENGINE_LOCK.acquire(timeout=0.1):
                        acquired = True
                        break
                if not acquired:
                    return
                generator = thumbnail_records(files[0][1], fields.get("password", ""),
                                              cancelled=cancelled.is_set)
                for record in generator:
                    if not offer(record):
                        break
                    ready.set()
            except Exception as exc:
                detail = str(exc) if isinstance(exc, PDFError) else "This PDF could not be previewed. It may be damaged."
                if not ready.is_set():
                    initial_error.append(detail)
                else:
                    offer({"error": detail})
            finally:
                try:
                    if generator is not None:
                        generator.close()
                finally:
                    if acquired:
                        ENGINE_LOCK.release()
                    ready.set()
                    offer(None)

        worker = threading.Thread(target=render, name="paperwork-thumbnails", daemon=True)
        worker.start()
        try:
            # The first record establishes that the PDF/password is valid, so
            # initial failures use the ordinary JSON error response.
            while not ready.is_set():
                if await request.is_disconnected():
                    cancelled.set()
                    raise InputError("The thumbnail request was cancelled.")
                await asyncio.sleep(0.02)
            if initial_error:
                cancelled.set()
                raise InputError(initial_error[0])
        except BaseException:
            cancelled.set()
            raise

        def cleanup():
            cancelled.set()
            # Release queued image strings immediately when the browser changes
            # documents or aborts before processing the PDF.
            while True:
                try:
                    records.get_nowait()
                except queue.Empty:
                    break

        async def stream():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        record = await run_in_threadpool(records.get, True, 0.1)
                    except queue.Empty:
                        continue
                    if record is None:
                        break
                    yield json.dumps(record, separators=(",", ":")) + "\n"
            finally:
                cleanup()

        return ThumbnailStreamingResponse(stream(), cleanup)

    @app.post("/api/process/{tool}")
    async def process(tool: str, request: Request):
        files, fields = await read_form(request)
        if not files:
            raise InputError("Choose at least one file.")
        try:
            options = json.loads(fields.get("options", "{}"))
        except (ValueError, TypeError) as exc:
            raise InputError("Invalid tool options.") from exc
        if not isinstance(options, dict):
            raise InputError("Tool options must be an object.")
        try:
            data, name, mime, summary = await run_in_threadpool(engine_call, operate, tool, files, options)
        except PDFError:
            raise
        except Exception:
            return JSONResponse({"detail": "This file could not be processed. It may be damaged or unsupported."}, status_code=422)
        ascii_name = re.sub(r'[^a-zA-Z0-9._-]', '_', name) or "result.pdf"
        disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'
        return Response(data, media_type=mime, headers={
            "Content-Disposition": disposition,
            "X-Result-Summary": quote(json.dumps(summary, ensure_ascii=False, separators=(",", ":")), safe=""),
            "X-Input-Bytes": str(sum(len(content) for _, content in files)),
            "X-Output-Bytes": str(len(data)),
        })

    @app.post("/api/shutdown")
    async def shutdown():
        if hosted:
            return JSONResponse({"detail": "Shutdown is unavailable on the hosted app."}, status_code=404)
        callback = getattr(app.state, "shutdown_callback", None)
        if callback:
            threading.Timer(0.4, callback).start()
        return {"status": "closing"}

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
    return app


app = create_app()
