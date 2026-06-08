"""Rotor-grade hybrid transcription+timing pipeline (gated, self-declining).

This module wires the R&D-validated hybrid pipeline into prod behind the
`ROTOR_PIPELINE_ENABLED` flag (default OFF — when unset/0 the prod cascade is
100% untouched). The logic here is PORTED, not re-invented, from the validated
experiment scripts:

  - whisper-1 word-stamps : scripts/exp_asr_text2.py::asr_whisper1
  - gemini-2.5-pro chunked: scripts/exp_gemini_primary.py::transcribe
  - re-segmentation       : pipeline._llm_segment_words
  - onset snap            : forced_align.snap_starts_to_vocal_onsets
  - stem / vocal regions  : vocal_sep.separate_vocals + anchor_align.vocal_regions

ROUTING (no manual toggle — reuses the existing divergent-live detection):
  divergent live  (audio_duration - lrc_duration > 60s, OR lrclib absent)
        → gemini-2.5-pro chunked-primary (forced coverage + per-line timestamps)
  otherwise (studio/normal)
        → whisper-1 word-stamps → pipeline._llm_segment_words

Both paths then snap line starts to the isolated vocal-stem onsets.

CONTRACT: `run_rotor_pipeline` is SELF-DECLINING. On ANY failure (missing
creds, library import error, empty/short output, exception) it returns None so
the caller falls back to the existing cascade. It NEVER raises. Every import
that isn't guaranteed at process start is lazy, so importing this module can
never break the app boot.

Measured (vs Rotor ground truth, R&D harness):
  studio  → AOO median ~0.25s (whisper-1 + _llm_segment_words + snap)
  live    → coverage ~93%, de-offset ~0.83s (gemini-2.5-pro chunked + snap)
"""
from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import unicodedata
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


# ── text norm (ported from the experiment scripts) ─────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(c for c in s.lower().split())


# ── whisper-1 word-stamps  (ported from exp_asr_text2.asr_whisper1, but
#    verbose_json with WORD granularity so we get per-word timing the way
#    pipeline._llm_segment_words expects) ────────────────────────────────
_W1_HALLUCINATION = re.compile(
    r"(amara\.org|subt[ií]tulos|subtitulado|traducido por|"
    r"www\.|\.com|gracias por ver|suscr[ií]b)", re.I)


def _is_degenerate(text: str) -> bool:
    """True if `text` is a Whisper hallucination loop — a single token/char
    repeated (e.g. 'ñi ñi ñi …', 'la la la la', 'aaaaaa'). Whisper-1 produces
    these on corrupt/near-silent audio (e.g. a bad re-encode). The credit/
    translation regex (_W1_HALLUCINATION) does NOT catch them, so this guard
    is what prevents 'ñiñiñi' garbage from ever being emitted."""
    n = _norm(text)
    if not n:
        return True
    compact = n.replace(" ", "")
    if len(compact) >= 4 and len(set(compact)) <= 2:
        return True  # 'ñiñiñiñi', 'aaaaaa', 'lalalala'
    toks = n.split()
    if len(toks) >= 4 and len(set(toks)) == 1:
        return True  # 'la la la la', 'no no no no'
    if len(toks) >= 8 and len(set(toks)) <= 2:
        return True  # 'na na na na na na na na'
    return False


