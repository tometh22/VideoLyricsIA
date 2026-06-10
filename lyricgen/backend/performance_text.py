"""Performance libretto — transcribe what THIS recording actually performs.

The visual-test lesson (Nada Fue live, 2026-06-10): a live repeats
verses/choruses more times than the reference text lists, and the crowd
sings its own variants. No timing engine can fix structurally-wrong
text. Rotor's recipe is transcribe-the-performance + align; this module
is our transcriber: Gemini chunked over the whole audio (validated R&D:
93% coverage on dense lives; 83 lines / 71 s / ~$0.03 on the benchmark
live), returning the ORDERED line texts. Timing is ctc_align's job.

Consumer: main._maybe_ctc_retime — when the CTC engine declines the
cascade's text for STRUCTURAL MISMATCH (>10% lines skipped), it asks for
this libretto and retries. Gated CTC_ALIGN_PERF_TEXT (default OFF).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from difflib import SequenceMatcher

logger = logging.getLogger("uvicorn.error")

_TRUE = {"1", "true", "yes", "on"}
MODEL = os.environ.get("PERF_TEXT_MODEL", "gemini-2.5-pro")
CHUNK_S = 30.0
OVERLAP_S = 6.0

_SYS_TS = (
    "Sos un transcriptor experto de letras en español, nivel Rotor.\n"
    "Te doy un FRAGMENTO de audio de ~{dur:.0f}s de una cancion{who} EN VIVO.\n"
    "Transcribi EXACTAMENTE lo que se canta en ESTE fragmento, una frase por linea,\n"
    "y al inicio de cada linea pone el timestamp RELATIVO (desde el comienzo de ESTE\n"
    "fragmento, no de la cancion) en formato [mm:ss.s].\n\n"
    "FORMATO EXACTO de cada linea: [mm:ss.s] texto de la frase\n"
    "Ejemplo:\n[00:01.2] Tengo una mala noticia\n[00:03.0] No fue de casualidad\n\n"
    "REGLAS DURAS:\n"
    "1. SOLO lo que escuchas en estos segundos. NO completes el estribillo entero,\n"
    "   NO repitas en loop una frase que no se repite en el audio.\n"
    "2. SI transcribi TODAS las repeticiones que realmente se cantan.\n"
    "3. NO cortes una frase a la mitad: escribila completa con el timestamp de cuando EMPIEZA.\n"
    "4. Ad-libs/gritos como se oyen ('Oh, oh', '¡papa!'). Si solo hay musica/aplausos NO escribas nada.\n"
    "5. NO inventes palabras: si no entendes, lo mas parecido foneticamente.\n"
    "6. Cada linea DEBE empezar con su [mm:ss.s]. SIN numeracion, SIN comentarios extra."
)

_TS_RE = re.compile(r"^\s*[\[(]?\s*(\d{1,2}):(\d{2}(?:\.\d{1,2})?)\s*[\])]?\s*(.*)$")
_JUNK = re.compile(r"^[\[\]()0-9:.\s¡!¿?]*$")
# Stage labels Gemini emits despite the prompt — observed leaking into a
# real staging video as displayed lines: "Público cantando y aplaudiendo",
# "silence" (English!), "Aplausos y ovación". Match generously: anything
# that DESCRIBES the room instead of quoting a lyric.
_LABEL = re.compile(
    r"^\(?\s*(grito|silencio|silence|instrumental|"
    r"aplausos?\b[^)]*|m[uú]sica\b[^)]*|p[uú]blico\b[^)]*|"
    r"ovaci[oó]n[^)]*|multitud\b[^)]*|crowd\b[^)]*|audiencia\b[^)]*)\s*\)?$",
    re.I)
_CHATTER = re.compile(r"^(gracias|chau|chao|adi[oó]s|buenas noches|muchas gracias|che)\b", re.I)
_PURE_VOCAL = re.compile(
    r"^[¡!\s]*((o+h+|a+h+|e+h+|u+h+|na+|la+|ja+|je+|wo+|yeah|hey|uh)[\s,.!¡]*)+$", re.I)


def _norm(t: str) -> str:
    return re.sub(r"\W+", "", t.lower())


def parse_ts_line(line: str):
    """(rel_seconds|None, text) — strips [mm:ss.s] and bracket junk. Pure."""
    raw = line.strip(" -•\t")
    m = _TS_RE.match(raw)
    if m:
        rel = int(m.group(1)) * 60 + float(m.group(2))
        text = m.group(3).strip(" -•\t[]()")
    else:
        rel, text = None, raw.strip(" -•\t[]()")
    if _JUNK.match(text) or not text:
        return rel, ""
    return rel, text


def drop_phantom_intro(rows: list[tuple[float, str]],
                       min_gap_s: float = 12.0) -> list[tuple[float, str]]:
    """Operator-reported (staging, Nada Fue): at 0:00 the crowd faintly
    pre-sings the chorus and Gemini transcribes it — a line isolated
    >12 s before the song's body, whose text repeats later (it IS the
    chorus). Rotor doesn't show it; neither do we. Pure."""
    while len(rows) >= 2 and rows[1][0] - rows[0][0] > min_gap_s:
        n0 = _norm(rows[0][1])
        if any(n0 in _norm(t) or _norm(t) in n0 for _, t in rows[1:]):
            rows = rows[1:]
            continue
        break
    return rows


