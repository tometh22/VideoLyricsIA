"""Genly CTC timing engine — full-song monotonic forced alignment.

Re-times the FINAL lyric lines of a job by aligning their text onto the
isolated vocal stem with a single CTC Viterbi pass over the whole song.
Text is never changed — only start/end (line + per-word karaoke stamps).

Why this engine (benchmark vs Rotor ground truth, 2026-06-09, 4 songs:
estudio repetitivo / cumbia / 2 vivos):
  - whisper-1 raw word-stamps: drift up to +32 s (DTW interpolation).
  - reconcile vs reference: breaks on repeated choruses.
  - chunked Replicate aligner: ~10 min/song; binds repeated lines to the
    wrong occurrence (each window aligns blind to global order).
  - THIS ENGINE: median onset offset vs Rotor 0.04-0.10 s on all four
    songs, ~15-25 s/song on CPU. Repeated choruses cannot cross-bind:
    one Viterbi over the full emission matrix is monotonic by
    construction — the 2nd chorus can only land after the 1st.

Two design pieces worth naming:
  - Chunked emissions, global Viterbi: wav2vec2 attention is O(T^2), so
    the encoder sees 30 s windows (±4 s acoustic context, trimmed after),
    but the Viterbi pass — where monotonicity matters — sees the whole
    song. Frame t always covers samples [t*320, (t+1)*320) of the full
    waveform, so global frame indices stay exact across chunk seams.
  - Synthetic star class: an extra emission column equal to
    max(non-blank log-prob) - CTC_ALIGN_STAR_DELTA per frame, inserted
    as a pseudo-token between lines. It absorbs sung audio that has no
    transcript line (instrumental-solo backing vocals, crowd noise,
    ad-libs) while losing ties against any real token that fits.
    Without it, lines stretch across instrumental solos (measured:
    4 lines off by 17-30 s on the 51 s solo of Me Gustas; with it,
    max error on that song drops to 2.1 s — a Rotor display choice).

Model: jonatasgrosman/wav2vec2-large-xlsr-53-spanish (Apache-2.0 — safe
for commercial use, verified 2026-06-09; the torchaudio MMS_FA bundle is
CC-BY-NC and must NOT ship in prod). On the benchmark the Spanish XLSR
beats MMS_FA on 3 of 4 songs (it resolved the cumbia compressed-cluster
failure and the live ad-lib bindings).

Ops: gated by CTC_ALIGN_ENABLED (default OFF). torch/torchaudio are
imported lazily — a container without them declines cleanly. First call
downloads ~1.2 GB of weights to the HF cache (survives worker recycles;
re-downloaded per deploy). Inference is CPU, single job at a time
(workers process jobs serially), peak RSS ~2.5 GB.

Known residual (documented, not silent): crowd-sung chorus sections in
live shows (demucs erases the crowd from the stem) still misplace 3-9
lines by 3-9 s on the worst live benchmark song; lexical-line median
there is still 0.09 s. The gap-transplant R&D branch targets exactly
that region.
"""
from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import Optional

logger = logging.getLogger("uvicorn.error")

MODEL_ID = os.environ.get(
    "CTC_ALIGN_MODEL", "jonatasgrosman/wav2vec2-large-xlsr-53-spanish")
SR = 16000
FRAME = 320          # wav2vec2 stride: 1 emission frame per 320 samples (20 ms)
CHUNK_S = 30.0       # encoder window
CTX_S = 4.0          # acoustic context per side, trimmed from emissions
_TRUE = {"1", "true", "yes", "on"}

_MODEL = None        # lazy singleton: (forward_fn, dictionary, blank_id)


def is_enabled() -> bool:
    return os.environ.get("CTC_ALIGN_ENABLED", "0").strip().lower() in _TRUE


def _star_delta() -> float:
    try:
        return float(os.environ.get("CTC_ALIGN_STAR_DELTA", "0.5"))
    except ValueError:
        return 0.5


def _max_audio_s() -> float:
    try:
        return float(os.environ.get("CTC_ALIGN_MAX_AUDIO_S", "900"))
    except ValueError:
        return 900.0


def norm_word(w: str) -> str:
    """Lowercase + keep only chars the Spanish CTC vocab can emit.
    Accented vowels / ñ / ü are IN the vocab — don't strip them."""
    w = unicodedata.normalize("NFC", w.lower())
    return re.sub(r"[^a-záéíóúñü']", "", w)