def whisper1_segments(audio_path: str, language: str | None) -> list[dict] | None:
    """whisper-1 (OpenAI) → [{start,end,text,words}].

    Re-encodes to a 16kHz mono mp3 (whisper-1 caps uploads at 25MB; our WAVs
    exceed it), requests verbose_json with WORD + SEGMENT timestamps, and
    filters the well-known Whisper credit/translation hallucinations
    ('Amara.org', 'Traducido por…', etc.) by their temporal span — a segment
    whose text matches the hallucination regex is dropped entirely (along with
    its words). Returns None (declines) on missing key / SDK error / empty out.
    Never raises."""
    try:
        if not os.environ.get("OPENAI_API_KEY"):
            logger.info("[ROTOR] whisper1: OPENAI_API_KEY not set — declining")
            return None
        import subprocess
        from openai import OpenAI

        client = OpenAI()
        # Re-encode to 16kHz mono mp3 with ffmpeg (whisper-1 caps uploads at
        # 25MB; our WAVs exceed it). We use ffmpeg — NOT soundfile — because
        # soundfile's mp3 encoder is absent on the prod/staging container, and
        # its ogg/opus fallback produced corrupt audio there → whisper-1
        # hallucinated 'ñiñiñi' garbage. ffmpeg is present and used everywhere
        # else in this app, so it re-encodes reliably across environments.
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        try:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                     "-ac", "1", "-ar", "16000", "-b:a", "64k", tmp.name],
                    check=True, capture_output=True, timeout=300)
            except Exception as enc_err:
                logger.warning("[ROTOR] whisper1: ffmpeg re-encode failed (%s) — declining", enc_err)
                return None
            if os.path.getsize(tmp.name) < 1024:
                logger.warning("[ROTOR] whisper1: re-encoded file is empty/tiny — declining")
                return None
            with open(tmp.name, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-1", file=f,
                    language=(language or "es"),
                    response_format="verbose_json",
                    timestamp_granularities=["segment", "word"],
                    temperature=0.0,
                )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        raw_words = list(getattr(resp, "words", None) or [])
        raw_segs = list(getattr(resp, "segments", None) or [])
        if not raw_segs:
            txt = (getattr(resp, "text", "") or "").strip()
            if not txt or _W1_HALLUCINATION.search(txt) or _is_degenerate(txt):
                return None
            return [{"start": 0.0, "end": 0.0, "text": txt, "words": []}]

        def _word_dicts(lo: float, hi: float) -> list[dict]:
            out = []
            for w in raw_words:
                ws = getattr(w, "start", None)
                we = getattr(w, "end", None)
                wt = getattr(w, "word", None)
                if ws is None or we is None or wt is None:
                    continue
                # words inside (or touching) this segment's span
                if float(we) >= float(lo) - 0.01 and float(ws) <= float(hi) + 0.01:
                    out.append({"word": str(wt), "start": float(ws), "end": float(we)})
            return out

        segs: list[dict] = []
        for s in raw_segs:
            text = (getattr(s, "text", "") or "").strip()
            if not text or _W1_HALLUCINATION.search(text) or _is_degenerate(text):
                continue  # drop hallucination / degenerate span (text + words)
            st = float(getattr(s, "start", 0.0) or 0.0)
            en = float(getattr(s, "end", st) or st)
            segs.append({"start": st, "end": en, "text": text,
                         "words": _word_dicts(st, en)})
        if not segs:
            return None
        # Global degeneracy guard: if the whole transcript collapses to a
        # repeated-token loop (per-segment guard can miss it when each segment
        # is a different single token), decline rather than emit garbage.
        if _is_degenerate(" ".join(s["text"] for s in segs)):
            logger.warning("[ROTOR] whisper1: transcript is degenerate (hallucination loop) — declining")
            return None
        return segs
    except Exception as e:
        logger.warning("[ROTOR] whisper1_segments declined: %s", e)
        return None


# ── whisper-1 × clean-text reconcile  (ADDITIVE; opt-in, no gating) ─────
def whisper1_reconcile(
    audio_path: str,
    clean_text: str,
    language: str | None = None,
    *,
    snap: bool = True,
    w1_segments: list[dict] | None = None,
    min_coverage: float = 0.5,
    max_drift: int = 40,
    drift_abort: int = 12,
) -> list[dict] | None:
    """Best-of-both: whisper-1's audio-pinned WORD TIMING + a clean reference
    TEXT (lrclib / canonical lyrics).

    WHY
    ---
    whisper-1 nails the line ONSET (timing) but mis-hears WORDS ("jabón" vs
    "jamón") and runs lines together. When we already hold trustworthy text
    for the song, we keep whisper-1's word stamps but adopt the clean text's
    spelling + line structure — driving WER toward 0 without disturbing the
    timing that makes the rotor (whisper-1) path good.

    HOW
    ---
      1. whisper-1 word-stamps  (`whisper1_segments`, or `w1_segments` if the
         caller already has them — e.g. a cached verbose_json).
      2. `whisperx_reconcile.reconcile(w1_segs, clean_text)` re-buckets those
         word stamps into the clean text's line structure (the same hardened
         `wordstamps_to_segments` walk with fuzzy re-anchor + drift abort) →
         clean text + whisper-1 timing.
      3. onset-snap line starts to the isolated vocal stem (same as the gated
         path), unless `snap=False`.

    SELF-DECLINING: returns None on missing key / no word stamps / unreliable
    reconciliation (drift or thin coverage) so the caller can fall back to raw
    whisper-1. Never raises. Purely additive — does not touch the gated
    `run_rotor_pipeline` behaviour."""
    try:
        if w1_segments is None:
            w1_segments = whisper1_segments(audio_path, language)
        if not w1_segments:
            logger.info("[ROTOR] whisper1_reconcile: no whisper-1 word stamps — declining")
            return None

        # Reuse the hardened word-stamp → known-lines walk directly (same
        # engine `whisperx_reconcile.reconcile` uses) so we can loosen its
        # re-anchor window: whisper-1 chunks long repeated chorus lines very
        # differently from the canonical line structure, and the default
        # max_drift=8 aborts on them. Larger window + drift budget lets the
        # walk skip past a mis-chunked run and re-anchor on the next line.
        import whisperx_reconcile
        from forced_align import wordstamps_to_segments

        words = whisperx_reconcile._flatten_words(w1_segments)
        if len(words) < 8:
            logger.info("[ROTOR] whisper1_reconcile: only %s word stamps — declining", len(words))
            return None
        lines = [ln.strip() for ln in (clean_text or "").splitlines() if ln.strip()]
        if len(lines) < 4:
            logger.info("[ROTOR] whisper1_reconcile: reference too short (%s lines) — declining",
                        len(lines))
            return None

        reconciled = wordstamps_to_segments(
            words, lines, max_drift=max_drift, drift_abort=drift_abort)
        if not reconciled:
            logger.info("[ROTOR] whisper1_reconcile: wordstamps_to_segments aborted (drift) — declining")
            return None
        coverage = len(reconciled) / max(1, len(lines))
        if coverage < min_coverage:
            logger.info("[ROTOR] whisper1_reconcile: thin coverage %.0f%% — declining",
                        coverage * 100)
            return None

        if snap:
            regions, _stem = _vocal_regions_for(audio_path)
            reconciled = _snap(reconciled, regions)
        return reconciled
    except Exception as e:
        logger.warning("[ROTOR] whisper1_reconcile declined: %s", e)
        return None


