#!/usr/bin/env python3
"""Materialize review-only T4 proposals as an isolated benchmark candidate.

Production never calls this script.  It applies only the proposals emitted by
``structural_t4_shadow`` so the frozen benchmark can measure target benefit and
collateral damage before any visible timing behavior is enabled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from structural_t4_shadow import build_structural_t4_shadow  # noqa: E402


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset).resolve()
    cases = args.case or sorted(path.name for path in dataset.iterdir() if path.is_dir())
    failures = 0
    for case_id in cases:
        try:
            case_dir = dataset / case_id
            source_path = case_dir / args.source_output
            source = json.loads(source_path.read_text(encoding="utf-8"))
            segments = source.get("segments") if isinstance(source, dict) else None
            if not isinstance(segments, list) or not segments:
                raise ValueError(f"source has no segments: {source_path}")
            audit = build_structural_t4_shadow(segments)
            output_segments = deepcopy(segments)
            for proposal in audit["proposals"]:
                index = int(proposal["segment_index"])
                candidate_end = float(proposal["candidate_display_end"])
                output_segments[index]["end"] = candidate_end
                output_segments[index]["display_end"] = candidate_end
                output_segments[index]["structural_t4_benchmark"] = {
                    "schema_version": audit["schema_version"],
                    "action": proposal["action"],
                    "previous_end": proposal["display_end"],
                    "candidate_end": candidate_end,
                    "occurrence_identity_attested": proposal[
                        "occurrence_identity_attested"
                    ],
                    "automatic_timing_change_allowed": False,
                }
            config = {
                "kind": "research-structural-t4-shadow",
                "source_system": source.get("system"),
                "policy": "word-clock-v1-occurrence-veto",
                "reference_used_at_inference": False,
                "automatic_timing_change_allowed": False,
            }
            payload = dict(source)
            payload.update({
                "research_only": True,
                "render": False,
                "system": args.system,
                "release": "structural-t4-shadow-v1",
                "config": config,
                "config_sha256": _hash(config),
                "segments": output_segments,
                "structural_t4_audit": audit,
                "source_config_sha256": source.get("config_sha256"),
            })
            destination = case_dir / "candidate_outputs" / f"{args.system}.json"
            _write(destination, payload)
            print(
                f"[OK] {case_id}: proposed {audit['proposal_count']}/"
                f"{audit['segment_count']} visible endpoints"
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            failures += 1
            print(f"[ERR] {case_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--dataset", required=True)
    cli.add_argument("--case", action="append", default=[])
    cli.add_argument(
        "--source-output", default="candidate_outputs/shadow-baseline.json",
    )
    cli.add_argument("--system", default="shadow-structural-t4-v1")
    return cli


if __name__ == "__main__":
    raise SystemExit(build(parser().parse_args()))
