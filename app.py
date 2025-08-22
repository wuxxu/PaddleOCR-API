from flask import Flask, request, Response, jsonify
from paddleocr import PaddleOCR
from docx import Document
from openpyxl import Workbook
from io import BytesIO
from zipfile import ZipFile
import tempfile, os, subprocess, re

app = Flask(__name__)

# Load OCR once (CPU)
ocr = PaddleOCR(use_angle_cls=True, lang='en', use_gpu=False)

@app.get("/")
def health():
    return {"ok": True, "service": "paddleocr-api"}, 200

def pdftopng(pdf_path: str, outdir: str, dpi: int = 300):
    """Use poppler's pdftoppm to create page-1.png, page-2.png, ..."""
    prefix = os.path.join(outdir, "page")
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", pdf_path, prefix], check=True)
    pages = sorted([os.path.join(outdir, f) for f in os.listdir(outdir)
                    if f.startswith("page-") and f.endswith(".png")])
    return pages

def single_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

@app.post("/convert")
def convert():
    # 1) basic checks
    if "pdf" not in request.files:
        return jsonify({"error": "send multipart/form-data with field 'pdf'"}), 400
    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "please upload a PDF"}), 400

    # optional DPI query: /convert?dpi=300|350|400
    try:
        dpi = int(request.args.get("dpi", "300"))
    except Exception:
        dpi = 300
    if dpi not in (300, 350, 400):
        dpi = 300

    # 2) work in a tempdir
    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, "in.pdf")
        f.save(pdf_path)

        # 3) PDF -> images
        try:
            pages = pdftopng(pdf_path, td, dpi=dpi)
        except subprocess.CalledProcessError as e:
            return jsonify({"error": f"pdftoppm failed: {e}"}), 500
        if not pages:
            return jsonify({"error": "no pages produced from PDF"}), 400

        # 4) OCR each page
        all_lines = []
        for img in pages:
            res = ocr.ocr(img, cls=True)
            # res is list of lines -> each line: [ [ [x,y]..4pts ], [ text, conf ] ]
            page_lines = []
            for line in res:
                # paddleocr returns [[x1,y1],[x2,y2],[x3,y3],[x4,y4]], [text, confidence]
                (box, rec) = line
                text = rec[0]
                # use top-left y and x for order
                y = min(p[1] for p in box)
                x = min(p[0] for p in box)
                page_lines.append((y, x, single_line(text)))
            page_lines.sort(key=lambda t: (t[0], t[1]))
            all_lines.append([t[2] for t in page_lines])

        # 5) Build DOCX (plain text for this first test)
        doc = Document()
        for i, lines in enumerate(all_lines, start=1):
            if i > 1:
                doc.add_page_break()
            for ln in lines:
                doc.add_paragraph(ln if ln else " ")
        docx_buf = BytesIO()
        doc.save(docx_buf)
        docx_bytes = docx_buf.getvalue()

        # 6) Build XLSX (placeholder for now)
        wb = Workbook()
        ws = wb.active
        ws.title = "TextPreview"
        r = 1
        for i, lines in enumerate(all_lines, start=1):
            ws.cell(row=r, column=1, value=f"Page {i}"); r += 1
            for ln in lines:
                ws.cell(row=r, column=1, value=ln); r += 1
            r += 1
        xlsx_buf = BytesIO(); wb.save(xlsx_buf); xlsx_bytes = xlsx_buf.getvalue()

        # 7) ZIP both and return
        zip_buf = BytesIO()
        with ZipFile(zip_buf, "w") as z:
            z.writestr("converted.docx", docx_bytes)
            z.writestr("tables.xlsx", xlsx_bytes)
        return Response(zip_buf.getvalue(),
                        mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=converted.zip"})
