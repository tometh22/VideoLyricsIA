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
    "1. SOLO lo que se OYE en estos segundos: no completes de memoria un\n"
    "   estribillo que no suene en el audio.\n"
    "2. Si una frase se canta VARIAS veces — la repita el cantante O LA COREE\n"
    "   EL PUBLICO — escribila TODAS las veces que suene, una linea por\n"
    "   repeticion. El publico coreando TAMBIEN es letra: transcribilo.\n"
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
                       min_gap_s: float = 8.0) -> list[tuple[float, str]]:
    """Operator-reported (staging, Nada Fue): at 0:00 the crowd faintly
    pre-sings the chorus and Gemini transcribes it — a line isolated
    >12 s before the song's body, whose text repeats later (it IS the
    chorus). Rotor doesn't show it; neither do we. Pure."""
    # CLUSTER pre-canción (gen-9): el dual-pass enriquece el pre-canto
    # débil del público en VARIAS líneas (0.5/5/31s) — el drop línea-a-
    # línea con gap>8s no lo caza. Regla general: la primera línea de
    # texto ÚNICO (no se repite después = verso) marca el inicio real;
    # todo lo anterior que se repita más adelante Y esté a >8s de ese
    # inicio es pre-canto → fuera. Un tema que ARRANCA con su estribillo
    # (gap <8s al cuerpo) no pierde nada.
    def _repeats_later(idx):
        ni = _norm(rows[idx][1])
        return any(ni in _norm(r[1]) or _norm(r[1]) in ni
                   for r in rows[idx + 1:])

    first_unique = next((i for i in range(len(rows))
                         if not _repeats_later(i)), 0)
    if first_unique > 0:
        body_ts = rows[first_unique][0]
        keep = [r for i, r in enumerate(rows)
                if i >= first_unique
                or not (_repeats_later(i) and body_ts - r[0] > min_gap_s)]
        rows = keep
    while len(rows) >= 2 and rows[1][0] - rows[0][0] > min_gap_s:
        n0 = _norm(rows[0][1])
        if any(n0 in _norm(r[1]) or _norm(r[1]) in n0 for r in rows[1:]):
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


def clean_libretto(items: list[tuple]) -> list[str]:
    """Window-seam artifacts → ordered clean line texts. Drops stage
    directions / pure-vocal intros / chatter / counts; strips
    '(Público cantando) ' prefixes. Pure — unit-testable.

    Dedup is WINDOW-AWARE (root-cause fix): a crowd chorus repeats the
    SAME phrase 8× a few seconds apart — a time-only dedup fused real
    repetitions into one line (measured: block 1:00-1:16 reduced to a
    single line). A duplicate is a SEAM only when it comes from a
    DIFFERENT window within the overlap zone, or from the same window at
    a near-identical timestamp (Gemini stutter). Same window, distinct
    timestamps, same text = the crowd really repeating → KEEP ALL.
    Items: (ts, text) or (ts, text, window_idx)."""
    rows: list[tuple[float, str, int]] = []
    for it in sorted(items, key=lambda x: x[0]):
        ts, raw = it[0], it[1]
        win = it[2] if len(it) > 2 else 0
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
            pts, pt, pwin = rows[j]
            if ts - pts > OVERLAP_S + 4.0:
                break
            seam = (win != pwin and ts - pts <= OVERLAP_S + 4.0) or \
                   (win == pwin and ts - pts < 1.5)
            if not seam:
                continue
            pn = _norm(pt)
            if n in pn or SequenceMatcher(None, n, pn).ratio() >= 0.80:
                merged = True
                break
            if pn in n:
                rows[j] = (pts, t, pwin)
                merged = True
                break
            # PARTIAL seam (job 400: "no quisiste fallar, aprendí la
            # diferencia" + "ahora aprendí la diferencia entre el juego
            # y el azar" overlapping by 3 s): window A cut the phrase,
            # window B completed it — whole-line similarity misses it.
            # A shared chunk of ≥14 normalized chars across windows in
            # the overlap zone = the same sung moment → keep the longer.
            if win != pwin and len(n) >= 14 and len(pn) >= 14:
                shared = any(n[k:k + 14] in pn
                             for k in range(0, len(n) - 13, 4))
                if shared:
                    if len(n) > len(pn):
                        rows[j] = (pts, t, pwin)
                    merged = True
                    break
        if not merged:
            rows.append((ts, t, win))
    # pure-vocal lines before the first lexical line
    first = next((i for i, r in enumerate(rows)
                  if not _PURE_VOCAL.match(r[1])), 0)
    rows = rows[first:]
    rows = drop_phantom_intro(rows)
    return [r[1] for r in rows]


