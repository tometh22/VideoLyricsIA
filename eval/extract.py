#!/usr/bin/env python3
"""SELECT-only UMG golden export with complete version/diff and R2 audio capture.

Required environment variables:
  DATABASE_URL, DELIVERIES_DATABASE_URL,
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL, R2_BUCKET

The command never writes to either database. It stages the dataset in a
``.partial`` directory and only promotes it after every audio checksum passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
from sqlalchemy import create_engine, text

from eval.canonical import (
    canonical_sha256, derive_edits, read_json, safe_extension, segments_to_lines,
    segments_to_words, write_json,
)

EXPECTED_ENV = (
    "DATABASE_URL", "DELIVERIES_DATABASE_URL", "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET",
)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _select(connection, statement: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Execute only SQL whose first token is SELECT."""
    if statement.lstrip().split(None, 1)[0].upper() != "SELECT":
        raise RuntimeError("golden extraction rejected a non-SELECT statement")
    return [dict(row) for row in connection.execute(text(statement), parameters or {}).mappings()]


def _table_exists(connection, table_name: str) -> bool:
    rows = _select(connection, "SELECT to_regclass(:name) AS relation", {"name": table_name})
    return bool(rows and rows[0]["relation"])


def _fetch_job(connection, job_id: str) -> dict[str, Any] | None:
    rows = _select(connection, """
        SELECT job_id, artist, song_title, filename, status, timing_source,
               completed_at, segments_json, segments_revision, input_r2_key,
               input_audio_sha256, transcription_quality, created_at
        FROM jobs WHERE job_id = :job_id LIMIT 1
    """, {"job_id": job_id})
    return rows[0] if rows else None


def _fetch_editor_data(connection, job_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    document = None
    versions: list[dict[str, Any]] = []
    if _table_exists(connection, "editor_documents"):
        rows = _select(connection, """
            SELECT job_id, current_segments, original_segments, revision,
                   updated_by, updated_at
            FROM editor_documents WHERE job_id = :job_id
        """, {"job_id": job_id})
        document = rows[0] if rows else None
    if _table_exists(connection, "editor_versions"):
        versions = _select(connection, """
            SELECT id, job_id, revision, segments, created_by, created_at,
                   reason, is_approved, provenance
            FROM editor_versions WHERE job_id = :job_id
            ORDER BY revision ASC, created_at ASC, id ASC
        """, {"job_id": job_id})
    return document, versions


def _fetch_audits(connection, job_id: str) -> list[dict[str, Any]]:
    if not _table_exists(connection, "audit_log"):
        return []
    return _select(connection, """
        SELECT id, user_id, action, detail, created_at
        FROM audit_log
        WHERE action = 'lyrics.segments_diff'
          AND (detail::jsonb)->>'job_id' = :job_id
        ORDER BY created_at ASC, id ASC
    """, {"job_id": job_id})


def _rewind(final_segments: list[dict[str, Any]], audits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]] | None, dict[str, int]]:
    if not audits:
        return None, {"events": 0, "truncated": 0, "invalid_indices": 0, "raw_text_changes": 0, "total_text_changes": 0}
    result = [dict(segment) for segment in final_segments]
    stats = {"events": len(audits), "truncated": 0, "invalid_indices": 0, "raw_text_changes": 0, "total_text_changes": 0}
    for audit in reversed(audits):
        detail = _json(audit.get("detail")) or {}
        stats["truncated"] += int(bool(detail.get("truncated")))
        for change in detail.get("changed") or []:
            raw_id = str(change.get("id") or "")
            if not raw_id.startswith("idx_"):
                stats["invalid_indices"] += 1
                continue
            try:
                index = int(raw_id[4:])
            except ValueError:
                stats["invalid_indices"] += 1
                continue
            if not 0 <= index < len(result):
                stats["invalid_indices"] += 1
                continue
            segment = result[index]
            if change.get("prev_start") is not None:
                segment["start"] = float(change["prev_start"])
            if change.get("prev_end") is not None:
                segment["end"] = float(change["prev_end"])
            if bool(change.get("text_changed")) or change.get("prev_text") is not None:
                stats["total_text_changes"] += 1
                if change.get("prev_text") is not None:
                    stats["raw_text_changes"] += 1
                    segment["text"] = str(change["prev_text"])
    return result, stats


