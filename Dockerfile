FROM python:3.10-slim

# + poppler-utils so we have `pdftoppm` for PDF -> images
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libsm6 libxrender1 libxext6 libgomp1 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . /app

ENV PORT=5000
EXPOSE 5000
CMD ["python", "app.py"]