# ── gemini-2.5-pro chunked-primary (ported from exp_gemini_primary) ─────
_SYS_TS = (
    "Sos un transcriptor experto de letras en español, nivel Rotor.\n"
    "Te doy un FRAGMENTO de audio de ~{dur:.0f}s de una cancion{who} EN VIVO.\n"
    "Transcribi EXACTAMENTE lo que se canta en ESTE fragmento, una frase por linea,\n"
    "y al inicio de cada linea pone el timestamp RELATIVO (desde el comienzo de ESTE\n"
    "fragmento, no de la cancion) en formato [mm:ss.s].\n\n"
    "FORMATO EXACTO de cada linea: [mm:ss.s] texto de la frase\n"
    "Ejemplo:\n"
    "[00:01.2] Tengo una mala noticia\n"
    "[00:03.0] No fue de casualidad\n\n"
    "REGLAS DURAS:\n"
    "1. SOLO lo que escuchas en estos segundos. NO completes el estribillo entero,\n"
    "   NO repitas en loop una frase que no se repite en el audio.\n"
    "2. SI transcribi TODAS las repeticiones que realmente se cantan (coros que repiten).\n"
    "3. NO cortes una frase a la mitad: si empieza aca y se completa despues,\n"
    "   escribila completa como la ois aca, con el timestamp de cuando EMPIEZA.\n"
    "4. Ad-libs/gritos/vocalizaciones: transcribilos como se oyen (ej. 'Oh, oh', 'Ah, ah',\n"
    "   '¡papa!', '¡no!'). Si hay un GRITO largo sin letra escribi '(grito)'. Si hay\n"
    "   solo musica/aplausos sin voz NO escribas nada. Si hay silencio total escribi '(silencio)'.\n"
    "5. NO inventes palabras: si no entendes algo, transcribi lo mas parecido foneticamente.\n"
    "6. Cada linea DEBE empezar con su [mm:ss.s]. SIN numeracion, SIN comentarios extra."
)

_TS_RE = re.compile(
    r"^\s*[\[(]?\s*(\d{1,2})\s*[:m]\s*(\d{1,2}(?:[.s]\d{1,2})?)\s*s?\s*[\])]?\s*(.*)$")
_JUNK = re.compile(r"^[\[\]()0-9:.\s¡!¿?]*$")

_CHATTER = re.compile(
    r"^(gracias|chau|chao|adi[oó]s|buenas noches|muchas gracias|che)\b", re.I)
_PURE_VOCAL = re.compile(
    r"^[¡!\s]*((o+h+|a+h+|e+h+|u+h+|na+|la+|ja+|je+|wo+|yeah|hey|uh)[\s,.!¡]*)+$",
    re.I)
_STAGE = re.compile(
    r"(m[uú]sica|p[uú]blico|aplausos?|ovaci[oó]n|risas?|instrumental|"
    r"silencio|coro\b|gritos?|cantando|chau|gracias)", re.I)


