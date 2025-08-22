import os, threading, io, re
from flask import Flask, request, send_file, jsonify
from pdf2image import convert_from_path
from paddleocr import PaddleOCR
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from PIL import Image
import numpy as np

# Allow very large PDFs, but we’ll downscale ourselves
Image.MAX_IMAGE_PIXELS = None

app = Flask(__name__)
ocr = PaddleOCR(use_angle_cls=True, lang='en')  # GPU arg removed

import shutil

@app.get("/debug")
def debug():
    return {
        "alive": True,
        "pdftoppm": shutil.which("pdftoppm") or "not-found",
    }, 200

# ---- OPTIONAL: warm up at startup (tiny image) ----
def _warmup():
    try:
        img = Image.new("RGB", (32, 32), "white")
        buf = "/tmp/warm.png"
        img.save(buf, "PNG")
        # call OCR on a tiny numpy image (no cls arg)
        _ = ocr.ocr(np.array(img))
        print("Warmup OCR done")
    except Exception as e:
        print("Warmup failed:", e)

threading.Thread(target=_warmup, daemon=True).start()

# ---------------- Small helpers ----------------
def single_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def is_bullet(text: str) -> bool:
    return bool(re.match(r"^\s*([•\-–—▪‣◦])\s+", text))

def is_numbered(text: str) -> bool:
    return bool(re.match(r"^\s*\(?\d{1,3}[\.\)]\s+", text))

def caps_ratio(s: str) -> float:
    letters = "".join(ch for ch in s if ch.isalpha())
    if not letters: return 0.0
    caps = sum(1 for ch in letters if ch.isupper())
    return caps / max(1, len(letters))

def box_left(box):  # min x
    return min(p[0] for p in box)

def box_right(box):  # max x
    return max(p[0] for p in box)

def box_top(box):  # min y
    return min(p[1] for p in box)

def box_bottom(box):  # max y
    return max(p[1] for p in box)

def box_height(box):
    return box_bottom(box) - box_top(box)

def group_lines_by_y(items, y_band):
    """items: list of dicts with y, x, text, h; returns list of line-lists"""
    items = sorted(items, key=lambda w: (w["y"], w["x"]))
    lines = []
    cur = []
    last_y = None
    for w in items:
        if last_y is not None and abs(w["y"] - last_y) > y_band:
            if cur: lines.append(cur)
            cur = []
        cur.append(w)
        last_y = w["y"]
    if cur: lines.append(cur)
    return lines

