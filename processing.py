"""
PDF processing: add a click-to-reveal page jump to PDFs.

Uses native PDF link annotations that jump to a reveal page appended to the end
of the document. Works identically in EVERY PDF viewer (Chrome, Safari, Firefox,
Preview, iOS/Android, Adobe Reader) — full click-to-open behavior with natural
page scrolling for unlimited text length.

Also handles DOCX → PDF conversion via LibreOffice headless.

Requires: pypdf, reportlab (add `reportlab` to requirements.txt if missing).
"""
import subprocess
import tempfile
import shutil
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    NameObject, DictionaryObject, ArrayObject,
    NumberObject, FloatObject, create_string_object,
    DecodedStreamObject,
)
from reportlab.pdfgen import canvas as rlcanvas
from reportlab.pdfbase.pdfmetrics import stringWidth


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


# Tab icon size (PDF points). MUST match frontend TAB_W / TAB_H.
TAB_W = 18.0
TAB_H = 18.0

# Reveal page layout
REVEAL_MARGIN = 56.0
REVEAL_BACK_LINK_TOP_OFFSET = 30.0   # distance from top of page to back-link baseline
REVEAL_BACK_LINK_WIDTH = 160.0
REVEAL_BODY_FONT = "Helvetica"
REVEAL_BODY_SIZE = 12
REVEAL_LINE_HEIGHT = 17.0
REVEAL_HEADER_FONT = "Helvetica-Bold"
REVEAL_HEADER_SIZE = 22


def _compute_layout(click_x, click_y, page_w, page_h):
    """Return tab rectangle clamped to page bounds. Click point = tab center."""
    tab_w, tab_h = TAB_W, TAB_H
    tab_x = max(2, min(page_w - tab_w - 2, click_x - tab_w / 2))
    tab_y = max(2, min(page_h - tab_h - 2, click_y - tab_h / 2))
    return {"tab_x": tab_x, "tab_y": tab_y, "tab_w": tab_w, "tab_h": tab_h}


def _build_icon_appearance_stream(writer):
    """Small white box with gray 'v' chevron, used as /AP /N on the link annotation."""
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


def _build_reveal_pdf_bytes(hidden_text: str, tab_label: str, page_w: float, page_h: float) -> bytes:
    """Render the hidden message as clean, word-wrapped PDF page(s) with back links."""
    buf = BytesIO()
    c = rlcanvas.Canvas(buf, pagesize=(page_w, page_h))

    raw_label = (tab_label or "").strip()
    show_title = bool(raw_label) and raw_label.lower() not in ("show details", "note", "details", "")

    max_width = page_w - REVEAL_MARGIN * 2
    min_y = REVEAL_MARGIN + 40

    def draw_back_link():
        y = page_h - REVEAL_BACK_LINK_TOP_OFFSET
        c.setFont(REVEAL_BODY_FONT, 10)
        c.setFillColorRGB(0.15, 0.39, 0.92)
        c.drawString(REVEAL_MARGIN, y, "\u2190 Back to document")
        c.setFillColorRGB(0.15, 0.15, 0.15)
        c.setFont(REVEAL_BODY_FONT, REVEAL_BODY_SIZE)

    def new_page():
        c.showPage()
        draw_back_link()

    draw_back_link()
    y = page_h - REVEAL_BACK_LINK_TOP_OFFSET - 30

    if show_title:
        c.setFont(REVEAL_HEADER_FONT, REVEAL_HEADER_SIZE)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawString(REVEAL_MARGIN, y - REVEAL_HEADER_SIZE, raw_label)
        y -= REVEAL_HEADER_SIZE + 14
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.5)
        c.line(REVEAL_MARGIN, y, page_w - REVEAL_MARGIN, y)
        y -= 22

    c.setFont(REVEAL_BODY_FONT, REVEAL_BODY_SIZE)
    c.setFillColorRGB(0.15, 0.15, 0.15)

    text = hidden_text or "(no message)"
    for raw_paragraph in text.split("\n"):
        if not raw_paragraph.strip():
            y -= REVEAL_LINE_HEIGHT * 0.6
            if y < min_y:
                new_page()
                y = page_h - REVEAL_BACK_LINK_TOP_OFFSET - 30
            continue
        words = raw_paragraph.split(" ")
        current = ""
        for word in words:
            candidate = (current + " " + word).strip() if current else word
            if stringWidth(candidate, REVEAL_BODY_FONT, REVEAL_BODY_SIZE) <= max_width:
                current = candidate
            else:
                if current:
                    c.drawString(REVEAL_MARGIN, y, current)
                    y -= REVEAL_LINE_HEIGHT
                    if y < min_y:
                        new_page()
                        y = page_h - REVEAL_BACK_LINK_TOP_OFFSET - 30
                current = word
        if current:
            c.drawString(REVEAL_MARGIN, y, current)
            y -= REVEAL_LINE_HEIGHT
            if y < min_y:
                new_page()
                y = page_h - REVEAL_BACK_LINK_TOP_OFFSET - 30
        y -= 5

    c.save()
    return buf.getvalue()


