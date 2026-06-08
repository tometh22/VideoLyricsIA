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
        import librosa
        import soundfile as sf
        from openai import OpenAI

        client = OpenAI()
        y, _sr = librosa.load(audio_path, sr=16000, mono=True)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        try:
            try:
                sf.write(tmp.name, y, 16000, format="MP3")
            except Exception:
                # soundfile may lack the mp3 encoder; fall back to ogg/opus
                sf.write(tmp.name, y, 16000, format="OGG", subtype="OPUS")
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
            if not txt or _W1_HALLUCINATION.search(txt):
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
            if not text or _W1_HALLUCINATION.search(text):
                continue  # drop hallucination span (text + its words)
            st = float(getattr(s, "start", 0.0) or 0.0)
            en = float(getattr(s, "end", st) or st)
            segs.append({"start": st, "end": en, "text": text,
                         "words": _word_dicts(st, en)})
        return segs or None
    except Exception as e:
        logger.warning("[ROTOR] whisper1_segments declined: %s", e)
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
        regions, _stem = _vocal_regions_for(audio_path)

        if divergent:
            logger.info("[ROTOR] routing → gemini-2.5-pro chunked (divergent live; "
                        "audio=%s lrc=%s)", audio_duration, lrc_duration)
            segs = gemini_pro_chunked_segments(audio_path, artist, title, language)
            if not segs:
                logger.info("[ROTOR] gemini-pro declined — falling back to old cascade")
                return None
            segs = _snap(segs, regions)
            return (segs, ROTOR_GEMINI_PRO)

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
        segs = _snap(segs, regions)
        return (segs, ROTOR_WHISPER1)
    except Exception as e:
        logger.warning("[ROTOR] run_rotor_pipeline declined (unexpected): %s", e)
        return None
