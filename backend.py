"""
FastAPI backend for the Interactive Tab PDF Builder.
- Uploads PDFs and DOCX files
- Stores metadata in SQLite
- Converts DOCX → PDF via LibreOffice headless
- Injects interactive tab per-file when config is saved
- Serves the React frontend as a static file
- Provides streaming zip download for bulk export
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

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

import processing

# ---------- Config ----------
BASE_DIR = Path(os.environ.get("BASE_DIR", "/data")).resolve()
UPLOAD_DIR = BASE_DIR / "uploads"     # original user files (incl. converted PDFs from docx)
PROCESSED_DIR = BASE_DIR / "processed" # final files with injected tab
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
    original_name = Column(String, nullable=False)
    kind = Column(String, nullable=False)          # "pdf" | "docx"
    source_path = Column(String, nullable=False)   # always a PDF after any docx conversion
    processed_path = Column(String, nullable=True)
    page_count = Column(Integer, default=1)
    page_sizes = Column(JSON, default=list)        # [[w,h], ...]
    config = Column(JSON, nullable=True)           # tab config
    status = Column(String, default="pending")     # pending|configured|processed|error|skipped
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Template(Base):
    __tablename__ = "templates"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    config = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Schemas ----------
class TabConfig(BaseModel):
    page_index: int = 0
    click_x: Optional[float] = None
    click_y: Optional[float] = None
    tab_label: str = "Show details"
    hidden_text: str = ""


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
)


# ---------- Helpers ----------
def _ensure_pdf(upload: UploadFile, save_dir: Path, file_id: str) -> tuple[str, str]:
    """Save the upload to disk. If DOCX, convert to PDF. Return (kind, pdf_path)."""
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

    # Convert DOCX to PDF
    try:
        pdf_path = processing.convert_docx_to_pdf(str(raw_path), str(dst_dir))
        return ("docx", pdf_path)
    except Exception as e:
        raise HTTPException(500, f"DOCX conversion failed: {e}")


def _process_file(file_id: str):
    """Background task: inject tab based on current config."""
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


@app.post("/api/files/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Upload one or more files (PDF or DOCX). Converts DOCX to PDF."""
    out = []
    for upload in files:
        file_id = uuid.uuid4().hex
        try:
            kind, pdf_path = _ensure_pdf(upload, UPLOAD_DIR, file_id)
            num_pages, sizes = processing.get_pdf_page_info(pdf_path)
            rec = FileRecord(
                id=file_id,
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
    return {"files": out}


@app.get("/api/files")
def list_files(
    status: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(FileRecord)
    if status:
        query = query.filter(FileRecord.status == status)
    if q:
        query = query.filter(FileRecord.original_name.ilike(f"%{q}%"))
    query = query.order_by(FileRecord.created_at.asc())
    return {"files": [FileOut.from_record(r).model_dump() for r in query.all()]}


@app.get("/api/files/stats")
def stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    rows = db.query(FileRecord.status, func.count(FileRecord.id)).group_by(FileRecord.status).all()
    counts = {s: c for s, c in rows}
    total = sum(counts.values())
    return {"total": total, "by_status": counts}


@app.get("/api/files/{file_id}")
def get_file(file_id: str, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    return FileOut.from_record(r).model_dump()


@app.get("/api/files/{file_id}/preview")
def get_preview(file_id: str, db: Session = Depends(get_db)):
    """Serve the original (converted if DOCX) PDF bytes for previewing in-browser."""
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    if not Path(r.source_path).exists():
        raise HTTPException(404, "Source file missing on disk")
    return FileResponse(r.source_path, media_type="application/pdf")


@app.put("/api/files/{file_id}/config")
def update_config(
    file_id: str,
    cfg: TabConfig,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    r.config = cfg.model_dump()
    r.status = "configured" if cfg.click_x is not None else "pending"
    r.updated_at = datetime.utcnow()
    db.commit()
    # Kick off processing if config is complete
    if r.status == "configured":
        background.add_task(_process_file, file_id)
    return FileOut.from_record(r).model_dump()


class SkipRequest(BaseModel):
    skip: bool = True


@app.post("/api/files/{file_id}/skip")
def skip_file(file_id: str, req: SkipRequest, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    r.status = "skipped" if req.skip else "pending"
    db.commit()
    return FileOut.from_record(r).model_dump()


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r:
        raise HTTPException(404, "File not found")
    # Clean up disk
    shutil.rmtree(UPLOAD_DIR / file_id, ignore_errors=True)
    shutil.rmtree(PROCESSED_DIR / file_id, ignore_errors=True)
    db.delete(r)
    db.commit()
    return {"ok": True}


@app.post("/api/files/bulk-apply")
def bulk_apply(
    req: BulkApply,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Apply the same config to many files at once."""
    cfg_obj = TabConfig(**req.config)
    updated = 0
    for fid in req.file_ids:
        r = db.query(FileRecord).filter_by(id=fid).first()
        if not r:
            continue
        r.config = cfg_obj.model_dump()
        r.status = "configured" if cfg_obj.click_x is not None else "pending"
        r.updated_at = datetime.utcnow()
        updated += 1
        if r.status == "configured":
            background.add_task(_process_file, fid)
    db.commit()
    return {"updated": updated}


@app.post("/api/files/bulk-delete")
def bulk_delete(req: BulkApply, db: Session = Depends(get_db)):
    # Reuses file_ids field from BulkApply for simplicity
    deleted = 0
    for fid in req.file_ids:
        r = db.query(FileRecord).filter_by(id=fid).first()
        if not r:
            continue
        shutil.rmtree(UPLOAD_DIR / fid, ignore_errors=True)
        shutil.rmtree(PROCESSED_DIR / fid, ignore_errors=True)
        db.delete(r)
        deleted += 1
    db.commit()
    return {"deleted": deleted}


@app.get("/api/files/{file_id}/download")
def download_one(file_id: str, db: Session = Depends(get_db)):
    r = db.query(FileRecord).filter_by(id=file_id).first()
    if not r or not r.processed_path or not Path(r.processed_path).exists():
        raise HTTPException(404, "Processed file not available")
    out_name = Path(r.original_name).stem + "_interactive.pdf"
    return FileResponse(r.processed_path, media_type="application/pdf", filename=out_name)


@app.get("/api/download-all")
def download_all(db: Session = Depends(get_db)):
    """Stream a zip of all processed files."""
    records = db.query(FileRecord).filter(FileRecord.status == "processed").all()
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


# ---------- Templates ----------
@app.get("/api/templates")
def list_templates(db: Session = Depends(get_db)):
    rows = db.query(Template).order_by(Template.created_at.desc()).all()
    return {"templates": [{"id": r.id, "name": r.name, "config": r.config} for r in rows]}


@app.post("/api/templates")
def create_template(t: TemplateIn, db: Session = Depends(get_db)):
    rec = Template(id=uuid.uuid4().hex, name=t.name, config=t.config)
    db.add(rec)
    db.commit()
    return {"id": rec.id, "name": rec.name, "config": rec.config}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str, db: Session = Depends(get_db)):
    r = db.query(Template).filter_by(id=template_id).first()
    if not r:
        raise HTTPException(404, "Template not found")
    db.delete(r)
    db.commit()
    return {"ok": True}


# ---------- Reset (danger zone) ----------
@app.post("/api/reset")
def reset_all(db: Session = Depends(get_db)):
    """Delete all files and records. Used by the 'Clear everything' button."""
    db.query(FileRecord).delete()
    db.commit()
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree(PROCESSED_DIR, ignore_errors=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