def _nw_simple(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", " ", s.lower()).split()


def _parse_ts_line(line: str):
    raw = line.strip(" -•\t")
    m = _TS_RE.match(raw)
    if m:
        rel = int(m.group(1)) * 60 + float(m.group(2).replace("s", "."))
        text = m.group(3).strip(" -•\t[]()")
    else:
        rel, text = None, raw.strip(" -•\t[]()")
    if _JUNK.match(text) or not text:
        return rel, ""
    return rel, text


def _is_label(text: str) -> bool:
    return bool(re.match(r"^\(?(grito|silencio|instrumental|aplausos?)\)?$",
                         text.strip(), re.I))


def _is_pure_vocal(text: str) -> bool:
    return bool(_PURE_VOCAL.match(text.strip()))


def _is_stage_direction(text: str) -> bool:
    raw = text.strip().strip("()[]¡!¿?.")
    toks = _nw_simple(raw)
    if not toks or len(toks) > 4:
        return False
    hits = sum(1 for t in toks if _STAGE.search(t))
    return hits >= max(1, len(toks) - 1)


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    if a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a):
        r = max(r, 0.8)
    return r


def _contained(a: str, b: str) -> bool:
    if not a or not b or a == b:
        return False
    return a in b or b in a


def _dedup_segments(segs: list[dict], time_win: float = 4.0) -> list[dict]:
    segs = sorted(segs, key=lambda s: (s["start"], -len(s["text"])))
    out: list[dict] = []
    for s in segs:
        sn = _norm(s["text"])
        merged = False
        for o in out[::-1]:
            if s["start"] - o["start"] > time_win:
                break
            on = _norm(o["text"])
            if _sim(sn, on) >= 0.80 or _contained(sn, on):
                if len(sn) > len(on):
                    o["text"] = s["text"]
                    o["start"] = min(o["start"], s["start"])
                merged = True
                break
        if not merged:
            out.append(s)
    out.sort(key=lambda s: s["start"])
    return out


def _absorb_fragments(segs: list[dict], time_win: float = 4.0) -> list[dict]:
    segs = sorted(segs, key=lambda s: s["start"])
    drop = [False] * len(segs)
    norm = [_norm(s["text"]) for s in segs]
    for i, n in enumerate(norm):
        if drop[i] or not n or len(n.split()) > 3:
            continue
        for j in range(len(segs)):
            if j == i or drop[j] or not norm[j] or norm[j] == n:
                continue
            if abs(segs[j]["start"] - segs[i]["start"]) > time_win:
                continue
            nj = norm[j]
            if len(nj) > len(n) and (nj.startswith(n) or nj.endswith(n) or n in nj):
                drop[i] = True
                break
    return [s for i, s in enumerate(segs) if not drop[i]]


def _strip_lead_adlibs(segs: list[dict]) -> list[dict]:
    first_real = None
    for i, s in enumerate(segs):
        if not _is_pure_vocal(s["text"]) and not _is_label(s["text"]):
            first_real = i
            break
    if first_real is None or first_real == 0:
        return segs
    return segs[first_real:]


def _gemini_window(client, genai, clip, sr, sysp, model, max_tokens=900):
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, clip, sr, format="WAV")
    cfg_kw = dict(system_instruction=sysp, temperature=0.0,
                  max_output_tokens=max_tokens)
    # flash supports thinking_budget=0; pro REJECTS 0 (requires thinking) and
    # its thinking tokens count against max_output_tokens → give it room.
    if "flash" in model:
        cfg_kw["thinking_config"] = genai.types.ThinkingConfig(thinking_budget=0)
    else:
        cfg_kw["thinking_config"] = genai.types.ThinkingConfig(thinking_budget=128)
        cfg_kw["max_output_tokens"] = max_tokens + 1024
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                genai.types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
                genai.types.Part.from_text(text="Transcribi este fragmento."),
            ],
            config=genai.types.GenerateContentConfig(**cfg_kw),
        )
        return [l for l in (resp.text or "").splitlines() if l.strip()]
    except Exception as e:
        logger.warning("[ROTOR] gemini window failed: %s", e)
        return []


