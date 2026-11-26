#!/usr/bin/env python3
"""Replay verified portal cuts and campaign drafts without printing lyric text."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delivery_preflight import build_delivery_preflight
from lyric_observation import build_lyric_observation


_EXACT_CUT = {"huella_del_render", "cadena_auditada"}
_TIMESTAMP = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
_ISSUE_TAGS = {
    "LYRIC_ORTHOGRAPHY_MISMATCH": {"letra", "tildes"},
    "LYRIC_TOKEN_TYPO": {"letra", "nombres"},
    "LYRIC_TERMINAL_PERIOD": {"signos"},
    "LYRIC_FRAGMENTATION": {"lectura"},
    "LYRIC_REPEAT_INCONSISTENCY": {"letra", "repeticiones", "tildes"},
    "LYRIC_END_BEFORE_WORD_END": {"salida"},
    "LYRIC_OVERLAP": {"entrada", "salida"},
}
_OBSERVATION_TAGS = {
    "duplicate_lyric_event": {"repeticiones"},
    "reused_word_clock": {"entrada", "salida", "repeticiones"},
    "possible_outro_caption": {"letra", "pantalla"},
}


def _display(segments):
    return [[row.get("start"), row.get("end"), row.get("text", "")] for row in segments]


def replay_requests(comparison, snapshots):
    versions = {
        f"{row['job_id']}:{row['revision']}": row["segments"] for row in snapshots
    }
    counts = Counter()
    tags = Counter()
    seen_ids = set()
    findings = Counter()
    for delivery in comparison["deliveries"]:
        if delivery.get("machine") != "snapshot_automatico_verificado":
            continue
        for request in delivery["requests"]:
            seen = request.get("seen") or {}
            request_id = request["id"]
            if seen.get("evidence") not in _EXACT_CUT or request_id in seen_ids:
                continue
            seen_ids.add(request_id)
            tags.update(request["tags"])
            counts["verified_requests"] += 1
            key = seen["key"]
            display = comparison["texts"][key]
            rows = versions.get(key) or []
            if rows and _display(rows) == display:
                counts["requests_with_word_clock_snapshot"] += 1
            else:
                rows = [{"start": start, "end": end, "text": text} for start, end, text in display]
            report = build_delivery_preflight(metadata={}, segments=rows)
            notes = report["lyric_observation"]["findings"]
            findings.update(note["code"] for note in notes)
            points = [
                60 * int(minute) + int(second)
                for minute, second in _TIMESTAMP.findall(request["comment"])
            ]
            if not points:
                continue
            counts["requests_with_timestamp"] += 1
            request_tags = set(request["tags"])
            if any(
                _ISSUE_TAGS.get(issue["code"], set()) & request_tags
                and any(abs(point - seconds) <= 2 for point in points for seconds in issue["seconds"])
                for issue in report["issues"]
            ):
                counts["existing_qc_tag_and_time_matches"] += 1
            if any(
                _OBSERVATION_TAGS[note["code"]] & request_tags
                and any(note["start"] - 2 <= point <= note["end"] + 2 for point in points)
                for note in notes
            ):
                counts["new_observation_tag_and_time_matches"] += 1
    return {
        "counts": dict(counts),
        "request_tags": dict(tags),
        "observation_counts_per_request": dict(findings),
        "matching": "Reported tag and time within two seconds; matches require manual validation.",
    }


def replay_campaign(campaign):
    tracks = 0
    counts = Counter()
    flagged_tracks = []
    for row in campaign:
        tracks += 1
        findings = build_lyric_observation(row["segments"])["findings"]
        counts.update(item["code"] for item in findings)
        if findings:
            flagged_tracks.append({"ordinal": row["ordinal"], "findings": findings})
    return {"tracks": tracks, "observation_counts": dict(counts), "flagged_tracks": flagged_tracks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison", type=Path)
    parser.add_argument("--snapshots", type=Path)
    parser.add_argument("--campaign", type=Path)
    args = parser.parse_args()
    snapshots = json.loads(args.snapshots.read_text(encoding="utf-8"))["versions"] if args.snapshots else []
    result = {"history": replay_requests(json.loads(args.comparison.read_text(encoding="utf-8")), snapshots)}
    if args.campaign:
        with args.campaign.open(encoding="utf-8") as source:
            result["campaign"] = replay_campaign(json.loads(line) for line in source if line.strip())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
