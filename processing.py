"""
PDF processing: add multiple click-to-reveal sticky-note popups to PDFs.

Supports unlimited tabs across any page. Each tab is a native PDF text
annotation with a companion popup annotation. Config format:

    {"tabs": [
        {"id": "abc", "page_index": 0, "click_x": 100, "click_y": 500,
         "tab_label": "Old Code", "hidden_text": "...",
         "anchor_word": "grounding", "anchor_word_key": "3:2"},
        ...
    ]}

Also handles legacy single-tab format for backward compatibility.
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


TAB_W = 18.0
TAB_H = 18.0


def _compute_layout(click_x, click_y, page_w, page_h):
    tab_w, tab_h = TAB_W, TAB_H
    tab_x = max(2, min(page_w - tab_w - 2, click_x - tab_w / 2))
    tab_y = max(2, min(page_h - tab_h - 2, click_y - tab_h / 2))

    popup_w = min(420, page_w * 0.62)
    popup_h = min(360, page_h * 0.48)
    popup_x = tab_x + tab_w + 12
    popup_y = tab_y + tab_h / 2 - popup_h / 2
    if popup_x + popup_w > page_w - 10:
        popup_x = tab_x - popup_w - 12
    popup_x = max(10, min(page_w - popup_w - 10, popup_x))
    popup_y = max(10, min(page_h - popup_h - 10, popup_y))

    return {
        "tab_x": tab_x, "tab_y": tab_y, "tab_w": tab_w, "tab_h": tab_h,
        "popup_x": popup_x, "popup_y": popup_y, "popup_w": popup_w, "popup_h": popup_h,
    }


def _build_icon_appearance_stream(writer):
    content = (
        b"q\n0.5 0.5 0.5 RG\n1 1 1 rg\n0.75 w\n1 1 16 16 re\nB\nQ\n"
        b"q\n0.3 0.3 0.35 rg\nBT\n/F1 10 Tf\n5 5 Td\n(v) Tj\nET\nQ\n"
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


def _parse_tabs(config):
    """Parse config into a list of tab dicts. Supports both multi-tab and legacy formats."""
    if "tabs" in config:
        return [t for t in config["tabs"] if t.get("click_x") is not None]
    elif config.get("click_x") is not None:
        return [config]
    return []


def inject_tab(input_pdf_path: str, output_pdf_path: str, config: dict):
    """
    Inject one or more click-to-reveal sticky-note popups into a PDF.

    Supports config with "tabs" array (multi-tab) or legacy single-tab format.
    Idempotent: strips all existing /Text and /Popup annotations before adding.
    """
    tabs = _parse_tabs(config)

    if not tabs:
        shutil.copy2(input_pdf_path, output_pdf_path)
        return

    reader = PdfReader(input_pdf_path)
    writer = PdfWriter(clone_from=reader)
    annots_key = NameObject("/Annots")

    # Strip ALL existing /Text and /Popup annotations from ALL pages
    for page in writer.pages:
        if annots_key in page:
            kept = ArrayObject()
            for a in page[annots_key]:
                try:
                    ao = a.get_object()
                    if ao.get("/Subtype") in ("/Text", "/Popup"):
                        continue
                except Exception:
                    pass
                kept.append(a)
            page[annots_key] = kept

    # Build shared appearance stream (reused for all tab icons)
    ap_ref = _build_icon_appearance_stream(writer)

    # Inject each tab
    for tab in tabs:
        page_index = int(tab.get("page_index", 0))
        if page_index >= len(writer.pages):
            continue

        hidden_text = (tab.get("hidden_text") or "").strip()
        if not hidden_text:
            continue

        click_x = float(tab["click_x"])
        click_y = float(tab["click_y"])
        tab_label = (tab.get("tab_label") or "Note").strip() or "Note"

        page = writer.pages[page_index]
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        layout = _compute_layout(click_x, click_y, page_w, page_h)

        # Text annotation (icon + content)
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
            NameObject("/C"): ArrayObject([FloatObject(1), FloatObject(1), FloatObject(1)]),
            NameObject("/F"): NumberObject(4),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): ap_ref}),
            NameObject("/DA"): create_string_object("/Helv 13 Tf 0.10 0.10 0.10 rg"),
            NameObject("/P"): page.indirect_reference,
        })
        text_ref = writer._add_object(text_annot)

        # Popup annotation (size + position)
        popup_annot = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Popup"),
            NameObject("/Rect"): ArrayObject([
                FloatObject(layout["popup_x"]),
                FloatObject(layout["popup_y"]),
                FloatObject(layout["popup_x"] + layout["popup_w"]),
                FloatObject(layout["popup_y"] + layout["popup_h"]),
            ]),
            NameObject("/Parent"): text_ref,
            NameObject("/Open"): NumberObject(0),
            NameObject("/F"): NumberObject(0),
        })
        popup_ref = writer._add_object(popup_annot)
        text_annot[NameObject("/Popup")] = popup_ref

        # Add both to page
        if annots_key not in page:
            page[annots_key] = ArrayObject()
        page[annots_key].append(text_ref)
        page[annots_key].append(popup_ref)

    with open(output_pdf_path, "wb") as f:
        writer.write(f)
