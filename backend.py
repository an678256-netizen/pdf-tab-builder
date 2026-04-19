"""
FastAPI backend for the Interactive Tab PDF Builder.
- Uploads PDFs and DOCX files
- Stores metadata in SQLite
- Converts DOCX → PDF via LibreOffice headless
- Injects interactive tabs per-file when config is saved
- Serves the React frontend as a static file
- Provides streaming zip download for bulk export
- Session-based user isolation (cookie)
"""
import os
import io
import json
import shutil
import uuid
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, Session

import processing

# ---------- Config ----------
BASE_DIR = Path(os.environ.get("BASE_DIR", "/data")).resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
PROCESSED_DIR = BASE_DIR / "processed"
DB_PATH = BASE_DIR / "app.db"
FRONTEND_HTML = Path(__file__).parent / "frontend.html"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Database ----------
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class FileRecord(Base):
    __tablename__ = "files"
    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=True, index=True)  # owner session
    original_name = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    source_path = Column(String, nullable=False)
    processed_path = Column(String, nullable=True)
    page_count = Column(Integer, default=1)
    page_sizes = Column(JSON, default=list)
    config = Column(JSON, nullable=True)
    status = Column(String, default="pending")
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Template(Base):
    __tablename__ = "templates"
    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False)
    config = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# Create tables (will add session_id column if DB already exists via ALTER TABLE)
Base.metadata.create_all(bind=engine)

# Migrate: add session_id column if it doesn't exist (for existing databases)
try:
    with engine.connect() as conn:
        from sqlalchemy import text
        try:
            conn.execute(text("ALTER TABLE files ADD COLUMN session_id VARCHAR"))
            conn.commit()
        except Exception:
            pass  # column already exists
        try:
            conn.execute(text("ALTER TABLE templates ADD COLUMN session_id VARCHAR"))
            conn.commit()
        except Exception:
            pass
except Exception:
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Session management ----------
SESSION_COOKIE = "tb_session"
SESSION_MAX_AGE = 365 * 24 * 60 * 60  # 1 year


