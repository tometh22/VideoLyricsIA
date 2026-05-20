# ============================================================
# GenLy AI — Multi-stage production Dockerfile
# ============================================================

# --- Stage 1: Frontend build ---
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Backend ---
FROM python:3.11-slim AS backend

# System deps for whisper, moviepy, imagemagick.
# rclone para el worker que sube ProRes (16 GB) desde R2 a Drive
# server-to-server (~30x más rápido que descargar-y-resubir desde casa).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    imagemagick \
    libmagickwand-dev \
    fonts-dejavu-core \
    fonts-liberation \
    ca-certificates \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rclone version

# Fix ImageMagick policy to allow PDF/SVG operations
RUN sed -i 's/rights="none" pattern="@\*"/rights="read|write" pattern="@*"/' /etc/ImageMagick-6/policy.xml 2>/dev/null || true

WORKDIR /app

# Install Python deps
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./backend/

# Copy built frontend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Create outputs dir
RUN mkdir -p /app/outputs

# Env defaults (override in docker-compose / deployment)
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend
ENV CORS_ORIGINS=*
ENV DATABASE_URL=postgresql://genly:genly@postgres:5432/genly
ENV SHOW_DOCS=true

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Uvicorn workers per replica. Reduced from 4 → 2 when we moved to
# numReplicas=2 in railway.toml (2026-05-19, post-UMG-signing). Keeps
# the per-service DB connection budget at 2×2×10 = 40 (unchanged),
# while letting Railway do truly rolling deploys with always-warm
# capacity. To scale capacity further, prefer bumping numReplicas in
# railway.toml over bumping workers here — replicas give failure
# isolation, workers within a replica share fate.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${UVICORN_WORKERS:-2} --app-dir /app/backend"]
