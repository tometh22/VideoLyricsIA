"""Diagnostic: render a video using Whisper RAW segments (no filters).

Use case: you suspect the regular pipeline is mangling timestamps and
want to compare against an unfiltered baseline. This script bypasses
_filter_whisper_hallucinations + _detect_hallucination + recovery
synthesize by sending the segments straight into /generate.

Costs ~$0.85 (one Veo Fast generation + Whisper API + ProRes if UMG).

Usage from `lyricgen/backend/`:

    GENLY_USERNAME=tomi GENLY_PASSWORD='...' \
    python scripts/render_with_raw_segments.py \
      --mp3 "/path/to/AIRBAG - Blues del Infierno - River Plate.mp3" \
      --segments-json /tmp/transcribe_compare/AIRBAG_-_Blues_del_Infierno_-_River_Plate/whisper-1.json \
      --artist "Airbag" \
      --api-url https://genly-ai.up.railway.app

Then watch the job in admin -> Jobs filtered by your tenant; once
done, open /videos/<id> to see the result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests


def _login(api_url: str, username: str, password: str) -> str:
    r = requests.post(
        f"{api_url}/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["token"]


def _submit(api_url: str, token: str, mp3_path: str, segments: list[dict],
            artist: str, language: str | None,
            delivery_profile: str) -> str:
    """POST /generate with raw segments. Returns job_id."""
    headers = {"Authorization": f"Bearer {token}"}
    with open(mp3_path, "rb") as f:
        files = {"file": (os.path.basename(mp3_path), f, "audio/mpeg")}
        data = {
            "artist": artist,
            "style": "oscuro",
            "segments_json": json.dumps(segments),
            "delivery_profile": delivery_profile,
        }
        if language:
            data["language"] = language
        r = requests.post(
            f"{api_url}/generate",
            headers=headers,
            data=data,
            files=files,
            timeout=120,
        )
    if not r.ok:
        print(f"[generate] HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
        r.raise_for_status()
    return r.json()["job_id"]


def _poll(api_url: str, token: str, job_id: str, timeout_min: int = 30) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + timeout_min * 60
    last_step = None
    while time.time() < deadline:
        r = requests.get(f"{api_url}/status/{job_id}", headers=headers, timeout=30)
        if r.ok:
            d = r.json()
            step = d.get("current_step")
            if step != last_step:
                print(f"  [{d.get('progress', 0):3d}%] {step}")
                last_step = step
            if d["status"] in ("done", "error", "validation_failed", "pending_review"):
                return d
        time.sleep(5)
    raise TimeoutError(f"job {job_id} did not finish in {timeout_min} min")


def _normalize_segments(raw_segments: list[dict]) -> list[dict]:
    """Strip everything except start/end/text. The backend doesn't need
    `words` and the renderer ignores extras, but defensive: a stray field
    could trip the json parsing or cost a few KB on the wire."""
    out = []
    for s in raw_segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue  # render barfs on empty text (ImageMagick "label expected")
        out.append({
            "start": float(s["start"]),
            "end": float(s["end"]),
            "text": text,
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mp3", required=True, help="Path to the audio file")
    p.add_argument("--segments-json", required=True,
                   help="Path to the Whisper output JSON (whisper-1.json from compare_transcribers)")
    p.add_argument("--artist", required=True)
    p.add_argument("--language", default="es")
    p.add_argument("--delivery-profile", default="youtube",
                   choices=["youtube", "umg", "both"])
    p.add_argument("--api-url",
                   default=os.environ.get("API_URL", "https://genly-ai.up.railway.app"))
    args = p.parse_args()

    if not Path(args.mp3).exists():
        print(f"mp3 not found: {args.mp3}", file=sys.stderr)
        return 1
    if not Path(args.segments_json).exists():
        print(f"segments json not found: {args.segments_json}", file=sys.stderr)
        return 1

    user = os.environ.get("GENLY_USERNAME")
    pw = os.environ.get("GENLY_PASSWORD")
    if not (user and pw):
        print("Set GENLY_USERNAME and GENLY_PASSWORD env vars", file=sys.stderr)
        return 1

    payload = json.load(open(args.segments_json))
    raw_segs = payload.get("segments") or []
    segs = _normalize_segments(raw_segs)
    if not segs:
        print("No segments to send (after stripping empty)", file=sys.stderr)
        return 1

    print(f"api:      {args.api_url}")
    print(f"mp3:      {args.mp3}")
    print(f"segments: {len(raw_segs)} raw → {len(segs)} non-empty")
    print(f"artist:   {args.artist}")
    print(f"profile:  {args.delivery_profile}")
    print(f"first 3:  {segs[:3]}")
    print()

    print("[1/3] login")
    token = _login(args.api_url, user, pw)

    print("[2/3] submitting /generate with RAW segments (no filters, no recovery)…")
    job_id = _submit(
        args.api_url, token, args.mp3, segs,
        artist=args.artist, language=args.language,
        delivery_profile=args.delivery_profile,
    )
    print(f"      job_id = {job_id}")
    print(f"      watch:  {args.api_url.replace('https://genly-ai.up.railway.app', 'https://genly.pro')}/videos/{job_id}")
    print()

    print("[3/3] polling status…")
    final = _poll(args.api_url, token, job_id, timeout_min=30)
    print()
    print(f"final status: {final['status']}")
    if final.get("error"):
        print(f"error: {final['error']}")
        return 2
    print(f"open /videos/{job_id} on the frontend to compare timestamps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
