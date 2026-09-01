"""Rescate de huecos: re-transcribir los tramos donde el ASR quedó SORDO.

EL DEFECTO (job dcf773b5, "Rodando Por Ahí", 29-07-2026)
--------------------------------------------------------
El video salió con un hueco de 30,7 s (3:29 → 4:00) sin un solo cartel,
mientras el audio canta el estribillo tres veces (medido con separación de
voz: canto real en 214-229 s). Ni la recuperación de huecos de
`whisperx_reconcile` ni `repetition_reconcile` lo taparon, y no por un bug:
ambos colocan texto donde el ASR oyó algo, y **whisperX no oyó nada ahí**.

Lo confirma nuestra propia métrica, que reportó `zonas_sin_letra=0` con el
hueco en pantalla: `audio_coverage` mide contra las PALABRAS del ASR, así
que un punto sordo del ASR le resulta invisible por construcción. Esa
ceguera es deliberada (evita falsos positivos en pasajes instrumentales),
pero deja este caso sin detectar.

Un whisper-1 sobre el mismo audio SÍ transcribió esa zona (8 palabras en
210-217, 7 en 219-223). O sea: no es que no se pueda oír — es que ESE
motor, en ESA pasada, se lo comió.

QUÉ HACE
--------
Post-pass gateado: busca huecos grandes entre carteles (y la cola), recorta
una ventana del audio QUE INCLUYE CONTEXTO CANTADO A AMBOS LADOS, la
re-transcribe con whisper-1 pidiendo timestamps por palabra, se queda sólo
con las palabras que caen DENTRO del hueco, y emite líneas con timing REAL
(no interpolado).

DOS COSAS SON IMPRESCINDIBLES, las dos medidas sobre el hueco real
(209,4-240,2 s del job dcf773b5):

1. EL STEM DE VOZ, no la mezcla. Con la voz enterrada bajo la banda, el
   ASR no engancha:

       mezcla, 2 corridas   ->  1 palabra dentro del hueco (0,03/s)
       stem,   2 corridas   -> 16 palabras dentro del hueco (0,53/s)

   Las dos corridas de cada fuente dieron idéntico: no es varianza, es que
   sobre la mezcla no se oye. El stem ya lo computa y cachea la cascada
   (`vocal_sep.separate_vocals`), así que sale gratis.

2. CONTEXTO CANTADO A AMBOS LADOS. Un clip aislado del hueco devuelve el
   emoji de música o la alucinación de Amara.org; con canto reconocible
   alrededor, el modelo se ancla. Por eso el módulo NO recorta el hueco:
   recorta el hueco MÁS su vecindad y descarta después lo ya cubierto.

Sobre la MEZCLA hubo además corridas que devolvieron 379 palabras en 70 s
(5,4 palabras/segundo: imposible cantando) — bucles de patrón del propio
modelo. De ahí el filtro de densidad y de alucinación: la evidencia débil
se descarta en vez de pintarse en pantalla.

DEFENSAS
--------
- Ventana topeada (`GAP_RESCUE_CLIP_MAX_S`): clips largos hacen que los ASR
  entren en bucle de patrón (documentado en `_recover_gap_lyrics`).
- Filtro de alucinaciones reusando `pipeline._filter_whisper_hallucinations`
  + descarte de bucles de una sola palabra.
- Exige densidad mínima de palabras: un "uh" suelto no arma línea.
- Nunca pisa carteles existentes: las líneas nuevas se recortan al hueco.
- Tope de huecos por canción y kill switch. Never raises.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.gap_rescue")

_TRUE = ("1", "true", "yes", "on")

# Hueco mínimo para molestarse en re-transcribir. Bajado de 12 a 8 con dos
# casos reales: el hueco histórico de UMG en Hombre Lobo mide 10,6s (con
# 12,0s de canto según el VAD del stem) y quedaba justo debajo del umbral
# viejo; ídem los huecos de Mercedes Sosa / Mujer Amante del batch. Con el
# gate de VAD de abajo, bajar el umbral no arriesga: solo se rescatan
# huecos donde el stem CANTA.
_MIN_GAP_S = 8.0
# Contexto cantado a cada lado del hueco. Sin esto el ASR no engancha
# (ver la medición en el docstring del módulo).
_CONTEXT_S = 20.0
# Tope de la ventana completa (hueco + contexto) que se manda al ASR.
_CLIP_MAX_S = 120.0
# Márgenes: entrar un toque después del cartel previo y salir antes del
# siguiente, para no re-transcribir lo que ya está cubierto.
_PAD_S = 0.4
# Densidad mínima para aceptar el rescate de una ventana.
_MIN_WORDS = 3
_MIN_WORDS_PER_S = 0.25
# Máximo de huecos rescatados por canción.
_MAX_GAPS = 4
# Corte de líneas dentro de lo rescatado (mismos valores que el resto).
_LINE_GAP_S = 1.2
_LINE_MAX_S = 6.0
# Sanidad FÍSICA por línea rescatada. En Hombre Lobo el rescate emitió
# líneas de 0,2-0,5s con 5 palabras (16 palabras/segundo: nadie canta así)
# sobre el outro instrumental — alucinación de whisper en eco. Nada humano
# supera ~6 palabras/s sostenidas, y un cartel de <0,5s es un destello.
_MIN_LINE_S = 0.5
_MAX_WORDS_PER_S = 6.0
# Gate de VAD: un hueco se rescata sólo si el stem CANTA ahí, y cada línea
# rescatada debe solaparse con una región de voz. Es la misma señal que usa
# el guardrail voiced_gaps — acá previene en vez de sólo detectar.
_VAD_MIN_VOICED_S = 3.0
_VAD_LINE_OVERLAP = 0.3
# Rescate por MISMATCH: un cartel largo cuyo texto no suena a lo que el ASR
# oyó en su ventana es un cartel MANCHADO — el alineador untó pocas palabras
# sobre audio que canta otra cosa (Hombre Lobo: "En el fondo" estirado
# 6,8s con "el"=2,4s y "fondo"=3,0s sobre "Nunca me imaginé / Cantando /
# Para vos / En un mundo"). Su zona se re-transcribe y el cartel se
# REEMPLAZA por lo realmente cantado.
_MISMATCH_MAX_RATIO = 0.3
_MISMATCH_MIN_DUR_S = 4.0


def is_enabled() -> bool:
    return os.environ.get("GAP_RESCUE_ENABLED", "0").strip().lower() in _TRUE


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def find_gaps(segments: list[dict], audio_duration: float | None = None, *,
              min_gap_s: float = _MIN_GAP_S,
              include_leading: bool = False) -> list[tuple[float, float]]:
    """Huecos entre carteles (y la cola) más largos que `min_gap_s`.

    `include_leading` is reserved for audio-as-truth live results.  Normal
    studio songs often have long instrumental intros; probing every intro adds
    cost and false-positive pressure.  Live jobs enable it because a missing
    sustained opening word is otherwise invisible to ASR-word coverage.

    Puro y testeable — no mira el audio."""
    segs = sorted((s for s in (segments or []) if isinstance(s, dict)),
                  key=lambda s: _f(s.get("start")))
    if not segs:
        return []
    gaps: list[tuple[float, float]] = []
    if include_leading:
        first_start = _f(segs[0].get("start"))
        if first_start >= min_gap_s:
            gaps.append((0.0, first_start))
    for a, b in zip(segs, segs[1:]):
        ini, fin = _f(a.get("end")), _f(b.get("start"))
        if fin - ini >= min_gap_s:
            gaps.append((ini, fin))
    if audio_duration:
        fin_ultimo = max(_f(s.get("end")) for s in segs)
        if audio_duration - fin_ultimo >= min_gap_s:
            gaps.append((fin_ultimo, float(audio_duration)))
    return gaps


def _agrupar_en_lineas(words: list[dict]) -> list[list[dict]]:
    """Corta las palabras rescatadas en líneas cantables."""
    lineas: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            hueco = _f(w.get("start")) - _f(cur[-1].get("end"))
            largo = _f(w.get("end")) - _f(cur[0].get("start"))
            if hueco > _LINE_GAP_S or largo > _LINE_MAX_S:
                lineas.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lineas.append(cur)
    return lineas


def _raw_provider_words(response: object) -> tuple[list[object], list[dict]]:
    """Return source rows plus a durable pre-mapping representation."""
    from recognition_provenance import bounded_provider_string
    try:
        source = list(getattr(response, "words", None) or [])
    except Exception:
        return [], [{"raw": bounded_provider_string(response)}]
    raw: list[dict] = []
    for word in source:
        if isinstance(word, dict):
            try:
                raw.append(dict(word))
            except Exception:
                raw.append({"raw": bounded_provider_string(word)})
            continue
        try:
            dumped = word.model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            raw.append(dumped)
            continue
        values = {}
        for key in ("word", "start", "end"):
            try:
                values[key] = getattr(word, key)
            except Exception:
                pass
        raw.append(values or {"raw": bounded_provider_string(word)})
    return source, raw


def _transcribe_window(audio_path: str, ini: float, dur: float,
                       language: str | None = None,
                       job_id: str | None = None, *,
                       provenance_step: str = "gap_rescue") -> list[dict]:
    """Recorta [ini, ini+dur] y lo transcribe con whisper-1 pidiendo
    timestamps por PALABRA. Devuelve words en el marco temporal del audio
    COMPLETO (ya desplazadas). [] ante cualquier fallo."""
    import subprocess
    import tempfile
    fd, clip = tempfile.mkstemp(suffix=".mp3", prefix="genly_gap_")
    os.close(fd)
    recorder = None
    try:
        # ffmpeg directo: este módulo NO importa `pipeline` (300+ MB de
        # moviepy/librosa) sólo para recortar 30 s de audio.
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ss", str(ini), "-t", str(dur),
             "-ac", "1", "-b:a", "128k", "-loglevel", "error", clip],
            check=True, timeout=90,
        )
        if not os.path.exists(clip) or os.path.getsize(clip) == 0:
            return []
        from openai import OpenAI
        with open(clip, "rb") as f:
            kwargs = {
                "model": "whisper-1",
                "file": f,
                "response_format": "verbose_json",
                "timestamp_granularities": ["word"],
                "temperature": 0.0,
            }
            if language:
                kwargs["language"] = language
            if job_id:
                from provenance import record_ai_call
                recorder = record_ai_call(
                    job_id=job_id,
                    step=provenance_step,
                    tool_name=f"whisper-1-{provenance_step.replace('_', '-')}",
                    tool_provider="openai",
                    prompt=(
                        f"Transcribe rescue window start={ini:.2f}s "
                        f"duration={dur:.2f}s language={language or 'auto'}"
                    ),
                    input_data_types=["audio_clip"],
                )
            # Cost-capped rescue owns its retry policy. SDK retries would
            # submit the same audio again without the caller being able to
            # reserve or report those extra billed seconds.
            r = OpenAI(timeout=60.0, max_retries=0).audio.transcriptions.create(**kwargs)
        provider_words, raw_words = _raw_provider_words(r)
        from recognition_provenance import record_completed
        record_completed(
            family="openai/whisper-1",
            events=raw_words,
            kind="word_stream",
            view="bounded_audio_window",
            transformation=f"{provenance_step}_raw",
        )
        if recorder:
            recorder.finish(response_summary="succeeded")
        out = []
        for w in provider_words:
            try:
                value = w if isinstance(w, dict) else {
                    "word": getattr(w, "word"),
                    "start": getattr(w, "start"),
                    "end": getattr(w, "end"),
                }
                out.append({
                    "word": str(value["word"]),
                    "start": float(value["start"]) + ini,
                    "end": float(value["end"]) + ini,
                })
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        return out
    except Exception as e:
        if recorder:
            recorder.finish(
                response_summary=f"error_type:{type(e).__name__}"
            )
        logger.warning(
            "[GAP-RESCUE] window transcription declined error_type=%s job=%s",
            type(e).__name__, job_id,
        )
        return []
    finally:
        try:
            os.unlink(clip)
        except OSError:
            pass


_HALLUC = (
    "amara.org", "subtitulos realizados por", "subtitled by",
    "subtitles by", "subtitulado por", "transcripcion por",
    "music", "gracias por ver", "suscribete",
)


def _texto_sospechoso(texto: str) -> bool:
    """Alucinaciones típicas de whisper sobre silencio/instrumental.
    Usa el filtro de `pipeline` si está importable; si no, la lista local
    (el módulo no debe depender de pipeline para funcionar)."""
    t = (texto or "").strip()
    if not t:
        return True
    try:
        from pipeline import _is_whisper_hallucination, _is_single_word_loop
        if _is_whisper_hallucination(t) or _is_single_word_loop(t):
            return True
    except Exception:
        low = t.lower()
        if any(h in low for h in _HALLUC):
            return True
    # Una sola palabra repetida (bucle corto que el filtro de arriba no toma).
    toks = [x.lower() for x in t.split()]
    return len(toks) >= 4 and len(set(toks)) == 1


def _vad_regions(stem_path: str | None) -> list[tuple]:
    """Compatibility accessor. Prefer ``_vad_evidence`` for decisions."""
    return list(_vad_evidence(stem_path).get("regions") or [])


def _vad_evidence(audio_path: str | None) -> dict:
    """Return regions plus availability; silence is not analyzer failure."""
    if not audio_path:
        return {"available": False, "regions": [], "error_code": "missing_audio"}
    try:
        from anchor_align import vocal_regions
        regions = vocal_regions(audio_path)
        if regions is None:
            return {
                "available": False, "regions": [],
                "error_code": "analyzer_returned_none",
            }
        return {"available": True, "regions": list(regions), "error_code": None}
    except Exception as exc:
        return {
            "available": False, "regions": [],
            "error_code": type(exc).__name__,
        }


def _voiced_overlap(a: float, b: float, regs: list[tuple]) -> float:
    return sum(max(0.0, min(b, rb) - max(a, ra)) for ra, rb in regs)


def _sparse_reference_cluster(
    words: list[dict], reference_tokens: set[str], regs: list[tuple],
    *, max_cadence_gap_s: float = 12.0,
) -> list[list[dict]]:
    """Return the strongest physically plausible sparse refrain cluster.

    Sparse live hooks need a different shape gate than prose: several isolated
    one-word lines, each long enough to be sung, occurring at a continuous
    cadence.  Dense timestamp bursts and isolated tail hallucinations are
    rejected even if their token appears somewhere in the reference.
    """
    import re as _re

    plausible: list[list[dict]] = []
    for group in _agrupar_en_lineas(words):
        if not group:
            continue
        start = _f(group[0].get("start"))
        end = _f(group[-1].get("end"))
        duration = end - start
        tokens = {
            token
            for word in group
            for token in _re.findall(
                r"[^\W\d_]+", str(word.get("word", "")).casefold(),
                _re.UNICODE,
            )
        }
        if (not tokens or not reference_tokens
                or not tokens.issubset(reference_tokens)
                or duration < _MIN_LINE_S or duration > _LINE_MAX_S
                or len(group) / max(duration, 0.1) > _MAX_WORDS_PER_S):
            continue
        if regs and _voiced_overlap(start, end, regs) < min(
                _VAD_LINE_OVERLAP, duration * 0.5):
            continue
        plausible.append(group)

    clusters: list[list[list[dict]]] = []
    current: list[list[dict]] = []
    for group in plausible:
        start = _f(group[0].get("start"))
        if (current and start - _f(current[-1][0].get("start"))
                > max_cadence_gap_s):
            clusters.append(current)
            current = []
        current.append(group)
    if current:
        clusters.append(current)
    best = max(clusters, key=len, default=[])
    return best if len(best) >= _MIN_WORDS else []


def rescue(segments: list[dict], audio_path: str, *,
           stem_path: str | None = None,
           audio_duration: float | None = None, language: str | None = None,
           lead_s: float = 0.0, hold_s: float = 0.0,
           asr_words: list[dict] | None = None,
           job_id: str | None = None,
           include_leading: bool = False,
           reference_text: str = "") -> tuple[list[dict], dict]:
    """Devuelve (segmentos + líneas rescatadas, stats). Nunca levanta.

    `stem_path`: stem de voz (demucs) si está cacheado. Es MUY superior a la
    mezcla para esto — ver el docstring del módulo. Sin stem se usa la
    mezcla, pero los gates de densidad harán declinar casi siempre.

    `asr_words`: stream de palabras del ASR de la cascada. Habilita el
    rescate por MISMATCH: carteles largos cuyo texto no suena a su ventana
    (`audio_coverage.text_mismatches`) se re-transcriben y reemplazan."""
    stats = {"gaps": 0, "rescued_lines": 0, "skipped": [], "source": "mix",
             "mismatch_replaced": 0, "view_disagreements": []}
    if not segments or not audio_path or not os.path.exists(audio_path):
        return list(segments or []), stats
    fuente = audio_path
    if stem_path and os.path.exists(stem_path):
        fuente, stats["source"] = stem_path, "stem"
    stem_vad = _vad_evidence(fuente)
    mix_vad = _vad_evidence(audio_path) if stats["source"] == "stem" else stem_vad
    regs = list(stem_vad.get("regions") or [])
    mix_regs = list(mix_vad.get("regions") or [])
    try:
        min_gap = _env_float("GAP_RESCUE_MIN_GAP_S", _MIN_GAP_S)
        clip_max = _env_float("GAP_RESCUE_CLIP_MAX_S", _CLIP_MAX_S)
        contexto = _env_float("GAP_RESCUE_CONTEXT_S", _CONTEXT_S)
        max_gaps = int(_env_float("GAP_RESCUE_MAX_GAPS", _MAX_GAPS))
        fin_audio = float(audio_duration) if audio_duration else None

        gaps = find_gaps(
            segments, audio_duration, min_gap_s=min_gap,
            include_leading=include_leading,
        )
        stats["gaps"] = len(gaps)
        if not gaps:
            return list(segments), stats

        import re as _re
        reference_tokens = set(_re.findall(
            r"[^\W\d_]+", (reference_text or "").casefold(), _re.UNICODE,
        ))
        nuevas: list[dict] = []
        for ini, fin in gaps[:max_gaps]:
            leading_gap = include_leading and ini <= 0.001
            # Zona útil (lo que puede aportar contenido nuevo) y ventana a
            # transcribir (zona + contexto cantado alrededor).
            zona_a = ini + _PAD_S
            zona_b = fin - (0.05 if leading_gap else _PAD_S)
            if zona_b - zona_a < 2.0:
                stats["skipped"].append((round(ini, 1), "ventana_corta"))
                continue
            # Gate de VAD del hueco: si el stem no canta ahí, no hay nada
            # que rescatar — es un pasaje instrumental (Hombre Lobo: el
            # outro de saxo hacía alucinar a whisper con el coro en eco).
            dual_view = stats["source"] == "stem"
            unavailable = [
                name for name, evidence in (("stem", stem_vad), ("mix", mix_vad))
                if not evidence.get("available")
            ]
            if unavailable:
                stats["view_disagreements"].append({
                    "start": round(zona_a, 3), "end": round(zona_b, 3),
                    "reason": "view_unavailable", "views": unavailable,
                    "error_codes": {
                        name: evidence.get("error_code")
                        for name, evidence in (("stem", stem_vad), ("mix", mix_vad))
                        if not evidence.get("available")
                    },
                })
                stats["skipped"].append((round(ini, 1), "vad_unavailable"))
                continue
            stem_voiced = (
                _voiced_overlap(zona_a, zona_b, regs)
                if dual_view or regs else None
            )
            mix_voiced = (
                _voiced_overlap(zona_a, zona_b, mix_regs)
                if dual_view or mix_regs else None
            )
            if stem_voiced is not None and mix_voiced is not None and (
                (stem_voiced >= _VAD_MIN_VOICED_S)
                != (mix_voiced >= _VAD_MIN_VOICED_S)
            ):
                stats["view_disagreements"].append({
                    "start": round(zona_a, 3), "end": round(zona_b, 3),
                    "stem_voiced_s": round(stem_voiced, 3),
                    "mix_voiced_s": round(mix_voiced, 3),
                })
                stats["skipped"].append((round(ini, 1), "stem_mix_disagreement"))
                continue
            if dual_view and max(stem_voiced or 0.0, mix_voiced or 0.0) < _VAD_MIN_VOICED_S:
                stats["skipped"].append((round(ini, 1), "sin_voz_vad"))
                continue
            if regs and (stem_voiced or 0.0) < _VAD_MIN_VOICED_S:
                stats["skipped"].append((round(ini, 1), "sin_voz_vad"))
                continue
            w_ini = max(0.0, zona_a - contexto)
            w_fin = zona_b + contexto
            if fin_audio:
                w_fin = min(w_fin, fin_audio)
            if w_fin - w_ini > clip_max:      # recortar contexto, no la zona
                sobra = (w_fin - w_ini) - clip_max
                w_ini = min(zona_a, w_ini + sobra / 2)
                w_fin = max(zona_b, w_fin - sobra / 2)
            words = _transcribe_window(fuente, w_ini, w_fin - w_ini,
                                       language, job_id=job_id)
            # Sólo lo que cae DENTRO del hueco: el contexto era para que el
            # ASR enganche, no para re-escribir lo ya cubierto.
            words = [w for w in words
                     if zona_a <= (_f(w.get("start")) + _f(w.get("end"))) / 2
                     <= zona_b]
            # Sparse repeated hooks ("Real" every 6s) are legitimate in live
            # outros but fail the normal prose-density gate. First score the
            # stem witness. Stem separation can distort the lexical vowel, so
            # if it has no physically plausible cadence cluster, compare one
            # independent pass over the original mix and use it only when it
            # supplies >=3 reference-backed, VAD-backed hits.
            sparse_groups: list[list[dict]] = []
            sparse_source = stats["source"]
            if include_leading and not leading_gap and reference_tokens:
                sparse_groups = _sparse_reference_cluster(
                    words, reference_tokens, regs,
                )
                if not sparse_groups and stats["source"] == "stem":
                    mix_words = _transcribe_window(
                        audio_path, w_ini, w_fin - w_ini,
                        language, job_id=job_id,
                    )
                    mix_words = [
                        w for w in mix_words
                        if zona_a <= (_f(w.get("start")) + _f(w.get("end"))) / 2
                        <= zona_b
                    ]
                    mix_groups = _sparse_reference_cluster(
                        mix_words, reference_tokens, regs,
                    )
                    if len(mix_groups) > len(sparse_groups):
                        sparse_groups = mix_groups
                        words = [word for group in mix_groups for word in group]
                        sparse_source = "mix-witness"
                if sparse_groups:
                    words = [word for group in sparse_groups for word in group]
                    stats["sparse_source"] = sparse_source
            sparse_live_refrain = bool(sparse_groups)
            if ((not leading_gap and len(words) < _MIN_WORDS)
                    or (not leading_gap and not sparse_live_refrain
                        and len(words) / (zona_b - zona_a)
                        < _MIN_WORDS_PER_S)
                    or (leading_gap and not words)):
                stats["skipped"].append((round(ini, 1), "sin_canto"))
                continue
            texto_total = " ".join(str(w.get("word", "")) for w in words)
            # The generic hallucination filter correctly rejects a repeated
            # single-word loop in prose.  A sparse live refrain has already
            # passed three stronger, independent guards (reference tokens,
            # at least three timestamped ASR hits, and per-line vocal VAD),
            # so rejecting it here would recreate the exact deaf outro this
            # branch exists to recover.
            if _texto_sospechoso(texto_total) and not sparse_live_refrain:
                stats["skipped"].append((round(ini, 1), "alucinacion"))
                continue
            ultima_txt = None
            for grupo in _agrupar_en_lineas(words):
                txt = " ".join(str(w.get("word", "")).strip()
                               for w in grupo).strip()
                if not txt:
                    continue
                g0, g1 = _f(grupo[0].get("start")), _f(grupo[-1].get("end"))
                # A single sustained opening word ("Hoy") is valid only with
                # two independent guards: it appears in the known reference
                # and the vocal stem supports it.  Everywhere else the normal
                # two-word minimum remains in force.
                single_leading = leading_gap and len(grupo) == 1
                single_live_refrain = sparse_live_refrain and len(grupo) == 1
                if len(grupo) < 2 and not (single_leading or single_live_refrain):
                    continue
                if single_leading or single_live_refrain:
                    txt_tokens = set(_re.findall(
                        r"[^\W\d_]+", txt.casefold(), _re.UNICODE,
                    ))
                    if (not reference_tokens
                            or not txt_tokens.issubset(reference_tokens)):
                        continue
                # Sanidad física: una "línea" de 0,3s con 5 palabras es un
                # destello alucinado, no canto.
                if g1 - g0 < _MIN_LINE_S:
                    continue
                if len(grupo) / max(g1 - g0, 0.1) > _MAX_WORDS_PER_S:
                    continue
                # Gate de VAD por línea: sin voz del stem debajo, afuera.
                if regs and _voiced_overlap(g0, g1, regs) < min(
                        _VAD_LINE_OVERLAP, (g1 - g0) * 0.5):
                    continue
                # Dedup: el eco repite la misma frase — extender la anterior
                # en vez de emitir un duplicado. Texto IDÉNTICO dentro de
                # 2s es eco; frases legítimas repetidas del coro vienen más
                # espaciadas (la cadencia mínima medida fue ~6s).
                if nuevas and txt.lower() == (ultima_txt or "").lower()                         and g0 - _f(nuevas[-1]["end"]) < 2.0:
                    nuevas[-1]["end"] = round(min(fin - 0.05, g1 + hold_s), 3)
                    continue
                s0 = max(ini + 0.05, g0 - lead_s)
                if single_leading and regs:
                    # Whisper timestamps often begin at the first alignable
                    # consonant and lose the onset of a held opening syllable.
                    # Extend only to a VAD region acoustically connected to the
                    # recovered word; unrelated intro energy cannot qualify.
                    connected = [
                        (ra, rb) for ra, rb in regs
                        if ra < g0 and rb >= g0 - 0.5
                    ]
                    if connected:
                        s0 = max(ini + 0.05, min(ra for ra, _ in connected))
                e0 = min(fin - 0.05, g1 + hold_s)
                if e0 - s0 < 0.3:
                    continue
                nuevas.append({"start": round(s0, 3), "end": round(e0, 3),
                               "text": txt, "words": [dict(w) for w in grupo],
                               "gap_rescued": True, "review": True})
                ultima_txt = txt
            logger.info(
                "[GAP-RESCUE] hueco %.1f-%.1fs: %d palabras rescatadas del "
                "%s (el ASR original no oyó nada ahí)", ini, fin, len(words),
                stats["source"])

        # ── Rescate por MISMATCH: reemplazar carteles manchados ──────────
        reemplazados: set = set()
        if asr_words and stats["source"] == "stem":
            try:
                from audio_coverage import text_mismatches
                sospechosos = [
                    m for m in text_mismatches(segments, asr_words,
                                               min_ratio=_MISMATCH_MAX_RATIO)
                    if (m["end"] - m["start"]) >= _MISMATCH_MIN_DUR_S
                ]
            except Exception:
                sospechosos = []
            ordenados = sorted((s for s in segments if isinstance(s, dict)),
                               key=lambda x: _f(x.get("start")))
            for m in sospechosos[:2]:          # tope conservador por canción
                idx = m["index"]
                card = segments[idx]
                # Zona = el cartel + el hueco que le sigue (el manchado suele
                # tapar el arranque de un hueco real, como en Hombre Lobo).
                zona_a = m["start"]
                zona_b = m["end"]
                for nx in ordenados:
                    if _f(nx.get("start")) > zona_b:
                        zona_b = _f(nx.get("start")) - _PAD_S
                        break
                if regs and _voiced_overlap(zona_a, zona_b, regs) < _VAD_MIN_VOICED_S:
                    stats["skipped"].append((round(zona_a, 1), "mismatch_sin_voz"))
                    continue
                w_ini = max(0.0, zona_a - contexto)
                w_fin = zona_b + contexto
                if fin_audio:
                    w_fin = min(w_fin, fin_audio)
                words = _transcribe_window(fuente, w_ini, w_fin - w_ini,
                                           language, job_id=job_id)
                words = [w for w in words
                         if zona_a - 0.2 <= (_f(w.get("start")) + _f(w.get("end"))) / 2
                         <= zona_b + 0.2]
                if len(words) < _MIN_WORDS:
                    stats["skipped"].append((round(zona_a, 1), "mismatch_sin_canto"))
                    continue
                if _texto_sospechoso(" ".join(str(w.get("word", "")) for w in words)):
                    stats["skipped"].append((round(zona_a, 1), "mismatch_alucinacion"))
                    continue
                lineas_zona = []
                for grupo in _agrupar_en_lineas(words):
                    g0, g1 = _f(grupo[0].get("start")), _f(grupo[-1].get("end"))
                    txt = " ".join(str(w.get("word", "")).strip()
                                   for w in grupo).strip()
                    if not txt or len(grupo) < 2 or g1 - g0 < _MIN_LINE_S:
                        continue
                    if len(grupo) / max(g1 - g0, 0.1) > _MAX_WORDS_PER_S:
                        continue
                    if regs and _voiced_overlap(g0, g1, regs) < min(
                            _VAD_LINE_OVERLAP, (g1 - g0) * 0.5):
                        continue
                    lineas_zona.append({
                        "start": round(max(zona_a, g0 - lead_s), 3),
                        "end": round(min(zona_b + _PAD_S, g1 + hold_s), 3),
                        "text": txt, "words": [dict(w) for w in grupo],
                        "gap_rescued": True, "review": True})
                # Reemplazo sólo si lo nuevo cubre razonablemente la zona del
                # cartel viejo: no cambiar un cartel malo por un agujero.
                cubre = sum(l["end"] - l["start"] for l in lineas_zona
                            if l["start"] < m["end"])
                if lineas_zona and cubre >= (m["end"] - m["start"]) * 0.4:
                    reemplazados.add(idx)
                    nuevas.extend(lineas_zona)
                    stats["mismatch_replaced"] += 1
                    logger.warning(
                        "[GAP-RESCUE] cartel manchado %.1f-%.1fs "
                        "(chars=%d, ratio<%.2f) reemplazado por %d línea(s)",
                        m["start"], m["end"],
                        len(str(card.get("text", ""))),
                        _MISMATCH_MAX_RATIO, len(lineas_zona))

        if not nuevas:
            return list(segments), stats
        stats["rescued_lines"] = len(nuevas)
        base = [s for i, s in enumerate(segments) if i not in reemplazados]
        out = sorted(base + nuevas, key=lambda s: _f(s.get("start")))
        for x, y in zip(out, out[1:]):
            if _f(x.get("end")) > _f(y.get("start")):
                x["end"] = round(max(_f(x.get("start")) + 0.1,
                                     _f(y.get("start")) - 0.01), 3)
        return out, stats
    except Exception as e:  # pragma: no cover — nunca romper la transcripción
        logger.warning(
            "[GAP-RESCUE] rescue declined error_type=%s",
            type(e).__name__,
        )
        return list(segments), stats