def build_targets(lines: list[str], dictionary: dict, star_id: int,
                  word_sep_id: int | None = None):
    """Flat token sequence for the whole lyric + per-word bookkeeping.

    Returns (targets, words) where words is a list of
    (line_idx, raw_word, n_tokens); line_idx == -1 marks a star filler.
    Words with no vocab-representable chars are skipped (the line is
    later interpolated if ALL its words drop). Pure — unit-testable.

    A star goes BETWEEN lines and also BEFORE the first / AFTER the last:
    without the edge stars, a spoken intro (live dedications, stage talk —
    it IS voice in the stem) can only bind to line 1's tokens, dragging it
    seconds early; same for outros on the last line (measured on
    Costumbres Argentinas live: line 1 stretched 3.1→36.6s, last line
    stretched 36s over the outro).

    `word_sep_id`: the CTC word-delimiter class ('|' in the XLSR vocab),
    inserted between words WITHIN a line. The model was trained emitting
    it at word boundaries; giving the Viterbi that anchor measurably
    tightens tails (nada_fue p95 26.5→9.2s in the corruption harness).
    The separator token is appended to the PRECEDING word's token count
    so span bookkeeping stays 1:1 with targets."""
    words: list[tuple[int, str, int]] = []
    targets: list[int] = []
    words.append((-1, "*", 1))
    targets.append(star_id)
    for li, line in enumerate(lines):
        line_words = []
        for raw in line.split():
            ids = [dictionary[c] for c in norm_word(raw) if c in dictionary]
            if ids:
                line_words.append((raw, ids))
        for wi, (raw, ids) in enumerate(line_words):
            if word_sep_id is not None and wi < len(line_words) - 1:
                ids = ids + [word_sep_id]
            words.append((li, raw, len(ids)))
            targets.extend(ids)
        words.append((-1, "*", 1))
        targets.append(star_id)
    return targets, words


def spans_to_lines(spans, words, n_lines: int, frame_to_s: float):
    """Group per-token spans back into word + line timings.

    `spans` is the merge_tokens output as plain (start_frame, end_frame,
    score) tuples, one per target token in order. Returns
    [(start, end, [(word, w_start, w_end, w_score)]) or None] per line —
    None where the line had no alignable words. Pure."""
    out: list = [None] * n_lines
    i = 0
    for li, raw, n_tok in words:
        chunk = spans[i:i + n_tok]
        i += n_tok
        if li < 0 or not chunk:
            continue
        ws = chunk[0][0] * frame_to_s
        we = chunk[-1][1] * frame_to_s
        score = sum(c[2] for c in chunk) / len(chunk)
        if out[li] is None:
            out[li] = [ws, we, []]
        out[li][1] = we
        out[li][2].append((raw, ws, we, score))
    return [tuple(o) if o else None for o in out]


BRIDGE_S = 8.0  # no word lasts 8s; no intra-line silence lasts 8s


def _eff_dur(word: str) -> float:
    """Plausible sung duration of a word: ~0.45s per alignable char +
    slack for held notes. 'anzuelo' → ~3.7s, never 27s."""
    return 0.45 * max(len(norm_word(word)), 2) + 0.6


def repair_bridge_words(line_times, regions=None):
    """Fix 'bridge' lines: a word stretched implausibly long (or a huge
    intra-line gap) is the CTC bridging two distinct vocal events — e.g.
    a live intro where the singer talks (binds the first words) and the
    real verse 30s later (binds the rest), with one word spanning the
    instrumental in between. Measured on Costumbres live: 'anzuelo'
    8.3→35.1s, 'decir' 157→180s over no-voice audio.

    For each line whose words contain a bridge (duration or gap >
    BRIDGE_S): split the words into clusters at the bridges (the bridge
    word joins the side it's temporally closer to, trimmed to its
    plausible duration), keep the cluster with the most words (tie:
    higher mean score), and re-derive the line's start/end from it.
    Dropped words lose their (wrong) stamps. `regions` (vocal VAD spans)
    break score ties toward clusters that overlap voice. Pure."""
    def in_voice(a, b):
        if not regions:
            return True
        return any(ra - 0.3 <= a <= rb + 0.3 or ra - 0.3 <= b <= rb + 0.3
                   or (a <= ra and b >= rb) for ra, rb in regions)

    out = []
    for lt in line_times:
        if lt is None or not lt[2]:
            out.append(lt)
            continue
        s, e, ws = lt
        # break BEFORE word i when the gap from word i-1 exceeds BRIDGE_S
        clusters, cur = [], [0]
        for i in range(1, len(ws)):
            gap = ws[i][1] - ws[i - 1][2]
            if gap > BRIDGE_S:
                clusters.append(cur)
                cur = []
            cur.append(i)
        clusters.append(cur)
        # a bridge WORD splits its own line: assign it to the nearer side
        # with its duration trimmed to plausible
        rebuilt = []
        for cl in clusters:
            parts = []
            for i in cl:
                w, a, b, sc = ws[i]
                if b - a > BRIDGE_S:
                    gap_l = a - ws[i - 1][2] if i > 0 else float("inf")
                    gap_r = ws[i + 1][1] - b if i + 1 < len(ws) else float("inf")
                    if gap_r <= gap_l:
                        a = max(a, b - _eff_dur(w))   # word belongs at its end
                    else:
                        b = min(b, a + _eff_dur(w))   # word belongs at its start
                parts.append((w, a, b, sc))
            # the trim may itself have created a >BRIDGE_S hole — re-split
            sub, cur2 = [], [parts[0]]
            for p in parts[1:]:
                if p[1] - cur2[-1][2] > BRIDGE_S:
                    sub.append(cur2)
                    cur2 = []
                cur2.append(p)
            sub.append(cur2)
            rebuilt.extend(sub)
        if len(rebuilt) == 1:
            cl = rebuilt[0]
            out.append((cl[0][1], cl[-1][2], cl))
            continue
        def rank(cl):
            mean_sc = sum(p[3] for p in cl) / len(cl)
            return (len(cl), in_voice(cl[0][1], cl[-1][2]), mean_sc)
        best = max(rebuilt, key=rank)
        out.append((best[0][1], best[-1][2], best))
    # repairs must not break global monotonicity
    prev_end = 0.0
    fixed = []
    for lt in out:
        if lt is None:
            fixed.append(lt)
            continue
        s, e, ws = lt
        s = max(s, prev_end - 0.2)
        e = max(e, s + 0.2)
        fixed.append((s, e, ws))
        prev_end = e
    return fixed