def _observed_edits(versions: list[dict[str, Any]], audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    for before_version, after_version in zip(versions, versions[1:]):
        pair = derive_edits(_json(before_version["segments"]), _json(after_version["segments"]))
        for edit in pair:
            edit.update({
                "seq": len(edits) + 1,
                "timestamp": _iso(after_version.get("created_at")),
                "user": after_version.get("created_by"),
                "source": "editor_version_pair",
                "from_revision": before_version.get("revision"),
                "to_revision": after_version.get("revision"),
                "derived": True,
            })
            edits.append(edit)
    if edits:
        return edits
    # Legacy audit events are exported verbatim separately. Build the strongest
    # field-level history possible without inventing missing raw text.
    for audit in audits:
        detail = _json(audit.get("detail")) or {}
        for change in detail.get("changed") or []:
            raw_id = str(change.get("id") or "")
            try:
                line_idx = int(raw_id[4:]) if raw_id.startswith("idx_") else None
            except ValueError:
                line_idx = None
            for field, op in (("start", "start_edit"), ("end", "end_edit"), ("text", "text_edit")):
                before, after = change.get(f"prev_{field}"), change.get(f"new_{field}")
                if before is None and after is None:
                    continue
                edits.append({
                    "seq": len(edits) + 1,
                    "timestamp": _iso(audit.get("created_at")),
                    "user": audit.get("user_id"),
                    "line_idx": line_idx,
                    "op": op,
                    "field": "start_s" if field == "start" else "end_s" if field == "end" else "text",
                    "before": before,
                    "after": after,
                    "derived": False,
                    "source": "legacy_audit_diff",
                })
    return edits


def _duration(path: Path) -> float:
    process = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(process.stdout.strip())


def _detect_language(text_value: str) -> tuple[str, float | None]:
    try:
        from lingua import LanguageDetectorBuilder
        detector = LanguageDetectorBuilder.from_all_languages().build()
        result = detector.compute_language_confidence_values(text_value)
        if result:
            return result[0].language.iso_code_639_1.name.lower(), float(result[0].value)
    except (ImportError, ValueError):
        pass
    return "unknown", None


def _download_audio(client, bucket: str, key: str, destination: Path, expected_sha256: str | None) -> str:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    client.download_file(bucket, key, str(temporary))
    digest = hashlib.sha256()
    with temporary.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected_sha256 and actual.lower() != expected_sha256.lower():
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"audio SHA-256 mismatch for {destination.name}: expected {expected_sha256}, got {actual}")
    temporary.replace(destination)
    return actual


