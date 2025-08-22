FROM python:3.10-slim

# System deps: poppler for pdf2image + minimal libs for cv/paddle
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils libglib2.0-0 libsm6 libxext6 libxrender1 libgl1 gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Keep libraries from spawning many threads (saves RAM)
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Preload PaddleOCR models into the image to avoid cold-start timeouts
RUN python - <<'PY'
from paddleocr import PaddleOCR
PaddleOCR(use_angle_cls=True, lang='en')
print("✅ PaddleOCR models preloaded.")
PY

# App code
COPY . .

EXPOSE 5000

# Single worker to keep memory predictable; generous timeouts
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "2", "--timeout", "600", "--graceful-timeout", "120", "app:app"]
