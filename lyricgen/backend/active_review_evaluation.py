"""Compare frozen listening proposals with exact historical editorial pairs.

No providers or product edits. Approved text is an editorial comparator, not
new acoustic ground truth. Unmatched changes are unknown, not counted as safe.
"""
from collections import Counter
from full_audio_review import binding
from reviewer_shadow import tokens


def distance(left, right):
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = row
    return previous[-1]


def segment_identity(segment):
    return tuple(segment[k] for k in ("start", "end", "text"))


def evaluate(snapshot, report, references):
    if report.get("binding") != binding(snapshot) or report.get("job_id") != snapshot.get("job_id"):
        raise ValueError("report_source_mismatch")
    segments = snapshot["segments"]
    proposals = {}
    for row in report["lines"]:
        index = row["index"]
        if type(index) is not int or not 0 <= index < len(segments):
            raise ValueError("invalid_proposal_index")
        if segment_identity(row["before"]) != segment_identity(segments[index]):
            raise ValueError("proposal_source_mismatch")
        if row["text"]["status"] == "TEXT_PROPOSAL":
            if index in proposals:
                raise ValueError("duplicate_proposal")
            proposals[index] = row["text"]["proposed_text"]
    eligible, skipped = [], Counter()
    for ref in references:
        if ref["job_id"] != snapshot["job_id"] or ref["source_revision"] != snapshot["revision"]:
            continue
        if ref.get("regression_target") is not True:
            skipped["not_regression_target"] += 1
            continue
        if ref["audio_sha256"] != snapshot["audio_sha256"]:
            skipped["audio_identity_mismatch"] += 1
            continue
        indices = ref["original_indices"]
        if (not indices or len(set(indices)) != len(indices)
                or any(type(i) is not int or not 0 <= i < len(segments) for i in indices)
                or [segment_identity(segments[i]) for i in indices] != [segment_identity(s) for s in ref["before"]]):
            skipped["source_segment_mismatch"] += 1
            continue
        eligible.append(ref)
    # Multiple historical approvals for the same original occurrence must not
    # inflate the denominator or silently select a convenient target.
    grouped = {}
    for ref in eligible:
        grouped.setdefault(tuple(ref["original_indices"]), []).append(ref)
    rows, covered = [], set()
    all_indices = [set(indices) for indices in grouped]
    for indices, refs in grouped.items():
        if any(set(indices) != other and set(indices) & other for other in all_indices):
            skipped["overlapping_reference_groups"] += len(refs)
            continue
        targets = {tuple(tokens(" ".join(s["text"] for s in r["after"]))) for r in refs}
        if len(targets) != 1:
            skipped["conflicting_approved_text"] += len(refs)
            continue
        target = next(iter(targets))
        before = tokens(" ".join(segments[i]["text"] for i in indices))
        candidate = tokens(" ".join(proposals.get(i, segments[i]["text"]) for i in indices))
        old_error, new_error = distance(before, target), distance(candidate, target)
        changed = before != candidate
        outcome = ("improved" if new_error < old_error else "worsened" if new_error > old_error
                   else "changed_neutral" if changed else "unresolved" if old_error else "unchanged_match")
        covered.update(indices)
        rows.append({"original_indices": list(indices), "example_ids": [r["example_id"] for r in refs],
                     "target_revisions": sorted({r["target_revision"] for r in refs}),
                     "baseline_token_errors": old_error, "candidate_token_errors": new_error,
                     "reference_tokens": len(target), "outcome": outcome,
                     "candidate_differs": changed, "acoustic_correctness_certified": False})
    denominator = sum(r["reference_tokens"] for r in rows)
    baseline = sum(r["baseline_token_errors"] for r in rows)
    candidate = sum(r["candidate_token_errors"] for r in rows)
    return {"schema_version": "active-review-editorial-evaluation-v1", "job_id": snapshot["job_id"],
            "binding": binding(snapshot), "comparator": "historical_approved_editorial_text",
            "punctuation_evaluated": False, "timing_accuracy_evaluated": False,
            "rows": rows, "outcomes": dict(Counter(r["outcome"] for r in rows)),
            "skipped": dict(skipped), "reference_pairs": len(rows),
            "baseline_token_errors": baseline, "candidate_token_errors": candidate,
            "reference_tokens": denominator,
            "baseline_wer": baseline / denominator if denominator else None,
            "candidate_wer": candidate / denominator if denominator else None,
            "text_proposals": len(proposals), "unmatched_proposal_indices": sorted(set(proposals) - covered),
            "human_precision_measured": False, "automatic_changes": 0}
