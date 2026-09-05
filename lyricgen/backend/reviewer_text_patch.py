"""Minimal text patches backed by two independent, reference-free audio views.

Only the changed span is supported, never the correctness of unchanged text.
Each view must uniquely contain the replacement plus a two-word adjacent
anchor from the current line. Forced alignment is not a recognition witness.
"""
from difflib import SequenceMatcher
import re

from reviewer_shadow import _family, tokens


def propose_patches(current, requests):
    words = tokens(current)
    spans = list(re.finditer(r"[^\W_]+(?:['’][^\W_]+)?", current))
    if len(spans) != len(words):
        return []
    witnesses = []
    for r in requests:
        if r.get("tool_status") != "ok" or r.get("received_audio") is not True or r.get("conditioning_texts") != []:
            continue
        response = r.get("response") or {}
        if response.get("editorial_ambiguity"):
            continue
        text = response.get("text") or " ".join(e.get("text", "") for e in response.get("events", []))
        family = _family(str(r.get("family") or r.get("model") or r.get("provider")))
        if family not in {"google_gemini", "openai_asr"} or r.get("view") == "alignment_audio":
            continue
        witnesses.append((family, tokens(text)))
    candidates = {}
    for _, heard in witnesses:
        for tag, a, b, c, d in SequenceMatcher(None, words, heard, autojunk=False).get_opcodes():
            if tag != "replace" or not 1 <= b - a <= 2 or not 1 <= d - c <= 3:
                continue
            replacement = heard[c:d]
            candidates[(a, b, tuple(replacement))] = replacement
    result = []
    for (a, b, _), replacement in candidates.items():
        anchors = []
        if a >= 2:
            anchors.append(words[a-2:a] + replacement)
        if len(words) - b >= 2:
            anchors.append(replacement + words[b:b+2])
        families = set()
        for family, heard in witnesses:
            if any(sum(heard[i:i+len(anchor)] == anchor
                       for i in range(len(heard)-len(anchor)+1)) == 1 for anchor in anchors):
                families.add(family)
        if len(families) < 2:
            continue
        updated = current[:spans[a].start()] + " ".join(replacement) + current[spans[b-1].end():]
        result.append({"decision": "propose", "text": updated,
            "changed_token_range": [a, b], "replacement_tokens": replacement,
            "families": sorted(families), "scope": "changed_span_only",
            "classification": "audio_supported_minimal_text_patch",
            "selector_policy": "minimal-audio-patch-v1",
            "correctness_certified": False, "unchanged_text_verified": False,
            "automatic_apply_allowed": False})
    # Conflicting patches require another pass, not arbitrary winner selection.
    return result if len(result) == 1 else []
