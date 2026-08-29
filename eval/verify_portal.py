#!/usr/bin/env python3
"""Verify the adversarial golden-set sample against the live UMG portal feed.

The portal does not expose lyric text or word timings.  Its read-only feed does,
however, identify the exact job rendered for each visible delivery.  This gate
therefore verifies the live job identity and metadata, all rendered artifacts,
the extracted approved snapshot checksum, and (where EditorVersion exists) the
immutable approved revision checksum.  Legacy audit-only jobs are reported as
such instead of pretending that the portal independently exposes their raw ASR.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.canonical import canonical_sha256, read_json, write_json


def _same_instant(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return left == right
    return datetime.fromisoformat(left).timestamp() == datetime.fromisoformat(right).timestamp()


def _portal_versions(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for song in payload.get("songs") or []:
        for version in song.get("versions") or []:
            job_id = str(version.get("job_id") or "")
            if not job_id:
                continue
            result.setdefault(job_id, []).append({
                **version,
                "portal_artist": song.get("artist"),
                "portal_title": song.get("song"),
            })
    return result


def verify_portal(golden: Path, portal_payload: Path, output: Path) -> dict[str, Any]:
    report = read_json(golden / "extraction_report.json")
    expected = report.get("portal_verification_sample") or []
    if len(expected) != 5:
        raise RuntimeError(f"portal gate requires exactly five cases, found {len(expected)}")
    live = _portal_versions(read_json(portal_payload))
    results = []
    for selected in expected:
        song_id = str(selected["song_id"])
        matches = live.get(song_id, [])
        if len(matches) != 1:
            raise RuntimeError(f"portal must expose exactly one delivery for {song_id}; found {len(matches)}")
        portal = matches[0]
        case = golden / song_id
        meta = read_json(case / "meta.json")
        approved = read_json(case / "approved.json")
        extracted_sha = canonical_sha256(approved)
        checks = {
            "job_identity": str(portal.get("job_id")) == song_id,
            "artist": portal.get("portal_artist") == meta.get("artist"),
            "title": portal.get("portal_title") == meta.get("title"),
            "approved_at": _same_instant(portal.get("approved_at"), meta.get("approved_at")),
            "approved_by": portal.get("approved_by_label") == meta.get("approved_by"),
            "approved_snapshot_sha": extracted_sha == meta.get("approved_sha256"),
            "rendered_files_visible": bool(portal.get("files")) and all(
                bool(item.get("available")) for item in portal.get("files") or []
            ),
        }
        versions = read_json(case / "versions.json")
        immutable = [version for version in versions if version.get("is_approved")]
        if immutable:
            latest = immutable[-1]
            checks["immutable_approved_revision_sha"] = (
                canonical_sha256(latest.get("segments") or []) == extracted_sha
            )
            snapshot_evidence = "latest_immutable_editor_version"
        else:
            snapshot_evidence = "legacy_job_snapshot_plus_audit_chain"
        verified = all(checks.values())
        if not verified:
            failed = ", ".join(name for name, passed in checks.items() if not passed)
            raise RuntimeError(f"portal verification failed for {song_id}: {failed}")
        results.append({
            **selected,
            "verified": True,
            "delivery_id": portal.get("delivery_id"),
            "portal_label": portal.get("label"),
            "snapshot_evidence": snapshot_evidence,
            "checks": checks,
            "note": (
                "The live portal confirms the delivered job and rendered files; "
                "raw pipeline text is not exposed by the portal."
            ),
        })
    verification = {
        "schema_version": 1,
        "source": "https://umg.genly.pro/api/deliveries/items",
        "read_only": True,
        "cases": results,
    }
    write_json(output, verification)
    return verification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--portal-payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_portal(
            args.golden.resolve(), args.portal_payload.resolve(), args.output.resolve(),
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"verified {len(report['cases'])} portal deliveries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
