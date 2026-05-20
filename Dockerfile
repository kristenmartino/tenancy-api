# Switched off Nixpacks because aptPkgs in nixpacks.toml silently didn't
# install (tesseract was missing at runtime). Dockerfile gives us explicit
# control over the system layer, and Railway auto-detects it over Nixpacks.

FROM python:3.12-slim

# Make print() / stderr show up in Railway logs in real-time instead of
# being buffered indefinitely. Why we lost "[ocr] done" messages while
# debugging Path A — OCR finished, but the buffered print never flushed
# before the next event came in.
ENV PYTHONUNBUFFERED=1

# OCR system dependencies for ocrmypdf
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        ghostscript \
        qpdf \
        unpaper \
        pngquant \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache the pip layer separately so app code changes don't bust it
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway sets $PORT at runtime. Shell form so the variable interpolates.
CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
