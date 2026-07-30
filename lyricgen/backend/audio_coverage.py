"""Cobertura de la letra medida contra el AUDIO.

POR QUÉ EXISTE
--------------
Toda métrica de calidad que había en el backend se mide contra la letra de
REFERENCIA (`coverage = len(out)/len(lines)` en whisperx_reconcile, y las
variantes en main.py). Esa métrica tiene un defecto estructural: si la
referencia viene recortada, el denominador se achica y la cobertura SUBE.
El fallo se autocertifica como éxito.

Medido en producción (28-07-2026): un job reportaba 15/15 = 100 % de
cobertura mientras sólo el 39 % de lo cantado tenía una línea encima. En un
segundo tema el gap fue 100 % reportado vs 62 % real. Y sobre 68 canciones
con la duración real del audio, el 22 % termina la letra a más de 30 s del
final y el 13 % a más de 45 s — de las 10 peores colas, 7 tenían canto real.

Esta métrica es la inversa: qué fracción de las palabras que el ASR oyó
queda debajo de alguna línea emitida. Sólo puede BAJAR si se pierde canto,
así que no se puede autocertificar.

DISEÑO
------
- Se mide contra las PALABRAS del ASR, no contra el tiempo: donde no hubo
  palabras (pasajes instrumentales, fade-outs) no cuenta como pérdida. Eso
  evita el falso positivo de "outro instrumental largo".
- Una palabra cuenta como cubierta si su punto medio cae dentro de algún
  segmento; el punto medio tolera el clamp monótono de los bordes.
- Puro, sin I/O, sin dependencias. Sirve para observabilidad y para gatear.
"""
from __future__ import annotations


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _intervals(segments) -> list[tuple[float, float]]:
    out = []
    for s in segments or []:
        if isinstance(s, dict):
            out.append((_f(s.get("start")), _f(s.get("end"))))
    return out


def _mid(w) -> float:
    a, b = _f(w.get("start")), _f(w.get("end"))
    return (a + b) / 2 if b > a else a


def audio_coverage(segments, words, *, pad: float = 0.05) -> float:
    """Fracción [0..1] de las palabras del ASR que alguna línea reclama.

    Devuelve 1.0 cuando no hay palabras contra las cuales medir (no se puede
    afirmar pérdida) y 0.0 cuando hay palabras pero ninguna línea."""
    ws = [w for w in (words or []) if isinstance(w, dict)]
    if not ws:
        return 1.0
    iv = _intervals(segments)
    if not iv:
        return 0.0
    n = 0
    for w in ws:
        m = _mid(w)
        if any(a - pad <= m <= b + pad for a, b in iv):
            n += 1
    return n / len(ws)


def uncovered_spans(segments, words, *, min_gap_s: float = 3.0,
                    min_words: int = 3, split_s: float = 2.5,
                    pad: float = 0.05) -> list[tuple[float, float, int]]:
    """Regiones con canto que ninguna línea reclama, como
    `(inicio, fin, n_palabras)`. Sirve para el log y para alertar: dice
    DÓNDE se perdió letra, no sólo cuánto."""
    ws = [w for w in (words or []) if isinstance(w, dict)]
    if not ws:
        return []
    iv = _intervals(segments)
    huerfanas: list[list[dict]] = []
    actual: list[dict] = []
    for w in ws:
        m = _mid(w)
        if any(a - pad <= m <= b + pad for a, b in iv):
            if actual:
                huerfanas.append(actual)
                actual = []
            continue
        if actual and (_f(w.get("start")) - _f(actual[-1].get("end"))) > split_s:
            huerfanas.append(actual)
            actual = []
        actual.append(w)
    if actual:
        huerfanas.append(actual)

    out = []
    for run in huerfanas:
        ini, fin = _f(run[0].get("start")), _f(run[-1].get("end"))
        if len(run) >= min_words and (fin - ini) >= min_gap_s:
            out.append((round(ini, 2), round(fin, 2), len(run)))
    return out


def _norm_tokens(text: str) -> list[str]:
    import unicodedata as _u
    s = _u.normalize("NFD", (text or "").lower())
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    return [t for t in s.split() if t]


def text_mismatches(segments, words, *, min_ratio: float = 0.4,
                    min_window_words: int = 2,
                    pad: float = 0.3) -> list[dict]:
    """Carteles cuyo TEXTO no suena a lo que el ASR oyó en su ventana.

    La cobertura dice si HAY cartel; esto dice si el cartel DICE lo que se
    canta en ese momento — la dimensión que faltaba: el job 6f4047db tenía
    76 % de cobertura y aun así 2 carteles pintaban la estrofa equivocada
    sobre el estribillo (0 % de coincidencia). El usuario lo vio a ojo; la
    instrumentación no podía.

    Compara con `_phonetic_ratio` (forced_align) para tolerar mishears.
    Ventanas con menos de `min_window_words` palabras no se evalúan (sin
    evidencia suficiente no se acusa a nadie)."""
    from forced_align import _phonetic_ratio
    ws = [w for w in (words or []) if isinstance(w, dict)]
    if not ws:
        return []
    out = []
    for i, s in enumerate(segments or []):
        if not isinstance(s, dict):
            continue
        toks = _norm_tokens(s.get("text", ""))
        if not toks:
            continue
        a, b = _f(s.get("start")), _f(s.get("end"))
        win = [w for w in ws if a - pad <= _mid(w) <= b + pad]
        if len(win) < min_window_words:
            continue
        win_toks = _norm_tokens(" ".join(str(w.get("word", "")) for w in win))
        ratio = _phonetic_ratio(toks, win_toks)
        if ratio < min_ratio:
            out.append({"index": i, "start": round(a, 2), "end": round(b, 2),
                        "ratio": round(ratio, 3)})
    return out


