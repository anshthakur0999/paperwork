# Paperwork

A PDF toolbox with a public web app and an offline Windows mode. All fonts, scripts, icons, and styles are bundled. No account, telemetry, or CDN is used.

## Web app

**[Open Paperwork](https://paperwork-pdf.onrender.com)** · [Source on GitHub](https://github.com/anshthakur0999/paperwork)

The hosted app runs on Render's free web service plan. Files are uploaded over HTTPS and processed in server memory. Documents and unfinished edits are saved only in your browser for 24 hours after the last edit. Passwords and generated results are not saved. Use the local Windows mode below when you want processing on your own computer.

Hosted uploads are limited to **25 MB total per operation**, with a 64 MB output limit, 16-megapixel input images, and 6-megapixel rendered pages to fit the free instance. Large documents or layout-aware text extraction may exceed the free service's memory. The free instance sleeps after 15 minutes without traffic; opening it again can take about a minute. Render's free instance hours are shared across services in the workspace. See [Render's free service limits](https://render.com/docs/free).

GitHub Actions runs the Python and browser-state tests, builds the hosted Docker image, and verifies all 15 tools in a container limited to 512 MB of memory. The current Render service uses the public repository URL, so after pushing to `main`, choose **Manual Deploy → Deploy latest commit** in the [Render dashboard](https://dashboard.render.com/web/srv-db7tqg7avr4c73dvlmcg). Render requires a connected Git provider for automatic deployments; see [deployment behavior](https://render.com/docs/deploys).

To deploy your own instance, use `render.yaml` or create a Docker web service with the repository's `Dockerfile`, choose the free plan, and configure `/api/health` as the health check. Render provides `RENDER_EXTERNAL_HOSTNAME` automatically. On another host, set `PAPERWORK_ALLOWED_HOSTS` to the exact public hostname. Environment examples are in `.env.example`; Python does not load that file automatically.

To run the hosted container locally:

```sh
docker build -t paperwork .
docker run --rm -p 10000:10000 -e PAPERWORK_ALLOWED_HOSTS=localhost paperwork
```

Open `http://localhost:10000`. Hosted mode disables the local shutdown endpoint, validates hostnames and request origins, and serializes PDF processing. This is a small free service; it does not provide accounts, a processing queue, or guaranteed availability.

## Open the local Windows app

Local mode connects to a Python app on your computer at `http://127.0.0.1:8765`. Documents stay on your computer and processing works offline after dependencies are installed.

Double-click **Launch Paperwork.cmd**. The packaged Windows build, when present, needs no Python installation or internet connection. Keep the whole `dist/Paperwork` folder together if copying the app to another computer.

On this computer, close and reopen your terminal once to use the short commands from any folder:

```powershell
paperwork start
paperwork stop
```

Running `paperwork` alone also starts the app. On another computer, add this project's folder to your user PATH to enable the commands globally.

The source launcher uses `.venv` and installs dependencies on its first run if needed. That initial installation downloads software packages; document processing works offline afterward. Python 3.12 or later is recommended.

The launcher opens your default browser. If the app is already running, it opens the existing instance. To stop it, press Ctrl+C in its console. Closing a browser tab does not stop the local server.

## Tools

- **Images to PDF:** JPEG, PNG, WebP, BMP, and TIFF images, including multiple TIFF frames. Drag image thumbnails into the PDF page order, or use Alt plus the arrow keys. Choose original image sizing, A4, or Letter.
- **PDF to images:** export selected pages as PNG or JPEG at 36-300 DPI.
- **Merge:** combine PDFs in the file-list order.
- **Split:** one file per page, fixed-size groups, or separate custom ranges.
- **Reorder pages:** drag page thumbnails into your preferred order, use the arrow controls, or enter page numbers for a precise order with optional repeats. Reset restores the original order.
- **Extract pages:** select ranges such as `1,3-5`.
- **Delete pages:** preview the entire document and click × on pages to remove. Restore individual pages or all pages before saving, or enter ranges such as `1,3-5`. At least one page must remain.
- **Rotate:** turn selected pages by 90, 180, or 270 degrees.
- **Compress:** lossless cleanup or balanced/strong recompression of embedded images while retaining text.
- **Watermark / page numbers:** add configurable text to selected pages.
- **Protect / unlock:** AES-256 password protection, permission choices, or removal with the existing password.
- **Extract text:** read selectable PDF text in column order, preserve tables in Markdown, or export TXT/JSON. Review and copy the result in the browser. Simple layout keeps original lines and processes faster. Supports up to 500 selected pages and 10 MB of extracted text per operation.
- **Extract images:** save unique embedded image assets in a ZIP.

Multiple outputs are downloaded together as a ZIP. Single outputs download directly. Original files are never overwritten.

## Local privacy and limits

Files are sent from your browser only to the local server. The browser saves the latest document workspace for each tool, including files, page selections, file order, and options, for **24 hours after the last edit**. Resume it from Saved work or by reopening that tool. Resuming does not extend its expiry. Passwords, generated results, and preview images are not saved; encrypted documents require their password again after a reload.

Saved drafts stay in this browser on this device and are limited to 200 MB total. The oldest drafts are removed if that limit is exceeded. Expired drafts cannot be restored and are deleted while the app is open or the next time it opens. **Clear saved work** removes all saved browser copies immediately; clearing a tool's files or choosing Start again removes its draft. New edits save a new draft. Browser site-data clearing, private browsing, or browser storage eviction can remove drafts sooner. Different browsers and local ports have separate storage.

Multipart inputs, processing, previews, and outputs on the Python server remain in RAM; the server does not create document temporary files or store drafts. Saving a result uses your browser's normal Downloads behavior. Browser extensions, browser-managed download history, and operating-system memory paging are outside the app's control.

The server binds only to `127.0.0.1`, verifies local Host headers, blocks cross-origin submissions, disables document caching, and serves no remote resources. It processes one MuPDF operation at a time. Inputs are limited to 200 MB total and 100 files per operation, with additional rendering limits to avoid excessive memory use.

Compression savings depend on the PDF; already-optimized files may not shrink. Balanced and strong modes can reduce image quality. PDF editing can invalidate digital signatures. Password permission flags depend on the reader application; they are not a substitute for encryption.

This covers common PDF tasks. OCR, Word/Excel conversion, digital signing, arbitrary existing-text editing, and certified redaction are not included. Text extraction requires a text layer. Embedded-image extraction differs from rendering entire PDF pages.

## Develop or rebuild

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe -m pytest tests -q
npm install
npm test
```

Use **Build Windows app.cmd** to package the source with PyInstaller. For automation, run `run.py --no-browser --port 8765`.

Tool names, categories, icons, and settings are defined in `static/tools.json`. The server includes the tool cards and stylesheet in the initial page response; JavaScript handles filtering and interaction afterward.

Paperwork is distributed under **AGPL-3.0**; see `LICENSE`. PyMuPDF, PyMuPDF4LLM, and PyMuPDF Layout use the AGPL or a commercial license; see the [official licensing information](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright). The public app links to its corresponding source. The layout model and runtime are included in dependency packages for offline use. Dependency license metadata is included in the packaged build where available.
