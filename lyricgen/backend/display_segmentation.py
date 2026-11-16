"""Final, reversible display segmentation. No recognizer, network, or retiming.

Only exact slices of current display tokens and current contained word clocks
may produce children. The planner never changes the input. The atomic applier
revalidates the full source (including locks/metadata), scope and result rather
than trusting a proposal or a language model. Historical/editor flows must not
call this stage. Uncertain boundaries remain explicit review items.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import re
import unicodedata

POLICY_VERSION = "display-phrases-v1"
SCHEMA = "display-segmentation-proposal-v1"
# End/start fragments that almost always strand a function word. Conservative
# across ES/EN/PT; unknown languages can still use punctuation + word clocks.
_DANGLING = frozenset("a al de del el la los las un una unos unas y o que por para con sin en mi tu su te me se le les lo pero ni the an and or of to with for e os as um uma do da dos das em com".split())
# Words that never close a sung phrase in ES/PT (articles, prepositions,
# contractions, prenominal possessives, relatives). Post-positive PT
# possessives ("amor meu") and ES "no" (negation) are deliberately absent.
_OPEN_ENDED = _DANGLING | frozenset(
    "quem quien cuyo cuya cujo cuja pelo pela pelos pelas num numa ao aos à às pro pra mis tus sus nuestro nuestra".split()
)
# Lowercase prepositions/articles that, opening the next card without any
# punctuation before them, continue the phrase ("passe | a enxergar").
# Conjunctions and relatives ("que", "y", "e", "mas") legitimately open a
# phrase and are absent; so are ES "no"/"nos" and the ambiguous "o".
_CONTINUES_PHRASE = frozenset(
    "a al de del el la los las un una unos unas en por para con sin os as um uma do da dos das em com ao aos à às pra pro pelo pela pelos pelas num numa na nas".split()
)
# Lowercase infinitive opening a card right after a verb ("quiero | ver").
_INFINITIVE = re.compile(r"^[^\W\d_]{2,}(?:ar|er|ir|ôr)$")
_TRAILING_PUNCTUATION = re.compile(r"[,;:.!?…][\)\]\"’”]*$")
_PRESENTATION = frozenset({
    "scale", "pos", "rot", "font", "font_size", "color", "event_type",
    "speaker", "layer", "style", "alignment", "voice", "language",
})


@dataclass(frozen=True)
class Config:
    target_words: int = 6
    max_words: int = 8
    max_chars: int = 42
    min_words: int = 3
    min_card_s: float = 0.65
    pause_s: float = 0.18
    punctuation_pause_s: float = 0.04


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()).hexdigest()


# off: nothing. shadow: plan + report only. suggest: shadow + the editor shows
# each proposal as a one-click reviewer suggestion (never applied by the
# pipeline). enforce: signed, fresh-job automatic application.
MODES = frozenset({"off", "shadow", "suggest", "enforce"})


def mode() -> str:
    value = os.environ.get("DISPLAY_SEGMENTATION_MODE", "shadow").strip().lower()
    return value if value in MODES else "off"


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_time")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("invalid_time")
    return value


def _token(value) -> str:
    # Preserve accents and lexical content. Punctuation is a display property;
    # it stays byte-for-byte in the source text, never reconstructed from ASR.
    return "".join(c for c in unicodedata.normalize("NFC", str(value)).casefold()
                   if c.isalnum() or c in {"'", "’"})


def _locked(row) -> bool:
    return any(row.get(key) for key in (
        "locked", "operator_locked", "timing_locked", "text_locked",
    ))


def _protected(row) -> bool:
    return _locked(row) or bool(row.get("display_segmentation"))


def _stale(row) -> bool:
    return bool(row.get("word_evidence_status") == "stale" or row.get("words_stale") or row.get("word_evidence_stale")
                or (isinstance(row.get("word_evidence"), dict)
                    and row["word_evidence"].get("stale")))


def _parent(row: dict) -> tuple[list[str], list[dict]]:
    if _protected(row):
        raise ValueError("protected_line")
    if row.get("live_structural_suggestion") or row.get("provider_timing_collapsed") or (
            isinstance(row.get("timing_validation"), dict)
            and row["timing_validation"].get("status") == "unvalidated"):
        raise ValueError("parent_quality_review_required")
    if row.get("provider_output_integrity") is False:
        raise ValueError("source_evidence_invalid")
    if _stale(row):
        raise ValueError("stale_word_evidence")
    if not isinstance(row.get("text"), str):
        raise ValueError("invalid_text")
    if any(row.get(key) for key in ("words_synthetic", "words_estimated")):
        raise ValueError("stale_or_synthetic_word_evidence")
    start, end = _number(row.get("start")), _number(row.get("end"))
    if start < 0 or end <= start:
        raise ValueError("invalid_parent_time")
    tokens = str(row.get("text") or "").split()
    words = row.get("words")
    if not isinstance(words, list) or len(words) != len(tokens) or not tokens:
        raise ValueError("word_coverage_incomplete")
    cursor = start
    for token, word in zip(tokens, words):
        if not isinstance(word, dict) or not _token(token) or _token(token) != _token(word.get("word", "")):
            raise ValueError("word_text_mismatch")
        if word.get("stale") or word.get("synthetic") or word.get("estimated"):
            raise ValueError("stale_or_synthetic_word_evidence")
        a, b = _number(word.get("start")), _number(word.get("end"))
        if a < cursor or b <= a or b > end:
            raise ValueError("word_clocks_invalid_or_overlapping")
        cursor = b
    return tokens, words


def _balanced(tokens: list[str]) -> bool:
    text = " ".join(tokens)
    return all(text.count(a) == text.count(b) for a, b in (("(", ")"), ("[", "]")))


def _phrase_continues(tokens, k) -> bool:
    """Reviewer-suggestion guard: the cut would strand a phrase.

    Never after a word that cannot close a phrase, and never before a
    lowercase preposition/article or infinitive that continues it, unless
    punctuation already closes the left side ("…gaveta, | pelo gambé").
    """
    left, right = tokens[k - 1], tokens[k]
    if _TRAILING_PUNCTUATION.search(left):
        return False
    if _token(left) in _OPEN_ENDED:
        return True
    first = right.lstrip("¿¡\"'“‘([")
    if not first or not first[0].islower():
        return False
    return _token(first) in _CONTINUES_PHRASE or bool(_INFINITIVE.match(_token(first)))


def _boundary(tokens, words, k, config, phrase_guard: bool = False) -> str | None:
    if _token(tokens[k - 1]) in _DANGLING:
        return None
    if phrase_guard and _phrase_continues(tokens, k):
        return None
    if not _balanced(tokens[:k]) or not _balanced(tokens[k:]):
        return None
    gap = words[k]["start"] - words[k - 1]["end"]
    if re.search(r"[,;:.!?…][\)\]\"’”]*$", tokens[k - 1]) and gap >= config.punctuation_pause_s:
        return "punctuation_and_pause"
    if gap >= config.pause_s:
        return "word_pause"
    # Repetition is an occurrence-aware candidate, not a text search in
    # another chorus. Require contiguous identical phrases + a real gap.
    if gap >= config.punctuation_pause_s and k >= config.min_words:
        if [_token(t) for t in tokens[:k]] == [_token(t) for t in tokens[k:2 * k]]:
            return "repeated_phrase_and_pause"
    return None


def _partition(row, config: Config, phrase_guard: bool = False):
    tokens, words = _parent(row)
    n = len(tokens)
    allowed = {k: _boundary(tokens, words, k, config, phrase_guard) for k in range(1, n)}
    # Endpoints stay EXACTLY as provided. Interior switches use the onset of
    # the next word, after the previous word has finished; no count-based time.
    clocks = {0: row["start"], n: row["end"], **{
        k: words[k]["start"] for k in range(1, n)
    }}
    best = {0: (0.0, [])}
    for end in range(1, n + 1):
        if end != n and not allowed.get(end):
            continue
        for start in range(max(0, end - config.max_words), end - config.min_words + 1):
            if start not in best:
                continue
            group = tokens[start:end]
            if (len(" ".join(group)) > config.max_chars
                    or clocks[end] - clocks[start] < config.min_card_s
                    or not _balanced(group)):
                continue
            cost = best[start][0] + (len(group) - config.target_words) ** 2 + 1
            if end not in best or cost < best[end][0]:
                best[end] = (cost, [*best[start][1], [start, end]])
    ranges = best.get(n, (None, []))[1]
    if len(ranges) < 2:
        raise ValueError("no_supported_readable_partition")
    return ranges, [{"token": end, "time": clocks[end], "basis": allowed[end]}
                    for _start, end in ranges[:-1]]


def plan(segments: list[dict], *, source: dict, config: Config = Config(),
         phrase_guard: bool = False) -> dict:
    """`phrase_guard` is only for reviewer suggestions; shadow/enforce keep
    the validated display-phrases-v1 boundaries byte for byte."""
    if not (2 <= config.min_words <= config.target_words <= config.max_words <= 32
            and 16 <= config.max_chars <= 160
            and all(math.isfinite(v) and v > 0 for v in (
                config.min_card_s, config.pause_s, config.punctuation_pause_s))):
        raise ValueError("invalid_segmentation_config")
    proposal = {
        "schema": SCHEMA, "policy_version": POLICY_VERSION,
        "source": deepcopy(source), "source_hash": digest(segments),
        "config": asdict(config), "changes": [], "review": [],
    }
    for index, row in enumerate(segments):
        if not isinstance(row, dict):
            raise ValueError("invalid_segment")
        text = str(row.get("text") or "")
        if len(text.split()) <= config.max_words and len(text) <= config.max_chars:
            continue  # A held note is not a reason to split a short phrase.
        base = {"index": index, "parent_id": row.get("_id", row.get("segment_id")),
                "start": row.get("start"), "end": row.get("end")}
        try:
            if index in source.get("protected_quality_indices", []):
                raise ValueError("parent_quality_review_required")
            ranges, boundaries = _partition(row, config, phrase_guard)
            proposal["changes"].append({**base, "ranges": ranges, "boundaries": boundaries})
        except ValueError as exc:
            proposal["review"].append({**base, "reason": str(exc)})
    proposal["proposal_hash"] = digest(proposal)
    return proposal


def _children(parent, index, change, proposal):
    tokens, words = _parent(parent)
    ranges, boundaries = _partition(parent, Config(**proposal["config"]))
    if ranges != change.get("ranges") or boundaries != change.get("boundaries"):
        raise ValueError("proposal_boundaries_changed")
    parent_id = parent.get("_id", parent.get("segment_id"))
    children = []
    for part, (a, b) in enumerate(ranges):
        identity = digest([proposal["source_hash"], index, parent_id, a, b, POLICY_VERSION])[:24]
        child = {key: deepcopy(parent[key]) for key in _PRESENTATION if key in parent}
        child.update({
            "_id": "dsp_" + identity,
            "text": " ".join(tokens[a:b]),
            "start": parent["start"] if a == 0 else words[a]["start"],
            "end": parent["end"] if b == len(tokens) else words[b]["start"],
            "words": deepcopy(words[a:b]),
            "display_segmentation": {
                "policy_version": POLICY_VERSION, "parent_id": parent_id,
                "parent_index": index, "token_range": [a, b],
                "source_hash": proposal["source_hash"],
                "proposal_hash": proposal["proposal_hash"],
                "derived_word_clocks": True,
            },
        })
        if "segment_id" in parent:
            child["segment_id"] = child["_id"]
        children.append(child)
    return children


def apply(segments: list[dict], proposal: dict, *, source: dict) -> list[dict]:
    """All or nothing. Never reorder, extend bounds, drop text, or trust clients."""
    unsigned = {k: v for k, v in proposal.items() if k != "proposal_hash"}
    if (proposal.get("schema") != SCHEMA or proposal.get("policy_version") != POLICY_VERSION
            or digest(unsigned) != proposal.get("proposal_hash")
            or digest(segments) != proposal.get("source_hash")
            or source != proposal.get("source")):
        raise ValueError("segmentation_proposal_stale_or_invalid")
    # Recompute every candidate, including scope. A self-consistent hash alone
    # does not authorize arbitrary indices or token ranges.
    expected = plan(segments, source=source, config=Config(**proposal["config"]))
    if expected != proposal:
        raise ValueError("segmentation_proposal_invalid")
    changes = {c["index"]: c for c in proposal["changes"]}
    output = []
    for i, parent in enumerate(segments):
        output.extend(_children(parent, i, changes[i], proposal) if i in changes else [deepcopy(parent)])
    for key in ("_id", "segment_id", "id"):
        ids = [str(row[key]) for row in output if row.get(key) is not None]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_segment_ids")
    previous_start = -1.0
    previous_max_end = -1.0
    for i, parent in enumerate(segments):
        start, end = _number(parent.get("start")), _number(parent.get("end"))
        if start < 0 or end <= start or start < previous_start:
            raise ValueError("invalid_source_timeline")
        previous_start = start
        if i in changes:
            # Simultaneous choruses need human interpretation; don't create
            # consecutive cards that compete with any still-active earlier
            # layer. The immediate predecessor can finish inside a held voice.
            if previous_max_end > start or (i + 1 < len(segments) and end > segments[i + 1]["start"]):
                raise ValueError("overlapping_vocal_layers")
        previous_max_end = max(previous_max_end, end)
    return output


# ── Reviewer suggestions (DISPLAY_SEGMENTATION_MODE=suggest) ─────────────────
# Read-only. The editor shows each item as "Cortar acá" / "Unir con la
# siguiente"; only the reviewer's click changes the document, through the
# same editor split/merge that keeps per-word clocks. Nothing here mutates.
SUGGESTION_SCHEMA = "display-segmentation-suggestions-v1"
MERGE_MAX_GAP_S = 1.5
_CLOSING_PUNCTUATION = re.compile(r"[,;:.!?…][\)\]\"’”]*$")


def _row_identity(row: dict):
    value = row.get("segment_id", row.get("_id"))
    return None if value is None else str(value)


def _merge_suggestions(segments, config, skip: set[int]) -> list[dict]:
    """A line that strands an open-ended word ("…feliz quem" / "Sabe", or an
    orphan "En") is joined with the next one when both fit one card."""
    out = []
    for i in range(len(segments) - 1):
        a, b = segments[i], segments[i + 1]
        if i in skip or i + 1 in skip or not isinstance(a, dict) or not isinstance(b, dict):
            continue
        if _protected(a) or _protected(b):
            continue
        ta, tb = str(a.get("text") or "").split(), str(b.get("text") or "").split()
        if not ta or not tb or _CLOSING_PUNCTUATION.search(ta[-1]) or _token(ta[-1]) not in _OPEN_ENDED:
            continue
        try:
            a_start, a_end = _number(a.get("start")), _number(a.get("end"))
            b_start, b_end = _number(b.get("start")), _number(b.get("end"))
        except ValueError:
            continue
        # Simultaneous voices or a nested echo are not one phrase.
        if b_start < a_start or b_end <= a_end or b_start - a_end > MERGE_MAX_GAP_S:
            continue
        merged = ta + tb
        if len(merged) > config.max_words or len(" ".join(merged)) > config.max_chars:
            continue
        out.append({
            "kind": "merge", "index": i,
            "segment_id": _row_identity(a), "next_segment_id": _row_identity(b),
            "text": a["text"], "next_text": b["text"], "merged_text": " ".join(merged),
            "start": a_start, "end": b_end, "reason": "open_ended_line",
        })
    return out


def suggestions(segments, *, protected_indices=(), config: Config = Config()) -> list[dict]:
    """Split/merge proposals for the reviewer, computed on the CURRENT lines.

    Splits are the planner's partitions (same word-clock contract as
    shadow/enforce) with an extra phrase guard: never cut after a word that
    cannot close a phrase, nor before a lowercase preposition/article or
    infinitive that continues it. Merges are conservative phrase-boundary hints: a line
    ending in an open-ended word, sung within MERGE_MAX_GAP_S of the next line,
    whose union still fits one card.
    Every item carries the exact text it was computed on so the client can
    refuse to apply it to a line that changed since.
    """
    if not isinstance(segments, list) or not segments:
        return []
    try:
        proposal = plan(segments, source={"protected_quality_indices": sorted(set(protected_indices))},
                        config=config, phrase_guard=True)
    except (ValueError, TypeError, KeyError):
        return []
    items = []
    split_indices = set()
    for change in proposal["changes"]:
        row = segments[change["index"]]
        tokens = row["text"].split()
        split_indices.add(change["index"])
        items.append({
            "kind": "split", "index": change["index"], "segment_id": _row_identity(row),
            "text": row["text"], "start": row["start"], "end": row["end"],
            "cuts": [boundary["token"] for boundary in change["boundaries"]],
            "basis": [boundary["basis"] for boundary in change["boundaries"]],
            "parts": [{"text": " ".join(tokens[a:b]), "token_range": [a, b]}
                      for a, b in change["ranges"]],
        })
    try:
        items.extend(_merge_suggestions(segments, config, split_indices))
    except (ValueError, TypeError, KeyError):
        pass
    for item in items:
        # Identity = line + content, not position: an edit above must not turn
        # a suggestion the reviewer dismissed into a "new" one.
        item["id"] = digest([SUGGESTION_SCHEMA, item["kind"], item["segment_id"],
                             item["text"], item.get("next_text"), item.get("cuts")])[:16]
    return sorted(items, key=lambda item: (item["index"], item["kind"]))


def run(segments, *, source, fresh: bool, authorization: dict) -> tuple[list, dict | None, dict | None]:
    """One finalizer stage; application requires an independently signed grant."""
    current_mode = mode()
    if current_mode == "off":
        return segments, None, None
    proposal = plan(segments, source=source)
    report = {k: deepcopy(proposal[k]) for k in (
        "schema", "policy_version", "source", "source_hash", "proposal_hash", "config", "review",
    )}
    # `suggest` is shadow for the pipeline: the proposal is only surfaced to
    # the reviewer by the editor API, which recomputes it on current lines.
    report.update(mode=current_mode, status="suggest" if current_mode == "suggest" else "shadow",
                  before=len(segments),
                  after=len(segments), proposed_count=len(proposal["changes"]), applied_count=0,
                  authorization_status=authorization.get("status", "missing"),
                  candidates=[{k: deepcopy(c[k]) for k in ("index", "parent_id", "start", "end")}
                              for c in proposal["changes"]])
    if not fresh:
        report["status"] = "ineligible_existing_work"
        return segments, report, proposal
    if current_mode != "enforce" or not authorization.get("authorized"):
        if current_mode == "enforce":
            report["status"] = "authorization_required"
        return segments, report, proposal
    if not proposal["changes"]:
        report["status"] = "review_required" if proposal["review"] else "not_needed"
        return segments, report, proposal
    try:
        output = apply(segments, proposal, source=source)
    except (ValueError, TypeError, KeyError) as exc:
        report.update(status="contract_rejected", failure_code=str(exc), applied_count=0)
        return segments, report, proposal
    report.update(status="applied", applied_count=len(proposal["changes"]), after=len(output),
                  contract_result_hash=digest(output), authorization_sha256=authorization.get("sha256"))
    return output, report, proposal


def runtime_config_fingerprint() -> str:
    """API/ALL worker parity, including the pinned grant (no self-reference).

    The grant is excluded from the configuration it signs, but all services
    must still pin the same grant/trust roots before accepting new work.
    Paths may legitimately differ between containers, so compare identities.
    """
    public_keys = os.environ.get("DISPLAY_SEGMENTATION_PUBLIC_KEYS", "")
    try:
        public_keys = json.loads(public_keys) if public_keys else {}
    except ValueError:
        public_keys = {"invalid": True}
    return digest({
        "policy_version": POLICY_VERSION, "mode": mode(), "config": asdict(Config()),
        "authorization_sha256": os.environ.get("DISPLAY_SEGMENTATION_AUTHORIZATION_SHA256", "").lower()
        if mode() == "enforce" else "",
        "public_keys": public_keys if mode() == "enforce" else {},
        "parent_evidence_thresholds": {
            key: os.environ.get(key, "") for key in (
                "QUALITY_LINE_ASR_SCORE_MIN", "QUALITY_LINE_CTC_SCORE_MIN",
            )
        },
    })
