#!/usr/bin/env python3
"""Define the hard-song and code-switch cohorts without leaking held-song audio.

Gold WER and approved language labels are scorer/training targets only.  The
production router consumes the acoustic feature table built separately.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Sequence

from lingua import Language, LanguageDetectorBuilder

from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import score_song


HARD_WER = 0.10
MIN_LANGUAGE_LINES = 2
MIN_LANGUAGE_CHARACTER_SHARE = 0.15
_LIVE = re.compile(r"\b(?:live|en vivo)\b", re.IGNORECASE)
_LID = LanguageDetectorBuilder.from_languages(Language.SPANISH, Language.ENGLISH).build()


def _line_language(text: str) -> tuple[str, float]:
    lexical = " ".join(re.findall(r"[^\W\d_]+", str(text or ""), flags=re.UNICODE))
    if len(lexical) < 8:
        return "unknown", 0.0
    confidence = _LID.compute_language_confidence_values(lexical)
    best = confidence[0] if confidence else None
    if not best or float(best.value) < 0.65:
        return "unknown", float(best.value) if best else 0.0
    if best.language == Language.SPANISH:
        return "es", float(best.value)
    if best.language == Language.ENGLISH:
        return "en", float(best.value)
    return "unknown", float(best.value)


def text_language_profile(lines: Sequence[dict[str, Any]]) -> dict[str, Any]:
    line_counts = {"es": 0, "en": 0, "unknown": 0}
    character_counts = {"es": 0, "en": 0, "unknown": 0}
    classified = []
    for index, line in enumerate(lines):
        text = str(line.get("text") or "").strip()
        language, confidence = _line_language(text)
        line_counts[language] += 1
        character_counts[language] += len("".join(re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)))
        classified.append({"line_idx": index, "language": language, "confidence": confidence})
    total_characters = max(1, sum(character_counts.values()))
    shares = {key: value / total_characters for key, value in character_counts.items()}
    mixed = bool(
        line_counts["es"] >= MIN_LANGUAGE_LINES
        and line_counts["en"] >= MIN_LANGUAGE_LINES
        and shares["es"] >= MIN_LANGUAGE_CHARACTER_SHARE
        and shares["en"] >= MIN_LANGUAGE_CHARACTER_SHARE
    )
    return {
        "line_counts": line_counts,
        "character_counts": character_counts,
        "character_shares": shares,
        "is_es_en_code_switch": mixed,
        "classified_lines": classified,
    }


def _active_minutes(audits: Sequence[dict[str, Any]]) -> dict[str, Any]:
    timestamps = []
    for row in audits:
        try:
            timestamps.append(dt.datetime.fromisoformat(str(row["created_at"])))
        except (KeyError, TypeError, ValueError):
            continue
    timestamps = sorted(set(timestamps))
    if not timestamps:
        return {"available": False, "active_minutes_proxy": None, "compact_session_minutes": None}
    gaps = [(right - left).total_seconds() for left, right in zip(timestamps, timestamps[1:])]
    # Each action starts a small active-work interval; gaps over ten minutes
    # begin a new session and can never turn overnight idle time into work.
    active_seconds = 30.0 + sum(min(gap, 120.0) for gap in gaps if gap <= 600.0)
    span_minutes = (timestamps[-1] - timestamps[0]).total_seconds() / 60.0
    return {
        "available": True,
        "events": len(timestamps),
        "active_minutes_proxy": active_seconds / 60.0,
        "compact_session_minutes": span_minutes if span_minutes <= 120.0 else None,
        "wall_clock_span_minutes": span_minutes,
        "warning": "sessionized proxy; not foreground editor time",
    }


def build(golden: Path, output: Path) -> dict[str, Any]:
    rows = []
    for item in read_json(golden / "manifest.json")["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        approved = read_json(case / "lines.json")
        observed_edits = read_json(case / "edits.json") if (case / "edits.json").is_file() else []
        observed_text_edits = sum(
            not bool(edit.get("derived")) and edit.get("op") in {"text_edit", "line_added", "line_deleted"}
            for edit in observed_edits
        )
        language = text_language_profile(approved)
        row: dict[str, Any] = {
            "song_id": item["song_id"],
            "raw_quality": item["raw_quality"],
            "artist": meta.get("artist"),
            "title": meta.get("title"),
            "duration_s": float(meta.get("duration_s") or 0.0),
            "is_live": bool(_LIVE.search(str(meta.get("title") or ""))),
            "text_language_gold": language,
            "historical_session": _active_minutes(
                read_json(case / "audit_diffs.json") if (case / "audit_diffs.json").is_file() else []
            ),
            "observed_text_edit_events": observed_text_edits,
            "wer_main": None,
            "word_errors": None,
            "reference_words": None,
            "difficult_gold": None,
            "difficulty_reasons": [],
        }
        if item["raw_quality"] in {"exact", "reconstructed"} and (case / "raw_pipeline_output.json").is_file():
            raw = segments_to_lines(read_json(case / "raw_pipeline_output.json")["segments"])
            metrics, _alignment, _errors = score_song(item["song_id"], approved, raw)
            row.update({
                "wer_main": float(metrics["wer_main"]),
                "word_errors": int(metrics["edit_counts_main"]["word_edits"]),
                "reference_words": int(metrics["edit_counts_main"]["reference_words"]),
            })
            reasons = []
            if row["wer_main"] >= HARD_WER:
                reasons.append("wer_ge_10pct")
            if row["is_live"]:
                reasons.append("live_tier2")
            compact = row["historical_session"].get("compact_session_minutes")
            if compact is not None and compact >= 20.0 and observed_text_edits >= 5:
                reasons.append("compact_text_session_ge_20m")
            row["difficulty_reasons"] = reasons
            row["difficult_gold"] = bool(reasons)
        rows.append(row)

    comparable = [row for row in rows if row["difficult_gold"] is not None]
    spanglish_comparable = [row for row in comparable if row["text_language_gold"]["is_es_en_code_switch"]]
    report = {
        "schema_version": 1,
        "definition": {
            "difficult": "WER >= 10%, live Tier 2, or compact >=20m session with >=5 observed text/line edits",
            "spanglish": "at least 2 es + 2 en lines and >=15% lexical-character share in each language",
            "gold_usage": "labels/scoring only; never router features",
        },
        "catalogue_songs": len(rows),
        "comparable_gold_songs": len(comparable),
        "difficult_gold_songs": sum(bool(row["difficult_gold"]) for row in comparable),
        "easy_gold_songs": sum(not bool(row["difficult_gold"]) for row in comparable),
        "spanglish_catalogue_song_ids": [row["song_id"] for row in rows if row["text_language_gold"]["is_es_en_code_switch"]],
        "spanglish_comparable_song_ids": [row["song_id"] for row in spanglish_comparable],
        "spanglish_gate": {
            "minimum_song_groups_for_ci": 3,
            "status": "READY" if len(spanglish_comparable) >= 3 else "BLOCKED_INSUFFICIENT_GOLD_SONGS",
            "songs": len(spanglish_comparable),
        },
        "cases": rows,
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/difficult_cohort/report.json"))
    args = parser.parse_args()
    print(json.dumps(build(args.golden.resolve(), args.output.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
