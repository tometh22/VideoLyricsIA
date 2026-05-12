FROM python:3.11-slim

# System deps: ffmpeg for rendering, imagemagick for text layers, fonts, libsndfile for librosa.
# rclone para el worker que sube ProRes (16 GB) directamente desde R2 a
# Google Drive del usuario (server-to-server, ~30x más rápido que el
# flow descargar-y-resubir desde la máquina del operador). Ver
# drive_uploader.py:upload_via_rclone.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    imagemagick \
    libsndfile1 \
    fonts-dejavu-core \
    ca-certificates \
    curl \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL https://rclone.org/install.sh | bash \
 && rclone version

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
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2"]