def gemini_pro_chunked_segments(
    audio_path: str, artist: str, song: str, language: str | None = None,
    *, model: str = "gemini-2.5-pro", chunk_s: float = 30.0,
    overlap_s: float = 6.0,
) -> list[dict] | None:
    """gemini-2.5-pro chunked-primary → [{start,end,text}] with per-line timing.

    Tiles the WHOLE audio 0..dur with no gaps (so a repeated chorus / long
    outro is never skipped). Each window returns sung lines with a RELATIVE
    [mm:ss.s] timestamp which we map to absolute (window_start + rel). Seam
    de-dup by time+text, drop stage-directions/chatter, strip phantom lead-in
    ad-libs. Returns None (declines) on no genai client / empty output.
    Never raises. Onset-snap is applied by the caller."""
    try:
        import librosa
        import pipeline
        from google import genai

        client = pipeline._get_genai_client()
        if client is None:
            logger.info("[ROTOR] gemini-pro: no genai client — declining")
            return None
        sr = 22050
        y, _sr = librosa.load(audio_path, sr=sr, mono=True)
        dur = len(y) / sr
        if dur < 1.0:
            return None
        who = f" ({artist} — {song})" if artist else ""

        raw_segs: list[dict] = []
        t = 0.0
        step = chunk_s - overlap_s
        while t < dur - 0.5:
            c0, c1 = t, min(dur, t + chunk_s)
            clip = y[int(c0 * sr):int(c1 * sr)]
            sysp = _SYS_TS.format(dur=(c1 - c0), who=who)
            lines = _gemini_window(client, genai, clip, sr, sysp, model)
            n = len([l for l in lines if l.strip()])
            for i, line in enumerate(lines):
                rel, text = _parse_ts_line(line)
                if not text:
                    continue
                if _CHATTER.match(text) and len(text.split()) <= 3:
                    continue
                if _is_label(text):
                    continue
                if _is_stage_direction(text):
                    continue
                if rel is not None:
                    abs_start = c0 + min(rel, c1 - c0)
                else:
                    abs_start = c0 + (i + 0.5) / max(1, n) * (c1 - c0)
                raw_segs.append({"start": round(abs_start, 3),
                                 "end": round(abs_start + 2.0, 3),
                                 "text": text})
            t += step

        if not raw_segs:
            return None
        segs = _dedup_segments(raw_segs)
        segs = _absorb_fragments(segs)
        segs = _strip_lead_adlibs(segs)
        segs.sort(key=lambda s: s["start"])
        # set ends to next start (non-overlapping), cap tail
        for i in range(len(segs) - 1):
            segs[i]["end"] = round(
                min(segs[i]["start"] + 6.0,
                    max(segs[i]["start"] + 0.5, segs[i + 1]["start"] - 0.05)), 3)
        if segs:
            segs[-1]["end"] = round(min(dur, segs[-1]["start"] + 4.0), 3)
        return segs or None
    except Exception as e:
        logger.warning("[ROTOR] gemini_pro_chunked_segments declined: %s", e)
        return None


# ── stem / onset snap helpers ───────────────────────────────────────────
def _vocal_regions_for(audio_path: str):
    """Best-effort isolated-vocal regions for `audio_path`. Returns (regions,
    stem_path|None). Empty list on any failure — snap then becomes a no-op."""
    try:
        import vocal_sep
        stem = vocal_sep.separate_vocals(audio_path)
    except Exception as e:
        logger.warning("[ROTOR] vocal_sep declined: %s", e)
        stem = None
    if not stem:
        return [], None
    try:
        import anchor_align
        return (anchor_align.vocal_regions(stem) or []), stem
    except Exception as e:
        logger.warning("[ROTOR] vocal_regions declined: %s", e)
        return [], stem


def _snap(segs: list[dict], vocal_regions) -> list[dict]:
    if not vocal_regions:
        return segs
    try:
        import forced_align
        return forced_align.snap_starts_to_vocal_onsets(
            segs, vocal_regions, max_snap_s=2.0)
    except Exception as e:
        logger.warning("[ROTOR] onset-snap declined: %s", e)
        return segs


# ── first-line + run-on guards (deterministic, source-agnostic) ─────────
def _word_count(seg: dict) -> int:
    ws = seg.get("words")
    if isinstance(ws, list) and ws:
        return len(ws)
    return len((seg.get("text") or "").split())


def drop_spurious_intro(segs: list[dict], *, min_gap_s: float = 8.0,
                        max_frag_words: int = 4) -> list[dict]:
    """Drop a spurious early FIRST line: a short fragment isolated far before
    the song body. whisper-1 run on the ISOLATED vocal stem occasionally
    transcribes a faint intro shout/crowd/ad-lib (e.g. '¿Quién es?' at 0:16
    when the verse really starts at 0:35) as the first line — it then drives a
    wrong 'song starts at 0:16' banner and shows a colgada line. Rotor renders
    the verse cleanly at its real onset; we match that by dropping the stray
    fragment.

    CONSERVATIVE — only fires when ALL hold, so a song that legitimately opens
    with a short early line + a real gap is left alone:
      • there are ≥3 lines (never empties the result),
      • line[0] is short (≤ max_frag_words words),
      • the gap from line[0] to line[1] is ≥ min_gap_s.
    Never raises."""
    try:
        if not segs or len(segs) < 3:
            return segs
        s0, s1 = segs[0], segs[1]
        gap = float(s1.get("start", 0.0)) - float(s0.get("start", 0.0))
        if gap >= min_gap_s and _word_count(s0) <= max_frag_words:
            logger.info("[ROTOR] dropping spurious intro fragment @%.1fs (gap %.1fs to body): %r",
                        float(s0.get("start", 0.0)), gap, (s0.get("text") or "")[:40])
            return segs[1:]
        return segs
    except Exception as e:
        logger.warning("[ROTOR] drop_spurious_intro declined: %s", e)
        return segs