def looks_collapsed(line_times, min_dur_s: float = 0.15,
                    max_collapsed_frac: float = 0.25) -> bool:
    """Structural failure guard: when alignment fails (wrong language,
    instrumental track, garbage text) the Viterbi crams many lines into
    near-zero-duration spans. >25% collapsed lines → decline. Pure."""
    timed = [t for t in line_times if t is not None]
    if not timed:
        return True
    collapsed = sum(1 for s, e, _ in timed if (e - s) < min_dur_s)
    return collapsed / len(timed) > max_collapsed_frac


def _load_model():
    """Lazy singleton. Raises if torch/transformers are unavailable —
    callers catch and decline."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    import torch
    from transformers import AutoModelForCTC, Wav2Vec2CTCTokenizer

    t0 = time.time()
    # NOT AutoProcessor: the model repo ships an optional LM decoder that
    # drags in pyctcdecode; we only need raw emissions + the char vocab.
    tok = Wav2Vec2CTCTokenizer.from_pretrained(MODEL_ID)
    model = AutoModelForCTC.from_pretrained(MODEL_ID).eval()
    dictionary = {k.lower(): v for k, v in tok.get_vocab().items() if len(k) == 1}
    blank_id = tok.pad_token_id
    logger.info("[CTC] model %s loaded in %.1fs", MODEL_ID, time.time() - t0)
    _MODEL = (model, dictionary, blank_id)
    return _MODEL


def _emissions(model, wav, blank_id: int, star_delta: float):
    """Full-song (T, C+1) log-prob matrix — chunked encoder, exact global
    frame indices, synthetic star appended as the last class."""
    import torch
    n = wav.shape[1]
    chunk, ctx = int(CHUNK_S * SR), int(CTX_S * SR)
    pieces = []
    with torch.inference_mode():
        for start in range(0, n, chunk):
            end = min(start + chunk, n)
            a, b = max(0, start - ctx), min(n, end + ctx)
            x = wav[:, a:b]
            x = (x - x.mean()) / (x.std() + 1e-7)  # do_normalize
            em = torch.log_softmax(model(x).logits[0], dim=-1)
            nb = em.clone()
            nb[:, blank_id] = float("-inf")
            star = nb.max(dim=-1, keepdim=True).values - star_delta
            em = torch.cat([em, star], dim=-1)
            # wav2vec2's conv stack emits floor((L-400)/320)+1 frames, so
            # the LAST chunk (no right context) can come out one frame
            # short of `hi` — python slicing tolerates it; the effect is
            # ≤20-40 ms at the very tail of the song.
            lo = (start - a) // FRAME
            hi = lo + (end - start) // FRAME
            pieces.append(em[lo:hi].cpu())
    return torch.cat(pieces)


def retime_segments(audio_path: str, segments: list[dict],
                    job_id: str = "") -> Optional[list[dict]]:
    """Align the segments' text onto `audio_path` (vocal stem preferred)
    and return NEW segments with replaced start/end + word stamps.
    Texts pass through verbatim. Returns None to decline (caller keeps
    the original timings). Never raises."""
    try:
        if not is_enabled() or not segments or len(segments) < 3:
            return None
        if not audio_path or not os.path.exists(audio_path):
            return None

        import torch
        import torchaudio
        import torchaudio.functional as AF

        model, dictionary, blank_id = _load_model()
        # star class is appended LAST in _emissions; its id == n_classes
        star_id = model.config.vocab_size

        lines = [(s.get("text") or "").strip() for s in segments]
        use_sep = os.environ.get("CTC_ALIGN_WORD_SEP", "1").strip().lower() in _TRUE
        word_sep_id = dictionary.get("|") if use_sep else None
        targets, words = build_targets(lines, dictionary, star_id,
                                       word_sep_id=word_sep_id)
        n_real_tokens = sum(n for li, _, n in words if li >= 0)
        n_chars = sum(len(norm_word(w)) for ln in lines for w in ln.split()) or 1
        if n_real_tokens / n_chars < 0.6:
            logger.info("[CTC] decline: only %.0f%% of chars alignable (job=%s)",
                        100 * n_real_tokens / n_chars, job_id)
            return None

        t0 = time.time()
        # Duration guard BEFORE decoding: a multi-hour upload would expand
        # to several GB of float32 in RAM if we torchaudio.load() first
        # (OOM risk in the uvicorn/ShortWorker containers).
        try:
            info = torchaudio.info(audio_path)
            est_dur = info.num_frames / (info.sample_rate or SR)
        except Exception:
            est_dur = None
        if est_dur is not None and (est_dur > _max_audio_s() or est_dur < 5):
            logger.info("[CTC] decline: audio %.0fs out of range (job=%s)",
                        est_dur, job_id)
            return None
        wav, sr = torchaudio.load(audio_path)
        wav = wav.mean(0, keepdim=True)
        if sr != SR:
            wav = torchaudio.functional.resample(wav, sr, SR)
        dur = wav.shape[1] / SR
        if dur > _max_audio_s() or dur < 5:
            logger.info("[CTC] decline: audio %.0fs out of range (job=%s)", dur, job_id)
            return None
        if len(targets) >= wav.shape[1] // FRAME:
            logger.info("[CTC] decline: more tokens than frames (job=%s)", job_id)
            return None

        emission = _emissions(model, wav, blank_id, _star_delta())
        aligned, scores = AF.forced_align(
            emission.unsqueeze(0),
            torch.tensor(targets, dtype=torch.int32).unsqueeze(0),
            blank=blank_id)
        # blank= must match forced_align's: with the default (0) and a
        # future CTC_ALIGN_MODEL whose pad token isn't 0, spans would
        # silently desync from targets (garbage timings, no crash).
        token_spans = AF.merge_tokens(aligned[0], scores[0].exp(),
                                      blank=blank_id)
        spans = [(sp.start, sp.end, float(sp.score)) for sp in token_spans]
        frame_to_s = FRAME / SR

        line_times = spans_to_lines(spans, words, len(lines), frame_to_s)
        if looks_collapsed(line_times):
            logger.warning("[CTC] decline: collapsed alignment (job=%s)", job_id)
            return None
        try:
            import anchor_align
            _regions = anchor_align.vocal_regions(audio_path)
        except Exception:
            _regions = []
        line_times = repair_bridge_words(line_times, _regions)

        # Interpolate lines whose words were all unalignable (numbers,
        # emoji): midpoint between neighbours keeps monotonic order.
        for i, lt in enumerate(line_times):
            if lt is None:
                prev_end = next((line_times[j][1] for j in range(i - 1, -1, -1)
                                 if line_times[j]), 0.0)
                nxt_start = next((line_times[j][0] for j in range(i + 1, len(line_times))
                                  if line_times[j]), dur)
                line_times[i] = (prev_end, max(prev_end, nxt_start), [])

        out = []
        for seg, (ls, le, wlist) in zip(segments, line_times):
            new = dict(seg)
            new["start"] = round(float(ls), 3)
            new["end"] = round(float(le), 3)
            if wlist:
                new["words"] = [
                    {"word": w, "start": round(float(a), 3),
                     "end": round(float(b), 3), "score": round(float(sc), 3)}
                    for (w, a, b, sc) in wlist]
            else:
                # Interpolated line: the inherited word stamps belong to
                # the OLD timing — keeping them would break karaoke.
                new.pop("words", None)
            out.append(new)
        logger.info("[CTC] retimed %d lines in %.1fs (audio %.0fs, job=%s)",
                    len(out), time.time() - t0, dur, job_id)
        return out
    except Exception as e:
        logger.warning("[CTC] decline on error: %s (job=%s)", e, job_id)
        return None
