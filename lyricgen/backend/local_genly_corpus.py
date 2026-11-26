"""Prepare a private, audio-bound development corpus from approved corrections.

This is not a fresh evaluation set. Published editorial line boundaries are
retained as line supervision, never relabelled as human word alignments.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import unicodedata


SCHEMA = "local-genly-corpus-v1"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artist(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def assign_splits(rows, registry):
    """Connect artist, original SHA and job aliases before assigning any role.

    Registry entries participate even when their job is absent from this export.
    A holdout in a connected component protects its entire artist/audio group.
    Existing training exposure is disclosed, so no resulting split is called
    clean test data. Unknown local groups use a deterministic development split.
    """
    parents = {}

    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def join(keys):
        if not keys:
            return
        first = root(keys[0])
        for key in keys[1:]:
            parents[root(key)] = first

    def keys(row, entry=False):
        result = []
        artist = _artist(row.get("artist"))
        if artist and artist != "unknown":
            result.append("artist:" + artist)
        sha = row.get("sha256" if entry else "audio_sha256")
        if sha:
            result.append("audio:" + str(sha).lower())
        jobs = [j.get("job_id") for j in row.get("known_job_ids", [])] if entry else [row.get("job_id")]
        result.extend("job:" + str(job) for job in jobs if job)
        return result

    entries = registry.get("entries", [])
    for row in rows:
        join(keys(row))
    for entry in entries:
        join(keys(entry, True))
    roles = defaultdict(set)
    for entry in entries:
        values = keys(entry, True)
        if values and entry.get("role"):
            roles[root(values[0])].add(entry["role"])
    components = defaultdict(list)
    for key in parents:
        components[root(key)].append(key)
    result = {}
    for row in rows:
        component = root(keys(row)[0])
        group = hashlib.sha256("\n".join(sorted(components[component])).encode()).hexdigest()
        known = roles[component]
        if "eval_holdout" in known:
            split = "development_replay"
        elif "val" in known:
            split = "calibration"
        elif "train" in known:
            split = "train"
        else:
            split = "train" if int(group[:8], 16) % 10 < 7 else "calibration"
        result[row["job_id"]] = {
            "split": split, "split_group": group,
            "known_registry_roles": sorted(known),
            "role_source": "existing_registry_component" if known else "explicit_local_development_assignment",
            "evaluation_claim": "development_only",
            "prior_training_exposure": "known" if "train" in known else "not_established",
        }
    return result


def probe_duration(path):
    value = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ], text=True, timeout=30)
    duration = float(json.loads(value)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_audio_duration")
    return duration


def _private_json(path, payload, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        if jsonl:
            for row in payload:
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        else:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")


def prepare(examples_path, audio_map_path, registry_path, output_dir, *, duration_probe=probe_duration):
    examples_path, audio_map_path, registry_path, output_dir = map(Path, (
        examples_path, audio_map_path, registry_path, output_dir,
    ))
    raw = [json.loads(line) for line in examples_path.read_text().splitlines() if line.strip()]
    mapping = json.loads(audio_map_path.read_text())
    registry = json.loads(registry_path.read_text())
    rejected = Counter()
    selected = []
    for row in raw:
        if row.get("regression_target") is not True:
            rejected["not_regression_target"] += 1
        elif row.get("supervision") != "approved_published_editorial_reference":
            rejected["unverified_approval"] += 1
        elif not row.get("audio_sha256") or not row.get("job_id"):
            rejected["missing_identity"] += 1
        elif not isinstance(row.get("source_revision"), int) or not isinstance(row.get("target_revision"), int):
            rejected["missing_revision"] += 1
        else:
            selected.append(row)
    # Multiple approved versions are legitimate history, not independent labels
    # for one ASR crop. Use the latest published reference in this frozen export.
    revisions = {}
    for row in selected:
        revisions[row["job_id"]] = max(revisions.get(row["job_id"], -1), row["target_revision"])
    chosen = [r for r in selected if r["target_revision"] == revisions[r["job_id"]]]
    rejected["older_approved_revision"] = len(selected) - len(chosen)
    assignments = assign_splits(chosen, registry)
    audio_by_job = defaultdict(list)
    for item in mapping:
        audio_by_job[str(item.get("job_id"))].append(item)
    verified = {}
    audio_issues = {}
    for job in sorted({r["job_id"] for r in chosen}):
        expected = {r["audio_sha256"] for r in chosen if r["job_id"] == job}
        if len(expected) != 1:
            audio_issues[job] = "conflicting_audio_identity"
            continue
        sha = next(iter(expected))
        matches = [m for m in audio_by_job[job] if m.get("original_sha256") == sha]
        if not matches:
            audio_issues[job] = "audio_map_missing"
            continue
        path = Path(matches[0]["source_path"]).expanduser().resolve()
        if not path.is_file():
            audio_issues[job] = "audio_file_missing"
            continue
        if sha256_file(path) != sha:
            audio_issues[job] = "audio_sha_mismatch"
            continue
        try:
            duration = duration_probe(path)
        except (ValueError, KeyError, subprocess.SubprocessError, OSError):
            audio_issues[job] = "audio_probe_failed"
            continue
        verified[job] = {"audio_path": str(path), "audio_sha256": sha, "audio_duration_s": duration}
    deltas, candidates = [], defaultdict(list)
    for row in chosen:
        job = row["job_id"]
        if job not in verified:
            rejected["unverified_audio"] += 1
            continue
        bound = {**row, **verified[job], **assignments[job], "schema": SCHEMA}
        bound["supervision_tiers"] = ["editorial_text", "historical_delta"]
        if row.get("timing_comparable") is True:
            bound["supervision_tiers"].append("editorial_line_timing")
        bound["word_timing_supervision"] = False
        deltas.append(bound)
        after = row.get("after", [])
        indices = row.get("approved_indices", [])
        if len(after) != len(indices):
            rejected["approved_index_mismatch"] += 1
            continue
        for index, line in zip(indices, after):
            candidates[(job, row["target_revision"], index)].append((bound, line))
    samples = []
    for (job, revision, index), labels in sorted(candidates.items()):
        signatures = {json.dumps({k: line.get(k) for k in ("text", "start", "end")}, sort_keys=True) for _, line in labels}
        if len(signatures) != 1:
            rejected["conflicting_approved_line"] += 1
            continue
        bound, line = labels[0]
        try:
            start, end = float(line["start"]), float(line["end"])
            valid = math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= bound["audio_duration_s"]
        except (KeyError, TypeError, ValueError):
            valid = False
        text = str(line.get("text") or "").strip()
        if not valid or not text:
            rejected["invalid_approved_crop"] += 1
            continue
        if end - start > 30:
            rejected["crop_over_30_seconds"] += 1
            continue
        samples.append({
            **{k: bound[k] for k in ("audio_path", "audio_sha256", "audio_duration_s", "artist", "source_revision", "target_revision")},
            **assignments[job], "schema": SCHEMA, "sample_id": f"{job}-r{revision}-l{index}",
            "song_id": job, "job_id": job, "start_s": start, "end_s": end,
            "text": text, "language": "es", "approved_index": index,
            "example_ids": sorted({b["example_id"] for b, _ in labels}),
            "supervision": "approved_editorial_line", "word_timing_supervision": False,
            "crop_alignment": "editorial_boundary_not_acoustically_certified",
        })
    audit = {
        "schema": SCHEMA, "input_examples": len(raw), "selected_latest_examples": len(chosen),
        "verified_songs": len(verified), "audio_issues": audio_issues,
        "samples": len(samples), "deltas": len(deltas), "rejected": dict(rejected),
        "sample_splits": dict(Counter(r["split"] for r in samples)),
        "song_splits": dict(Counter(assignments[job]["split"] for job in verified)),
        "group_splits": dict(Counter({r["split_group"]: r["split"] for r in samples}.values())),
        "line_timing_examples": sum(r.get("timing_comparable") is True for r in deltas),
        "fresh_holdout": False, "evaluation_claim": "development_only",
        "limitations": ["historical songs previously used in research", "editorial line crops are not acoustically adjudicated", "no human word-boundary labels", "Spanish catalog assumption must be checked before multilingual training"],
        "assignments": assignments,
    }
    for name, payload in (("samples.jsonl", samples), ("deltas.jsonl", deltas), ("audit.json", audit)):
        _private_json(output_dir / name, payload, jsonl=name.endswith("jsonl"))
    manifest = {
        "schema": SCHEMA, "evaluation_claim": "development_only", "fresh_holdout": False,
        "sources": {str(path.resolve()): sha256_file(path) for path in (examples_path, audio_map_path, registry_path)},
        "outputs": {name: {"path": str((output_dir / name).resolve()), "sha256": sha256_file(output_dir / name)} for name in ("samples.jsonl", "deltas.jsonl", "audit.json")},
    }
    _private_json(output_dir / "manifest.json", manifest)
    return audit
