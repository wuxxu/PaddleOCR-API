FROM python:3.10-slim

# deps incl. poppler for pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils libglib2.0-0 libsm6 libxext6 libxrender-dev libgl1 gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# --- PRELOAD PADDLEOCR MODELS DURING BUILD (no cold-start) ---
# This downloads the 'en' detection/recognition models into the image layer.
RUN python - <<'PY'
from paddleocr import PaddleOCR
# This line triggers model download & caches inside the image
PaddleOCR(use_angle_cls=True, lang='en')
print("PaddleOCR models preloaded.")
PY

COPY . .
EXPOSE 5000

# increase worker timeout so heavy PDFs don’t kill the worker prematurely
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--timeout", "300", "app:app"]