def _make_link_annot(rect, target_page_ref):
    """Create a /Link annotation with /GoTo action to a specific page ref."""
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
        NameObject("/Border"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)]),
        NameObject("/A"): DictionaryObject({
            NameObject("/Type"): NameObject("/Action"),
            NameObject("/S"): NameObject("/GoTo"),
            NameObject("/D"): ArrayObject([target_page_ref, NameObject("/Fit")]),
        }),
        NameObject("/H"): NameObject("/I"),
        NameObject("/F"): NumberObject(4),
    })


def inject_tab(input_pdf_path: str, output_pdf_path: str, config: dict):
    """
    Add a click-to-reveal page-jump to the PDF.

    config = {
        "page_index": 0,
        "click_x": 300, "click_y": 80,
        "tab_label": "Details",   # used as title on reveal page if set
        "hidden_text": "Revealed content",
    }

    Clicking the "v" icon jumps to a reveal page appended at the end of the PDF
    containing the hidden_text formatted for easy reading. Each reveal page has
    a "← Back to document" link at top-left returning to the source page.

    Idempotent: strips previously-added reveal pages and tab annotations so
    re-processing the same file produces exactly one tab / one set of reveal
    pages.
    """
    page_index = int(config.get("page_index", 0))
    click_x = float(config["click_x"])
    click_y = float(config["click_y"])
    tab_label = (config.get("tab_label") or "").strip()
    hidden_text = config.get("hidden_text") or ""

    reader = PdfReader(input_pdf_path)

    # Detect and strip previously-added reveal pages
    reveal_key = NameObject("/TabBuilderReveal")
    has_old_reveal = any(reveal_key in p for p in reader.pages)
    if has_old_reveal:
        writer = PdfWriter()
        for p in reader.pages:
            if reveal_key in p:
                continue
            writer.add_page(p)
    else:
        writer = PdfWriter(clone_from=reader)

    if page_index >= len(writer.pages):
        page_index = 0

    source_page = writer.pages[page_index]
    box = source_page.mediabox
    page_w = float(box.width)
    page_h = float(box.height)

    # Strip old tab annotations
    annots_key = NameObject("/Annots")
    if annots_key in source_page:
        existing = source_page[annots_key]
        kept = ArrayObject()
        for a in existing:
            try:
                ao = a.get_object()
                if ao.get("/Subtype") == "/Text":
                    continue
                if ao.get("/Subtype") == "/Link" and ao.get("/TabBuilder") == NameObject("/Source"):
                    continue
            except Exception:
                pass
            kept.append(a)
        source_page[annots_key] = kept

    layout = _compute_layout(click_x, click_y, page_w, page_h)

    # Build and append reveal pages
    reveal_bytes = _build_reveal_pdf_bytes(hidden_text, tab_label, page_w, page_h)
    reveal_reader = PdfReader(BytesIO(reveal_bytes))
    reveal_page_refs = []
    for rp in reveal_reader.pages:
        added = writer.add_page(rp)
        added[reveal_key] = NameObject("/True")
        reveal_page_refs.append(added.indirect_reference)

    # Add "← Back to document" link on each reveal page
    source_page_ref = source_page.indirect_reference
    back_link_baseline_y = page_h - REVEAL_BACK_LINK_TOP_OFFSET
    back_link_rect = [
        REVEAL_MARGIN - 4,
        back_link_baseline_y - 4,
        REVEAL_MARGIN + REVEAL_BACK_LINK_WIDTH,
        back_link_baseline_y + 12,
    ]
    for rp_ref in reveal_page_refs:
        rp = rp_ref.get_object()
        back_annot = _make_link_annot(back_link_rect, source_page_ref)
        back_ref = writer._add_object(back_annot)
        if annots_key in rp:
            rp[annots_key].append(back_ref)
        else:
            rp[annots_key] = ArrayObject([back_ref])

    # Build the clickable "v" icon on the source page
    ap_ref = _build_icon_appearance_stream(writer)
    tab_rect = [
        layout["tab_x"],
        layout["tab_y"],
        layout["tab_x"] + layout["tab_w"],
        layout["tab_y"] + layout["tab_h"],
    ]
    link_annot = _make_link_annot(tab_rect, reveal_page_refs[0])
    link_annot[NameObject("/AP")] = DictionaryObject({NameObject("/N"): ap_ref})
    link_annot[NameObject("/TabBuilder")] = NameObject("/Source")

    link_ref = writer._add_object(link_annot)
    if annots_key in source_page:
        source_page[annots_key].append(link_ref)
    else:
        source_page[annots_key] = ArrayObject([link_ref])

    with open(output_pdf_path, "wb") as f:
        writer.write(f)
