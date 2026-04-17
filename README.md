# PDF Tab Builder

A self-hosted web app for adding click-to-toggle interactive tabs to PDFs at scale (900+ documents).

## What it does

For each PDF (or DOCX), you click where you want a "tab" on the page, type a label and some hidden text, and hit save. The output is a PDF with an interactive button: in Adobe Acrobat Reader, clicking the button toggles the hidden text in and out of view.

- **PDFs** keep full fidelity.
- **DOCX** files are converted to PDF server-side using **LibreOffice headless** (preserves images, tables, formatting — not just text).
- Built to click through hundreds of documents fast: keyboard shortcuts, position presets, templates, bulk operations.

## Tech

- **Backend:** FastAPI + SQLite + SQLAlchemy
- **PDF:** pypdf (low-level form fields and JavaScript actions)
- **DOCX → PDF:** LibreOffice headless
- **Frontend:** React (single HTML file via CDN) + pdf.js for previews
- **Storage:** local disk, mounted as Docker volume

## Quick start

```bash
cd pdf-tab-builder
docker compose up -d --build
```

Open http://localhost:8000.

Files and the SQLite database persist in `./data`.

## Deploying on a DigitalOcean droplet

1. SSH to the droplet.
2. Install Docker if you haven't:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. Copy this directory to the server (or `git clone` it).
4. `docker compose up -d --build`
5. If you want HTTPS and a domain: put nginx (or Caddy) in front. Minimal nginx snippet:
   ```nginx
   server {
       server_name pdf.yourdomain.com;
       client_max_body_size 500M;
       location / {
           proxy_pass http://localhost:8000;
           proxy_read_timeout 300s;
           proxy_request_buffering off;
       }
   }
   ```
   Then `certbot --nginx -d pdf.yourdomain.com`.

## How to use it

1. **Upload.** Drag-drop your PDFs / DOCX files (up to 10 per batch — the UI batches automatically).
2. **Click Start →** on the file list.
3. **For each doc:**
   - Click on the PDF preview where you want the tab, OR press a number key `1`–`9` to snap to a grid position (top-left, top-center, top-right, center-left, center, etc.).
   - Type the tab label (e.g. "Show details").
   - Type the hidden text.
   - Press `Enter` to save and move to the next doc.
4. **Download.** When done (or any time), click "Download N done" on the file list. You get a zip of all processed PDFs.

### Keyboard shortcuts (editor)

| Key | Action |
|-----|--------|
| `Enter` | Save & advance to next doc |
| `1`–`9` | Snap tab to 3×3 grid position |
| `→` / `←` | Next / previous doc (without saving) |
| `S` | Skip current doc |
| `T` | Focus "Tab label" field |
| `H` | Focus "Hidden text" field |
| `C` | Copy config from previous doc |
| `Esc` | Back to file list |

### Templates

Save a configuration (tab label + hidden text + position) as a template, then one-click apply it to later docs. Useful when groups of docs share the same revealed text.

### Bulk operations

- **Multi-select** files with checkboxes on the list.
- **Delete selected** in one click.
- To bulk-apply a config to many files: use the `POST /api/files/bulk-apply` endpoint directly, or extend the UI (the backend already supports it).

## Viewer compatibility

The interactive toggle uses PDF JavaScript and AcroForm fields. Compatibility:

| Viewer | Works? |
|--------|--------|
| **Adobe Acrobat Reader** (Windows/macOS/Linux) | ✅ Yes |
| **Adobe Acrobat Reader mobile** (iOS/Android) | ✅ Yes |
| **Foxit Reader** | ✅ Yes |
| Chrome / Edge / Firefox built-in viewer | ❌ Static only |
| Apple Preview (macOS) | ❌ Static only |
| iOS Files / Quick Look | ❌ Static only |

If your end users aren't on Adobe Reader, the tab won't do anything — they'll just see a static PDF with the hidden content invisible. Plan accordingly.

## API

- `POST /api/files/upload` — multipart files
- `GET  /api/files` — list (supports `?q=search&status=pending`)
- `GET  /api/files/stats` — status counts
- `GET  /api/files/{id}` — one file's metadata
- `GET  /api/files/{id}/preview` — original PDF bytes (for preview render)
- `PUT  /api/files/{id}/config` — save tab config + trigger processing
- `POST /api/files/{id}/skip` — mark skipped
- `DELETE /api/files/{id}` — delete one
- `POST /api/files/bulk-apply` — apply one config to many files
- `POST /api/files/bulk-delete` — delete many
- `GET  /api/files/{id}/download` — processed PDF
- `GET  /api/download-all` — streaming zip of all processed
- `GET  /api/templates`, `POST /api/templates`, `DELETE /api/templates/{id}`
- `POST /api/reset` — nuke everything

## Scaling notes

- **SQLite** is fine for single-user, 10K+ files. If you want multi-user, swap to Postgres by changing the `create_engine` URL in `backend.py`.
- **DOCX conversion** is the slowest step (~2–5 sec per file). Conversion happens at upload time, not later.
- **Tab injection** is fast (<100ms per PDF).
- **Uvicorn workers:** default is 1. Bump `--workers 2` in the Dockerfile if you upload + process simultaneously a lot.
- **Memory:** the backend streams uploads, so memory stays flat regardless of file count.

## Troubleshooting

- **DOCX fails to convert:** check the container can run `soffice`. `docker exec pdf-tab-builder soffice --version` should print a version.
- **Large uploads fail:** if behind nginx/Cloudflare, bump `client_max_body_size` (nginx) or equivalents.
- **Tab doesn't toggle in the viewer:** the recipient needs Adobe Acrobat Reader. Other viewers ignore PDF JavaScript.
- **Processing stuck on "configured":** check `docker logs pdf-tab-builder` for errors. Processing runs as a FastAPI BackgroundTask — errors land in the file's `error_msg` field, shown in the editor sidebar.

## Local dev without Docker

```bash
pip install -r requirements.txt
# Install LibreOffice separately (Homebrew: `brew install --cask libreoffice`)
BASE_DIR=./data python backend.py
```

## License

Use it however you want.
