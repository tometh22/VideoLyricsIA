#!/bin/bash
# GenLy AI - Start servers with auto-restart

while true; do
  echo "[$(date)] Starting backend..."
  cd /Users/tomi/VideoLyricsIA/lyricgen/backend
  source venv/bin/activate
  uvicorn main:app --host 0.0.0.0 --port 8000 2>&1 | tee -a /tmp/lyricgen_backend.log
  echo "[$(date)] Backend crashed, restarting in 3s..."
  sleep 3
done &

while true; do
  echo "[$(date)] Starting frontend..."
  cd /Users/tomi/VideoLyricsIA/lyricgen/frontend
  npm run dev 2>&1 | tee -a /tmp/lyricgen_frontend.log
  echo "[$(date)] Frontend crashed, restarting in 3s..."
  sleep 3
done &

echo "Both servers running with auto-restart. PID: $$"
echo "To stop: kill $$"
wait
