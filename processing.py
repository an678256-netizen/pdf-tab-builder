"""
PDF processing: add a click-to-reveal sticky-note popup to PDFs.

Uses native PDF text annotations (/Subtype /Text) with an explicit /Popup
companion annotation that controls the popup's size and position. This gives
a large, readable popup that doesn't cover the tab icon.

Viewer behavior:
- Preview (Mac): popup opens as a floating window with scrollbar. Respects
  the /Popup rect for size and position. Click icon to open/close.
- Adobe Reader: same — floating, scrollable, respects size/position.
- Chrome: renders as a yellow box. Size/position partially respected.
  Long text: scroll the PDF page to see more.
- Firefox, mobile: popup behavior varies but content always accessible.

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
    """Return tab rect + popup rect. Popup is large and positioned to the
    right of the icon (or left if no space), never overlapping the icon."""
    tab_w, tab_h = TAB_W, TAB_H
    tab_x = max(2, min(page_w - tab_w - 2, click_x - tab_w / 2))
    tab_y = max(2, min(page_h - tab_h - 2, click_y - tab_h / 2))

    # Popup: ~60% page width, ~45% page height — large enough for real reading
    popup_w = min(420, page_w * 0.62)
    popup_h = min(360, page_h * 0.48)

    # Try placing popup to the RIGHT of the icon with a 12pt gap
    popup_x = tab_x + tab_w + 12
    # Vertically center on the icon
    popup_y = tab_y + tab_h / 2 - popup_h / 2

    # If popup goes off the right edge, place it to the LEFT of the icon
    if popup_x + popup_w > page_w - 10:
        popup_x = tab_x - popup_w - 12

    # Clamp to page bounds
    popup_x = max(10, min(page_w - popup_w - 10, popup_x))
    popup_y = max(10, min(page_h - popup_h - 10, popup_y))

    return {
        "tab_x": tab_x, "tab_y": tab_y, "tab_w": tab_w, "tab_h": tab_h,
        "popup_x": popup_x, "popup_y": popup_y, "popup_w": popup_w, "popup_h": popup_h,
    }


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
    Add a click-to-reveal sticky-note popup to the PDF with a properly sized
    and positioned popup window.

    Idempotent: strips existing text/popup annotations on the target page first.
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

    # Strip existing /Text and /Popup annotations (idempotent re-processing)
    annots_key = NameObject("/Annots")
    if annots_key in page:
        existing = page[annots_key]
        kept = ArrayObject()
        for a in existing:
            try:
                ao = a.get_object()
                if ao.get("/Subtype") in ("/Text", "/Popup"):
                    continue
            except Exception:
                pass
            kept.append(a)
        page[annots_key] = kept

    layout = _compute_layout(click_x, click_y, page_w, page_h)
    ap_ref = _build_icon_appearance_stream(writer)

    # --- Build the text annotation (the icon + content) ---
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
        # Default appearance: larger font for the popup content text
        NameObject("/DA"): create_string_object("/Helv 13 Tf 0.10 0.10 0.10 rg"),
        NameObject("/P"): page.indirect_reference,
    })
    text_annot_ref = writer._add_object(text_annot)

    # --- Build the popup annotation (controls popup window size + position) ---
    popup_annot = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Popup"),
        NameObject("/Rect"): ArrayObject([
            FloatObject(layout["popup_x"]),
            FloatObject(layout["popup_y"]),
            FloatObject(layout["popup_x"] + layout["popup_w"]),
            FloatObject(layout["popup_y"] + layout["popup_h"]),
        ]),
        NameObject("/Parent"): text_annot_ref,
        NameObject("/Open"): NumberObject(0),
        NameObject("/F"): NumberObject(0),   # don't print the popup window
    })
    popup_ref = writer._add_object(popup_annot)

    # Link the text annotation to its popup
    text_annot[NameObject("/Popup")] = popup_ref

    # Add both annotations to the page
    if annots_key in page:
        page[annots_key].append(text_annot_ref)
        page[annots_key].append(popup_ref)
    else:
        page[annots_key] = ArrayObject([text_annot_ref, popup_ref])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)