def voiced_gaps(segments, stem_path: str | None, *,
                audio_duration: float | None = None,
                min_gap_s: float = 8.0,
                min_voiced_s: float = 3.0,
                min_voiced_frac: float = 0.0,
                rescue_skipped=None) -> list[dict]:
    """Huecos entre carteles que contienen CANTO según el VAD del stem.

    Es el guardrail que la cobertura por palabras no puede dar: `audio_coverage`
    mide contra lo que el ASR oyó, así que un punto sordo del ASR le resulta
    invisible — el job dcf773b5 reportó `zonas_sin_letra=0` con un hueco de
    30,7 s cantado en pantalla. El stem no depende del ASR: energía en el stem
    aislado = alguien canta ahí. Si un hueco entre carteles se solapa con
    regiones de voz del stem por más de `min_voiced_s`, ese hueco es letra
    faltante, no un respiro instrumental.

    ENERGÍA NO ES CANTO (batch de 12, 30-07-2026). El stem de demucs arrastra
    fuga: solos de guitarra, vientos, platillos y colas de reverb tienen
    energía en la banda vocal. Con sólo `min_voiced_s` absoluto, el breaker
    disparó en 3 de 12 canciones y las 8 zonas acusadas eran FALSAS — al
    transcribirlas sobre el stem devolvieron nada o pura alucinación
    ("Subtítulos realizados por la comunidad de Amara.org", "no no no no").
    Un breaker que grita una de cada cuatro veces sin razón se vuelve ruido
    y el operador deja de mirarlo: exactamente lo que un guardrail no puede
    permitirse.

LA FRACCIÓN NO SIRVE PARA DISTINGUIRLOS — probado y descartado. Tentaba
    exigir que la voz DOMINE el hueco en vez de salpicarlo, pero medido
    contra los casos reales las dos poblaciones se solapan:

        hueco REAL de UMG (Hombre Lobo 116,9-131,0)      100%
        hueco REAL de la cola (Rodando, 55s cantados)     70%
        FALSO POSITIVO (Pericos, break de reggae)         65%
        FALSO POSITIVO (Mercedes Sosa)                    50%
        hueco REAL fundacional (Rodando 209-240)          45%  <-- el más bajo
        FALSO POSITIVO (Rata Blanca, outro de guitarra)    5%

    El hueco que originó todo este trabajo tiene MENOS fracción cantada que
    dos de los falsos positivos: cualquier corte que mate a los falsos mata
    también al verdadero. `min_voiced_frac` queda como perilla (default 0,
    inerte) para que nadie vuelva a intentarlo sin mirar esta tabla.

    LO QUE SÍ DISTINGUE SON LAS PALABRAS. `rescue_skipped` trae los arranques
    de hueco que `gap_rescue` ya sondeó con un ASR sobre el stem y descartó
    (sin voz / sin canto / alucinación). Al re-transcribir las 8 zonas
    acusadas, ninguna devolvió letra: o silencio, o alucinación de manual
    ("Subtítulos realizados por la comunidad de Amara.org", "no no no no").
    Los huecos verdaderos, en cambio, devuelven versos (16 palabras en el
    caso fundacional). Si el sondeo ya dijo que no hay nada, esta métrica no
    puede contradecirlo con energía: la energía la ensucia la fuga de
    guitarras, vientos y colas de reverb que demucs deja en el stem.

    Sin lista de veredictos (gap_rescue apagado) el comportamiento es el de
    antes: energía sola, con su ruido conocido.

    Devuelve [{"start", "end", "voiced_s"}] por hueco culpable. [] si no hay
    stem o librosa (nunca acusa sin evidencia). Never raises."""
    if not stem_path:
        return []
    try:
        from anchor_align import vocal_regions
        from gap_rescue import find_gaps
        regs = vocal_regions(stem_path)
        if not regs:
            return []
        descartados = []
        for x in (rescue_skipped or []):
            try:
                descartados.append(float(x[0] if isinstance(x, (list, tuple))
                                         else x))
            except (TypeError, ValueError, IndexError):
                continue
        out = []
        for a, b in find_gaps(segments, audio_duration, min_gap_s=min_gap_s):
            largo = b - a
            if largo <= 0:
                continue
            voiced = sum(max(0.0, min(b, rb) - max(a, ra)) for ra, rb in regs)
            if voiced < min_voiced_s or voiced / largo < min_voiced_frac:
                continue
            if any(abs(a - d) <= 1.0 for d in descartados):
                continue          # gap_rescue ya lo sondeó: no hay palabras
            out.append({"start": round(a, 2), "end": round(b, 2),
                        "voiced_s": round(voiced, 2)})
        return out
    except Exception:
        return []


def summarize(segments, words, *, stem_path: str | None = None,
              rescue_skipped=None,
              audio_duration: float | None = None) -> dict:
    """Resumen listo para loguear/persistir."""
    spans = uncovered_spans(segments, words)
    duraciones = [b - a for a, b, _ in spans]
    mismatches = text_mismatches(segments, words)
    vg = voiced_gaps(segments, stem_path, audio_duration=audio_duration,
                     rescue_skipped=rescue_skipped)
    return {
        "audio_coverage": round(audio_coverage(segments, words), 4),
        "uncovered_spans": len(spans),
        "uncovered_seconds": round(sum(duraciones), 2),
        "worst_span_s": round(max(duraciones), 2) if duraciones else 0.0,
        "text_mismatches": len(mismatches),
        "voiced_gaps": len(vg),
        "voiced_gap_s": round(sum(g["voiced_s"] for g in vg), 2),
    }
