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


def group_consecutive(idxs: list[int]) -> list[list[int]]:
    """[7,8,20,31,32] → [[7,8],[20],[31,32]]. Pure."""
    if not idxs:
        return []
    groups, cur = [], [idxs[0]]
    for i in idxs[1:]:
        if i == cur[-1] + 1:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    return groups


def recovery_window(line_times, grp: list[int], total_dur: float,
                    pad: float = 1.0, min_s: float = 2.0, max_s: float = 60.0):
    """Window for re-aligning a skipped group: the span between its
    anchored (non-None) neighbours. None when the window is degenerate
    or too large to trust (an 82 s outro window aligns garbage). Pure."""
    lo = 0.0
    for j in range(grp[0] - 1, -1, -1):
        if line_times[j] is not None:
            lo = line_times[j][1]
            break
    hi = total_dur
    for j in range(grp[-1] + 1, len(line_times)):
        if line_times[j] is not None:
            hi = line_times[j][0]
            break
    lo, hi = max(0.0, lo - pad), min(total_dur, hi + pad)
    if hi - lo < min_s or hi - lo > max_s:
        return None
    return lo, hi


def skip_arc_align(emission, targets, line_token_ranges, blank_id: int,
                   lam: float = 6.0):
    """Forced alignment with per-line SKIP ARCS — our own CTC Viterbi.

    Standard CTC forced alignment must spend every target token somewhere:
    a lyric line that was never sung (lrclib returns the studio bridge for
    a live that skips it) steals audio from its neighbours (measured:
    ghost-line blocks displace real lines by 8-14 s). Here every line has
    an explicit alternative path — an epsilon arc from the state before
    its first token to the state after its last token, paid once with
    penalty `lam` — so the Viterbi can declare "this line was not sung"
    when the acoustics don't support it.

    Trellis: the classic 2L+1 CTC states (blank interleaved between
    tokens). Per frame: stay / advance-1 / advance-2 (the standard skip
    over a blank between distinct tokens), then a relaxation pass over
    the epsilon skip arcs (they consume no frames; forward-only, so one
    pass per frame is exact).

    With lam=inf this degenerates to vanilla forced alignment — the
    parity test pins it against torchaudio's forced_align. Replacing the
    torchaudio call also removes the dependency on an API deleted in
    torchaudio 2.9.

    Args:
      emission: (T, C) log-probs (star column already appended).
      targets: list[int] token ids.
      line_token_ranges: [(tok_start, tok_end_exclusive)] per REAL line
        (indices into `targets`) — each gets a skip arc.
      lam: one-time log-prob penalty for skipping a line.
    Returns (spans, skipped): spans = [(start_frame, end_frame, score)]
      per target token (skipped lines get zero-length spans at their skip
      point); skipped = set of line_range indices the Viterbi bypassed.
    """
    import numpy as np

    em = emission.numpy() if hasattr(emission, "numpy") else np.asarray(emission)
    T = em.shape[0]
    L = len(targets)
    S = 2 * L + 1  # blank,tok,blank,tok,...,blank
    NEG = -1e30

    state_tok = np.full(S, blank_id, dtype=np.int64)
    state_tok[1::2] = targets
    # advance-2 allowed into token state s when its token differs from the
    # previous token state's (CTC rule)
    adv2_ok = np.zeros(S, dtype=bool)
    for s in range(3, S, 2):
        adv2_ok[s] = targets[(s - 1) // 2] != targets[(s - 3) // 2]
    # skip arcs in STATE space: from blank state before the line's first
    # token to blank state after its last token
    arcs = [(2 * a, 2 * b, li) for li, (a, b) in enumerate(line_token_ranges)]

    alpha = np.full(S, NEG)
    alpha[0] = em[0, blank_id]
    if S > 1:
        alpha[1] = em[0, state_tok[1]]
    bp = np.zeros((T, S), dtype=np.int8)  # 0=stay 1=adv1 2=adv2 3=skip-arc
    for src, dst, li in arcs:
        if alpha[src] - lam > alpha[dst]:
            alpha[dst] = alpha[src] - lam
            bp[0, dst] = 3
    for t in range(1, T):
        stay = alpha
        adv1 = np.concatenate(([NEG], alpha[:-1]))
        adv2 = np.where(adv2_ok, np.concatenate(([NEG, NEG], alpha[:-2])), NEG)
        best = np.maximum(np.maximum(stay, adv1), adv2)
        choice = np.zeros(S, dtype=np.int8)
        choice[adv1 > stay] = 1
        choice[adv2 > np.maximum(stay, adv1)] = 2
        alpha = best + em[t, state_tok]
        bp[t] = choice
        for src, dst, li in arcs:  # epsilon relaxation, forward-only
            if alpha[src] - lam > alpha[dst]:
                alpha[dst] = alpha[src] - lam
                bp[t, dst] = 3
    # backtrack from the better of the two legal CTC end states
    s = S - 1 if alpha[S - 1] >= alpha[S - 2] or S < 2 else S - 2
    arc_by_dst = {dst: (src, li) for src, dst, li in arcs}
    tok_frames: list[list] = [[] for _ in range(L)]
    skipped: set[int] = set()
    t = T - 1
    while t >= 0:
        if s % 2 == 1:
            tok_frames[(s - 1) // 2].append(t)
        move = bp[t, s]
        if move == 3 and s in arc_by_dst:
            src, li = arc_by_dst[s]
            skipped.add(li)
            s = src
            continue  # epsilon: no frame consumed
        if move == 1:
            s -= 1
        elif move == 2:
            s -= 2
        t -= 1
    spans = []
    for ti in range(L):
        fr = tok_frames[ti]
        if fr:
            spans.append((min(fr), max(fr) + 1,
                          float(np.exp(em[fr, targets[ti]].mean()))))
        else:
            spans.append((0, 0, 0.0))
    return spans, skipped


# Spanish vs English function words — language guard. The XLSR-es vocab
# covers a-z, so English text passes the char-alignability gate with
# ratio 1.0 and aligns SILENTLY WRONG (measured: 7 rings → median word
# score 0.21 vs 0.62-0.87 on Spanish songs, lines off by +54/+133s).
_ES_STOP = frozenset(
    "de la que el en y a los se del las un por con no una su para es al lo "
    "como mas pero sus le ya o este si porque esta entre cuando muy sin "
    "sobre tambien me hasta donde quien desde todo nos ni contra eso yo tu "
    "te mi tus vos usted nunca siempre aqui asi fue soy eres somos estoy "
    "esta estas estoy son era eran tengo tiene tienes vamos voy va".split())
_EN_STOP = frozenset(
    "the and you that was for are with his her they this have from not but "
    "what can out get got like just know take into your some could them see "
    "other than then now look only come its over think also back after use "
    "two how our work well way even new want because any these give day i "
    "it my me a to of in is on he as do at by we or an will so if no when "
    "all be been being am dont aint gonna wanna".split())


def guess_text_lang(lines: list[str]) -> str:
    """'es' / 'en' / 'unknown' by function-word voting. Pure."""
    es = en = 0
    for line in lines:
        for w in line.lower().split():
            w = re.sub(r"[^a-záéíóúñü']", "", w)
            if w in _ES_STOP:
                es += 1
            if w in _EN_STOP:
                en += 1
    total = es + en
    if total < 5:
        return "unknown"
    if es >= 2 * en:
        return "es"
    if en >= 2 * es:
        return "en"
    return "unknown"


BRIDGE_S = 8.0  # no word lasts 8s; no intra-line silence lasts 8s


def _eff_dur(word: str) -> float:
    """Plausible sung duration of a word: ~0.45s per alignable char +
    slack for held notes. 'anzuelo' → ~3.7s, never 27s."""
    return 0.45 * max(len(norm_word(word)), 2) + 0.6


def finalize_line(seg: dict, ls: float, le: float, wlist, lr,
                  skipped: bool, recovered: bool) -> dict:
    """Construye el dict final de una línea retimada. Pura, unit-testeable.

    Regla del flag `review` ("timing aproximado", puesto por el camino
    scaffold): una línea que CTC retimó tiene precisión word-level → el
    flag heredado se limpia. Sin esto, el wizard pintaba de ámbar las 66
    líneas de una canción perfectamente sincronizada (operador, 03/07).
    Las líneas SALTEADAS conservan el flag: su timing viene del fallback
    y sí es aproximado."""
    new = dict(seg)
    new["start"] = round(float(ls), 3)
    new["end"] = round(float(le), 3)
    if lr is not None:
        new["ctc_lr"] = round(lr, 3)
    if skipped:
        new["ctc_skipped"] = True  # Viterbi: línea no cantada
    else:
        new.pop("review", None)
    if recovered:
        new["ctc_recovered"] = "mix"  # M5: rescatada del mix
    if wlist:
        new["words"] = [
            {"word": w, "start": round(float(a), 3),
             "end": round(float(b), 3), "score": round(float(sc), 3)}
            for (w, a, b, sc) in wlist]
    else:
        # Interpolated line: the inherited word stamps belong to
        # the OLD timing — keeping them would break karaoke.
        new.pop("words", None)
    return new


def trim_unvoiced_edges(line_times, regions,
                        score_floor=None):
    """Snap low-confidence EDGE words to the measured voice regions.

    Caso real (Hada y el Mago, 03/07, confirmado a oído por el operador):
    CTC ancló la primera palabra "Uh," en 6.02→7.9 con score 0.12 — casi
    todo sobre SILENCIO medido (la voz entra a 7.7). La línea aparecía
    ~2s antes de que nadie cante. repair_bridge_words no lo agarra: el
    estiramiento (1.9s) queda por debajo de BRIDGE_S=8.

    Regla: si la PRIMERA palabra de una línea tiene score < floor, su
    start se ajusta al inicio de la primera región de voz que su span
    toca (nunca hacia antes, nunca más allá de su end). Simétrico para
    la ÚLTIMA palabra (su end se recorta al fin de la región). Palabras
    confiables no se tocan — el score bajo es lo que dice "acá CTC está
    adivinando"; la energía del stem dice dónde hay canto de verdad.

    Gate: CTC_ALIGN_EDGE_SNAP (default on; el retime entero ya está
    detrás de CTC_ALIGN_ENABLED). floor: CTC_ALIGN_EDGE_SNAP_SCORE
    (default 0.30). Sin regiones (VAD falló) → no-op. Pura."""
    if os.environ.get("CTC_ALIGN_EDGE_SNAP", "1").strip().lower() not in _TRUE:
        return line_times
    if not regions:
        return line_times
    if score_floor is None:
        try:
            score_floor = float(os.environ.get("CTC_ALIGN_EDGE_SNAP_SCORE", "0.30"))
        except (TypeError, ValueError):
            score_floor = 0.30

    out = []
    for lt in line_times:
        if lt is None or not lt[2]:
            out.append(lt)
            continue
        _s, _e, ws = lt
        ws = list(ws)
        w0, a0, b0, sc0 = ws[0]
        if sc0 < score_floor:
            # primera región de voz que el span [a0, b0] toca
            snap = None
            for ra, rb in regions:
                if rb <= a0:
                    continue
                if ra >= b0:
                    break
                snap = max(a0, ra)
                break
            if snap is not None and snap > a0:
                ws[0] = (w0, round(snap, 3), b0, sc0)
        wn, an, bn, scn = ws[-1]
        if scn < score_floor:
            # última región de voz que el span [an, bn] toca
            snap = None
            for ra, rb in regions:
                if ra >= bn:
                    break
                if rb <= an:
                    continue
                snap = min(bn, rb)
            if snap is not None and snap < bn:
                ws[-1] = (wn, an, round(snap, 3), scn)
        out.append((ws[0][1], ws[-1][2], ws))
    return out


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


def place_unaligned(line_times, originals, total_dur: float, slack: float = 4.0):
    """Place lines the engine could NOT time (skipped / unalignable).

    Spreading them evenly over the hole (the v1 behaviour) looks
    DISASTROUS in the real video: each chorus line sits frozen 15-20 s in
    the wrong place (staging job 8be12628e28b). The cascade's original
    timing for those lines — whisperX heard the chorus roughly right — is
    a far better prior: use it whenever it falls inside the hole (±slack),
    clamped to the anchored neighbours. Fall back to the spread only when
    the original is missing/implausible. `originals` = [(start,end)|None]
    per line. Pure."""
    out = list(line_times)
    for i, lt in enumerate(out):
        if lt is not None:
            continue
        prev_end = next((out[j][1] for j in range(i - 1, -1, -1)
                         if out[j] is not None), 0.0)
        nxt_start = next((out[j][0] for j in range(i + 1, len(out))
                          if out[j] is not None), total_dur)
        o = originals[i] if i < len(originals) else None
        if (o and o[1] > o[0] > 0
                and prev_end - slack <= o[0] <= nxt_start + slack):
            hi = max(nxt_start, prev_end + 0.4)
            s = min(max(o[0], prev_end), hi - 0.2)
            e = max(min(o[1], hi), s + 0.2)
            out[i] = (s, e, [])
        else:
            out[i] = (prev_end, max(prev_end, nxt_start), [])
    return out


def condense_repeated_skips(segments: list[dict]) -> list[dict]:
    """Rotor-style condensation for crowd-chorus passages (libretto path).

    REDESIGN after the operator's text-fidelity findings: the old version
    chained only text-SIMILAR neighbours, so (a) the block read the same
    phrase repeated mechanically while the real chant has variants and
    interjections ("nada, nada de esto fue un error OH OH, nada de esto
    fue un error"), (b) dissimilar interjections broke the chain leaving
    stacked zero-length leftovers (job 400: COND3+COND2+COND17 at 251.8).

    Now: a RUN of consecutive UNANCHORED lines (skipped or near-zero
    duration — the engine had no individual acoustic evidence) that
    contains a repeated-text signature (>=2 mutually similar lines)
    becomes ONE block: span from the run's start to the next anchored
    line, text = the run's REAL line sequence joined in order (variants
    and interjections included), capped at ~90 chars. Anchored lines
    (including mix-recovered ones with real duration) are never touched —
    studio songs and well-heard verses keep full per-line dynamism.
    Single stray skips stay as-is (cascade fallback placement). Pure."""
    def n(t):
        return re.sub(r"\W+", "", (t or "").lower())

    def unanchored(s):
        return bool(s.get("ctc_skipped")) or (s["end"] - s["start"]) < 0.3

    out: list[dict] = []
    i = 0
    while i < len(segments):
        if not unanchored(segments[i]):
            out.append(dict(segments[i]))
            i += 1
            continue
        j = i
        while j < len(segments) and unanchored(segments[j]):
            j += 1
        run = [dict(x) for x in segments[i:j]]
        norms = [n(x.get("text")) for x in run]
        chant = len(run) >= 2 and any(
            len(a) > 8 and len(b) > 8 and (a in b or b in a)
            for k, a in enumerate(norms) for b in norms[k + 1:])
        if chant:
            # CANONICAL text (operator finding, gen-6): the block must not
            # inherit Gemini mishears ("nada de ESO") nor vary run-to-run.
            # Each run line whose text is ~equal to an ANCHORED line of the
            # song (the known chorus) is rewritten with that canonical
            # text; Gemini contributes only the COUNT and order.
            from difflib import SequenceMatcher as _SM
            canon = {}
            for x in segments:
                if not unanchored(x):
                    cn = n(x.get("text"))
                    if len(cn) > 8 and cn not in canon:
                        canon[cn] = (x.get("text") or "").strip()
            for x in run:
                xn = n(x.get("text"))
                if len(xn) > 8:
                    # DESAMBIGUACIÓN (GT Rotor: el tema tiene DOS frases de
                    # coro 87% similares — "nada fue un error" y "nada de
                    # esto fue un error" — y el primer-match las confundía
                    # homogeneizando el bloque). Solo canonizar con el MEJOR
                    # match y margen claro sobre el segundo.
                    scored = sorted(((_SM(None, xn, cn).ratio(), ct)
                                     for cn, ct in canon.items()),
                                    reverse=True)
                    if scored and scored[0][0] >= 0.85 and (
                            len(scored) < 2
                            or scored[0][0] - scored[1][0] >= 0.06
                            or scored[0][0] >= 0.97):
                        x["text"] = scored[0][1]
            start_t = min(x["start"] for x in run)
            nxt = segments[j]["start"] if j < len(segments) else None
            end_t = max(x["end"] for x in run)
            if nxt is not None and nxt - 0.2 > end_t:
                end_t = nxt - 0.2
            if end_t <= start_t:
                end_t = start_t + 0.4
            text = (run[0].get("text") or "").strip()
            for x in run[1:]:
                t2 = (x.get("text") or "").strip()
                if not t2:
                    continue
                cand = f"{text}, {t2[:1].lower() + t2[1:]}"
                if len(cand) > 90:
                    break
                text = cand
            blk = dict(run[0])
            blk["start"] = round(start_t, 3)
            blk["end"] = round(end_t, 3)
            blk["text"] = text
            blk["ctc_condensed"] = len(run)
            blk.pop("ctc_skipped", None)
            blk.pop("words", None)
            out.append(blk)
        else:
            out.extend(dict(x) for x in run)
        i = j
    # a stray skipped singleton spread over a huge hole (job-400 outro:
    # one mishear line stretched 290→366 s) has no evidence and no chant
    # signature — drop it rather than freeze it on screen for a minute
    final = [s for s in out
             if (0.3 <= (s["end"] - s["start"]) <= 15.0)
             or not s.get("ctc_skipped")]
    # GUARANTEED extension on the final list (gen-6: a C6 block ended at
    # 70.1 leaving a 5 s hole before the 75.1 anchor): any condensed
    # block followed by a gap stretches to the next line's start.
    for k, s in enumerate(final):
        if s.get("ctc_condensed") and k + 1 < len(final):
            nxt = final[k + 1]["start"]
            if nxt - 0.2 > s["end"]:
                s["end"] = round(nxt - 0.2, 3)
    return final


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


# Why the last retime_segments call declined ("structural" | "other" | "").
# The wrapper composes on it: structural mismatch → try the performance
# libretto (performance_text) and retry. Workers process one job at a time
# (single-threaded per process), so a module global is safe here.
last_decline_reason = ""


def retime_segments(audio_path: str, segments: list[dict],
                    job_id: str = "",
                    mix_path: Optional[str] = None,
                    max_skip_frac: Optional[float] = None) -> Optional[list[dict]]:
    """Align the segments' text onto `audio_path` (vocal stem preferred)
    and return NEW segments with replaced start/end + word stamps.
    Texts pass through verbatim. Returns None to decline (caller keeps
    the original timings). Never raises.

    `max_skip_frac` overrides CTC_ALIGN_MAX_SKIP_FRAC for this call — the
    performance-libretto retry tolerates more skips (crowd lines ARE in
    the libretto but invisible in the stem until M5/transplant get them).

    `mix_path` (the original un-separated audio): when given and the
    skip-arc pass skipped lines, a M5 RECOVERY pass re-aligns each
    skipped group against MIX emissions, confined to the window between
    its anchored neighbours. The crowd's voice IS in the mix — demucs
    erases it from the stem (the reason those lines were skipped).
    Acceptance-gated: measured on the live benchmark, recoveries with
    mean word score ≥0.35 landed at 0.10 s of Rotor; <0.25 were wrong."""
    global last_decline_reason
    last_decline_reason = ""
    try:
        if not is_enabled() or not segments or len(segments) < 3:
            return None
        if not audio_path or not os.path.exists(audio_path):
            return None

        lines = [(s.get("text") or "").strip() for s in segments]
        lang = guess_text_lang(lines)
        if lang != "es":
            # The model is Spanish-only; English aligns silently wrong
            # (it passes the char gate with ratio 1.0). Until a per-language
            # model registry exists, declining is the honest move. Checked
            # BEFORE any torch import / 1.2 GB model load.
            logger.info("[CTC] decline: text language=%s (job=%s)", lang, job_id)
            return None

        import torch
        import torchaudio
        import torchaudio.functional as AF

        model, dictionary, blank_id = _load_model()
        # star class is appended LAST in _emissions; its id == n_classes
        star_id = model.config.vocab_size

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
        use_skip = os.environ.get("CTC_ALIGN_SKIP_ARCS", "0").strip().lower() in _TRUE
        skipped_lines: set[int] = set()
        if use_skip:
            # Our own Viterbi with per-line skip arcs: a ghost line (lrclib
            # studio text over a live that skips it) costs lam instead of
            # stealing audio from its neighbours. Also removes the
            # torchaudio.forced_align dependency (deleted in 2.9).
            tok_pos, line_ranges, line_of_range = 0, [], []
            for li, _raw, n_tok in words:
                if li >= 0:
                    if not line_of_range or line_of_range[-1] != li:
                        line_ranges.append([tok_pos, tok_pos + n_tok])
                        line_of_range.append(li)
                    else:
                        line_ranges[-1][1] = tok_pos + n_tok
                tok_pos += n_tok
            lam = float(os.environ.get("CTC_ALIGN_SKIP_LAMBDA", "6.0"))
            spans, skipped_ranges = skip_arc_align(
                emission, targets, [tuple(r) for r in line_ranges],
                blank_id, lam=lam)
            skipped_lines = {line_of_range[i] for i in skipped_ranges}
            frame_scores = None  # LR telemetry only on the torchaudio path
        else:
            aligned, scores = AF.forced_align(
                emission.unsqueeze(0),
                torch.tensor(targets, dtype=torch.int32).unsqueeze(0),
                blank=blank_id)
            # blank= must match forced_align's: with the default (0) and a
            # future CTC_ALIGN_MODEL whose pad token isn't 0, spans would
            # silently desync from targets (garbage timings, no crash).
            token_spans = AF.merge_tokens(aligned[0], scores[0].exp(),
                                          blank=blank_id)
            frame_scores = scores[0]        # chosen-path log prob per frame
            spans = [(sp.start, sp.end, float(sp.score)) for sp in token_spans]
        # The null hypothesis for a line is NOT the star alone: if the
        # line didn't exist, the Viterbi would alternate star and BLANK
        # over its span (blank scores very high on silence). Comparing
        # against star only inflates the ratio of any line with pauses —
        # measured: bad lines outscored good ones on megustas. The
        # per-frame max(star, blank) is the 1-state Viterbi of the
        # line-absent path.
        star_col = torch.maximum(emission[:, star_id], emission[:, blank_id])
        frame_to_s = FRAME / SR

        line_times = spans_to_lines(spans, words, len(lines), frame_to_s)
        for li in skipped_lines:
            # The Viterbi declared this line not sung — its (zero-length)
            # spans are meaningless. Interpolation below places it between
            # its neighbours; telemetry marks it for the editor/shadow logs.
            line_times[li] = None
        if looks_collapsed(line_times):
            logger.warning("[CTC] decline: collapsed alignment (job=%s)", job_id)
            return None
        try:
            import anchor_align
            _regions = anchor_align.vocal_regions(audio_path)
        except Exception:
            _regions = []
        line_times = repair_bridge_words(line_times, _regions)

        # M5 — recover skipped lines from the MIX, window-confined.
        recovered_lines: set[int] = set()
        mix_ok = (skipped_lines and mix_path and os.path.exists(mix_path)
                  and os.environ.get("CTC_ALIGN_MIX_RECOVER", "1").strip().lower() in _TRUE)
        if mix_ok:
            accept = float(os.environ.get("CTC_ALIGN_MIX_ACCEPT", "0.35"))
            # CONTEXTUAL threshold (root-cause test, Nada Fue block 1:05):
            # with the CORRECT chorus text the mix-CTC places crowd lines at
            # +0.6/+1.7 s of Rotor but scores 0.21-0.29 — the flat 0.35
            # threshold rejected good placements. When the skipped line's
            # text REPEATS an already-anchored line (a known chorus, not
            # inventable), the prior is strong → accept from 0.20.
            accept_known = float(os.environ.get("CTC_ALIGN_MIX_ACCEPT_KNOWN", "0.20"))
            anchored_norms = [re.sub(r"\W+", "", lines[i].lower())
                              for i in range(len(lines))
                              if i not in skipped_lines and len(lines[i]) > 10]
            mwav, msr = torchaudio.load(mix_path)
            mwav = mwav.mean(0, keepdim=True)
            if msr != SR:
                mwav = torchaudio.functional.resample(mwav, msr, SR)
            for grp in group_consecutive(sorted(skipped_lines)):
                win = recovery_window(line_times, grp, mwav.shape[1] / SR)
                if not win:
                    continue
                lo, hi = win
                chunk = mwav[:, int(lo * SR):int(hi * SR)]
                gem = _emissions(model, chunk, blank_id, _star_delta())
                gtexts = [lines[i] for i in grp]
                gtargets, gwords = build_targets(gtexts, dictionary, star_id,
                                                 word_sep_id=word_sep_id)
                if len(gtargets) >= gem.shape[0]:
                    continue
                galigned, gscores = AF.forced_align(
                    gem.unsqueeze(0),
                    torch.tensor(gtargets, dtype=torch.int32).unsqueeze(0),
                    blank=blank_id)
                gspans_t = AF.merge_tokens(galigned[0], gscores[0].exp(),
                                           blank=blank_id)
                gspans = [(sp.start, sp.end, float(sp.score)) for sp in gspans_t]
                glt = spans_to_lines(gspans, gwords, len(gtexts), frame_to_s)
                for k, li in enumerate(grp):
                    if glt[k] is None or not glt[k][2]:
                        continue
                    gs, ge, gws = glt[k]
                    msc = sum(w[3] for w in gws) / len(gws)
                    n = re.sub(r"\W+", "", lines[li].lower())
                    gn = [re.sub(r"\W+", "", t.lower()) for t in gtexts]
                    group_chant = sum(
                        1 for k2, a2 in enumerate(gn) for b2 in gn[k2 + 1:]
                        if len(a2) > 8 and len(b2) > 8
                        and (a2 in b2 or b2 in a2)) >= 1
                    # the relaxed threshold let a chorus line land where the
                    # bridge is sung (gen-6, 224.57): known-text acceptance
                    # only when the GROUP itself has a chant signature
                    known = group_chant and len(n) > 10 and any(
                        n in a or a in n for a in anchored_norms)
                    if msc < (accept_known if known else accept):
                        continue
                    # End discipline: a recovered line whose CTC end drags
                    # across the window (job 400: '¡Dos!...' 75.9→92.9,
                    # 17 s) gets clamped to a plausible sung duration.
                    max_dur = max(2.0, sum(_eff_dur(w) for (w, _a, _b, _sc)
                                           in gws))
                    if ge - gs > max_dur:
                        ge = gs + max_dur
                        gws = [(w, a, min(b, gs + max_dur), sc)
                               for (w, a, b, sc) in gws if a < gs + max_dur]
                    off = lo
                    line_times[li] = (gs + off, ge + off,
                                      [(w, a + off, b + off, sc)
                                       for (w, a, b, sc) in gws])
                    recovered_lines.add(li)
            if recovered_lines:
                logger.info("[CTC] mix-recovery: %d/%d skipped lines recovered "
                            "(job=%s)", len(recovered_lines), len(skipped_lines),
                            job_id)
                skipped_lines -= recovered_lines

        # STRUCTURAL-MISMATCH guard (staging lesson, Nada Fue live): when
        # the live's structure differs from the reference text (extra
        # verse/chorus repetitions the text doesn't list), the monotonic
        # Viterbi binds lines to the WRONG occurrence with high confidence
        # — the user heard verse 2 sung at 1:32 while the engine placed it
        # at 3:03 (its third occurrence). A high skip fraction IS the
        # symptom (8/49=16% there; 0 on every healthy song measured).
        # No timing engine can fix structurally-wrong text → decline the
        # whole song, keep the cascade. The real fix is performance text
        # (Etapa A: ASR of the actual live + reference orthography).
        if max_skip_frac is None:
            max_skip_frac = float(os.environ.get("CTC_ALIGN_MAX_SKIP_FRAC", "0.10"))
        if len(skipped_lines) / max(len(lines), 1) > max_skip_frac:
            logger.warning("[CTC] decline: %d/%d lines skipped (>%.0f%%) — "
                           "structural mismatch between text and performance "
                           "(job=%s)", len(skipped_lines), len(lines),
                           100 * max_skip_frac, job_id)
            last_decline_reason = "structural"
            return None

        # Lines the engine could not time (skipped / unalignable words):
        # prefer the CASCADE's original timing inside the hole over a
        # blind spread — see place_unaligned's docstring (staging lesson).
        originals = [((float(s.get("start") or 0), float(s.get("end") or 0))
                      if s.get("start") is not None else None)
                     for s in segments]
        line_times = place_unaligned(line_times, originals, dur)

        # Bordes low-score sobre silencio (caso "Uh, no, no" @6.0 con voz
        # @7.7, 03/07): snap a las regiones de voz del STEM. Va DESPUÉS de
        # M5/place_unaligned porque la línea problemática entra justamente
        # por la recuperación sobre la mezcla (el pase principal la había
        # salteado) — antes de este punto sus words todavía no existen.
        # Una línea de público SIN región de stem que la toque queda
        # intacta (el snap solo ajusta bordes que pisan una región).
        line_times = trim_unvoiced_edges(line_times, _regions)

        # Per-line confidence as a LIKELIHOOD-RATIO vs the star: how much
        # better does this line explain its span than "unlabeled singing"?
        # Raw word posteriors are NOT calibrated across songs (measured:
        # real crowd-chorus lines score 0.087 while a ghost line scores
        # 0.13); the ratio is. lr ≈ star_delta when the text IS the best
        # explanation; strongly negative on ghosts / wrong language.
        def _line_lr(ls: float, le: float) -> float:
            fs, fe = int(ls / frame_to_s), max(int(le / frame_to_s), 1)
            fs = max(0, min(fs, len(frame_scores) - 1))
            fe = max(fs + 1, min(fe, len(frame_scores)))
            return float((frame_scores[fs:fe] - star_col[fs:fe]).mean())

        lrs = [(_line_lr(lt[0], lt[1]) if (lt and frame_scores is not None) else None)
               for lt in line_times]
        word_scores = [w[3] for lt in line_times if lt for w in lt[2]]
        med_score = sorted(word_scores)[len(word_scores) // 2] if word_scores else 0.0
        min_med = float(os.environ.get("CTC_ALIGN_MIN_MED_SCORE", "0.35"))
        if med_score < min_med:
            # Whole-song low confidence = wrong language / unintelligible
            # vocals / broken stem (measured: English song med 0.21 vs
            # 0.62-0.87 on healthy Spanish material).
            logger.warning("[CTC] decline: median word score %.2f < %.2f (job=%s)",
                           med_score, min_med, job_id)
            return None

        out = []
        for li, (seg, (ls, le, wlist), lr) in enumerate(zip(segments, line_times, lrs)):
            out.append(finalize_line(seg, ls, le, wlist, lr,
                                     li in skipped_lines, li in recovered_lines))
        logger.info("[CTC] retimed %d lines in %.1fs (audio %.0fs, job=%s)",
                    len(out), time.time() - t0, dur, job_id)
        return out
    except Exception as e:
        logger.warning("[CTC] decline on error: %s (job=%s)", e, job_id)
        return None
