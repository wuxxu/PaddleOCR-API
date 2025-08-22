FROM python:3.10-slim

# System deps (for image I/O and PDF rasterization if you later add it)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libsm6 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python deps
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy app code
COPY . /app

# Flask default
ENV PORT=5000
EXPOSE 5000

# If the repo's entrypoint is app.py with Flask:
CMD ["python", "app.py"]
