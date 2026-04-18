"""
PDF processing: add a click-to-reveal "sticky note" annotation to PDFs.

Uses native PDF text annotations (/Subtype /Text) instead of form fields +
JavaScript. This works in EVERY PDF viewer (Chrome, Safari, Firefox, Preview,
iOS/Android, Adobe Reader) without needing any special software.

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
    """Return (num_pages, [(w,h), ...])."""
    reader = PdfReader(pdf_path)
    sizes = []
    for page in reader.pages:
        box = page.mediabox
        sizes.append((float(box.width), float(box.height)))
    return len(reader.pages), sizes


# Tab icon size (PDF points). MUST match the frontend TAB_W/TAB_H.
TAB_W = 18.0
TAB_H = 18.0


def _compute_layout(click_x, click_y, page_w, page_h):
    """Return tab rectangle clamped to page bounds. Click point = tab center."""
    tab_w, tab_h = TAB_W, TAB_H
    tab_x = max(2, min(page_w - tab_w - 2, click_x - tab_w / 2))
    tab_y = max(2, min(page_h - tab_h - 2, click_y - tab_h / 2))
    return {"tab_x": tab_x, "tab_y": tab_y, "tab_w": tab_w, "tab_h": tab_h}


def _build_icon_appearance_stream(writer) -> "IndirectObject":
    """Create a Form XObject that draws a small white box with gray 'v' chevron.
    Used as the custom appearance (/AP /N) for our annotation so the icon looks
    consistent across viewers instead of their default sticky-note graphic."""
    # PDF content stream commands:
    # - Draw an 18x18 white rectangle with a gray border
    # - Draw a gray "v" inside using Helvetica 10pt
    content = (
        b"q\n"
        b"0.5 0.5 0.5 RG\n"        # stroke color = gray
        b"1 1 1 rg\n"              # fill color = white
        b"0.75 w\n"                # line width
        b"1 1 16 16 re\n"          # rectangle path (1,1) 16x16
        b"B\n"                     # fill + stroke
        b"Q\n"
        b"q\n"
        b"0.3 0.3 0.35 rg\n"       # text fill = dark gray
        b"BT\n"
        b"/F1 10 Tf\n"             # Helvetica 10pt
        b"5 5 Td\n"                # move to text origin
        b"(v) Tj\n"                # show "v"
        b"ET\n"
        b"Q\n"
    )

    # Helvetica font resource (core PDF font, no embedding required)
    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    font_ref = writer._add_object(font_dict)

    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): font_ref,
        })
    })

    # The appearance stream itself
    ap_stream = DecodedStreamObject()
    ap_stream.set_data(content)
    ap_stream[NameObject("/Type")] = NameObject("/XObject")
    ap_stream[NameObject("/Subtype")] = NameObject("/Form")
    ap_stream[NameObject("/FormType")] = NumberObject(1)
    ap_stream[NameObject("/BBox")] = ArrayObject([
        NumberObject(0), NumberObject(0),
        NumberObject(18), NumberObject(18),
    ])
    ap_stream[NameObject("/Resources")] = resources

    return writer._add_object(ap_stream)


def inject_tab(input_pdf_path: str, output_pdf_path: str, config: dict):
    """
    Add a click-to-reveal sticky-note annotation to the PDF.

    config = {
        "page_index": 0,
        "click_x": 300, "click_y": 80,   # tab center in PDF coords
        "tab_label": "Note",              # popup title
        "hidden_text": "Revealed content",
    }

    Strips any existing /Text annotations on the target page first, so each
    PDF ends up with exactly one tab regardless of re-processing.
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

    # --- Strip any existing /Text annotations on this page -----------------
    # This handles two cases:
    #   1) Re-processing a file that we already processed (old tab would stay)
    #   2) Uploading a PDF that already contains sticky-notes from elsewhere
    annots_key = NameObject("/Annots")
    if annots_key in page:
        existing = page[annots_key]
        kept = ArrayObject()
        for a in existing:
            try:
                ao = a.get_object()
                if ao.get("/Subtype") == "/Text":
                    continue  # drop it
            except Exception:
                pass
            kept.append(a)
        page[annots_key] = kept

    layout = _compute_layout(click_x, click_y, page_w, page_h)

    # Build custom appearance stream (small white box with "v")
    ap_ref = _build_icon_appearance_stream(writer)

    # Build the text (sticky-note) annotation
    # This is a native PDF feature — no JavaScript, works in all viewers.
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
        NameObject("/Open"): NumberObject(0),          # popup closed by default
        NameObject("/C"): ArrayObject([                 # popup/icon color (white, was yellow sticky-note)
            FloatObject(1.0), FloatObject(1.0), FloatObject(1.0),
        ]),
        NameObject("/F"): NumberObject(4),              # flags: Print
        NameObject("/AP"): DictionaryObject({           # custom appearance
            NameObject("/N"): ap_ref,
        }),
        NameObject("/P"): page.indirect_reference,
    })

    annot_ref = writer._add_object(text_annot)

    # page[annots_key] was already normalized to an ArrayObject above
    if annots_key in page:
        page[annots_key].append(annot_ref)
    else:
        page[annots_key] = ArrayObject([annot_ref])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)
