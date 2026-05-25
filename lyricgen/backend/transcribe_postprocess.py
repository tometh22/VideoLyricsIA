"""Post-processing helpers for the transcription pipeline output.

`normalize_words` is the contract that protects Bug B (line 3741 of
main.py used to strip `words` UNCONDITIONALLY before persisting, even
when the words carried the per-word `score` from forced alignment that
PR-D needs for karaoke + confidence highlighting). The fix is
conditional: preserve when score is present (FA aligner output),
strip when not (Whisper-1 raw — redundant with line-level timing and
~30% JSON bloat for nothing).
"""
from __future__ import annotations


def normalize_words(segments: list) -> list:
    """Apply the per-word stripping policy on a list of segment dicts.

    Rules:
    - A segment without a `words` key passes through unchanged.
    - A segment whose `words` list contains AT LEAST one entry with a
      non-None `score` field is treated as forced-aligner output — keep
      the `words` intact (each carries start/end + score; karaoke +
      confidence-highlight read them).
    - A segment whose `words` list has no `score` anywhere is treated as
      Whisper-1 raw output — strip the `words` key. The line-level
      start/end already covers everything the editor needs from that
      path, and the per-word stamps add tens of KB per song to the
      segments_json with no visible win.

    Bug B incident (2026-05-24): the previous unconditional strip in
    `main.py:3741` killed FA wordstamps when the flow happened to land
    on the tail return path — anyone who saw Karaoke working in QA
    didn't notice because that path wasn't exercised. The QA full run
    finally hit it on Cosas Mías auto-recovery and we lost karaoke +
    confidence on jobs that should have had both.

    Pure function. Doesn't mutate the input. Unit-testable without
    pytest fixtures, replicate mocks, or an event loop.
    """
    out = []
    for s in segments:
        words = s.get("words")
        if not words:
            out.append(s)
            continue
        if any(w.get("score") is not None for w in words):
            out.append(s)
        else:
            out.append({k: v for k, v in s.items() if k != "words"})
    return out


def dedup_collisions(segments: list, *, epsilon_s: float = 0.10) -> list:
    """Merge near-identical duplicates that forced_align/reconcile sometimes
    emits when the lyrics text has repeated chorus lines ("Legalícenla /
    Legalícenla / Oh-oh-oh"). Two segments collide when they share (a) the
    same start within `epsilon_s` and (b) the same case-insensitive text.
    The first occurrence wins; its end is extended to the max of the group;
    the rest are dropped.

    INCIDENT 2026-05-25: forced_align on songs whose lrclib lyrics include
    repeated chorus lines occasionally assigned all the repetitions to a
    single time bucket (~50-100 ms apart). The frontend was patched to show
    these as Gantt-style overlap lanes, but the root cause is the aligner
    emitting collisions in the first place. We dedup at the chokepoint so
    every emit path benefits (FA, whisperX_reconciled, whisper_recover…).

    DELIBERATELY does NOT merge segments that share `start` but differ in
    `text` — those are legitimate co-occurring lines (chorus harmony, two
    voices singing different words at once). The frontend handles those
    correctly via stack-rendering.

    Pure function. Doesn't mutate inputs. Returns a new list of new dicts.
    """
    if not segments or len(segments) < 2:
        return list(segments) if segments else []
    out: list = []
    for s in sorted(segments, key=lambda x: float(x.get("start") or 0)):
        if not out:
            out.append(dict(s))
            continue
        prev = out[-1]
        same_text = (
            (s.get("text") or "").strip().lower()
            == (prev.get("text") or "").strip().lower()
        )
        close_start = abs(
            float(s.get("start") or 0) - float(prev.get("start") or 0)
        ) < epsilon_s
        if same_text and close_start:
            prev["end"] = max(
                float(prev.get("end") or 0),
                float(s.get("end") or 0),
            )
            continue
        out.append(dict(s))
    return out
