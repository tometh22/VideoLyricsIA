#!/usr/bin/env python3
"""One-click vocalization and melisma suggestions with explicit abstention.

The module consumes pre-human content-gate windows. It is never allowed to
turn a lexical window into a vocalization. Text is inferred from independent
non-lexical candidates when available, otherwise from a conservative formant
estimate; failure to identify a vowel means abstention.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

import librosa
import numpy as np

from eval.canonical import read_json, write_json
from eval.metrics import normalize_text


MIN_DURATION_S = 0.75
VOCAL_TOKENS = {"ah": "ah", "aah": "ah", "eh": "eh", "oh": "oh", "ooh": "oh", "oo": "oh", "uh": "uh", "uoh": "uoh", "woah": "uoh"}
VOWEL_FORMANTS = {
    "ah": (730.0, 1090.0), "eh": (530.0, 1840.0), "ih": (270.0, 2290.0),
    "oh": (570.0, 840.0), "uh": (300.0, 870.0),
}


def _compact_token(token: str) -> str | None:
    token = re.sub(r"[^a-z]", "", normalize_text(token))
    token = re.sub(r"([aeiou])\1+", r"\1", token)
    if token in VOCAL_TOKENS:
        return VOCAL_TOKENS[token]
    if re.fullmatch(r"a+h*", token):
        return "ah"
    if re.fullmatch(r"e+h*", token):
        return "eh"
    if re.fullmatch(r"o+h*", token):
        return "oh"
    if re.fullmatch(r"u+o*h*", token):
        return "uoh" if "o" in token else "uh"
    return None


def canonical_vocalization(text: str) -> list[str] | None:
    tokens = re.findall(r"[^\W\d_]+", normalize_text(text), flags=re.UNICODE)
    if not tokens:
        return None
    compact = [_compact_token(token) for token in tokens]
    return compact if all(compact) else None


def _candidate_vote(candidates: Sequence[dict[str, Any]]) -> tuple[str | None, list[str]]:
    by_family: dict[str, str] = {}
    for candidate in candidates:
        family = str(candidate.get("family_group") or candidate.get("family") or "")
        canonical = canonical_vocalization(str(candidate.get("text") or ""))
        if family and canonical:
            by_family[family] = Counter(canonical).most_common(1)[0][0]
    votes = Counter(by_family.values())
    if not votes:
        return None, []
    winner, count = votes.most_common(1)[0]
    supporters = sorted(family for family, token in by_family.items() if token == winner)
    return (winner if count >= 2 else None), supporters


def _estimate_formants(audio: np.ndarray, sample_rate: int) -> tuple[float, float] | None:
    if len(audio) < int(0.25 * sample_rate):
        return None
    audio = librosa.effects.preemphasis(audio)
    frame_length, hop = int(0.04 * sample_rate), int(0.02 * sample_rate)
    values = []
    for start in range(0, max(1, len(audio) - frame_length), hop):
        frame = audio[start:start + frame_length]
        if len(frame) < frame_length or float(np.sqrt(np.mean(frame * frame))) < 1e-4:
            continue
        try:
            roots = np.roots(librosa.lpc(frame * np.hamming(len(frame)), order=12))
        except (FloatingPointError, ValueError):
            continue
        roots = roots[np.imag(roots) >= 0]
        frequencies = sorted(
            float(value) for value in np.angle(roots) * sample_rate / (2 * np.pi)
            if 150.0 <= value <= 3500.0
        )
        if len(frequencies) >= 2:
            values.append(frequencies[:2])
    if len(values) < 3:
        return None
    return float(np.median([row[0] for row in values])), float(np.median([row[1] for row in values]))


def _formant_vowel(audio: np.ndarray, sample_rate: int) -> tuple[str | None, float]:
    formants = _estimate_formants(audio, sample_rate)
    if not formants:
        return None, 0.0
    distances = {
        token: math.sqrt(((formants[0] - target[0]) / 500.0) ** 2 + ((formants[1] - target[1]) / 1500.0) ** 2)
        for token, target in VOWEL_FORMANTS.items()
    }
    ordered = sorted(distances.items(), key=lambda row: row[1])
    confidence = max(0.0, min(1.0, (ordered[1][1] - ordered[0][1]) / max(0.20, ordered[1][1])))
    return (ordered[0][0] if confidence >= 0.20 else None), confidence


def pitch_articulations(audio: np.ndarray, sample_rate: int) -> int:
    hop = 320
    pitch, voiced, probability = librosa.pyin(
        audio, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
        sr=sample_rate, frame_length=2048, hop_length=hop,
    )
    voiced = np.asarray(voiced, dtype=bool) & (np.asarray(probability) >= 0.60)
    onset = librosa.onset.onset_strength(y=audio, sr=sample_rate, hop_length=hop)
    peaks = librosa.util.peak_pick(onset, pre_max=2, post_max=2, pre_avg=4, post_avg=4, delta=.25, wait=8)
    voiced_peaks = [index for index in peaks if index < len(voiced) and voiced[index]]
    # A continuous sustained vowel is one articulation, never zero.
    return max(1, min(12, len(voiced_peaks)))


def propose_vocalization(window: dict[str, Any], audio: np.ndarray, sample_rate: int) -> dict[str, Any]:
    duration = float(window["end_s"]) - float(window["start_s"])
    if window.get("content_type") != "vocalization" or float(window.get("content_confidence") or 0) < .85:
        return {"status": "ABSTAIN", "reason": "content_gate_not_vocalization"}
    if duration < MIN_DURATION_S:
        return {"status": "ABSTAIN", "reason": "shorter_than_editorial_rule"}
    token, supporters = _candidate_vote(window.get("candidates") or [])
    source, confidence = "independent_candidate_consensus", 1.0 if token else 0.0
    if not token:
        token, confidence = _formant_vowel(audio, sample_rate)
        source = "formant_estimate"
    if not token:
        return {"status": "ABSTAIN", "reason": "dominant_vowel_uncertain"}
    repetitions = pitch_articulations(audio, sample_rate)
    return {
        "status": "PROPOSE", "category": "vocalization",
        "text": f"({' '.join([token] * repetitions)})",
        "start_s": float(window["start_s"]), "end_s": float(window["end_s"]),
        "repetitions": repetitions, "dominant_vocalization": token,
        "source": source, "supporting_families": supporters,
        "confidence": confidence, "auto_apply": False,
    }


def melisma_end(
    audio: np.ndarray, sample_rate: int, current_end_s: float, next_start_s: float | None,
    *, maximum_extension_s: float = 4.0,
) -> dict[str, Any]:
    """Extend only a contiguous same-note pitch tail; otherwise abstain."""
    hop = 320
    pitch, voiced, probability = librosa.pyin(
        audio, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
        sr=sample_rate, frame_length=2048, hop_length=hop,
    )
    pitch, voiced, probability = np.asarray(pitch), np.asarray(voiced), np.asarray(probability)
    edge = int(round(current_end_s * sample_rate / hop))
    reference = pitch[max(0, edge - 8):edge]
    reference = reference[np.isfinite(reference)]
    if not len(reference):
        return {"status": "ABSTAIN", "reason": "no_final_word_pitch"}
    reference_pitch = float(np.median(reference))
    last, allowed_gap = edge, int(round(.12 * sample_rate / hop))
    gap = 0
    limit_s = min(len(audio) / sample_rate, current_end_s + maximum_extension_s)
    if next_start_s is not None:
        limit_s = min(limit_s, float(next_start_s))
    limit = min(len(pitch), int(round(limit_s * sample_rate / hop)))
    for index in range(edge, limit):
        valid = bool(voiced[index]) and float(probability[index]) >= .60 and np.isfinite(pitch[index])
        same_note = valid and abs(12.0 * math.log2(float(pitch[index]) / reference_pitch)) <= 2.0
        if same_note:
            last, gap = index, 0
        else:
            gap += 1
            if gap > allowed_gap:
                break
    proposed = last * hop / sample_rate
    if proposed <= current_end_s + .08:
        return {"status": "ABSTAIN", "reason": "no_contiguous_same_pitch_tail"}
    return {
        "status": "PROPOSE", "category": "timing", "current_end_s": current_end_s,
        "proposed_end_s": proposed, "auto_apply": False,
        "rule": "contiguous_tail_within_2_semitones_and_no_next_line_overlap",
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _mapping(values: Sequence[str]) -> dict[str, Path]:
    output = {}
    for value in values:
        key, separator, path = value.partition("=")
        if not separator or not key or not path:
            raise ValueError("mapping must be case_id=/absolute/path")
        output[key] = Path(path).resolve()
    return output


def prepare_windows(content_reports: Sequence[str], stems: Sequence[str], output: Path) -> dict[str, Any]:
    reports, stem_paths = _mapping(content_reports), _mapping(stems)
    rows = []
    for case_id, report_path in reports.items():
        payload = read_json(report_path)
        for window in payload.get("windows") or []:
            decision = window.get("content_decision") or {}
            candidates = []
            for hypothesis in window.get("ranked_hypotheses") or []:
                source = str(hypothesis.get("source") or "")
                family = "whisper" if source in {"raw_asr", "contextual_asr"} else source
                candidates.append({"family_group": family, "text": hypothesis.get("text")})
            rows.append({
                "song_id": case_id, "window_id": window["window_id"],
                "start_s": float(window["start"]), "end_s": float(window["end"]),
                "stem_path": str(stem_paths[case_id]),
                "content_type": decision.get("content_type"),
                "content_confidence": 1.0 if decision.get("content_type") == "vocalization" else 0.0,
                "candidates": candidates,
                "source_content_gate": "pre_human_live_window_content_gate",
            })
    _write_jsonl(output, rows)
    return {"windows": len(rows), "output": str(output), "gold_visible": False}


def run(windows_path: Path, stems: Path, output: Path) -> dict[str, Any]:
    rows, abstentions = [], Counter()
    for window in _read_jsonl(windows_path):
        stem_path = Path(window["stem_path"]) if window.get("stem_path") else stems / str(window["song_id"]) / "vocals.wav"
        if not stem_path.is_file():
            abstentions["missing_stem"] += 1
            continue
        audio, sample_rate = librosa.load(
            str(stem_path), sr=16000, mono=True,
            offset=float(window["start_s"]), duration=float(window["end_s"]) - float(window["start_s"]),
        )
        proposal = propose_vocalization(window, audio, sample_rate)
        if proposal["status"] == "PROPOSE":
            rows.append({"song_id": window["song_id"], "window_id": window.get("window_id"), **proposal})
        else:
            abstentions[proposal["reason"]] += 1
    report = {
        "schema_version": 1, "windows": len(_read_jsonl(windows_path)), "proposals": len(rows),
        "abstentions": dict(abstentions), "auto_correction": False,
        "gate": {"status": "PENDING_HUMAN_GOLD", "requirements": ">=60% pre-resolved; zero invented over lexical lyrics"},
        "proposal_rows": rows,
    }
    write_json(output, report)
    return report


def _gold_vocalization(text: str) -> list[str] | None:
    quoted = re.findall(r'["“”]([^"“”]+)["“”]', str(text or ""))
    return canonical_vocalization(quoted[-1] if quoted else text)


def score_report(proposals_path: Path, gold_csv: Path, output: Path) -> dict[str, Any]:
    proposals = read_json(proposals_path).get("proposal_rows") or []
    proposed = {(str(row["song_id"]), str(row.get("window_id"))): row for row in proposals}
    with gold_csv.open(newline="", encoding="utf-8-sig") as handle:
        gold_rows = list(csv.DictReader(handle))
    positives, resolved, invented = [], [], []
    for row in gold_rows:
        key = (str(row.get("case_id")), str(row.get("window_id")))
        heard = _gold_vocalization(str(row.get("heard_text") or ""))
        editorial_positive = row.get("gap_contains_lyric") == "yes" and bool(heard)
        proposal = proposed.get(key)
        if editorial_positive:
            positives.append(key)
            if proposal and canonical_vocalization(str(proposal.get("text") or "")) == heard:
                resolved.append(key)
        elif proposal and row.get("gap_contains_lyric") == "yes":
            invented.append(key)
    coverage = len(resolved) / max(1, len(positives))
    passed = bool(positives and coverage >= .60 and not invented)
    report = {
        "schema_version": 1, "gold_vocalization_windows": len(positives),
        "pre_resolved": len(resolved), "pre_resolved_rate": coverage,
        "invented_over_lexical_windows": [list(key) for key in invented],
        "gate": {
            "requirements": ">=60% pre-resolved; zero invented over lexical lyrics",
            "status": "GO_STAGING_SUGGESTIONS" if passed else "BLOCKED_NO_GOLD_VOCALIZATIONS" if not positives else "NO_GO",
        },
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--content-report", action="append", required=True)
    prepare.add_argument("--stem", action="append", required=True)
    prepare.add_argument("--output", type=Path, default=Path("eval/runs/vocalization_resolver/windows.jsonl"))
    propose = sub.add_parser("propose")
    propose.add_argument("--windows", type=Path, required=True)
    propose.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    propose.add_argument("--output", type=Path, default=Path("eval/runs/vocalization_resolver/proposals.json"))
    measure = sub.add_parser("score")
    measure.add_argument("--proposals", type=Path, default=Path("eval/runs/vocalization_resolver/proposals.json"))
    measure.add_argument("--gold-csv", type=Path, required=True)
    measure.add_argument("--output", type=Path, default=Path("eval/runs/vocalization_resolver/report.json"))
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare_windows(args.content_report, args.stem, args.output.resolve())
    elif args.action == "propose":
        result = run(args.windows.resolve(), args.stems.resolve(), args.output.resolve())
    else:
        result = score_report(args.proposals.resolve(), args.gold_csv.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
