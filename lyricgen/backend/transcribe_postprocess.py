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
