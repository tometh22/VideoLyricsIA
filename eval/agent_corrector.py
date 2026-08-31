"""Layer-D replay: verified candidates -> Gemini choice -> human-gold score.

The module deliberately keeps three artifacts apart:

* ``requests.jsonl``: exactly what the agent may see (raw state, context,
  clip and independently verified candidates).
* ``gold.jsonl``: Agus' historical decision, consumed only by ``score``.
* ``responses.jsonl``: validated agent decisions, never candidate sources.

No request is replayable unless every proposal has support from at least two
independent model families. Approved text/timing is forbidden from candidate
provenance and from the agent request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

from eval.canonical import read_json, segments_to_lines, write_json
from eval.metrics import align_lines, normalize_text
from eval.raw_cohort import RAW_TRUSTED


CATEGORIES = ("text", "timing", "vocalization")
ACTIONS = {"choose_candidate", "edit_candidate", "abstain"}
FORBIDDEN_PROVENANCE = {"approved", "approved_text", "approved_timing", "human_gold", "agus"}
AGENT_FAMILY = "gemini"
AGENT_MODEL_DEFAULT = "gemini-2.5-pro"
MIN_RESOLVED_PER_CATEGORY = 50
MIN_SONGS_PER_CATEGORY = 10
FUNCTIONAL_AGREEMENT_GATE = 0.80
FALSE_RESOLVED_GATE = 0.03


def canonical_family(value: Any) -> str:
    """Collapse model variants that share one acoustic/model family.

    In particular, Whisper v2 and v3 are useful candidate generators but are
    not independent witnesses.  Never trust a producer-supplied ``group`` to
    establish independence on its own.
    """
    name = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")
    if not name:
        return ""
    aliases = (
        (("gemini",), "gemini"),
        (("whisper",), "whisper"),
        (("qwen",), "qwen_asr"),
        (("wav2vec", "xlsr", "mms", "ctc"), "ctc_text_decoder"),
        (("firered", "fire_red"), "firered_asr"),
        (("cohere",), "cohere_transcribe"),
        (("granite",), "granite_speech"),
    )
    for needles, family in aliases:
        if any(needle in name for needle in needles):
            return family
    return name


def _canonical_supporting_families(proposal: dict[str, Any]) -> list[dict[str, str]]:
    families: dict[str, str] = {}
    for item in proposal.get("supporting_families") or []:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("name") or item.get("model") or item.get("group") or "").strip()
        family = canonical_family(source_name)
        if family:
            families.setdefault(family, source_name)
    return [{"name": families[family], "group": family} for family in sorted(families)]


def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _strict_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).strip().split())


def _vocalization_text(text: str) -> str:
    value = normalize_text(text)
    tokens = []
    for token in value.split():
        token = re.sub(r"([aeiou])\1+", r"\1", token)
        aliases = {"ooo": "oh", "oo": "oh", "ooh": "oh", "uhh": "uh", "ahh": "ah"}
        tokens.append(aliases.get(token, token))
    return " ".join(tokens)


def _looks_vocalization(text: str) -> bool:
    tokens = normalize_text(text).split()
    if not tokens:
        return False
    vocabulary = {"ah", "aha", "eh", "eoh", "oh", "ooh", "uh", "uhh", "uo", "uoh", "yeah", "na", "la"}
    return all(token in vocabulary or bool(re.fullmatch(r"[aeiouh]{1,12}", token)) for token in tokens)


def _proposal_value_valid(category: str, value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if category in {"text", "vocalization"}:
        if value.get("delete") is True:
            return True
        return bool(str(value.get("text") or "").strip())
    try:
        start, end = float(value["start"]), float(value["end"])
        return math.isfinite(start) and math.isfinite(end) and 0 <= start < end
    except (KeyError, TypeError, ValueError):
        return False


def validate_proposal(proposal: dict[str, Any]) -> tuple[bool, str]:
    category = str(proposal.get("category") or "")
    if category not in CATEGORIES:
        return False, "invalid_category"
    if not str(proposal.get("candidate_id") or ""):
        return False, "missing_candidate_id"
    if not _proposal_value_valid(category, proposal.get("value")):
        return False, "invalid_value"
    provenance_blob = json.dumps(proposal.get("provenance") or [], ensure_ascii=False).casefold()
    if any(token in provenance_blob for token in FORBIDDEN_PROVENANCE) or proposal.get("derived_from_approved"):
        return False, "approved_gold_provenance_forbidden"
    groups = {item["group"] for item in _canonical_supporting_families(proposal)}
    if AGENT_FAMILY in groups:
        return False, "agent_family_cannot_be_candidate_source"
    if len(groups) < 2:
        return False, "fewer_than_two_independent_family_groups"
    return True, "ok"


def _candidate_inventory(path: Path | None) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    by_zone: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reasons: dict[str, int] = defaultdict(int)
    seen_ids: set[str] = set()
    for bundle in _read_jsonl(path):
        zone_id = str(bundle.get("zone_id") or "")
        if not zone_id:
            reasons["missing_zone_id"] += len(bundle.get("proposals") or [None])
            continue
        for proposal in bundle.get("proposals") or []:
            valid, reason = validate_proposal(proposal)
            if not valid:
                reasons[reason] += 1
                continue
            candidate_id = str(proposal["candidate_id"])
            if candidate_id in seen_ids:
                reasons["duplicate_candidate_id"] += 1
                continue
            seen_ids.add(candidate_id)
            by_zone[zone_id].append({
                "candidate_id": candidate_id,
                "category": proposal["category"],
                "value": proposal["value"],
                "supporting_families": _canonical_supporting_families(proposal),
                "confidence": proposal.get("confidence"),
            })
            reasons["accepted"] += 1
    return by_zone, dict(sorted(reasons.items()))


def _category_gold(raw: dict[str, Any], approved: dict[str, Any] | None) -> list[str]:
    if approved is None:
        return ["vocalization" if _looks_vocalization(str(raw.get("text") or "")) else "text"]
    categories = []
    raw_text, approved_text = str(raw.get("text") or ""), str(approved.get("text") or "")
    if _looks_vocalization(raw_text) or _looks_vocalization(approved_text):
        if normalize_text(raw_text) != normalize_text(approved_text):
            categories.append("vocalization")
    elif normalize_text(raw_text) != normalize_text(approved_text):
        categories.append("text")
    if (
        abs(float(raw["start_s"]) - float(approved["start_s"])) > 0.001
        or abs(float(raw["end_s"]) - float(approved["end_s"])) > 0.001
    ):
        categories.append("timing")
    return categories


def _extract_clip(audio_path: Path, destination: Path, start_s: float, end_s: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", str(audio_path),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination),
    ], check=True)


def prepare(
    golden: Path, flags: Path, candidates_path: Path | None, output: Path,
    clips: Path, extract_clips: bool,
) -> dict[str, Any]:
    flag_report = read_json(flags)
    selected = [row for row in flag_report["selected_rows"] if row.get("selected")]
    candidates, candidate_reasons = _candidate_inventory(candidates_path)
    by_song: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        by_song[str(row["song_id"])].append(row)

    requests, gold_rows = [], []
    counts = defaultdict(int)
    manifest = read_json(golden / "manifest.json")
    allowed = {
        item["song_id"]: item for item in manifest["cases"]
        if item["raw_quality"] in RAW_TRUSTED
    }
    for song_id, zone_rows in sorted(by_song.items()):
        item = allowed.get(song_id)
        if not item:
            continue
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        raw_segments = read_json(case / "raw_pipeline_output.json")["segments"]
        raw_lines = segments_to_lines(raw_segments)
        approved_lines = segments_to_lines(read_json(case / "approved.json"))
        alignment = align_lines(approved_lines, raw_lines)
        approved_by_raw = {
            int(match["hyp_idx"]): approved_lines[int(match["ref_idx"])]
            for match in alignment["matches"]
        }
        context = "\n".join(f"{idx + 1}. {line['text']}" for idx, line in enumerate(raw_lines))
        audio_path = case / meta["audio"]["filename"]
        for flag in sorted(zone_rows, key=lambda row: int(row["line_idx"])):
            line_idx = int(flag["line_idx"])
            if not 0 <= line_idx < len(raw_lines):
                counts["invalid_line_idx"] += 1
                continue
            raw_line = raw_lines[line_idx]
            zone_id = f"{song_id}:{line_idx}"
            proposals = candidates.get(zone_id, [])
            status = "replayable" if proposals else "no_valid_consensus_candidate"
            clip_start = max(0.0, float(raw_line["start_s"]) - 4.0)
            clip_end = min(float(meta["duration_s"]), float(raw_line["end_s"]) + 4.0)
            clip_path = clips / song_id / f"line-{line_idx:04d}.wav"
            if extract_clips and proposals and not clip_path.is_file():
                _extract_clip(audio_path, clip_path, clip_start, clip_end)
            request = {
                "schema_version": 1,
                "zone_id": zone_id,
                "song_id": song_id,
                "line_idx": line_idx,
                "status": status,
                "is_live": bool(re.search(r"\b(?:live|en vivo)\b", str(meta.get("title") or ""), re.I)),
                "song": {"artist": meta.get("artist"), "title": meta.get("title"), "language": (meta.get("language") or {}).get("value")},
                "raw_line": raw_line,
                "flag_scores": {"predictor": flag.get("predictor"), "timing": flag.get("timing")},
                "song_context_pre_human": context,
                "clip": {
                    "path": str(clip_path), "start_s": clip_start, "end_s": clip_end,
                    "source_audio_sha256": meta["audio"]["sha256"], "mime_type": "audio/wav",
                },
                "proposals": proposals,
                "constraints": {
                    "actions": sorted(ACTIONS), "free_generation": False,
                    "minimum_independent_family_groups": 2,
                },
            }
            # This assertion prevents accidental future leakage during refactors.
            serialized = json.dumps(request, ensure_ascii=False).casefold()
            if any(key in serialized for key in ('"approved"', '"human_gold"', '"agus"')):
                raise RuntimeError(f"gold leakage in agent request {zone_id}")
            requests.append(request)
            approved = approved_by_raw.get(line_idx)
            categories = _category_gold(raw_line, approved)
            matching_candidate = {
                category: any(_proposal_agrees(proposal, approved, category, functional=True) for proposal in proposals)
                for category in CATEGORIES
            }
            gold_rows.append({
                "schema_version": 1, "zone_id": zone_id, "song_id": song_id,
                "line_idx": line_idx, "categories": categories,
                "raw": raw_line, "approved": approved,
                "candidate_can_reproduce_human": matching_candidate,
                "difficulty": (
                    "not_applicable" if not categories
                    else "no_correct_candidate" if not any(matching_candidate[c] for c in categories)
                    else "candidate_available"
                ),
            })
            counts[status] += 1
            for category in categories or ["none"]:
                counts[f"gold_{category}"] += 1

    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "requests.jsonl", requests)
    _write_jsonl(output / "gold.jsonl", gold_rows)
    report = {
        "schema_version": 1, "mode": "layer_d_agent_replay_prepare",
        "songs": len({row["song_id"] for row in requests}), "zones": len(requests),
        "counts": dict(sorted(counts.items())), "candidate_validation": candidate_reasons,
        "candidate_input": str(candidates_path) if candidates_path else None,
        "agent_requests_contain_approved_gold": False,
        "clip_policy": "raw line ±4s; extracted only for replayable zones",
        "outputs": {"requests": str(output / "requests.jsonl"), "gold": str(output / "gold.jsonl")},
    }
    write_json(output / "prepare_report.json", report)
    return report


def _proposal_agrees(
    proposal: dict[str, Any], approved: dict[str, Any] | None, category: str, functional: bool,
) -> bool:
    if proposal.get("category") != category:
        return False
    value = proposal.get("value") or {}
    if approved is None:
        return value.get("delete") is True
    if value.get("delete") is True:
        return False
    if category == "timing":
        tolerance = 0.30 if functional else 0.15
        try:
            return (
                abs(float(value["start"]) - float(approved["start_s"])) <= tolerance
                and abs(float(value["end"]) - float(approved["end_s"])) <= tolerance
            )
        except (KeyError, TypeError, ValueError):
            return False
    if category == "vocalization" and functional:
        return _vocalization_text(str(value.get("text") or "")) == _vocalization_text(str(approved.get("text") or ""))
    if functional:
        return normalize_text(str(value.get("text") or "")) == normalize_text(str(approved.get("text") or ""))
    return _strict_text(str(value.get("text") or "")) == _strict_text(str(approved.get("text") or ""))


def _edit_distance(left: str, right: str) -> int:
    left, right = normalize_text(left), normalize_text(right)
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left_char != right_char)))
        previous = current
    return previous[-1]


def validate_agent_response(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    proposals = {str(item["candidate_id"]): item for item in request.get("proposals") or []}
    seen_categories = set()
    decisions = []
    for raw in response.get("decisions") or []:
        category, action = str(raw.get("category") or ""), str(raw.get("action") or "")
        if category not in CATEGORIES or category in seen_categories or action not in ACTIONS:
            raise ValueError("invalid or duplicate category/action")
        seen_categories.add(category)
        if action == "abstain":
            decisions.append({"category": category, "action": action, "reason": str(raw.get("reason") or "")[:300]})
            continue
        candidate_id = str(raw.get("candidate_id") or "")
        proposal = proposals.get(candidate_id)
        if not proposal or proposal["category"] != category:
            raise ValueError("decision must reference a candidate of the same category")
        value = proposal["value"]
        if action == "edit_candidate":
            value = raw.get("value")
            if not _proposal_value_valid(category, value):
                raise ValueError("invalid edited value")
            if category in {"text", "vocalization"}:
                if proposal["value"].get("delete") is True or value.get("delete") is True:
                    raise ValueError("delete candidates can be chosen but not edited")
                base, edited = str(proposal["value"]["text"]), str(value["text"])
                limit = max(2, math.ceil(0.20 * max(1, len(normalize_text(base)))))
                if _edit_distance(base, edited) > limit:
                    raise ValueError("text edit is not minimal")
            else:
                if max(abs(float(value[key]) - float(proposal["value"][key])) for key in ("start", "end")) > 1.0:
                    raise ValueError("timing edit exceeds 1s minimal-edit limit")
        decisions.append({
            "category": category, "action": action, "candidate_id": candidate_id,
            "value": value, "reason": str(raw.get("reason") or "")[:300],
            "confidence": str(raw.get("confidence") or ""),
        })
    return {"zone_id": request["zone_id"], "decisions": decisions}


def _gemini_client():
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return genai.Client(api_key=api_key)
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global").strip()
    if project:
        return genai.Client(vertexai=True, project=project, location=location)
    raise RuntimeError("Gemini credentials missing: set GEMINI_API_KEY or GOOGLE_CLOUD_PROJECT + ADC")


def run_agent(requests_path: Path, output: Path, model: str, limit: int | None) -> dict[str, Any]:
    if os.environ.get("ALLOW_EXTERNAL_CLIENT_AUDIO_AGENT_REPLAY") != "1":
        raise RuntimeError("client-audio egress blocked; explicit ALLOW_EXTERNAL_CLIENT_AUDIO_AGENT_REPLAY=1 required")
    from google import genai

    requests = [row for row in _read_jsonl(requests_path) if row.get("status") == "replayable"]
    if limit is not None:
        requests = requests[:limit]
    existing = {row["zone_id"]: row for row in _read_jsonl(output)}
    client = _gemini_client()
    system = (
        "You are a conservative lyric-review operator, never a transcription source. "
        "For each category choose a supplied verified candidate, minimally edit one, or abstain. "
        "Never invent a correction without candidate_id. Return JSON {decisions:[{category,action," 
        "candidate_id,value,confidence,reason}]}. Omit candidate_id/value only for abstain."
    )
    for index, request in enumerate(requests, 1):
        if request["zone_id"] in existing:
            continue
        clip = Path(request["clip"]["path"])
        if not clip.is_file():
            raise RuntimeError(f"missing prepared clip: {clip}")
        visible = {key: value for key, value in request.items() if key != "clip"}
        prompt = system + "\nINPUT:\n" + json.dumps(visible, ensure_ascii=False)
        response = client.models.generate_content(
            model=model,
            contents=[
                genai.types.Part.from_text(text=prompt),
                genai.types.Part.from_bytes(data=clip.read_bytes(), mime_type="audio/wav"),
            ],
            config=genai.types.GenerateContentConfig(
                temperature=0, max_output_tokens=1000, response_mime_type="application/json",
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        parsed = json.loads(response.text)
        validated = validate_agent_response(request, parsed)
        existing[request["zone_id"]] = {
            **validated, "schema_version": 1, "model": model,
            "agent_family": AGENT_FAMILY, "input_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
        _write_jsonl(output, [existing[key] for key in sorted(existing)])
        print(f"agent replay {index}/{len(requests)} {request['zone_id']}", flush=True)
    return {"model": model, "eligible": len(requests), "completed": len(existing), "output": str(output)}


def _decision_agrees(decision: dict[str, Any], approved: dict[str, Any] | None, functional: bool) -> bool:
    if decision.get("action") == "abstain":
        return False
    proposal = {"category": decision["category"], "value": decision.get("value") or {}}
    return _proposal_agrees(proposal, approved, decision["category"], functional)


def score(
    requests_path: Path, gold_path: Path, responses_path: Path,
    adjudications_path: Path | None, output: Path,
) -> dict[str, Any]:
    requests = {row["zone_id"]: row for row in _read_jsonl(requests_path)}
    gold = {row["zone_id"]: row for row in _read_jsonl(gold_path)}
    responses = {row["zone_id"]: row for row in _read_jsonl(responses_path)}
    adjudications = _read_jsonl(adjudications_path)
    votes: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in adjudications:
        if canonical_family(row.get("judge_family")) == AGENT_FAMILY:
            continue
        votes[(str(row.get("zone_id")), str(row.get("category")))].append(row)

    rows_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    disagreements = []
    for zone_id, request in requests.items():
        gold_row = gold[zone_id]
        decisions = {row["category"]: row for row in (responses.get(zone_id, {}).get("decisions") or [])}
        for category in CATEGORIES:
            proposals = [row for row in request.get("proposals") or [] if row.get("category") == category]
            if not proposals:
                continue
            decision = decisions.get(category, {"category": category, "action": "abstain", "reason": "no_agent_decision"})
            resolved = decision["action"] != "abstain"
            exact = _decision_agrees(decision, gold_row.get("approved"), functional=False)
            functional = _decision_agrees(decision, gold_row.get("approved"), functional=True)
            human_changed = category in gold_row.get("categories", [])
            row_votes = votes.get((zone_id, category), [])
            by_judge = {
                canonical_family(v.get("judge_family")): v
                for v in row_votes
                if canonical_family(v.get("judge_family")) not in {"", AGENT_FAMILY}
            }
            valid_votes = set(by_judge)
            false_resolved = None
            if len(valid_votes) >= 3:
                wrong = sum(v.get("verdict") == "agent_wrong" for v in by_judge.values())
                valid_tie = sum(v.get("verdict") in {"both_valid", "agus_wrong"} for v in by_judge.values())
                false_resolved = wrong >= 2 and wrong > valid_tie
            row = {
                "zone_id": zone_id, "category": category, "resolved": resolved,
                "exact_agreement": exact, "functional_agreement": functional,
                "human_changed": human_changed, "abstained": not resolved,
                "difficulty": gold_row.get("difficulty"), "false_resolved": false_resolved,
                "judge_families": sorted(valid_votes), "is_live": request.get("is_live", False),
                "agent_decision": decision,
                "human_approved": gold_row.get("approved"),
            }
            rows_by_category[category].append(row)
            if resolved and not functional:
                disagreements.append(row)

    categories = {}
    for category in CATEGORIES:
        rows = rows_by_category.get(category, [])
        resolved = [row for row in rows if row["resolved"]]
        judged = [row for row in resolved if row["false_resolved"] is not None]
        songs = {row["zone_id"].split(":", 1)[0] for row in resolved}
        agreement = sum(row["functional_agreement"] for row in resolved) / max(1, len(resolved))
        false_rate = sum(bool(row["false_resolved"]) for row in judged) / max(1, len(judged))
        required_judgments = min(30, sum(row["resolved"] and not row["functional_agreement"] for row in rows))
        judge_complete = len([row for row in judged if not row["functional_agreement"]]) >= required_judgments
        enough = len(resolved) >= MIN_RESOLVED_PER_CATEGORY and len(songs) >= MIN_SONGS_PER_CATEGORY
        gate = (
            "GO_TIER_AGENT" if enough and judge_complete and agreement >= FUNCTIONAL_AGREEMENT_GATE and false_rate < FALSE_RESOLVED_GATE
            else "BLOCKED_PENDING_JUDGMENT" if enough and not judge_complete
            else "BLOCKED_INSUFFICIENT_EVIDENCE" if not enough
            else "NO_GO"
        )
        categories[category] = {
            "tasks": len(rows), "resolved": len(resolved),
            "songs_resolved": len(songs),
            "resolution_rate": len(resolved) / max(1, len(rows)),
            "exact_agreement": sum(row["exact_agreement"] for row in resolved) / max(1, len(resolved)),
            "functional_agreement": agreement,
            "abstention_rate": sum(row["abstained"] for row in rows) / max(1, len(rows)),
            "abstention_on_no_correct_candidate": sum(row["abstained"] and row["difficulty"] == "no_correct_candidate" for row in rows) / max(1, sum(row["difficulty"] == "no_correct_candidate" for row in rows)),
            "false_resolved": sum(bool(row["false_resolved"]) for row in judged),
            "false_resolved_rate_judged": false_rate,
            "judged_resolved": len(judged), "required_disagreement_judgments": required_judgments,
            "gate_contract": {
                "minimum_resolved": MIN_RESOLVED_PER_CATEGORY,
                "minimum_songs": MIN_SONGS_PER_CATEGORY,
                "minimum_functional_agreement": FUNCTIONAL_AGREEMENT_GATE,
                "maximum_false_resolved_rate": FALSE_RESOLVED_GATE,
            },
            "gate": gate,
            "production": {"tier_agent_enabled": gate == "GO_TIER_AGENT", "live_enabled": False},
        }
    report = {
        "schema_version": 1, "mode": "layer_d_agent_replay_score",
        "identity_rule": "sources generate; independent consensus verifies; agent chooses; human audits",
        "zones_with_responses": len(responses), "categories": categories,
        "disagreements": disagreements,
        "global_auto_enable_forbidden": True,
    }
    write_json(output, report)
    return report


def make_judge_sample(
    score_path: Path, requests_path: Path, gold_path: Path, output: Path, count: int,
) -> dict[str, Any]:
    report = read_json(score_path)
    requests = {row["zone_id"]: row for row in _read_jsonl(requests_path)}
    gold = {row["zone_id"]: row for row in _read_jsonl(gold_path)}
    disagreements = report.get("disagreements") or []
    generator = random.Random(20260829)
    chosen = generator.sample(disagreements, min(count, len(disagreements)))
    rows = []
    for row in sorted(chosen, key=lambda value: (value["zone_id"], value["category"])):
        zone_id = row["zone_id"]
        rows.append({
            "zone_id": zone_id, "category": row["category"],
            "clip": requests[zone_id]["clip"], "raw_line": requests[zone_id]["raw_line"],
            "agent_decision": row["agent_decision"],
            "agus_decision": gold[zone_id].get("approved"),
            "judge_family": "", "verdict": "", "confidence": "", "note": "",
            "allowed_verdicts": ["agent_wrong", "agus_wrong", "both_valid", "uncertain"],
        })
    _write_jsonl(output, rows)
    return {"disagreements": len(disagreements), "sample": len(rows), "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    prepare_parser.add_argument("--flags", type=Path, default=Path("eval/runs/flag_union/report.json"))
    prepare_parser.add_argument("--candidates", type=Path)
    prepare_parser.add_argument("--output", type=Path, default=Path("eval/runs/agent_corrector"))
    prepare_parser.add_argument("--clips", type=Path, default=Path("eval/cache/agent_corrector_clips"))
    prepare_parser.add_argument("--extract-clips", action="store_true")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--requests", type=Path, default=Path("eval/runs/agent_corrector/requests.jsonl"))
    run_parser.add_argument("--output", type=Path, default=Path("eval/runs/agent_corrector/responses.jsonl"))
    run_parser.add_argument("--model", default=AGENT_MODEL_DEFAULT)
    run_parser.add_argument("--limit", type=int)
    score_parser = sub.add_parser("score")
    score_parser.add_argument("--requests", type=Path, default=Path("eval/runs/agent_corrector/requests.jsonl"))
    score_parser.add_argument("--gold", type=Path, default=Path("eval/runs/agent_corrector/gold.jsonl"))
    score_parser.add_argument("--responses", type=Path, default=Path("eval/runs/agent_corrector/responses.jsonl"))
    score_parser.add_argument("--adjudications", type=Path)
    score_parser.add_argument("--output", type=Path, default=Path("eval/runs/agent_corrector/report.json"))
    sample_parser = sub.add_parser("sample")
    sample_parser.add_argument("--score", type=Path, default=Path("eval/runs/agent_corrector/report.json"))
    sample_parser.add_argument("--requests", type=Path, default=Path("eval/runs/agent_corrector/requests.jsonl"))
    sample_parser.add_argument("--gold", type=Path, default=Path("eval/runs/agent_corrector/gold.jsonl"))
    sample_parser.add_argument("--output", type=Path, default=Path("eval/runs/agent_corrector/judge_sample.jsonl"))
    sample_parser.add_argument("--count", type=int, default=30)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.golden.resolve(), args.flags.resolve(), args.candidates.resolve() if args.candidates else None, args.output.resolve(), args.clips.resolve(), args.extract_clips)
    elif args.command == "run":
        result = run_agent(args.requests.resolve(), args.output.resolve(), args.model, args.limit)
    elif args.command == "score":
        result = score(args.requests.resolve(), args.gold.resolve(), args.responses.resolve(), args.adjudications.resolve() if args.adjudications else None, args.output.resolve())
    else:
        result = make_judge_sample(args.score.resolve(), args.requests.resolve(), args.gold.resolve(), args.output.resolve(), args.count)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