def extract(output: Path, expected_count: int) -> dict[str, Any]:
    missing = [name for name in EXPECTED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists():
        raise RuntimeError(f"output and partial paths must not exist: {output}, {partial}")
    portal_engine = create_engine(os.environ["DELIVERIES_DATABASE_URL"], pool_pre_ping=True)
    staging_engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    r2 = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    bucket = os.environ["R2_BUCKET"]
    partial.mkdir(parents=True)
    cases = []
    quality_counts = Counter()
    origin_counts = Counter()
    legacy_text = Counter()
    try:
        with portal_engine.connect() as portal, staging_engine.connect() as staging:
            deliveries = _select(portal, """
                SELECT id, job_id, artist_snapshot, song_title_snapshot,
                       approved_at, approved_by_label, added_at
                FROM deliveries
                WHERE removed_at IS NULL AND approved_at IS NOT NULL
                ORDER BY approved_at ASC, id ASC
            """)
            if len(deliveries) != expected_count:
                raise RuntimeError(f"expected {expected_count} approved deliveries, found {len(deliveries)}")
            if len({row["job_id"] for row in deliveries}) != expected_count:
                raise RuntimeError("approved delivery registry contains duplicate job_ids")
            for delivery in deliveries:
                job_id = str(delivery["job_id"])
                owner, origin = staging, "staging"
                job = _fetch_job(staging, job_id)
                if job is None:
                    owner, origin = portal, "production"
                    job = _fetch_job(portal, job_id)
                if job is None:
                    raise RuntimeError(f"delivery {delivery['id']} has no job {job_id}")
                approved = _json(job["segments_json"])
                if not isinstance(approved, list) or not approved:
                    raise RuntimeError(f"approved job {job_id} has no segments")
                document, versions = _fetch_editor_data(owner, job_id)
                audits = _fetch_audits(owner, job_id)
                rewound, rewind_stats = _rewind(approved, audits)
                legacy_text.update({
                    "events": rewind_stats["events"],
                    "text_changes": rewind_stats["total_text_changes"],
                    "raw_text_changes": rewind_stats["raw_text_changes"],
                    "jobs_with_any_raw_text": int(rewind_stats["raw_text_changes"] > 0),
                })
                if document and document.get("original_segments"):
                    raw, raw_quality = _json(document["original_segments"]), "exact"
                elif rewound is not None:
                    raw = rewound
                    raw_quality = "estimated" if (rewind_stats["truncated"] or rewind_stats["invalid_indices"]) else "reconstructed"
                else:
                    raw, raw_quality = None, "none"
                quality_counts[raw_quality] += 1
                origin_counts[origin] += 1
                case = partial / job_id
                case.mkdir()
                write_json(case / "approved.json", approved)
                write_json(case / "lines.json", segments_to_lines(approved))
                words = segments_to_words(approved)
                if words:
                    write_json(case / "words.json", words)
                write_json(case / "versions.json", [{**row, "segments": _json(row["segments"]), "created_at": _iso(row.get("created_at"))} for row in versions])
                write_json(case / "audit_diffs.json", [{**row, "detail": _json(row.get("detail")), "created_at": _iso(row.get("created_at"))} for row in audits])
                if raw is not None:
                    write_json(case / "raw_pipeline_output.json", {
                        "schema_version": 1, "job_id": job_id, "historical": True,
                        "raw_quality": raw_quality, "segments": raw,
                        "pipeline": {"timing_source": job.get("timing_source"), "quality": _json(job.get("transcription_quality"))},
                    })
                write_json(case / "edits.json", _observed_edits(versions, audits) if (versions or audits) else (derive_edits(raw, approved) if raw else []))
                extension = safe_extension(job.get("filename"))
                audio_path = case / f"audio{extension}"
                if not job.get("input_r2_key"):
                    raise RuntimeError(f"approved job {job_id} has no R2 audio key")
                actual_sha = _download_audio(r2, bucket, str(job["input_r2_key"]), audio_path, job.get("input_audio_sha256"))
                duration = _duration(audio_path)
                language, language_confidence = _detect_language(" ".join(str(x.get("text") or "") for x in approved))
                meta = {
                    "schema_version": 1, "song_id": job_id,
                    "artist": delivery.get("artist_snapshot"), "title": delivery.get("song_title_snapshot"),
                    "isrc": None,
                    "language": {"value": language, "confidence": language_confidence, "derived": True},
                    "duration_s": duration, "duration_derived": True,
                    "approved_at": _iso(delivery.get("approved_at")),
                    "approved_by": delivery.get("approved_by_label"),
                    "source_url": "https://umg.genly.pro/",
                    "job_origin": origin, "raw_quality": raw_quality,
                    "has_raw": raw is not None, "raw_historical": raw is not None,
                    "audio": {"filename": audio_path.name, "sha256": actual_sha, "expected_sha256": job.get("input_audio_sha256"), "verified": True},
                    "approved_sha256": canonical_sha256(approved),
                    "versions_count": len(versions), "audit_diff_events_count": len(audits),
                }
                write_json(case / "meta.json", meta)
                cases.append({"song_id": job_id, "path": job_id, "raw_quality": raw_quality, "has_raw": raw is not None, "job_origin": origin})
        rng = random.Random(20260829)
        portal_sample = sorted(item["song_id"] for item in rng.sample(cases, 5))
        report = {
            "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "awaiting_portal_verification",
            "songs": len(cases), "raw_quality_counts": dict(sorted(quality_counts.items())),
            "job_origin_counts": dict(sorted(origin_counts.items())),
            "legacy_raw_text_retention": dict(sorted(legacy_text.items())),
            "portal_verification_sample": [{"song_id": song_id, "verified": False} for song_id in portal_sample],
            "cases": cases,
        }
        write_json(partial / "manifest.json", report)
        write_json(partial / "extraction_report.json", report)
        return report
    except Exception:
        # Keep partial evidence for diagnosis; never present it as completed gold.
        raise
    finally:
        portal_engine.dispose()
        staging_engine.dispose()


def finalize_extraction(output: Path, verification_path: Path) -> dict[str, Any]:
    partial = output.with_name(output.name + ".partial")
    if output.exists():
        raise RuntimeError(f"final output already exists: {output}")
    if not partial.is_dir():
        raise RuntimeError(f"partial extraction does not exist: {partial}")
    report = read_json(partial / "extraction_report.json")
    verification = read_json(verification_path)
    rows = verification.get("cases") if isinstance(verification, dict) else verification
    if not isinstance(rows, list):
        raise RuntimeError("portal verification must be a list or an object with cases")
    by_song = {str(row.get("song_id")): row for row in rows if isinstance(row, dict)}
    required = [row["song_id"] for row in report["portal_verification_sample"]]
    missing = [song_id for song_id in required if not bool((by_song.get(song_id) or {}).get("verified"))]
    if missing:
        raise RuntimeError(f"five-case portal verification is incomplete: {', '.join(missing)}")
    report["portal_verification_sample"] = [by_song[song_id] for song_id in required]
    report["status"] = "complete"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(partial / "manifest.json", report)
    write_json(partial / "extraction_report.json", report)
    (partial / "README.md").write_text(
        "# UMG golden set\n\n"
        f"- Canciones aprobadas: **{report['songs']}**.\n"
        f"- Calidad de crudo: `{json.dumps(report['raw_quality_counts'], sort_keys=True)}`.\n"
        f"- Origen de jobs: `{json.dumps(report['job_origin_counts'], sort_keys=True)}`.\n"
        "- Los audios fueron descargados desde R2 y verificados por SHA-256.\n"
        "- Cinco casos de la muestra determinista fueron comparados con el portal.\n"
        "- `exact+reconstructed` y `exact+reconstructed+estimated` se reportan como cohortes separadas.\n",
        encoding="utf-8",
    )
    partial.replace(output)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/golden"))
    parser.add_argument("--expected-count", type=int, default=65)
    parser.add_argument("--finalize-verification", type=Path)
    args = parser.parse_args()
    try:
        report = (
            finalize_extraction(args.output.resolve(), args.finalize_verification.resolve())
            if args.finalize_verification
            else extract(args.output.resolve(), args.expected_count)
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