def get_session_id(request: Request, response: Response) -> str:
    """Get or create a session ID from cookies."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = uuid.uuid4().hex
    # Always refresh the cookie (extends expiry)
    response.set_cookie(
        SESSION_COOKIE, sid,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return sid


# ---------- Schemas ----------
class FileOut(BaseModel):
    id: str
    original_name: str
    kind: str
    page_count: int
    page_sizes: list
    config: Optional[dict]
    status: str
    error_msg: Optional[str]
    has_processed: bool

    @classmethod
    def from_record(cls, r: FileRecord):
        return cls(
            id=r.id,
            original_name=r.original_name,
            kind=r.kind,
            page_count=r.page_count,
            page_sizes=r.page_sizes or [],
            config=r.config,
            status=r.status,
            error_msg=r.error_msg,
            has_processed=bool(r.processed_path and Path(r.processed_path).exists()),
        )


class TemplateIn(BaseModel):
    name: str
    config: dict


class BulkApply(BaseModel):
    file_ids: List[str]
    config: dict


# ---------- FastAPI ----------
app = FastAPI(title="PDF Interactive Tab Builder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ---------- Helpers ----------
def _ensure_pdf(upload: UploadFile, save_dir: Path, file_id: str) -> tuple:
    name = upload.filename or "upload"
    ext = Path(name).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(400, f"Unsupported file type: {ext}")

    dst_dir = save_dir / file_id
    dst_dir.mkdir(parents=True, exist_ok=True)

    raw_path = dst_dir / f"original{ext}"
    with open(raw_path, "wb") as f:
        shutil.copyfileobj(upload.file, f)

    if ext == ".pdf":
        return ("pdf", str(raw_path))

    try:
        pdf_path = processing.convert_docx_to_pdf(str(raw_path), str(dst_dir))
        return ("docx", pdf_path)
    except Exception as e:
        raise HTTPException(500, f"DOCX conversion failed: {e}")


def _process_file(file_id: str):
    """Background task: inject tabs based on current config."""
    db = SessionLocal()
    try:
        r = db.query(FileRecord).filter_by(id=file_id).first()
        if not r or not r.config:
            return
        if r.config.get("click_x") is None or r.config.get("click_y") is None:
            return
        out_dir = PROCESSED_DIR / file_id
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(r.original_name).stem
        out_path = out_dir / f"{stem}_interactive.pdf"
        try:
            processing.inject_tab(r.source_path, str(out_path), r.config)
            r.processed_path = str(out_path)
            r.status = "processed"
            r.error_msg = None
        except Exception as e:
            r.status = "error"
            r.error_msg = str(e)[:500]
        r.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


# ---------- Routes ----------
@app.get("/")
def serve_frontend():
    if FRONTEND_HTML.exists():
        return FileResponse(FRONTEND_HTML)
    return JSONResponse({"error": "frontend.html not found"}, status_code=500)


@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}


# --- File management (session-scoped) ---

@app.post("/api/files/upload")
async def upload_files(
    request: Request,
    response: Response,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    out = []
    for upload in files:
        file_id = uuid.uuid4().hex
        try:
            kind, pdf_path = _ensure_pdf(upload, UPLOAD_DIR, file_id)
            num_pages, sizes = processing.get_pdf_page_info(pdf_path)
            rec = FileRecord(
                id=file_id,
                session_id=sid,
                original_name=upload.filename,
                kind=kind,
                source_path=pdf_path,
                page_count=num_pages,
                page_sizes=[list(s) for s in sizes],
                status="pending",
            )
            db.add(rec)
            db.commit()
            out.append(FileOut.from_record(rec).model_dump())
        except HTTPException as e:
            out.append({"error": e.detail, "name": upload.filename})
        except Exception as e:
            out.append({"error": str(e), "name": upload.filename})
    return out if not out else {"files": out}


@app.get("/api/files")
def list_files(
    request: Request,
    response: Response,
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    query = db.query(FileRecord).filter(FileRecord.session_id == sid)
    if status:
        query = query.filter(FileRecord.status == status)
    if q:
        query = query.filter(FileRecord.original_name.ilike(f"%{q}%"))
    query = query.order_by(FileRecord.created_at.asc())
    return {"files": [FileOut.from_record(r).model_dump() for r in query.all()]}


@app.get("/api/files/stats")
def stats(request: Request, response: Response, db: Session = Depends(get_db)):
    sid = get_session_id(request, response)
    from sqlalchemy import func
    rows = (
        db.query(FileRecord.status, func.count(FileRecord.id))
        .filter(FileRecord.session_id == sid)
        .group_by(FileRecord.status)
        .all()
    )
    counts = {s: c for s, c in rows}
    total = sum(counts.values())
    return {"total": total, "by_status": counts}


# --- Single file operations ---
# get_file, preview, download are PUBLIC (anyone with the ID can access)
# This allows viewer links to work without authentication

@app.get("/api/files/{file_id}")
def get_file(file_id: str, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    return FileOut.from_record(r).model_dump()


@app.get("/api/files/{file_id}/preview")
def get_preview(file_id: str, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    if not Path(r.source_path).exists():
        raise HTTPException(404, "Source file missing on disk")
    return FileResponse(r.source_path, media_type="application/pdf")


@app.get("/api/files/{file_id}/download")
def download_one(file_id: str, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r or not r.processed_path or not Path(r.processed_path).exists():
        raise HTTPException(404, "Processed file not available")
    out_name = Path(r.original_name).stem + "_interactive.pdf"
    return FileResponse(r.processed_path, media_type="application/pdf", filename=out_name)


# --- Config update (session-scoped: only owner can edit) ---

@app.put("/api/files/{file_id}/config")
async def update_config(
    file_id: str,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    if r.session_id and r.session_id != sid:
        raise HTTPException(403, "Not your file")

    # Accept arbitrary JSON config (supports both old and new multi-tab format)
    body = await request.json()

    r.config = body
    r.status = "configured" if body.get("click_x") is not None else "pending"
    r.updated_at = datetime.utcnow()
    db.commit()
    if r.status == "configured":
        background.add_task(_process_file, file_id)
    return FileOut.from_record(r).model_dump()


@app.post("/api/files/{file_id}/skip")
def skip_file(
    file_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    if r.session_id and r.session_id != sid:
        raise HTTPException(403, "Not your file")
    r.status = "skipped"
    db.commit()
    return FileOut.from_record(r).model_dump()


@app.delete("/api/files/{file_id}")
def delete_file(
    file_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    if r.session_id and r.session_id != sid:
        raise HTTPException(403, "Not your file")
    shutil.rmtree(UPLOAD_DIR / file_id, ignore_errors=True)
    shutil.rmtree(PROCESSED_DIR / file_id, ignore_errors=True)
    db.delete(r)
    db.commit()
    return {"ok": True}


@app.post("/api/files/bulk-delete")
def bulk_delete(
    req: BulkApply,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    deleted = 0
    for fid in req.file_ids:
        r = db.query(FileRecord).filter_by(id=fid).first()
        if not r:
            continue
        if r.session_id and r.session_id != sid:
            continue
        shutil.rmtree(UPLOAD_DIR / fid, ignore_errors=True)
        shutil.rmtree(PROCESSED_DIR / fid, ignore_errors=True)
        db.delete(r)
        deleted += 1
    db.commit()
    return {"deleted": deleted}


@app.get("/api/download-all")
def download_all(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    sid = get_session_id(request, response)
    records = (
        db.query(FileRecord)
        .filter(FileRecord.session_id == sid, FileRecord.status == "processed")
        .all()
    )
    if not records:
        raise HTTPException(404, "No processed files to download")

    def generate():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in records:
                if r.processed_path and Path(r.processed_path).exists():
                    arcname = Path(r.original_name).stem + "_interactive.pdf"
                    zf.write(r.processed_path, arcname)
        buf.seek(0)
        while True:
            chunk = buf.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    date = datetime.utcnow().strftime("%Y-%m-%d")
    headers = {"Content-Disposition": f'attachment; filename="interactive_pdfs_{date}.zip"'}
    return StreamingResponse(generate(), media_type="application/zip", headers=headers)


# ---------- Templates (session-scoped) ----------
@app.get("/api/templates")
def list_templates(request: Request, response: Response, db: Session = Depends(get_db)):
    sid = get_session_id(request, response)
    rows = db.query(Template).filter(Template.session_id == sid).order_by(Template.created_at.desc()).all()
    return {"templates": [{"id": r.id, "name": r.name, "config": r.config} for r in rows]}


@app.post("/api/templates")
def create_template(t: TemplateIn, request: Request, response: Response, db: Session = Depends(get_db)):
    sid = get_session_id(request, response)
    rec = Template(id=uuid.uuid4().hex, session_id=sid, name=t.name, config=t.config)
    db.add(rec)
    db.commit()
    return {"id": rec.id, "name": rec.name, "config": rec.config}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
    sid = get_session_id(request, response)
    r = db.query(Template).filter_by(id=template_id).first()
    if not r:
        raise HTTPException(404, "Template not found")
    if r.session_id and r.session_id != sid:
        raise HTTPException(403, "Not your template")
    db.delete(r)
    db.commit()
    return {"ok": True}


# ---------- Reset (session-scoped: only resets YOUR files) ----------
@app.post("/api/reset")
def reset_all(request: Request, response: Response, db: Session = Depends(get_db)):
    sid = get_session_id(request, response)
    records = db.query(FileRecord).filter(FileRecord.session_id == sid).all()
    for r in records:
        shutil.rmtree(UPLOAD_DIR / r.id, ignore_errors=True)
        shutil.rmtree(PROCESSED_DIR / r.id, ignore_errors=True)
        db.delete(r)
    db.commit()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
