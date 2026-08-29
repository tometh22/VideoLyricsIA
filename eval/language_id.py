#!/usr/bin/env python3
"""Derive per-song language from a vocal stem and approved lyric text."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from eval.canonical import read_json, write_json
from eval.extract import _detect_language


def _first_lyric_start(case: Path) -> float:
    lines = read_json(case / "lines.json")
    starts = [float(line.get("start_s", 0)) for line in lines if str(line.get("text") or "").strip()]
    return max(0.0, (min(starts) if starts else 0.0) - 2.0)


def prepare_stem(case: Path, cache: Path, *, duration_s: int = 30) -> Path:
    meta = read_json(case / "meta.json")
    audio = case / meta["audio"]["filename"]
    stem = cache / "stems" / case.name / "vocals.wav"
    if stem.is_file():
        return stem
    clip = cache / "clips" / f"{case.name}.wav"
    clip.parent.mkdir(parents=True, exist_ok=True)
    stem.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-ss", str(_first_lyric_start(case)),
        "-t", str(duration_s), "-i", str(audio), "-ac", "2", "-ar", "44100", str(clip),
    ], check=True)
    demucs_root = cache / "demucs"
    subprocess.run([
        "python3", "-m", "demucs.separate", "-n", "htdemucs", "--two-stems", "vocals",
        "--shifts", "0", "--overlap", "0.1", "--segment", "7", "-d", "cpu", "-j", "2",
        "--filename", f"{case.name}/{{stem}}.{{ext}}", "-o", str(demucs_root), str(clip),
    ], check=True)
    generated = demucs_root / "htdemucs" / case.name / "vocals.wav"
    if not generated.is_file():
        raise RuntimeError(f"Demucs did not produce a vocal stem for {case.name}")
    generated.replace(stem)
    return stem


def whisper_lid(stems: dict[str, Path], model_name: str) -> dict[str, dict[str, Any]]:
    import whisper

    model = whisper.load_model(model_name)
    results = {}
    for song_id, stem in stems.items():
        audio = whisper.load_audio(str(stem))[: 30 * whisper.audio.SAMPLE_RATE]
        mel = whisper.log_mel_spectrogram(audio, n_mels=model.dims.n_mels).to(model.device)
        _, probabilities = model.detect_language(mel)
        language = max(probabilities, key=probabilities.get)
        results[song_id] = {
            "value": language,
            "confidence": float(probabilities[language]),
            "model": f"openai-whisper-{model_name}",
            "input": "local_htdemucs_vocal_stem_30s",
        }
    return results


def llm_text_lid(golden: Path, song_ids: list[str], model_name: str) -> dict[str, str]:
    """Confirm text language with the local Ollama model (no client data egress)."""
    cases = []
    for song_id in song_ids:
        text_value = " ".join(line["text"] for line in read_json(golden / song_id / "lines.json"))
        # A few lyric lines are ample for dominant-language confirmation and
        # keep the local model comfortably inside its small serving context.
        cases.append({"song_id": song_id, "approved_lyrics_excerpt": text_value[:240]})
    def ask(chunk: list[dict[str, str]]) -> dict[str, str]:
        prompt = (
            "Identify the dominant language of each approved song lyric. Return only JSON: "
            "{\"languages\": {\"song_id\": \"ISO-639-1 lowercase\"}}. "
            "Names and isolated foreign phrases do not change the dominant language.\n\n" +
            json.dumps(chunk, ensure_ascii=False)
        )
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps({
                "model": model_name, "prompt": prompt, "format": "json", "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 256},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            envelope = json.loads(response.read())
        payload = json.loads(envelope.get("response") or "{}")
        return {str(key): str(value).lower() for key, value in (payload.get("languages") or {}).items()}

    languages: dict[str, str] = {}
    for offset in range(0, len(cases), 5):
        chunk = cases[offset:offset + 5]
        answer = ask(chunk)
        languages.update(answer)
        # Local models occasionally truncate a multi-item JSON response. Retry
        # only omitted cases one by one; never substitute a heuristic silently.
        for case in chunk:
            if case["song_id"] not in answer:
                languages.update(ask([case]))
    missing = [song_id for song_id in song_ids if song_id not in languages]
    if missing:
        raise RuntimeError(f"LLM language response omitted: {', '.join(missing)}")
    return {song_id: str(languages[song_id]).lower() for song_id in song_ids}


def derive_languages(
    golden: Path, output: Path, cache: Path, *, whisper_model: str, llm_model: str,
) -> dict[str, Any]:
    manifest = read_json(golden / "manifest.json")
    song_ids = [str(item["song_id"]) for item in manifest["cases"]]
    stems: dict[str, Path] = {}
    for index, song_id in enumerate(song_ids, 1):
        print(f"[{index}/{len(song_ids)}] vocal stem {song_id}", flush=True)
        stems[song_id] = prepare_stem(golden / song_id, cache)
    acoustic_cache = cache / f"whisper_{whisper_model}_lid.json"
    if acoustic_cache.is_file():
        acoustic = read_json(acoustic_cache)
    else:
        acoustic = whisper_lid(stems, whisper_model)
        write_json(acoustic_cache, acoustic)
    llm_cache = cache / f"llm_{llm_model.replace(':', '_')}_lid.json"
    if llm_cache.is_file():
        text_confirmation = read_json(llm_cache)
    else:
        text_confirmation = llm_text_lid(golden, song_ids, llm_model)
        write_json(llm_cache, text_confirmation)
    disagreements = []
    unresolved = []
    rows = []
    for song_id in song_ids:
        meta_path = golden / song_id / "meta.json"
        meta = read_json(meta_path)
        acoustic_value = acoustic[song_id]["value"]
        text_value = text_confirmation[song_id]
        agreed = acoustic_value == text_value
        if not agreed:
            disagreements.append(song_id)
        approved_text = " ".join(line["text"] for line in read_json(golden / song_id / "lines.json"))
        lingua_value, lingua_confidence = _detect_language(approved_text)
        votes = Counter((acoustic_value, text_value, lingua_value))
        final_value, vote_count = votes.most_common(1)[0]
        if vote_count < 2:
            unresolved.append(song_id)
        meta["language"] = {
            "value": final_value,
            "confidence": acoustic[song_id]["confidence"] if final_value == acoustic_value else lingua_confidence,
            "derived": True,
            "agreement": agreed,
            "resolved_by_majority": vote_count >= 2,
            "acoustic": acoustic[song_id],
            "text_confirmation": {"value": text_value, "model": llm_model},
            "text_lingua": {"value": lingua_value, "confidence": lingua_confidence},
        }
        write_json(meta_path, meta)
        rows.append({"song_id": song_id, **meta["language"]})
    report = {
        "schema_version": 1,
        "songs": len(rows),
        "whisper_model": whisper_model,
        "llm_model": llm_model,
        "agreement_count": sum(row["agreement"] for row in rows),
        "disagreement_count": len(disagreements),
        "disagreement_song_ids": disagreements,
        "unresolved_count": len(unresolved),
        "unresolved_song_ids": unresolved,
        "cases": rows,
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/reports/language_id.json"))
    parser.add_argument("--cache", type=Path, default=Path("eval/cache/language_id"))
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--llm-model", default="qwen3.5:9b")
    args = parser.parse_args()
    report = derive_languages(
        args.golden.resolve(), args.output.resolve(), args.cache.resolve(),
        whisper_model=args.whisper_model, llm_model=args.llm_model,
    )
    print(json.dumps({key: report[key] for key in ("songs", "agreement_count", "disagreement_count")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