def median(nums):
    if not nums: return 0
    s = sorted(nums)
    n = len(s)
    return (s[n//2] if n % 2 == 1 else (s[n//2-1] + s[n//2]) / 2)


def clamp_image(pil_img, max_pixels=60_000_000, max_side=4000):
    w, h = pil_img.size
    if w * h <= max_pixels and max(w, h) <= max_side:
        return pil_img
    # scale to satisfy both constraints
    from math import sqrt
    sf1 = sqrt(max_pixels / float(w * h))    # pixel budget
    sf2 = max_side / float(max(w, h))        # longest side
    sf = min(sf1, sf2, 1.0)
    new_w, new_h = max(1, int(w * sf)), max(1, int(h * sf))
    return pil_img.resize((new_w, new_h), Image.LANCZOS)

# ---------------- Column detection ----------------
def split_columns(line_items, page_width):
    """
    Split into up to 2 columns using simple midpoint heuristic.
    line_items: list of {x,y,h,text,box}
    """
    if not line_items:
        return [line_items]

    xs = [w["x"] for w in line_items]
    minx, maxx = min(xs), max(xs)
    width = maxx - minx
    if width < 1:
        return [line_items]

    # If there is a clear gap near page middle, split; else single column
    mid = page_width / 2.0
    left_col = [w for w in line_items if w["x"] < mid]
    right_col = [w for w in line_items if w["x"] >= mid]

    # Require both sides to have content; else return single column
    if len(left_col) >= max(6, len(line_items) * 0.2) and len(right_col) >= max(6, len(line_items) * 0.2):
        # Sort each by y then x
        left_col.sort(key=lambda w: (w["y"], w["x"]))
        right_col.sort(key=lambda w: (w["y"], w["x"]))
        return [left_col, right_col]
    else:
        return [sorted(line_items, key=lambda w: (w["y"], w["x"]))]

# ---------------- Table detection ----------------
def detect_table_rows(line_words):
    """
    Given words belonging to the same 'block' (here we use full page/column),
    try to build rows by Y and columns by X gaps.
    Return rows (list of list of strings) if a table is likely, else None.
    """
    if len(line_words) < 8:
        return None

    # Group into lines by Y proximity
    hs = [w["h"] for w in line_words]
    medH = median(hs) or 12
    y_band = max(12, int(round(medH * 0.9)))
    lines = group_lines_by_y(line_words, y_band)

    # For each text line, split into cells by large X gaps
    rows = []
    multi_count = 0
    for ln in lines:
        ln = sorted(ln, key=lambda w: w["x"])
        # compute gaps
        gaps = []
        for i in range(1, len(ln)):
            gaps.append(ln[i]["x"] - ln[i-1]["x"])
        medGap = median(gaps) or 32
        x_gap = max(28, int(round(medGap * 1.6)))

        cells = []
        buf = ""
        last_x = None
        for w in ln:
            if last_x is not None and (w["x"] - last_x) > x_gap:
                if buf.strip():
                    cells.append(single_space(buf))
                buf = ""
            buf += ("" if not buf else " ") + w["text"]
            last_x = w["x"]
        if buf.strip():
            cells.append(single_space(buf))

        if len(cells) >= 2:
            multi_count += 1
        rows.append(cells)

    # Decide if it's really a table: need at least 2 rows with 2+ cells
    if multi_count >= 2:
        # Normalize all rows to same number of cols by padding
        maxc = max(len(r) for r in rows)
        for r in rows:
            while len(r) < maxc:
                r.append("")
        return rows
    return None

# ---------------- Heading / list / paragraph decisions ----------------
def classify_block(lines_in_block, medH):
    """
    lines_in_block: list of line dicts [{text,left,right,avgH,cx}]
    Returns one of:
      ('heading', level, text)
      ('list', ordered, items)
      ('paragraph', text)
    """
    if not lines_in_block:
        return ('paragraph', None, "")

    # Single string joined by newlines
    full = "\n".join([ln["text"] for ln in lines_in_block]).strip()
    if not full:
        return ('paragraph', None, "")

    sizes = [ln["avgH"] for ln in lines_in_block]
    avgSize = sum(sizes) / max(1, len(sizes))
    isShort = len(re.sub(r"\s+", "", full)) <= 80
    capR = caps_ratio(full)

    # Heading if bigger than normal and short
    if avgSize > 1.4 * medH and isShort and capR >= 0.4:
        level = 1 if avgSize > 1.8 * medH else 2
        return ('heading', level, single_space(full))

    # List detection
    first = lines_in_block[0]["text"]
    bullet = is_bullet(first)
    numbered = is_numbered(first)
    if bullet or numbered:
        items = [single_space(ln["text"]) for ln in lines_in_block]
        return ('list', numbered, items)

    # Paragraph
    return ('paragraph', None, re.sub(r"(\w)-\n(\w)", r"\1\2", full))

# ---------------- Core: convert a single page image to DOCX content ----------------
def convert_page_to_docx(image, doc: Document):
    img_small = clamp_image(image)        # downscale if needed
    res = ocr.ocr(np.array(img_small))    # newer paddleocr: no cls kwarg
    
    if not res or not res[0]:
        return

    page_width, page_height = image.size
    lines = res[0]  # list of [box, (text, conf)]

    # word-level items for this page
    words = []
    for box, (text, conf) in lines:
        if not text:
            continue
        x = box_left(box)
        y = box_top(box)
        h = max(1, box_height(box))
        words.append({"text": single_space(text), "x": x, "y": y, "h": h, "box": box})

    if not words:
        return

    # Split into 1 or 2 columns
    columns = split_columns(words, page_width)

    # Process each column independently in reading order (left first)
    for col_words in columns:
        if not col_words:
            continue

        # Median line height on this column
        medH = median([w["h"] for w in col_words]) or 12
        y_band = max(12, int(round(medH * 0.9)))

        # First, try table detection on this column; if table-like, insert a table and continue
        maybe_table = detect_table_rows(col_words)
        if maybe_table:
            tbl = doc.add_table(rows=len(maybe_table), cols=max(len(r) for r in maybe_table))
            tbl.style = "Table Grid"
            for r_i, row in enumerate(maybe_table):
                for c_i, cell in enumerate(row):
                    tbl.cell(r_i, c_i).text = cell
            doc.add_paragraph()  # spacing after table
            continue

        # Otherwise, produce paragraphs/lists/headings.
        # Build text lines (group words by Y), then make "line objects"
        line_groups = group_lines_by_y(col_words, y_band)
        line_objs = []
        for ln in line_groups:
            ln = sorted(ln, key=lambda w: w["x"])
            text = " ".join(w["text"] for w in ln).strip()
            if not text:
                continue
            left = min(w["x"] for w in ln)
            right = max(w["x"] + 1 for w in ln)  # +1 to avoid zero
            avgH = sum(w["h"] for w in ln) / max(1, len(ln))
            cx = (left + right) / 2
            line_objs.append({"text": text, "left": left, "right": right, "avgH": avgH, "cx": cx})

        # Now chunk the line_objs into blocks separated by blank lines / big Y gaps
        # (We already grouped by Y; treat each line as its own unless list/headings combine them.)
        # For simplicity, we’ll scan line_objs in order and form blocks where bullet/numbering continues.
        blocks = []
        buf = []
        for lo in line_objs:
            if not buf:
                buf.append(lo)
                continue
            # If previous looked like the same list type (bullet/numbered), keep grouping
            prev = buf[-1]
            prev_is_list = is_bullet(prev["text"]) or is_numbered(prev["text"])
            cur_is_list = is_bullet(lo["text"]) or is_numbered(lo["text"])
            if prev_is_list and cur_is_list:
                buf.append(lo)
            else:
                # start a new block if text class probably changes
                blocks.append(buf)
                buf = [lo]
        if buf:
            blocks.append(buf)

        # Render blocks
        for block in blocks:
            kind, meta, content = classify_block(block, medH)
            if kind == 'heading':
                level = meta or 2
                doc.add_heading(content, level=level)
            elif kind == 'list':
                ordered = bool(meta)
                for i, item in enumerate(content, start=1):
                    p = doc.add_paragraph()
                    run = p.add_run(f"{i}. " if ordered else "• ")
                    run.bold = True
                    p.add_run(item)
                    p.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
            else:  # paragraph
                # Alignment heuristic based on average center
                centers = [lo["cx"] for lo in block]
                avg_cx = sum(centers) / max(1, len(centers))
                if avg_cx < page_width * 0.35:
                    align = WD_PARAGRAPH_ALIGNMENT.LEFT
                elif avg_cx > page_width * 0.65:
                    align = WD_PARAGRAPH_ALIGNMENT.RIGHT
                else:
                    align = WD_PARAGRAPH_ALIGNMENT.CENTER

                # Font size heuristic based on avg height
                avgH_block = sum(lo["avgH"] for lo in block) / max(1, len(block))
                size_pt = Pt(min(max(int(avgH_block / 2), 9), 16))

                p = doc.add_paragraph()
                p.alignment = align
                r = p.add_run(content)
                r.font.size = size_pt

# ---------------- Routes ----------------
@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "OCR PDF → DOCX (layout)"}), 200

