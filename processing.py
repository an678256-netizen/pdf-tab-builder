"""
PDF processing: inject an interactive click-to-toggle tab into PDFs.
Also handles DOCX → PDF conversion via LibreOffice headless (full fidelity).
"""
import subprocess
import uuid
import os
import tempfile
from pathlib import Path
from io import BytesIO

from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.colors import HexColor
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    NameObject, DictionaryObject, ArrayObject,
    NumberObject, FloatObject, create_string_object,
)


def convert_docx_to_pdf(docx_path: str, output_dir: str) -> str:
    """Convert DOCX to PDF using LibreOffice headless. Returns path to PDF."""
    docx_path = Path(docx_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use a unique profile dir per conversion to allow concurrent calls
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        cmd = [
            "soffice",
            "--headless",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf",
            "--outdir", str(output_dir),
            str(docx_path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")

        # LibreOffice produces <stem>.pdf in outdir
        out = output_dir / (docx_path.stem + ".pdf")
        if not out.exists():
            raise RuntimeError("LibreOffice did not produce expected output file")
        return str(out)
    finally:
        import shutil
        shutil.rmtree(profile, ignore_errors=True)


def get_pdf_page_info(pdf_path: str):
    """Return (num_pages, [(w,h), ...])."""
    reader = PdfReader(pdf_path)
    sizes = []
    for page in reader.pages:
        box = page.mediabox
        sizes.append((float(box.width), float(box.height)))
    return len(reader.pages), sizes


def _tab_size(label: str):
    """Compute tab dimensions from label text. MUST match frontend tabSize()."""
    s = label or "Show details"
    char_width = 7.0   # ~7pt per char at 11pt Helvetica
    padding = 20.0
    w = max(60.0, min(160.0, len(s) * char_width + padding))
    return w, 26.0


def _compute_layout(click_x, click_y, page_w, page_h, tab_label="Show details", panel_h_hint=130):
    """Given a click point (= desired tab center) in PDF coords, compute tab and
    panel rectangles. MUST match the frontend computeLayout() in frontend.html."""
    tab_w, tab_h = _tab_size(tab_label)
    tab_w = min(tab_w, page_w - 40)
    panel_w = min(page_w - 40, 420.0)
    panel_h = min(panel_h_hint, page_h - tab_h - 60)

    tab_x = click_x - tab_w / 2
    tab_y = click_y - tab_h / 2
    tab_x = max(20, min(page_w - tab_w - 20, tab_x))
    tab_y = max(20, min(page_h - tab_h - 20, tab_y))

    panel_x = tab_x + tab_w / 2 - panel_w / 2
    panel_x = max(20, min(page_w - panel_w - 20, panel_x))

    # Prefer above the tab (higher y = above in PDF coords)
    panel_y = tab_y + tab_h + 10
    if panel_y + panel_h > page_h - 20:
        panel_y = tab_y - panel_h - 10
        if panel_y < 20:
            panel_y = 20

    return dict(
        tab_x=tab_x, tab_y=tab_y, tab_w=tab_w, tab_h=tab_h,
        panel_x=panel_x, panel_y=panel_y, panel_w=panel_w, panel_h=panel_h,
    )


def inject_tab(input_pdf_path: str, output_pdf_path: str, config: dict):
    """
    Inject a click-to-toggle tab into a PDF.

    config = {
        "page_index": 0,              # which page (0-based)
        "click_x": 300, "click_y": 80, # tab center in PDF coords
        "tab_label": "Show details",
        "hidden_text": "Revealed content here",
    }
    """
    page_index = int(config.get("page_index", 0))
    click_x = float(config["click_x"])
    click_y = float(config["click_y"])
    tab_label = config.get("tab_label", "Show details")
    hidden_text = config.get("hidden_text", "")

    # First, we need a text-field widget on the target page. reportlab can only
    # create forms on a new doc, so we build a minimal overlay PDF with just
    # the text field, then merge it in. Actually it's easier to add the text
    # field directly using pypdf low-level objects.

    reader = PdfReader(input_pdf_path)
    writer = PdfWriter(clone_from=reader)

    if page_index >= len(writer.pages):
        page_index = 0
    page = writer.pages[page_index]
    box = page.mediabox
    page_w = float(box.width)
    page_h = float(box.height)

    layout = _compute_layout(click_x, click_y, page_w, page_h, tab_label=tab_label)

    uniq = uuid.uuid4().hex[:8]
    panel_name = f"hp_{uniq}"
    btn_name = f"tb_{uniq}"

    show_cap = tab_label
    hide_cap = ("Hide " + tab_label.lower()[5:]) if tab_label.lower().startswith("show ") else "Hide"

    toggle_js = (
        f'var f=this.getField("{panel_name}");'
        f'var b=this.getField("{btn_name}");'
        f'if(f.display==display.hidden){{f.display=display.visible;b.buttonSetCaption({_js_str(hide_cap)});}}'
        f'else{{f.display=display.hidden;b.buttonSetCaption({_js_str(show_cap)});}}'
    )
    open_js = (
        f'this.getField("{panel_name}").display=display.hidden;'
        f'this.getField("{btn_name}").buttonSetCaption({_js_str(show_cap)});'
    )

    # --- Build the text field (hidden panel) ---
    # Default appearance string: Helvetica 11, dark blue text
    panel_da = create_string_object("/Helv 11 Tf 0.10 0.21 0.36 rg")

    panel_mk = DictionaryObject({
        NameObject("/BC"): ArrayObject([FloatObject(0.17), FloatObject(0.42), FloatObject(0.69)]),
        NameObject("/BG"): ArrayObject([FloatObject(0.92), FloatObject(0.96), FloatObject(1.0)]),
    })
    panel_bs = DictionaryObject({
        NameObject("/Type"): NameObject("/Border"),
        NameObject("/W"): NumberObject(1),
        NameObject("/S"): NameObject("/S"),
    })

    panel_field = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"),
        NameObject("/Ff"): NumberObject((1 << 0) | (1 << 12)),  # ReadOnly + Multiline
        NameObject("/T"): create_string_object(panel_name),
        NameObject("/TU"): create_string_object("Hidden panel"),
        NameObject("/V"): create_string_object(hidden_text),
        NameObject("/DV"): create_string_object(hidden_text),
        NameObject("/Rect"): ArrayObject([
            FloatObject(layout["panel_x"]),
            FloatObject(layout["panel_y"]),
            FloatObject(layout["panel_x"] + layout["panel_w"]),
            FloatObject(layout["panel_y"] + layout["panel_h"]),
        ]),
        NameObject("/MK"): panel_mk,
        NameObject("/BS"): panel_bs,
        NameObject("/DA"): panel_da,
        # F flag bit 2 = Hidden: not shown on screen or print until JS reveals it.
        # This makes the panel invisible in viewers without JS (Preview.app, browsers).
        NameObject("/F"): NumberObject(2),
        NameObject("/P"): page.indirect_reference,
    })
    panel_ref = writer._add_object(panel_field)

    # --- Build the pushbutton (tab) ---
    btn_mk = DictionaryObject({
        NameObject("/CA"): create_string_object(show_cap),
        NameObject("/BC"): ArrayObject([FloatObject(0.17), FloatObject(0.42), FloatObject(0.69)]),
        NameObject("/BG"): ArrayObject([FloatObject(0.17), FloatObject(0.42), FloatObject(0.69)]),
    })
    btn_bs = DictionaryObject({
        NameObject("/Type"): NameObject("/Border"),
        NameObject("/W"): NumberObject(1),
        NameObject("/S"): NameObject("/S"),
    })
    action = DictionaryObject({
        NameObject("/Type"): NameObject("/Action"),
        NameObject("/S"): NameObject("/JavaScript"),
        NameObject("/JS"): create_string_object(toggle_js),
    })
    btn_da = create_string_object("/Helv 13 Tf 1 1 1 rg")

    btn_field = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Btn"),
        NameObject("/Ff"): NumberObject(1 << 16),  # pushbutton
        NameObject("/T"): create_string_object(btn_name),
        NameObject("/TU"): create_string_object("Click to show/hide"),
        NameObject("/Rect"): ArrayObject([
            FloatObject(layout["tab_x"]),
            FloatObject(layout["tab_y"]),
            FloatObject(layout["tab_x"] + layout["tab_w"]),
            FloatObject(layout["tab_y"] + layout["tab_h"]),
        ]),
        NameObject("/MK"): btn_mk,
        NameObject("/BS"): btn_bs,
        NameObject("/A"): action,
        NameObject("/H"): NameObject("/P"),
        NameObject("/DA"): btn_da,
        NameObject("/F"): NumberObject(4),
        NameObject("/P"): page.indirect_reference,
    })
    btn_ref = writer._add_object(btn_field)

    # Attach to page annotations
    annots_key = NameObject("/Annots")
    if annots_key in page:
        page[annots_key].append(panel_ref)
        page[annots_key].append(btn_ref)
    else:
        page[annots_key] = ArrayObject([panel_ref, btn_ref])

    # Ensure AcroForm exists and register fields there
    root = writer._root_object
    if "/AcroForm" not in root:
        root[NameObject("/AcroForm")] = DictionaryObject({
            NameObject("/Fields"): ArrayObject(),
        })
    acroform = root["/AcroForm"]
    if "/Fields" not in acroform:
        acroform[NameObject("/Fields")] = ArrayObject()
    acroform["/Fields"].append(panel_ref)
    acroform["/Fields"].append(btn_ref)
    acroform[NameObject("/NeedAppearances")] = NumberObject(1)

    # Document-level open action (JS)
    writer.add_js(open_js)

    os.makedirs(os.path.dirname(output_pdf_path), exist_ok=True)
    with open(output_pdf_path, "wb") as f:
        writer.write(f)


def _js_str(s: str) -> str:
    """JSON-style quote a JS string."""
    import json
    return json.dumps(s)
