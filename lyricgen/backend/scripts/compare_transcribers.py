"""Compare transcription services on a single audio file.

Standalone — does NOT touch the production pipeline. Runs whichever
backends have credentials/dependencies available, dumps raw JSON per
service, and prints a side-by-side word-timeline so timestamp accuracy
can be eyeballed.

Backends:
  - openai-whisper-1   (needs OPENAI_API_KEY) — same call the prod
                        pipeline does, but also requests word-level
                        timestamps so we can compare apples to apples.
  - assemblyai         (needs ASSEMBLYAI_API_KEY)
  - elevenlabs-scribe  (needs ELEVENLABS_API_KEY)
  - whisperx           (needs `pip install whisperx` — local, slow on
                        first run while it downloads the alignment model)

Usage:
  cd lyricgen/backend
  source venv/bin/activate
  export OPENAI_API_KEY=...
  export ASSEMBLYAI_API_KEY=...     # optional
  export ELEVENLABS_API_KEY=...     # optional
  python scripts/compare_transcribers.py "/path/to/song.mp3" [--lang es]

Output:
  /tmp/transcribe_compare/<basename>/
    whisper-1.json
    assemblyai.json
    elevenlabs.json
    whisperx.json
    REPORT.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _save(out_dir: Path, name: str, payload: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.json"
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return p


# ---------------------------------------------------------------------------
# Backend: OpenAI whisper-1 (with word-level timestamps requested)
# ---------------------------------------------------------------------------
def run_openai(mp3_path: str, language: str | None) -> dict | None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("[skip] openai-whisper-1: OPENAI_API_KEY not set")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("[skip] openai-whisper-1: openai package not installed")
        return None

    print("[run]  openai-whisper-1 …")
    t0 = time.time()
    client = OpenAI()
    kwargs = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"],
        "temperature": 0.0,
    }
    if language:
        kwargs["language"] = language
    with open(mp3_path, "rb") as f:
        kwargs["file"] = f
        resp = client.audio.transcriptions.create(**kwargs)

    elapsed = time.time() - t0
    words = [{"text": w.word, "start": w.start, "end": w.end}
             for w in (resp.words or [])]
    segments = [{"text": s.text, "start": s.start, "end": s.end}
                for s in (resp.segments or [])]
    print(f"       done in {elapsed:.1f}s — {len(words)} words, "
          f"{len(segments)} segments")
    return {
        "service": "openai-whisper-1",
        "elapsed_sec": elapsed,
        "language": resp.language,
        "duration": resp.duration,
        "text": resp.text,
        "words": words,
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# Backend: AssemblyAI
# ---------------------------------------------------------------------------
def run_assemblyai(mp3_path: str, language: str | None) -> dict | None:
    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        print("[skip] assemblyai: ASSEMBLYAI_API_KEY not set")
        return None
    try:
        import requests
    except ImportError:
        print("[skip] assemblyai: requests not installed")
        return None

    print("[run]  assemblyai …")
    t0 = time.time()
    headers = {"authorization": api_key}
    base = "https://api.assemblyai.com/v2"

    with open(mp3_path, "rb") as f:
        up = requests.post(f"{base}/upload", headers=headers, data=f, timeout=300)
    up.raise_for_status()
    audio_url = up.json()["upload_url"]

    payload = {
        "audio_url": audio_url,
        "punctuate": True,
        "format_text": True,
        # As of 2026-05, accounts must explicitly pick speech_models
        # (plural, list). universal-2 is the standard tier and supports Spanish.
        "speech_models": ["universal-2"],
    }
    if language:
        payload["language_code"] = language
    sub = requests.post(f"{base}/transcript", headers=headers,
                        json=payload, timeout=60)
    sub.raise_for_status()
    transcript_id = sub.json()["id"]

    while True:
        time.sleep(3)
        poll = requests.get(f"{base}/transcript/{transcript_id}",
                            headers=headers, timeout=30)
        poll.raise_for_status()
        data = poll.json()
        if data["status"] == "completed":
            break
        if data["status"] == "error":
            raise RuntimeError(f"AssemblyAI error: {data.get('error')}")

    elapsed = time.time() - t0
    words = [{"text": w["text"], "start": w["start"] / 1000.0, "end": w["end"] / 1000.0}
             for w in (data.get("words") or [])]
    print(f"       done in {elapsed:.1f}s — {len(words)} words")
    return {
        "service": "assemblyai",
        "elapsed_sec": elapsed,
        "language": data.get("language_code"),
        "duration": (data.get("audio_duration") or 0),
        "text": data.get("text") or "",
        "words": words,
        "segments": [],  # assemblyai groups via utterances; words are enough here
    }


# ---------------------------------------------------------------------------
# Backend: ElevenLabs Scribe
# ---------------------------------------------------------------------------
def run_elevenlabs(mp3_path: str, language: str | None) -> dict | None:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("[skip] elevenlabs: ELEVENLABS_API_KEY not set")
        return None
    try:
        import requests
    except ImportError:
        print("[skip] elevenlabs: requests not installed")
        return None

    print("[run]  elevenlabs-scribe …")
    t0 = time.time()
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": api_key}
    data = {"model_id": "scribe_v1"}
    if language:
        # ElevenLabs uses ISO-639-1 for language_code
        data["language_code"] = language
    with open(mp3_path, "rb") as f:
        files = {"file": (os.path.basename(mp3_path), f, "audio/mpeg")}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=600)
    resp.raise_for_status()
    payload = resp.json()

    elapsed = time.time() - t0
    raw_words = payload.get("words") or []
    words = []
    for w in raw_words:
        if w.get("type") and w["type"] != "word":
            continue
        words.append({
            "text": w.get("text") or w.get("word") or "",
            "start": w.get("start", 0.0),
            "end": w.get("end", 0.0),
        })
    print(f"       done in {elapsed:.1f}s — {len(words)} words")
    return {
        "service": "elevenlabs-scribe",
        "elapsed_sec": elapsed,
        "language": payload.get("language_code"),
        "duration": payload.get("audio_duration") or 0,
        "text": payload.get("text") or "",
        "words": words,
        "segments": [],
    }


# ---------------------------------------------------------------------------
# Backend: WhisperX (local, with forced alignment)
# ---------------------------------------------------------------------------
def run_whisperx(mp3_path: str, language: str | None) -> dict | None:
    try:
        import whisperx  # type: ignore
    except ImportError:
        print("[skip] whisperx: not installed (pip install whisperx)")
        return None

    print("[run]  whisperx (local) …")
    t0 = time.time()
    import torch  # type: ignore
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    model = whisperx.load_model("large-v3", device, compute_type=compute_type,
                                language=language)
    audio = whisperx.load_audio(mp3_path)
    result = model.transcribe(audio, batch_size=8, language=language)

    align_model, metadata = whisperx.load_align_model(
        language_code=result["language"], device=device)
    aligned = whisperx.align(result["segments"], align_model, metadata,
                             audio, device, return_char_alignments=False)

    elapsed = time.time() - t0
    words: list[dict] = []
    for seg in aligned["segments"]:
        for w in seg.get("words", []):
            if "start" not in w or "end" not in w:
                continue
            words.append({"text": w["word"], "start": w["start"], "end": w["end"]})
    segments = [{"text": s["text"], "start": s["start"], "end": s["end"]}
                for s in aligned["segments"]]
    print(f"       done in {elapsed:.1f}s — {len(words)} words, "
          f"{len(segments)} segments")
    return {
        "service": "whisperx",
        "elapsed_sec": elapsed,
        "language": result["language"],
        "duration": 0,
        "text": " ".join(s["text"] for s in segments),
        "words": words,
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def fmt_ts(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:05.2f}"


def write_report(out_dir: Path, results: list[dict], mp3_path: str,
                 window_sec: float = 45.0) -> Path:
    """Write a markdown report with side-by-side word timelines for the
    first `window_sec` seconds. Lets the operator eyeball whether one
    backend's word boundaries fall on the actual sung syllables.
    """
    lines: list[str] = []
    lines.append(f"# Transcribe comparison — {os.path.basename(mp3_path)}\n")
    lines.append(f"Window shown: first {window_sec:.0f} seconds.\n")

    # Summary table
    lines.append("## Summary\n")
    lines.append("| service | elapsed | words | segments | language |")
    lines.append("|---|---:|---:|---:|---|")
    for r in results:
        lines.append(
            f"| {r['service']} | {r['elapsed_sec']:.1f}s | "
            f"{len(r['words'])} | {len(r['segments'])} | {r.get('language', '?')} |"
        )
    lines.append("")

    # Per-service word timeline (first window_sec)
    lines.append(f"## Word timelines (first {window_sec:.0f}s)\n")
    for r in results:
        lines.append(f"### {r['service']}")
        lines.append("```")
        for w in r["words"]:
            if w["start"] > window_sec:
                break
            dur = w["end"] - w["start"]
            lines.append(
                f"{fmt_ts(w['start'])} → {fmt_ts(w['end'])} "
                f"({dur:5.2f}s)  {w['text']}"
            )
        lines.append("```\n")

    # Full text per service (for quick read-through of accuracy)
    lines.append("## Full transcript (text only)\n")
    for r in results:
        lines.append(f"### {r['service']}")
        lines.append("```")
        lines.append((r.get("text") or "").strip()[:4000])
        lines.append("```\n")

    p = out_dir / "REPORT.md"
    p.write_text("\n".join(lines))
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="Path to audio file (mp3/wav/m4a)")
    ap.add_argument("--lang", default="es",
                    help="Language code (default: es). Pass empty string to auto-detect.")
    ap.add_argument("--out", default=None,
                    help="Output dir (default: /tmp/transcribe_compare/<basename>)")
    ap.add_argument("--window", type=float, default=45.0,
                    help="Seconds of audio shown in the side-by-side report")
    args = ap.parse_args()

    mp3 = os.path.abspath(args.audio)
    if not os.path.exists(mp3):
        print(f"error: file not found: {mp3}")
        return 2
    lang = args.lang or None
    base = Path(mp3).stem.replace(" ", "_")
    out_dir = Path(args.out) if args.out else Path("/tmp/transcribe_compare") / base

    print(f"audio:   {mp3}")
    print(f"language: {lang or '(auto)'}")
    print(f"out:     {out_dir}\n")

    runners = [
        ("whisper-1", run_openai),
        ("assemblyai", run_assemblyai),
        ("elevenlabs", run_elevenlabs),
        ("whisperx", run_whisperx),
    ]
    results: list[dict] = []
    for name, fn in runners:
        try:
            r = fn(mp3, lang)
        except Exception as e:
            print(f"[fail] {name}: {type(e).__name__}: {e}")
            continue
        if r is None:
            continue
        _save(out_dir, name, r)
        results.append(r)

    if not results:
        print("\nNo backends produced output. Set at least one API key or install whisperx.")
        return 1

    report = write_report(out_dir, results, mp3, window_sec=args.window)
    print(f"\nreport: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