@app.route("/pdf-to-docx", methods=["POST"])
def pdf_to_docx():
    if "file" not in request.files:
        return jsonify({"error": "send multipart/form-data with field 'file' (PDF)"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "please upload a PDF"}), 400

    # Controls to keep under timeout
    try:
        dpi = int(request.args.get("dpi", "300"))
    except Exception:
        dpi = 300
    dpi = 300 if dpi not in (300, 350, 400) else dpi

    # Limit pages for speed (especially on first run)
    # Usage: ?first=1&last=2  -> only first two pages
    first = request.args.get("first")
    last  = request.args.get("last")
    first_page = int(first) if first and first.isdigit() else None
    last_page  = int(last)  if last  and last.isdigit()  else None

    input_path = "/tmp/input.pdf"
    output_path = "/tmp/output.docx"
    f.save(input_path)

    try:
        if first_page or last_page:
            kw = {}
            if first_page: kw["first_page"] = first_page
            if last_page:  kw["last_page"]  = last_page
            images = convert_from_path(input_path, dpi=dpi, **kw)
        else:
            images = convert_from_path(input_path, dpi=dpi)
    except Exception as e:
        return jsonify({"error": f"PDF rasterization failed: {e}"}), 500


    doc = Document()

    for page_idx, image in enumerate(images, start=1):
        convert_page_to_docx(image, doc)
        if page_idx < len(images):
            doc.add_page_break()

    try:
        doc.save(output_path)
    except Exception as e:
        return jsonify({"error": f"DOCX save failed: {e}"}), 500

    return send_file(output_path, as_attachment=True, download_name="converted.docx")