def _one_pass(client, genai, y, sr, dur, who, win_base: int = 0,
              job_id: str | None = None):
    """Una pasada de ventanas Gemini → items (ts, texto, win)."""
    items: list[tuple[float, str, int]] = []
    t = 0.0
    fails = 0
    wi = win_base
    while t < dur - 0.5:
        c0, c1 = t, min(dur, t + CHUNK_S)
        clip = y[int(c0 * sr):int(c1 * sr)]
        recorder = None
        provider_completed = False
        provider_recorded = False
        try:
            import io as _io
            import soundfile as sf
            buf = _io.BytesIO()
            sf.write(buf, clip, sr, format="WAV")
            from pipeline import _call_with_timeout
            if job_id:
                from provenance import record_ai_call

                recorder = record_ai_call(
                    job_id=job_id,
                    step="performance_text",
                    tool_name=MODEL,
                    tool_provider="google_vertex",
                    prompt=_SYS_TS.format(dur=c1 - c0, who=who),
                    input_data_types=["audio_chunk"],
                )
            resp = _call_with_timeout(
                lambda: client.models.generate_content(
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
                        thinking_config=genai.types.ThinkingConfig(
                            thinking_budget=0 if "flash" in MODEL else 128),
                    ),
                ),
                45.0,
                label=f"perf-text chunk {c0:.0f}-{c1:.0f}s",
            )
            provider_completed = True
            raw_text = str(resp.text or "")
            from recognition_provenance import record_completed
            record_completed(
                family=f"google/{MODEL}",
                events=([{"text": raw_text}] if raw_text else []),
                kind="text",
                view="performance_audio_window",
                transformation=(
                    f"performance_text_raw:window={wi};start={c0:.3f};end={c1:.3f}"
                ),
            )
            provider_recorded = True
            lines = [line for line in raw_text.splitlines() if line.strip()]
            if recorder:
                recorder.finish(response_summary=(
                    f"performance_text window={c0:.1f}-{c1:.1f}s "
                    f"lines={len(lines)}"
                ))
        except Exception as e:
            if provider_completed and not provider_recorded:
                from recognition_provenance import record_completed
                record_completed(
                    family=f"google/{MODEL}",
                    events=[],
                    kind="text",
                    view="performance_audio_window",
                    transformation=(
                        f"performance_text_unreadable:window={wi};"
                        f"start={c0:.3f};end={c1:.3f}"
                    ),
                )
            if recorder:
                recorder.finish(response_summary=(
                    f"error: performance_text window={c0:.1f}-{c1:.1f}s: "
                    f"{str(e)[:300]}"
                ))
            logger.warning("[PERF-TEXT] window %.0f-%.0f failed: %s", c0, c1, e)
            lines = []
            fails += 1
            if fails >= 4:
                return items
        for line in lines:
            rel, text = parse_ts_line(line)
            if text:
                items.append((c0 + min(rel if rel is not None else 0.0,
                                       c1 - c0), text, wi))
        t += CHUNK_S - OVERLAP_S
        wi += 1
    return items


def transcribe_performance(audio_path: str, artist: str = "",
                           title: str = "",
                           reference: str = "",
                           job_id: str | None = None) -> list[str] | None:
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
        items = _one_pass(client, genai, y, sr, dur, who, job_id=job_id)
        if os.environ.get("PERF_TEXT_PASSES", "2").strip() == "2":
            # DUAL-PASS UNION (la lección de las 7 generaciones): la
            # varianza corrida-a-corrida de Gemini en zonas de público es
            # estructural (59/71/99/104/106 líneas). La unión de DOS
            # pasadas mata la varianza en el origen: si una pasada trae el
            # coro y la otra no, la unión lo trae. El dedup por ventana ya
            # distingue costura (otra ventana, mismo momento) de
            # repetición real — la pasada 2 usa índices de ventana
            # corridos (+1000) así sus líneas dedupean contra la pasada 1
            # como costuras del mismo momento.
            items2 = _one_pass(
                client, genai, y, sr, dur, who,
                win_base=1000, job_id=job_id,
            )
            # semántica de unión (gate-1 del dual-pass): la pasada 1 es la
            # BASE; la 2 solo aporta momentos donde la 1 no tiene NADA en
            # ±2s — variantes del mismo momento no se duplican (116 líneas
            # con 64 saltos en la unión ingenua).
            base_ts = sorted(t0 for t0, _, _ in items)
            import bisect as _bi
            extra = []
            for it2 in items2:
                k = _bi.bisect_left(base_ts, it2[0])
                near = min((abs(base_ts[j] - it2[0])
                            for j in (k - 1, k) if 0 <= j < len(base_ts)),
                           default=99.0)
                if near > 2.0:
                    extra.append(it2)
            if extra:
                logger.info("[PERF-TEXT] dual-pass: +%d momentos que la "
                            "pasada 1 no tenía", len(extra))
            items = sorted(items + extra, key=lambda x: x[0])
        out = clean_libretto(items)
        out = drop_hallucinated_lines(out, reference)
        logger.info("[PERF-TEXT] libreto: %d líneas de %.0fs de audio (%s)",
                    len(out), dur, MODEL)
        return out if len(out) >= 8 else None
    except Exception as e:
        logger.warning("[PERF-TEXT] declined: %s", e)
        return None
