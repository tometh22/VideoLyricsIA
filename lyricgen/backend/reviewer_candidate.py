"""Complete isolated song candidates; never editor writes or approvals."""
from collections import Counter
from copy import deepcopy
import math

from reviewer_shadow import assert_current, review_window, sequence_discrepancies, source_binding, tokens, validate_snapshot
from shadow_reference_import import digest


def check_song(segments, *, duration, baseline):
    """Whole-song structural checks, including rows outside repaired windows."""
    findings = []
    for i, row in enumerate(segments):
        start, end = row.get("start"), row.get("end")
        valid = all(isinstance(x, (float, int)) and math.isfinite(x) for x in (start, end))
        if not valid or not 0 <= start < end <= duration:
            findings.append({"line_index": i, "reason": "invalid_timeline", "kind": "technical"})
        if valid and i and start < segments[i-1]["end"]:
            findings.append({"line_index": i, "reason": "overlap_requires_policy", "kind": "timing"})
        if not tokens(row.get("text", "")):
            findings.append({"line_index": i, "reason": "empty_lexical_content", "kind": "content"})
        if i < len(baseline) and (baseline[i].get("locked") or baseline[i].get("operator_locked")) and row != baseline[i]:
            raise ValueError("protected_content_changed")
    return findings


def interpretation(song, hypotheses):
    """Ordered occurrence ledger with alternate whole-audio interpretations.

    This is not an acoustic PerformanceGraph: do not relabel lyric-derived
    boundaries as independent acoustic evidence. Attach acoustic graphs only
    when actual feature extraction has run in a verified clock.
    """
    seen = Counter()
    nodes = []
    for i, s in enumerate(song["segments"]):
        phrase_key = digest(tokens(s.get("text", "")))
        seen[phrase_key] += 1
        nodes.append({"id": f"{song['job_id']}:{song['segments_revision']}:{i}",
            "line_index": i, "phrase_key": phrase_key, "occurrence_number": seen[phrase_key],
            "text": s["text"], "start": s["start"], "end": s["end"],
            "words": deepcopy(s.get("words", [])), "clock": "baseline_unverified",
            "section": None, "section_status": "not_acoustically_inferred",
            "verification": "preserved_not_certified"})
    return {"schema": "song-occurrence-ledger-v1", "occurrences": nodes,
        "order_edges": [[a["id"], b["id"]] for a, b in zip(nodes, nodes[1:])],
        "hypotheses": deepcopy(hypotheses), "acoustic_performance_graph": None,
        "repetitions_are_independent_recognition_families": False}


def build_candidate(song, decisions=(), *, hypotheses=(), external_reference=None):
    validate_snapshot(song)
    baseline = deepcopy(song["segments"])
    candidate = deepcopy(baseline)
    changes, unresolved, errors = [], [], []
    seen = set()
    for supplied in decisions:
        assert_current(supplied, song)
        decision = review_window(song, supplied["window"], evidence=supplied["evidence"], commit=supplied["commit"])
        i = decision["window"]["line_index"]
        errors.extend(decision["tool_errors"])
        if i in seen:
            unresolved.append({"line_index": i, "reason": "multiple_decisions_require_reconciliation"})
            # Fail closed for the complete line, including an earlier decision.
            candidate[i] = deepcopy(baseline[i])
            changes = [c for c in changes if c["line_index"] != i]
            continue
        seen.add(i)
        if baseline[i].get("locked") or baseline[i].get("operator_locked"):
            continue
        for kind, field, target in (("content", "text", "text"), ("timing", "end", "end_seconds")):
            verdict = decision[kind]
            if verdict["decision"] == "propose":
                old = candidate[i][field]
                candidate[i][field] = verdict[target]
                if field == "text" and candidate[i][field] != old:
                    candidate[i].pop("words", None)
                    candidate[i]["word_alignment_status"] = "not_certified_after_text_change"
                changes.append({"line_index": i, "field": field, "before": old,
                    "after": candidate[i][field], "author_kind": "machine_candidate",
                    "evidence_id": decision["proposal_id"], "evidence_sha256": digest(decision["evidence"]),
                    "selector": verdict, "human_decision": False})
            elif verdict["decision"] == "abstain":
                unresolved.append({"line_index": i, "kind": kind, "reason": verdict.get("reason")})
    structural = check_song(candidate, duration=float(song["duration_seconds"]), baseline=baseline)
    # Never leave a newly invalid line in a draft merely because it is isolated.
    invalid = {f["line_index"] for f in structural if f["reason"] == "invalid_timeline"}
    for i in invalid:
        candidate[i] = deepcopy(baseline[i])
        changes = [c for c in changes if c["line_index"] != i]
    discrepancies = []
    for h in hypotheses:
        if h.get("view") != "full_audio_without_reference":
            continue
        text = "\n".join(e.get("text", "") for e in h.get("events", []))
        discrepancies.extend({**d, "source_family": h.get("family"),
                              "audio_error_confirmed": False}
                             for d in sequence_discrepancies(candidate, text)
                             if d["operation"] != "format")
    external = None
    if external_reference and external_reference.get("matched_job_id") == song["job_id"] and external_reference.get("association") == "unique_metadata_candidate" and external_reference.get("availability") == "present":
        external = {"source": {k: external_reference.get(k) for k in
            ("workbook_sha256", "sheet", "row", "content_sha256", "association", "recording_correspondence")},
            "role": "auxiliary_hypothesis_not_audio_certification",
            "differences": sequence_discrepancies(candidate, external_reference["lyrics"])}
    from spanish_orthography import analyze_spanish_orthography
    orthography = analyze_spanish_orthography(candidate)
    result = {"schema": "isolated-song-candidate-v1", "source": source_binding(song),
        "baseline": baseline, "baseline_sha256": digest(baseline),
        "segments": candidate, "candidate_sha256": digest(candidate), "changes": changes,
        "interpretation": interpretation({**song, "segments": candidate}, hypotheses),
        "residual_qc": {"timeline": structural, "hypothesis_discrepancies": discrepancies,
            "orthography": orthography, "external_reference": external,
            "unresolved_decisions": unresolved, "tool_errors": errors,
            "all_lines_structurally_checked": len(candidate),
            "independently_verified_lines": [], "complete_audio_coverage_verified": False,
            "unmodified_lines_are_not_certified": True},
        "decision_evidence": deepcopy(list(decisions)), "isolated_candidate_only": True,
        "production_apply_allowed": False, "approved": False,
        "human_review_required": True, "machine_changes_locked": False}
    result["id"] = digest({"source": result["source"], "candidate": result["candidate_sha256"], "changes": changes})
    return result
