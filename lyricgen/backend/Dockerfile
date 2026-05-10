FROM python:3.11-slim

# System deps: ffmpeg for rendering, imagemagick for text layers, fonts, libsndfile for librosa.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    imagemagick \
    libsndfile1 \
    fonts-dejavu-core \
    ca-certificates \
    curl \
 && rm -rf /var/lib/apt/lists/*

# ImageMagick's default policy blocks @file read which breaks moviepy's TextClip.
# We only use it for short lyric text — safe to relax. Debian 12+ ships
# ImageMagick 7 (policy at /etc/ImageMagick-7/policy.xml), older Debian had
# v6 (/etc/ImageMagick-6/...). Patch whichever one exists.
RUN for f in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml; do \
      if [ -f "$f" ]; then \
        sed -i 's|<policy domain="path" rights="none" pattern="@\*"/>||g' "$f"; \
        echo "Patched ImageMagick policy: $f"; \
      fi; \
    done

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    ENV=prod

EXPOSE 8000

# Default command runs the API; override with `python worker.py` for workers.
# Use ${PORT:-8000} so Railway's injected PORT env var is honoured — Railway
# sets PORT to match the "Target port" in Dashboard (e.g. 8080). Hardcoding
# 8000 while Railway targets a different port causes connection-refused on
# every healthcheck attempt ("service unavailable", all retries).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
