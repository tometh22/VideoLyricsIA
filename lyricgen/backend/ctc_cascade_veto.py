"""CTC cascade veto — protects high-confidence whisperX segments from Gemini overwrite.

Standalone module so it can be imported without pulling in main.py's FastAPI stack.
"""
from __future__ import annotations

import logging
import os
import re as _re

logger = logging.getLogger("genly.ctc_cascade_veto")


def _ctc_cascade_veto(
    retimed_segs: list,
    cascade_segs: list,
    *,
    ws_floor: float | None = None,
    max_midpoint_dist: float = 20.0,
) -> list:
    """Protect high-confidence cascade segments from being overwritten by the
    Gemini performance-libretto — but ONLY when the texts are completely
    different (not when Gemini is adding a prefix, fixing punctuation, etc.).

    Veto fires when ALL four conditions hold:
    1. Nearest cascade segment midpoint is within max_midpoint_dist seconds
       (default 20s) of the retimed segment's midpoint. Midpoint distance is
       robust to whisperX timestamp non-determinism: whisperX may place
       "frágil espejo" at 110s in one run and 126s in another, but Gemini
       always retimes the wrong text to ~127s — midpoint distance ≤20s in
       both cases, so the veto fires consistently.
    2. That cascade segment's mean word-score ≥ ws_floor (default 0.70)
    3. Texts differ
    4. containment < 0.40 — fewer than 40% of cascade's (≥3) unique words
       appear in Gemini's text. This distinguishes a true hallucination
       ("tanto miedo tu don" vs "frágil espejo de voz", containment=0) from
       a correction ("Dentro de tu piel…" vs "tu piel…", containment=1.0).

    Rationale: whisperX forced-alignment is phoneme-level; ws ≥ 0.70 means
    the audio genuinely matches the cascade words. Gemini can hallucinate at
    repeated melodic phrases because it lacks the per-phoneme alignment signal,
    but it's also the canonical text-cleaner — the containment guard lets
    small corrections through.
    """
    if ws_floor is None:
        try:
            ws_floor = float(
                os.environ.get("CTC_CASCADE_VETO_WS_FLOOR", "0.70")
            )
        except (TypeError, ValueError):
            ws_floor = 0.70

    def _mean_ws(seg: dict) -> float | None:
        words = seg.get("words") or []
        scores = [float(w["score"]) for w in words if w.get("score") is not None]
        return sum(scores) / len(scores) if scores else None

    def _tokens(text: str) -> set:
        return set(_re.sub(r"[^a-z0-9áéíóúüñ\s]", " ", text.lower()).split())

    # Each cascade segment can only be "claimed" by the retimed segment whose
    # midpoint is closest to it. Once claimed, it won't match any other retimed
    # segment. This prevents a cascade "Tomas del miedo" from vetoing a later
    # CTC "Frágil espejo" when whisperX's fresh run placed "Frágil espejo"
    # nowhere near 1:50 (leaving "Tomas del miedo" as the only nearby cascade).
    claimed: set = set()

    out = []
    for seg in retimed_segs:
        t0 = float(seg.get("start", 0))
        t1 = float(seg.get("end", 0))
        dur = t1 - t0
        retimed_text = (seg.get("text") or "").strip()

        # Find nearest unclaimed cascade segment by midpoint distance within window.
        # Midpoint matching is robust to whisperX timestamp variance: even if
        # whisperX places "frágil espejo" at 110s in one run and 126s in
        # another, Gemini retimes the wrong text to ~127s both times, so
        # midpoint distance stays ≤20s regardless of the run.
        r_mid = (t0 + t1) / 2
        best_dist, best_ws, best_text, best_ci = float("inf"), 0.0, None, -1
        for ci, cs in enumerate(cascade_segs):
            if ci in claimed:
                continue
            cs0 = float(cs.get("start", 0))
            cs1 = float(cs.get("end", 0))
            c_mid = (cs0 + cs1) / 2
            dist = abs(r_mid - c_mid)
            if dist > max_midpoint_dist:
                continue
            ws = _mean_ws(cs)
            if ws is not None and ws >= ws_floor and dist < best_dist:
                best_dist, best_ws, best_text, best_ci = (
                    dist, ws, (cs.get("text") or "").strip(), ci
                )

        # Claim this cascade segment regardless of whether the veto fires —
        # it won't be available to match a different retimed segment.
        if best_ci >= 0:
            claimed.add(best_ci)

        if not (best_text and dur > 0 and best_text != retimed_text):
            out.append(seg)
            continue

        # Containment guard: if most cascade words appear in Gemini text,
        # Gemini is likely correcting/extending — let it through.
        cascade_tok = _tokens(best_text)
        if len(cascade_tok) < 3:
            out.append(seg)
            continue
        containment = len(cascade_tok & _tokens(retimed_text)) / len(cascade_tok)
        if containment >= 0.40:
            out.append(seg)
            continue

        logger.info(
            "[CTC-VETO] %.1f–%.1fs cascade ws=%.2f dist=%.1fs preserves %r "
            "(gemini had: %r, containment=%.2f)",
            t0, t1, best_ws, best_dist, best_text[:50], retimed_text[:50], containment,
        )
        out.append({**seg, "text": best_text})
    return out