_CLAUSE_END_CH = ("?", "!", ".", ",", ";", ":")


def _clean_join(words: list[dict]) -> str:
    txt = " ".join((w.get("word") or "").strip() for w in words)
    txt = re.sub(r"\s+([?!.,;:])", r"\1", txt)   # no space before punctuation
    return re.sub(r"\s{2,}", " ", txt).strip()


def split_run_on_lines(segs: list[dict], *, max_words: int = 8,
                       max_dur_s: float = 5.5, min_chunk: int = 3) -> list[dict]:
    """Split over-long 'run-on' lines into Rotor-length karaoke lines, breaking
    at clause boundaries (?!.,; or a ¿/¡ opener) and re-timing each sub-line
    from its OWN words' real timestamps.

    `_llm_segment_words` (Gemini) is non-deterministic about line length: it
    sometimes packs 3+ Rotor lines into one ('¿Quién sos? ¿Cómo sos? ¿Cuándo
    venís? ¿Cuándo llegaste?…'). This deterministic pass guarantees short,
    well-placed lines regardless of the LLM's mood. A line is only split when
    it exceeds the word OR duration budget AND has enough words to split into
    two ≥min_chunk pieces; lines without word stamps are passed through
    untouched. Never raises."""
    try:
        out: list[dict] = []
        for s in segs or []:
            words = s.get("words")
            dur = float(s.get("end", 0.0)) - float(s.get("start", 0.0))
            if (not isinstance(words, list) or len(words) < 2 * min_chunk
                    or (_word_count(s) <= max_words and dur <= max_dur_s)):
                out.append(s)
                continue
            chunks = _pack_words(words, max_words=max_words, min_chunk=min_chunk)
            if len(chunks) <= 1:
                out.append(s)
                continue
            for ch in chunks:
                out.append({"start": float(ch[0].get("start", s.get("start", 0.0))),
                            "end": float(ch[-1].get("end", s.get("end", 0.0))),
                            "text": _clean_join(ch), "words": ch})
        return out
    except Exception as e:
        logger.warning("[ROTOR] split_run_on_lines declined: %s", e)
        return segs


def _pack_words(words: list[dict], *, max_words: int, min_chunk: int) -> list[list[dict]]:
    """Greedily pack words into ≤max_words lines, cutting at the latest clause
    boundary that keeps the line within budget (falling back to a hard cut)."""
    breaks: set[int] = set()           # break AFTER index i
    for i, w in enumerate(words):
        t = (w.get("word") or "").strip()
        if t.endswith(_CLAUSE_END_CH):
            breaks.add(i)
        if i > 0 and t[:1] in ("¿", "¡"):
            breaks.add(i - 1)          # break BEFORE a clause opener
    n = len(words)
    lines: list[list[dict]] = []
    start = 0
    while start < n:
        hi = min(n - 1, start + max_words - 1)
        if hi >= n - 1:                # remainder fits in one line
            lines.append(words[start:])
            break
        cut = None
        for b in range(hi, start + min_chunk - 2, -1):
            if b in breaks:
                cut = b
                break
        if cut is None:
            cut = hi                   # forced cut at the word budget
        lines.append(words[start:cut + 1])
        start = cut + 1
    if len(lines) >= 2 and len(lines[-1]) < min_chunk:   # absorb tiny tail
        lines[-2] = lines[-2] + lines[-1]
        lines.pop()
    return lines


# ── acoustic re-timing (fix whisper-1 / gemini word-timestamp DRIFT) ─────
def _lines_text(segs) -> str:
    """Newline-joined line text — feeds the forced aligner the exact line
    structure we want acoustically re-timed."""
    return "\n".join((s.get("text") or "").strip() for s in (segs or [])
                     if (s.get("text") or "").strip())


