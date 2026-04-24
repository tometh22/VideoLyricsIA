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
# We only use it for short lyric text — safe to relax.
RUN sed -i 's|<policy domain="path" rights="none" pattern="@\*"/>||' /etc/ImageMagick-6/policy.xml || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    ENV=prod

EXPOSE 8000

# Default command runs the API; override with `python worker.py` for workers.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
