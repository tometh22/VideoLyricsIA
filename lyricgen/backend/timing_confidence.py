"""Timing-quality gates for the karaoke alignment pipeline.

WHY
---
Some timing candidates are confidently wrong: an lrclib *synced* version whose
timestamps belong to a DIFFERENT edit of the song (longer album cut, a live
re-arrangement, a remix) lands lines well past the end of THIS audio. The
"Cosas Mías" incident (synced 35 s off) and the cumbia "Luz de Día" lab case
(synced runs to 248 s on a 169 s audio → −79 s of overshoot) are exactly this:
the text/line-order is right, but the timeline is foreign.

`span_gate` is a pure, cheap arithmetic guard that rejects such candidates by
comparing their last timestamp against the audio duration — BEFORE they can be
emitted as if they were aligned to this recording. It is the duration-sanity
primitive the anchor/scaffold path (next stage) leans on; it is deliberately
conservative (only flags egregious OVERSHOOT) because a song can legitimately
end with a long instrumental outro, so UNDER-coverage cannot be judged from
duration alone — that needs the vocal envelope (a later stage).

CONTRACT
--------
- Pure: no I/O, no model calls, no globals. None-safe on audio_dur.
- `span_gate(segments, audio_dur) -> SpanVerdict(ok, reason, last_end)`.
  `ok=False` means "do not trust this candidate's timeline" — the caller keeps
  its other fallbacks. NEVER deletes anything itself.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpanVerdict:
    ok: bool
    reason: str
    last_end: float


def _last_end(segments) -> float:
    """Largest end (or start) over the segments; 0.0 when empty/unusable."""
    best = 0.0
    for s in segments or []:
        try:
            v = s.get("end")
            if v is None:
                v = s.get("start")
            if v is not None:
                best = max(best, float(v))
        except (AttributeError, TypeError, ValueError):
            continue
    return best


def span_gate(
    segments,
    audio_dur,
    *,
    overshoot_ratio: float = 1.15,
    overshoot_pad: float = 5.0,
) -> SpanVerdict:
    """Reject a timing candidate whose last line ends well past the audio.

    A candidate is REJECTED (`ok=False`) when its last timestamp exceeds
    `audio_dur * overshoot_ratio + overshoot_pad` — i.e. the timeline clearly
    belongs to a longer/different version than the uploaded recording. The
    ratio+pad tolerates normal trailing slack (a held final word, a couple of
    seconds of reverb) without flagging legitimate results.

    Conservative by design:
      - Empty candidate → not ok (nothing to trust).
      - Missing/zero `audio_dur` → ok=True with reason "no_duration" (we cannot
        judge, so we do not block — the caller's other guards still apply).
      - UNDER-coverage is intentionally NOT judged here: a real instrumental
        outro legitimately leaves the last lyric far from the audio end, and
        only the vocal envelope can tell that apart from a too-short synced
        version. That check lives in the vocal-envelope stage.
    """
    if not segments:
        return SpanVerdict(False, "empty", 0.0)
    last = _last_end(segments)
    if not audio_dur or audio_dur <= 0:
        return SpanVerdict(True, "no_duration", last)
    limit = audio_dur * overshoot_ratio + overshoot_pad
    if last > limit:
        return SpanVerdict(
            False,
            f"overshoot last_end={last:.1f}s > limit={limit:.1f}s "
            f"(audio={audio_dur:.1f}s) — foreign/longer version",
            last,
        )
    return SpanVerdict(True, "ok", last)