def _looks_drifty(segs, regions, *, out_of_voice_frac: float = 0.35) -> bool:
    """Do the line onsets look like whisper-1's word timing DRIFTED off the
    audio? whisper-1 stamps are DTW-interpolated (±500ms) and on some songs
    accumulate multi-second lag the ≤2s onset-snap can't fix. A tight transcript
    has nearly all line starts on a voiced span; a drifty one strands many in
    instrumental gaps, worsening toward the end.

    True if the fraction of starts OUTSIDE any vocal region exceeds
    `out_of_voice_frac`, OR the last third is materially worse than the first
    (monotonic lag growth). Needs VAD regions — returns False when absent (can't
    measure → don't pay for re-alignment). Never raises."""
    try:
        if not regions or not segs or len(segs) < 6:
            return False
        import anchor_align
        out = [not anchor_align._in_voice(float(s.get("start", 0.0)), regions)
               for s in segs]
        n = len(out)
        frac = sum(out) / n
        third = max(1, n // 3)
        first = sum(out[:third]) / third
        last = sum(out[-third:]) / third
        drifty = frac >= out_of_voice_frac or (last - first) >= 0.30
        if drifty:
            logger.info("[ROTOR] drift detected (%.0f%% of onsets off-voice, "
                        "first⅓=%.0f%% last⅓=%.0f%%) → acoustic re-time",
                        frac * 100, first * 100, last * 100)
        return drifty
    except Exception as e:
        logger.warning("[ROTOR] _looks_drifty declined: %s", e)
        return False


def anchor_first_line_to_voice(segs: list[dict], regions, *,
                               min_gap_s: float = 4.0) -> list[dict]:
    """Re-anchor a first line that the aligner stranded BEFORE the song's real
    vocal onset. Forced-align sometimes dumps line 1 at ~0s on tracks with a
    long instrumental intro (measured: "Nada Fue" line 1 placed at 0.15s when
    singing starts at ~39s). The internal ≤2s onset-snap can't reach that far.

    If line 1 starts ≥min_gap_s before the FIRST voiced region AND ≥min_gap_s
    before line 2 (i.e. it's stranded, not a legit early line), pull its start
    to the first vocal onset the VAD map already knows. Conservative + never
    raises."""
    try:
        if not segs or not regions:
            return segs
        first_voice = float(regions[0][0])
        s0 = float(segs[0].get("start", 0.0))
        s1 = float(segs[1].get("start", first_voice)) if len(segs) > 1 else first_voice
        if (first_voice - s0) >= min_gap_s and (s1 - s0) >= min_gap_s:
            new0 = round(min(first_voice, s1 - 0.5), 3)
            if new0 > s0:
                logger.info("[ROTOR] re-anchoring stranded first line %.2fs → %.2fs "
                            "(first voice @%.2fs)", s0, new0, first_voice)
                out = dict(segs[0])
                out["start"] = new0
                if out.get("end") is not None and float(out["end"]) < new0:
                    out["end"] = round(new0 + 0.5, 3)
                return [out] + list(segs[1:])
        return segs
    except Exception as e:
        logger.warning("[ROTOR] anchor_first_line declined: %s", e)
        return segs


def _retime_acoustic(audio_path: str, segs: list[dict], regions,
                     language: str | None):
    """Replace drifty ASR timing with ACOUSTIC (text→audio) timing while keeping
    the clean line TEXT. Cascade: cureau chunked forced-align (±50ms target,
    crash-proof, covers dense lines whisperX drops) → whisperX-reconcile
    (<100ms, proven in the old cascade) → None (caller keeps native timing).
    Adopts a result only at ≥70% line coverage. Self-declining; never raises.
    All imports lazy (module boot safety).

    Opt-in: the whole acoustic re-time is gated by FORCED_ALIGNER_ENABLED so the
    default rotor behaviour (gemini-pro + onset-snap on lives) is unchanged."""
    if os.environ.get("FORCED_ALIGNER_ENABLED", "0").strip().lower() not in (
            "1", "true", "yes", "on"):
        return None
    text = _lines_text(segs)
    n = len([s for s in (segs or []) if (s.get("text") or "").strip()])
    if text.count("\n") < 3 or n < 4:          # need ≥4 lines to align
        return None
    # 1) cureau chunked forced alignment (gated by FORCED_ALIGNER_ENABLED;
    #    declines to None when the flag is off → falls through)
    try:
        import forced_align
        fa = forced_align.forced_align_lyrics_chunked(
            audio_path, text, vocal_regions=regions)
        if fa and len(fa) >= 0.7 * n:
            fa = anchor_first_line_to_voice(fa, regions)   # fix stranded line 1
            logger.info("[ROTOR] re-timed via cureau chunked FA (%d/%d lines)", len(fa), n)
            return fa                          # already onset-snapped internally
    except Exception as e:
        logger.warning("[ROTOR] chunked FA declined: %s", e)
    # 2) whisperX reconcile (timing from whisperX <100ms, text from our lines)
    try:
        import whisperx_transcribe
        import whisperx_reconcile
        wx = whisperx_transcribe.transcribe_whisperx(
            audio_path, language, lyrics_hint=text[:800])
        if wx:
            rec = whisperx_reconcile.reconcile(wx, text)
            if rec and len(rec) >= 0.7 * n:
                logger.info("[ROTOR] re-timed via whisperX-reconcile (%d/%d lines)", len(rec), n)
                return _snap(rec, regions)
    except Exception as e:
        logger.warning("[ROTOR] whisperX-reconcile declined: %s", e)
    return None


# ── public entrypoint ───────────────────────────────────────────────────
def _is_divergent_live(lrc_duration, audio_duration) -> bool:
    """Divergent live = the upload is >60s longer than the lrclib record.
    REQUIRES a real lrclib duration to compare. lrclib-ABSENT is NOT treated
    as divergent: a song without an lrclib match is usually a normal studio
    track with bad/missing metadata, and whisper-1 (tight onsets) handles it
    far better than gemini-pro (measured: whisper-1 de-offset ~0.4s vs
    gemini-pro ~2s on studio). Only genuinely extended/live recordings — where
    lrclib EXISTS but the audio is much longer — route to gemini-pro."""
    if lrc_duration is None or audio_duration is None:
        return False  # can't measure divergence → default to whisper-1 path
    try:
        return (float(audio_duration) - float(lrc_duration)) > 60.0
    except (TypeError, ValueError):
        return False


def run_rotor_pipeline(
    audio_path: str, *, artist: str = "", title: str = "",
    language: str | None = None, lrc_duration=None, audio_duration=None,
) -> tuple[list[dict], str] | None:
    """Route + run the hybrid Rotor pipeline. Returns (segments, source_path)
    where source_path is one of timing_sources.ROTOR_WHISPER1 /
    ROTOR_GEMINI_PRO, or None to DECLINE (caller falls back to the old
    cascade). NEVER raises.

      divergent live → gemini-2.5-pro chunked-primary
      otherwise      → whisper-1 word-stamps → pipeline._llm_segment_words

    Both then snap line starts to the vocal-stem onsets."""
    try:
        from timing_sources import ROTOR_GEMINI_PRO, ROTOR_WHISPER1

        divergent = _is_divergent_live(lrc_duration, audio_duration)

        # STUDIO / normal path defers to the OLD cascade by default (see the
        # block below). Decide this BEFORE separating the vocal stem so a
        # deferral costs nothing extra.
        studio_enabled = os.environ.get(
            "ROTOR_STUDIO_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
        if not divergent and not studio_enabled:
            logger.info("[ROTOR] studio/normal — deferring to old cascade "
                        "(ROTOR_STUDIO_ENABLED off)")
            return None

        regions, _stem = _vocal_regions_for(audio_path)

        if divergent:
            logger.info("[ROTOR] routing → gemini-2.5-pro chunked (divergent live; "
                        "audio=%s lrc=%s)", audio_duration, lrc_duration)
            segs = gemini_pro_chunked_segments(audio_path, artist, title, language)
            if not segs:
                logger.info("[ROTOR] gemini-pro declined — falling back to old cascade")
                return None
            segs = split_run_on_lines(drop_spurious_intro(segs))
            # gemini line timestamps are coarse — re-time acoustically (text→audio)
            # when forced-align is available; else snap. FA already snaps internally.
            retimed = _retime_acoustic(audio_path, segs, regions, language)
            segs = retimed if retimed else _snap(segs, regions)
            return (segs, ROTOR_GEMINI_PRO)

        # STUDIO / normal path (only reached when ROTOR_STUDIO_ENABLED is on —
        # see the deferral above). The OLD cascade (whisperX forced-aligned +
        # lrclib/reference reconcile) is measured to already be near-Rotor on
        # studio tracks AND keeps Rotor-style line grouping, whereas whisper-1's
        # DTW-interpolated word timing drifts and over-segments here (colliding
        # line onsets break the karaoke highlighter). Kept behind the flag for
        # A/B only; the rotor pipeline's real win is divergent lives (above).
        logger.info("[ROTOR] routing → whisper-1 + _llm_segment_words (studio/normal)")
        segs = whisper1_segments(audio_path, language)
        if not segs:
            logger.info("[ROTOR] whisper-1 declined — falling back to old cascade")
            return None
        try:
            import pipeline
            segs = pipeline._llm_segment_words(
                segs, audio_path=audio_path, artist=artist, song=title)
        except Exception as e:
            logger.warning("[ROTOR] _llm_segment_words raised: %s — using raw whisper-1", e)
        if not segs:
            return None
        segs = split_run_on_lines(drop_spurious_intro(segs))
        # whisper-1 timing drifts on some songs (DTW interpolation); when the
        # onsets look drifty re-time acoustically, else just snap.
        if _looks_drifty(segs, regions):
            retimed = _retime_acoustic(audio_path, segs, regions, language)
            segs = retimed if retimed else _snap(segs, regions)
        else:
            segs = _snap(segs, regions)
        return (segs, ROTOR_WHISPER1)
    except Exception as e:
        logger.warning("[ROTOR] run_rotor_pipeline declined (unexpected): %s", e)
        return None
