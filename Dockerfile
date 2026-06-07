# syntax=docker/dockerfile:1

# ──────────────────────────────────────────────────────────────────────────
# FlowDraft / ArchDraft — FastAPI vision + compliance + finance API
# Run:  docker compose up --build   →   http://localhost:8000
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System libraries required at runtime:
#   tesseract-ocr           → pytesseract OCR (handwriting / dimensions)
#   libgl1 + libglib2.0-0   → OpenCV (cv2) shared libs
#   libgomp1                → OpenMP runtime (torch / sklearn / opencv)
#   curl                    → container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python deps (layer-cached on requirements.txt) ──────────────────────────
COPY requirements.txt ./

# Install CPU-only torch/torchvision first so we don't pull multi-GB CUDA wheels,
# then the rest of the stack (torch>=2.0 in requirements is already satisfied).
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install -r requirements.txt

# spaCy model used by the compliance rule extractor (bake it in so the first
# request doesn't try to download at runtime).
RUN python -m spacy download en_core_web_sm

# ── Application code ────────────────────────────────────────────────────────
COPY . .

# Writable dirs for runtime artifacts (parsed graphs, overlays, SQLite cache).
RUN mkdir -p data src/compliance_checker/data/.rules_cache

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