def drop_hallucinated_lines(texts: list[str], reference: str,
                            min_words: int = 6,
                            min_overlap: float = 0.35) -> list[str]:
    """Operator-reported: Gemini INVENTED a plausible verse ('el error es
    todo lo que no hicimos por temor') that is never sung. Long
    lyric-like lines (>= min_words) whose content words barely appear in
    the reference lyrics are hallucinations → drop. Short lines and
    ad-libs ('¡Mirá dónde estamos, papá!') stay — show moments are
    real and wanted even though the reference doesn't list them. Pure."""
    if not reference:
        return texts
    ref_vocab = {w for w in re.findall(r"[a-záéíóúñü']+", reference.lower())
                 if len(w) > 3}
    if len(ref_vocab) < 10:
        return texts
    out = []
    for t in texts:
        words = [w for w in re.findall(r"[a-záéíóúñü']+", t.lower())
                 if len(w) > 3]
        if len(t.split()) >= min_words and words:
            overlap = sum(1 for w in words if w in ref_vocab) / len(words)
            if overlap < min_overlap:
                logger.info("[PERF-TEXT] drop alucinación (overlap %.0f%%): %s",
                            100 * overlap, t[:60])
                continue
        out.append(t)
    return out


def clean_libretto(items: list[tuple[float, str]]) -> list[str]:
    """Window-seam artifacts → ordered clean line texts. Drops stage
    directions / pure-vocal intros / chatter / counts; strips
    '(Público cantando) ' prefixes; merges contained duplicates within
    6 s (a fragment and its fuller version from overlapping windows).
    Pure — unit-testable."""
    rows: list[tuple[float, str]] = []
    for ts, raw in sorted(items, key=lambda x: x[0]):
        t = raw.strip()
        if ")" in t[:25]:
            t = re.sub(r"^[^)]*\)\s*", "", t).strip()
        if not t or _LABEL.match(t):
            continue
        if _CHATTER.match(t.lstrip("¡¿!? ")) and len(t.split()) <= 3:
            continue
        if len(re.sub(r"[^a-záéíóúñüa-z]", "", t.lower())) < 6:
            continue
        n = _norm(t)
        merged = False
        for j in range(len(rows) - 1, -1, -1):
            pts, pt = rows[j]
            # 10 s window / 0.80 ratio: a seam duplicate slipped through at
            # 6 s / 0.85 (operator-reported: "aprendí la diferencia" ×2)
            if ts - pts > 10.0:
                break
            pn = _norm(pt)
            if n in pn or SequenceMatcher(None, n, pn).ratio() >= 0.80:
                merged = True
                break
            if pn in n:
                rows[j] = (pts, t)
                merged = True
                break
        if not merged:
            rows.append((ts, t))
    # pure-vocal lines before the first lexical line
    first = next((i for i, (_, t) in enumerate(rows)
                  if not _PURE_VOCAL.match(t)), 0)
    rows = rows[first:]
    rows = drop_phantom_intro(rows)
    return [t for _, t in rows]


def transcribe_performance(audio_path: str, artist: str = "",
                           title: str = "",
                           reference: str = "") -> list[str] | None:
    """Ordered line texts of what THIS audio performs. None on any
    failure (caller falls back). Cost ~$0.02-0.05/song, ~60-90 s."""
    try:
        import librosa
        import pipeline
        from google import genai

        client = pipeline._get_genai_client()
        if client is None:
            return None
        sr = 22050
        y, _ = librosa.load(audio_path, sr=sr, mono=True)
        dur = len(y) / sr
        who = f" ({artist} — {title})" if artist else ""
        items: list[tuple[float, str]] = []
        t = 0.0
        fails = 0
        while t < dur - 0.5:
            c0, c1 = t, min(dur, t + CHUNK_S)
            clip = y[int(c0 * sr):int(c1 * sr)]
            try:
                import soundfile as sf
                buf = io.BytesIO()
                sf.write(buf, clip, sr, format="WAV")
                resp = client.models.generate_content(
                    model=MODEL,
                    contents=[
                        genai.types.Part.from_bytes(
                            data=buf.getvalue(), mime_type="audio/wav"),
                        genai.types.Part.from_text(
                            text="Transcribi este fragmento."),
                    ],
                    config=genai.types.GenerateContentConfig(
                        system_instruction=_SYS_TS.format(dur=c1 - c0, who=who),
                        temperature=0.0,
                        max_output_tokens=900,
                        # pro exige thinking_budget>=128; flash acepta 0
                        thinking_config=genai.types.ThinkingConfig(
                            thinking_budget=0 if "flash" in MODEL else 128),
                    ),
                )
                lines = [l for l in (resp.text or "").splitlines() if l.strip()]
            except Exception as e:
                logger.warning("[PERF-TEXT] window %.0f-%.0f failed: %s", c0, c1, e)
                lines = []
                fails += 1
                if fails >= 4:
                    return None
            for line in lines:
                rel, text = parse_ts_line(line)
                if text:
                    items.append((c0 + min(rel if rel is not None else 0.0,
                                           c1 - c0), text))
            t += CHUNK_S - OVERLAP_S
        out = clean_libretto(items)
        out = drop_hallucinated_lines(out, reference)
        logger.info("[PERF-TEXT] libreto: %d líneas de %.0fs de audio (%s)",
                    len(out), dur, MODEL)
        return out if len(out) >= 8 else None
    except Exception as e:
        logger.warning("[PERF-TEXT] declined: %s", e)
        return None
