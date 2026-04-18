"""
PDF processing: add a click-to-reveal sticky-note popup to PDFs.

Uses native PDF text annotations (/Subtype /Text). Click the tab icon in any
PDF viewer and the popup opens. Click outside to close.

Viewer behavior:
- Chrome: yellow popup rendered as part of the page. Long text extends below
  viewport; scroll the PDF page to see more.
- Preview, Firefox, Adobe Reader, mobile: popup opens as a floating window
  with its own scroll area. Click-to-open, click-to-close.

Also handles DOCX → PDF conversion via LibreOffice headless.
"""
import subprocess
import tempfile
import shutil
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    NameObject, DictionaryObject, ArrayObject,
    NumberObject, FloatObject, create_string_object,
    DecodedStreamObject,
)


def convert_docx_to_pdf(docx_path: str, output_dir: str) -> str:
    """Convert DOCX to PDF using LibreOffice headless. Returns path to PDF."""
    docx_path = Path(docx_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    profile = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        cmd = [
            "soffice", "--headless",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf",
            "--outdir", str(output_dir),
            str(docx_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")
        out = output_dir / (docx_path.stem + ".pdf")
        if not out.exists():
            raise RuntimeError("LibreOffice did not produce expected output file")
        return str(out)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def get_pdf_page_info(pdf_path: str):
    reader = PdfReader(pdf_path)
    sizes = []
    for page in reader.pages:
        box = page.mediabox
        sizes.append((float(box.width), float(box.height)))
    return len(reader.pages), sizes


# Tab icon size (PDF points). MUST match frontend TAB_W / TAB_H.
TAB_W = 18.0
TAB_H = 18.0


def _compute_layout(click_x, click_y, page_w, page_h):
    """Return tab rectangle clamped to page bounds. Click point = tab center."""
    tab_w, tab_h = TAB_W, TAB_H
    tab_x = max(2, min(page_w - tab_w - 2, click_x - tab_w / 2))
    tab_y = max(2, min(page_h - tab_h - 2, click_y - tab_h / 2))
    return {"tab_x": tab_x, "tab_y": tab_y, "tab_w": tab_w, "tab_h": tab_h}


def _build_icon_appearance_stream(writer):
    """Small white box with gray 'v' chevron, used as /AP /N on the annotation."""
    content = (
        b"q\n"
        b"0.5 0.5 0.5 RG\n"
        b"1 1 1 rg\n"
        b"0.75 w\n"
        b"1 1 16 16 re\n"
        b"B\n"
        b"Q\n"
        b"q\n"
        b"0.3 0.3 0.35 rg\n"
        b"BT\n"
        b"/F1 10 Tf\n"
        b"5 5 Td\n"
        b"(v) Tj\n"
        b"ET\n"
        b"Q\n"
    )
    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_ref = writer._add_object(font_dict)
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
    })
    ap_stream = DecodedStreamObject()
    ap_stream.set_data(content)
    ap_stream[NameObject("/Type")] = NameObject("/XObject")
    ap_stream[NameObject("/Subtype")] = NameObject("/Form")
    ap_stream[NameObject("/FormType")] = NumberObject(1)
    ap_stream[NameObject("/BBox")] = ArrayObject([
        NumberObject(0), NumberObject(0), NumberObject(18), NumberObject(18),
    ])
    ap_stream[NameObject("/Resources")] = resources
    return writer._add_object(ap_stream)


def inject_tab(input_pdf_path: str, output_pdf_path: str, config: dict):
    """
    Add a click-to-reveal sticky-note popup annotation to the PDF.

    Idempotent: strips any existing text annotations on the target page first,
    so re-processing a file produces exactly one tab, not a stack.
    """
    page_index = int(config.get("page_index", 0))
    click_x = float(config["click_x"])
    click_y = float(config["click_y"])
    tab_label = (config.get("tab_label") or "Note").strip() or "Note"
    hidden_text = config.get("hidden_text") or ""

    reader = PdfReader(input_pdf_path)
    writer = PdfWriter(clone_from=reader)

    if page_index >= len(writer.pages):
        page_index = 0
    page = writer.pages[page_index]
    box = page.mediabox
    page_w = float(box.width)
    page_h = float(box.height)

    # Strip any existing /Text annotations on the target page
    annots_key = NameObject("/Annots")
    if annots_key in page:
        existing = page[annots_key]
        kept = ArrayObject()
        for a in existing:
            try:
                ao = a.get_object()
                if ao.get("/Subtype") == "/Text":
                    continue
            except Exception:
                pass
            kept.append(a)
        page[annots_key] = kept

    layout = _compute_layout(click_x, click_y, page_w, page_h)
    ap_ref = _build_icon_appearance_stream(writer)

    text_annot = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Text"),
        NameObject("/Rect"): ArrayObject([
            FloatObject(layout["tab_x"]),
            FloatObject(layout["tab_y"]),
            FloatObject(layout["tab_x"] + layout["tab_w"]),
            FloatObject(layout["tab_y"] + layout["tab_h"]),
        ]),
        NameObject("/Contents"): create_string_object(hidden_text),
        NameObject("/T"): create_string_object(tab_label),
        NameObject("/Name"): NameObject("/Comment"),
        NameObject("/Open"): NumberObject(0),
        NameObject("/C"): ArrayObject([
            FloatObject(1.0), FloatObject(1.0), FloatObject(1.0),
        ]),
        NameObject("/F"): NumberObject(4),
        NameObject("/AP"): DictionaryObject({
            NameObject("/N"): ap_ref,
        }),
        NameObject("/P"): page.indirect_reference,
    })
    annot_ref = writer._add_object(text_annot)

    if annots_key in page:
        page[annots_key].append(annot_ref)
    else:
        page[annots_key] = ArrayObject([annot_ref])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)
