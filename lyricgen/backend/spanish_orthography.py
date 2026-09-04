"""Conservative Spanish orthography checks that do not need song lyrics.

The checker is deliberately narrower than a general spell checker: it emits
review suggestions only for deterministic accent forms and near-exact tokens
from a small, audited Spanish lexicon.  It never mutates transcription output.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Mapping, Sequence


POLICY_VERSION = "spanish-orthography-observe-v1"
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Unaccented spellings whose accented form is not a grammatical homograph.
# Ambiguous pairs such as si/sí, el/él, tu/tú, aun/aún and solo/sólo are
# intentionally absent: audio or sentence-level evidence must decide those.
_FIXED_ACCENTS = {
    "ademas": "además", "adios": "adiós", "ahi": "ahí",
    "alla": "allá", "alli": "allí", "algun": "algún",
    "angel": "ángel", "angeles": "ángeles", "arbol": "árbol",
    "cancion": "canción", "carcel": "cárcel", "corazon": "corazón",
    "debil": "débil", "despues": "después", "dificil": "difícil",
    "facil": "fácil", "jamas": "jamás", "lagrima": "lágrima",
    "lagrimas": "lágrimas", "magico": "mágico", "magica": "mágica",
    "miercoles": "miércoles", "musica": "música", "ningun": "ningún",
    "numero": "número", "ojala": "ojalá", "pagina": "página",
    "perdon": "perdón", "quiza": "quizá", "quizas": "quizás",
    "razon": "razón", "sabado": "sábado", "segun": "según",
    "tambien": "también", "telefono": "teléfono", "ultimo": "último",
    "ultima": "última", "unico": "único", "unica": "única",
}

# Dictionary targets for a one-edit/transposition typo.  The first regression
# is the exact UMG AVENTRUA -> AVENTURA finding.  Expansion is evidence-driven:
# every new label QC report adds a tested target rather than granting a fuzzy
# dictionary blanket authority over artist names or slang.
_TYPO_DICTIONARY = {"aventura"}

_COMMON_INFINITIVES = {
    "amar", "bailar", "buscar", "cambiar", "cantar", "caminar", "dejar",
    "encontrar", "esperar", "estar", "ganar", "hablar", "llegar", "llorar",
    "mirar", "olvidar", "pasar", "pensar", "quedar", "regresar", "soñar",
    "tocar", "tomar", "trabajar", "volar", "volver", "beber", "comer",
    "conocer", "creer", "deber", "entender", "hacer", "poder", "poner",
    "querer", "saber", "ser", "tener", "valer", "venir", "ver", "vivir",
    "decir", "dormir", "escribir", "ir", "morir", "partir", "salir",
    "sentir", "seguir",
}
_IRREGULAR_FUTURE_STEMS = {
    "cabr", "dir", "habr", "har", "podr", "pondr", "querr", "sabr",
    "saldr", "tendr", "valdr", "vendr",
}


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _copy_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper() and source[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _damerau_one(left: str, right: str) -> bool:
    if left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        diffs = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        if len(diffs) == 1:
            return True
        return (
            len(diffs) == 2 and diffs[1] == diffs[0] + 1
            and left[diffs[0]] == right[diffs[1]]
            and left[diffs[1]] == right[diffs[0]]
        )
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1:] == shorter:
            return True
    return False


def _future_suggestion(folded: str) -> tuple[str, str] | None:
    # Requested deterministic surface checks: future second/third person
    # endings -ás/-á (and plural -án).  Regular -aras/-iera forms can also be
    # valid subjunctives, so they remain medium-confidence review candidates.
    endings = (("as", "ás"), ("an", "án"), ("a", "á"))
    for plain, accented in endings:
        if not folded.endswith(plain) or len(folded) <= len(plain) + 2:
            continue
        stem = folded[:-len(plain)]
        if stem in _IRREGULAR_FUTURE_STEMS:
            return stem + accented, "high"
        if stem in _COMMON_INFINITIVES:
            return stem + accented, "medium"
    return None


def _token_suggestion(token: str) -> tuple[str, str, str] | None:
    folded = _fold(token)
    fixed = _FIXED_ACCENTS.get(folded)
    if fixed and token != _copy_case(token, fixed):
        return _copy_case(token, fixed), "fixed_accent_dictionary", "high"
    future = _future_suggestion(folded)
    if future:
        replacement, confidence = future
        candidate = _copy_case(token, replacement)
        if token != candidate:
            return candidate, "future_accent_rule", confidence
    if len(folded) >= 6:
        for expected in _TYPO_DICTIONARY:
            if folded[:1] == expected[:1] and _damerau_one(folded, expected):
                return _copy_case(token, expected), "spanish_dictionary_near_match", "high"
    return None


def analyze_spanish_orthography(
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return per-token findings and one-click, per-line review candidates."""
    findings: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for segment_index, raw in enumerate(segments):
        segment = dict(raw)
        text = str(segment.get("text") or segment.get("t") or "")
        replacements: list[tuple[int, int, str, str, str, str]] = []
        for token_index, match in enumerate(_WORD_RE.finditer(text)):
            actual = match.group(0)
            suggestion = _token_suggestion(actual)
            if not suggestion:
                continue
            expected, detector, confidence = suggestion
            replacements.append((match.start(), match.end(), actual, expected, detector, confidence))
            findings.append({
                "segment_index": segment_index,
                "token_index": token_index,
                "start": float(segment.get("start", segment.get("s", 0)) or 0),
                "actual": actual,
                "expected": expected,
                "detector": detector,
                "confidence": confidence,
                "automatic_apply_allowed": False,
            })
        if not replacements:
            continue
        proposed_text = text
        for start, end, _actual, expected, _detector, _confidence in reversed(replacements):
            proposed_text = proposed_text[:start] + expected + proposed_text[end:]
        proposed = dict(segment)
        proposed["text" if "text" in segment or "t" not in segment else "t"] = proposed_text
        start = float(segment.get("start", segment.get("s", 0)) or 0)
        end = float(segment.get("end", segment.get("e", start)) or start)
        if end <= start:
            end = start + 0.001
        digest = hashlib.sha256(
            f"{segment_index}|{text}|{proposed_text}".encode("utf-8")
        ).hexdigest()[:16]
        confidences = {item[5] for item in replacements}
        candidates.append({
            "kind": "operator_review_candidate",
            "id": f"spanish-orthography-{digest}",
            "start": start,
            "end": end,
            "reasons": sorted({item[4] for item in replacements}),
            "current_segments": [segment],
            "proposed_segments": [proposed],
            "suggestion_type": "text",
            "confidence": "medium" if "medium" in confidences else "high",
            "impact_ms": max(1, int((end - start) * 1000)),
            "selector_policy": POLICY_VERSION,
            "source_families": ["deterministic_spanish_orthography"],
            "automatic_apply_allowed": False,
        })
    return {
        "schema_version": POLICY_VERSION,
        "mode": "observe",
        "reference_required": False,
        "automatic_apply_allowed": False,
        "finding_count": len(findings),
        "candidate_count": len(candidates),
        "findings": findings,
        "candidates": candidates,
    }
