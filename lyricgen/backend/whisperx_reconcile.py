"""WhisperX × reference-lyrics reconciliation.

WHY
---
WhisperX gives us word-level timing pinned to the actual audio (truth for
timestamps), but its TEXT — what it heard the singer say — can be rough
on names, mondegreens, or odd phrasings. Meanwhile lrclib/Gemini gives us
curated TEXT with proper line breaks and spelling (truth for the lyric)
but no timestamps tied to *this* audio.

The combination is what beats Rotor: whisperX's audio-anchored word stamps
+ lrclib's clean text = best of both. Implementation reuses the hardened
`wordstamps_to_segments` (forced_align.py) which is already designed to
bucket a word-stream into known lyric lines with drift detection.

CONTRACT
--------
- `reconcile(wx_segs, reference_text) -> list[dict] | None`: returns
  segments with REFERENCE text + WHISPERX timing, or None when
  reconciliation looks unreliable (caller falls back to wx_segs).
- Pure (no I/O); the only dependency is `forced_align.wordstamps_to_segments`.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.whisperx_reconcile")

# Voz cantada que la referencia no cubre. `wordstamps_to_segments` itera
# sobre las LÍNEAS DE LA REFERENCIA, así que emite como mucho una línea por
# línea de referencia: si la letra viene incompleta (pasa seguido — lrclib
# devuelve registros con la duración correcta pero sólo las estrofas, sin las
# repeticiones ni el outro), las palabras que whisperX oyó y que ninguna
# línea reclamó quedan huérfanas y NUNCA se emiten. Peor: `coverage` se mide
# contra la referencia, así que una referencia incompleta achica el
# denominador y la cobertura SUBE — el fallo se autocertifica como 100 %.
#
# Caso testigo: "Rodando Por Ahí" (Intoxicados). La referencia trae 15 líneas;
# la canción tiene ~36. Contra la transcripción de Rotor, al resultado le
# faltaban 13 líneas: 7 en la cola (después de 196 s) y 6 en huecos INTERNOS
# (la repetición de la segunda estrofa). Por eso la recuperación no puede ser
# sólo de cola: se recupera cualquier región con voz sin letra encima.
#
# El gatillo son las PALABRAS huérfanas, no el tiempo vacío: donde no hay
# palabras (pasajes instrumentales, como los 12,9 s finales y el hueco de
# 11,2 s del outro de esta canción) no se recupera nada. El guardrail es
# auto-limitante por construcción.
_GAP_MIN_S = 3.0         # span mínimo de voz sin cubrir que amerita recuperación
_GAP_MIN_WORDS = 3       # palabras huérfanas mínimas (evita ruido suelto)
_GAP_SPLIT_S = 2.5       # silencio que separa dos regiones huérfanas distintas
_TAIL_LINE_GAP_S = 1.2   # silencio que corta una línea (fallback por palabras)
_TAIL_LINE_MAX_S = 6.0   # largo máximo de una línea recuperada


def _gap_recovery_enabled() -> bool:
    return os.environ.get(
        "RECONCILE_GAP_RECOVERY_ENABLED", "1"
    ).strip().lower() not in ("0", "false", "off", "no")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Piso de cobertura medida contra el AUDIO. APAGADO por default (0.0) — a
# propósito.
#
# La idea: por debajo de cierta cobertura la referencia no representa a esta
# grabación y quedarse con el ASR da mejor resultado (medido: en el caso
# testigo declinar sube de 13/26 a 26/26 líneas contra Rotor).
#
# Por qué NO gatea todavía: la métrica SUB-REPORTA cuando el texto canónico
# tokeniza distinto que el ASR. Caso real del corpus (Legalícenla): la letra
# tiene una palabra compuesta donde whisperX oyó tres; el segmento emitido
# cubre una de esas tres y las otras dos cuentan como "canto sin letra"
# aunque la línea esté perfectamente puesta. Ahí la cobertura da 54 % en un
# resultado CORRECTO.
#
# Con un umbral calibrado sobre UNA canción (39 %) ese caso caía del lado
# equivocado. Así que primero se juntan datos reales por rama —para eso está
# la instrumentación de `_emit_segments` y del worker— y recién después se
# elige el piso. Se enciende con RECONCILE_MIN_AUDIO_COVERAGE=0.45 (p.ej.).
_MIN_AUDIO_COVERAGE = 0.0


def _min_audio_coverage() -> float:
    """Piso configurable; `RECONCILE_MIN_AUDIO_COVERAGE=0` apaga el gate."""
    try:
        return float(os.environ.get("RECONCILE_MIN_AUDIO_COVERAGE",
                                    _MIN_AUDIO_COVERAGE))
    except (TypeError, ValueError):
        return _MIN_AUDIO_COVERAGE


def _audio_coverage(out: list[dict], words: list[dict]) -> float:
    """Fracción de las palabras del ASR que alguna línea emitida reclama.
    A diferencia de `coverage` (que se mide contra la referencia y por eso
    SUBE cuando la referencia viene recortada), esta métrica sólo puede
    bajar si se pierde canto — es la que detecta el fallo real.

    Implementación en `audio_coverage.py`, compartida con la instrumentación
    del punto de salida de la cascada."""
    from audio_coverage import audio_coverage as _cov
    return _cov(out, words)


def _lineas_desde_palabras(orphan: list[dict], covered_end: float) -> list[dict]:
    """Agrupa palabras huérfanas en líneas cantables (corta por silencio o
    por largo). Fallback para cuando whisperX no dejó texto por segmento."""
    lineas: list[dict] = []
    actual: list[dict] = []

    def cerrar():
        if not actual:
            return
        texto = " ".join(
            str(w.get("word", "")).strip() for w in actual
            if str(w.get("word", "")).strip()
        ).strip()
        if not texto:
            actual.clear()
            return
        ini = max(_f(actual[0].get("start")), covered_end + 0.05)
        fin = _f(actual[-1].get("end"))
        if fin - ini >= 0.3:
            lineas.append({
                "start": round(ini, 3), "end": round(fin, 3),
                "text": texto, "words": list(actual), "gap_recovered": True,
            })
        actual.clear()

    for w in orphan:
        if actual:
            hueco = _f(w.get("start")) - _f(actual[-1].get("end"))
            largo = _f(w.get("end")) - _f(actual[0].get("start"))
            if hueco > _TAIL_LINE_GAP_S or largo > _TAIL_LINE_MAX_S:
                cerrar()
        actual.append(w)
    cerrar()
    return lineas


def _regiones_huerfanas(out: list[dict], words: list[dict]) -> list[list[dict]]:
    """Agrupa en regiones las palabras que ninguna línea emitida reclama.
    Una palabra cuenta como cubierta si su punto medio cae dentro de algún
    segmento de `out` (el punto medio evita que el clamp monótono de los
    bordes deje palabras falsamente huérfanas)."""
    covered = [(_f(s.get("start")), _f(s.get("end"))) for s in out]

    def cubierta(w) -> bool:
        a, b = _f(w.get("start")), _f(w.get("end"))
        mid = (a + b) / 2 if b > a else a
        return any(ini - 0.05 <= mid <= fin + 0.05 for ini, fin in covered)

    regiones: list[list[dict]] = []
    actual: list[dict] = []
    for w in words:
        if cubierta(w):
            if actual:
                regiones.append(actual)
                actual = []
            continue
        if actual and (_f(w.get("start")) - _f(actual[-1].get("end"))) > _GAP_SPLIT_S:
            regiones.append(actual)
            actual = []
        actual.append(w)
    if actual:
        regiones.append(actual)
    return regiones


def _texto_para_region(region: list[dict], wx_segs: list[dict],
                       covered: list[tuple]) -> list[dict]:
    """Líneas para una región huérfana. Prefiere la segmentación propia de
    whisperX (trae puntuación y cortes de frase); si ahí no hay texto usable,
    arma las líneas agrupando las palabras."""
    ini_r = _f(region[0].get("start"))
    fin_r = _f(region[-1].get("end"))
    lineas: list[dict] = []
    for s in wx_segs or []:
        if not isinstance(s, dict):
            continue
        texto = (s.get("text") or "").strip()
        if not texto:
            continue
        a, b = _f(s.get("start")), _f(s.get("end"))
        if b <= ini_r - 0.05 or a >= fin_r + 0.05 or b <= a:
            continue
        # Recortar contra la región y contra lo ya cubierto.
        a, b = max(a, ini_r), min(b, fin_r)
        for c_ini, c_fin in covered:
            if c_ini <= a < c_fin:
                a = c_fin + 0.05
            if c_ini < b <= c_fin:
                b = c_ini - 0.05
        if b - a < 0.3:
            continue
        seg = {"start": round(a, 3), "end": round(b, 3), "text": texto,
               "gap_recovered": True}
        ws = [w for w in (s.get("words") or [])
              if isinstance(w, dict) and a - 0.05 <= _f(w.get("start")) <= b + 0.05]
        if ws:
            seg["words"] = ws
        lineas.append(seg)
    if lineas:
        return lineas
    return _lineas_desde_palabras(region, ini_r - 0.05)


def _recover_uncovered_regions(out: list[dict], wx_segs: list[dict],
                               words: list[dict]) -> list[dict]:
    """Devuelve `out` + líneas para toda región con voz que ninguna línea
    reclamó (huecos internos y cola). No-op cuando no sobran palabras
    (`_GAP_MIN_S` / `_GAP_MIN_WORDS`).

    Nunca levanta: ante cualquier problema devuelve `out` intacto."""
    if not out or not words:
        return out
    try:
        covered = [(_f(s.get("start")), _f(s.get("end"))) for s in out]
        recuperadas: list[dict] = []
        n_reg = 0
        for region in _regiones_huerfanas(out, words):
            span = _f(region[-1].get("end")) - _f(region[0].get("start"))
            if len(region) < _GAP_MIN_WORDS or span < _GAP_MIN_S:
                continue
            nuevas = _texto_para_region(region, wx_segs, covered)
            if nuevas:
                recuperadas.extend(nuevas)
                n_reg += 1
        if not recuperadas:
            return out

        logger.warning(
            "[RECONCILE] la referencia no cubre toda la voz: %d región(es) "
            "con canto sin letra — recuperadas %d línea(s)",
            n_reg, len(recuperadas),
        )
        return sorted(out + recuperadas, key=lambda s: _f(s.get("start")))
    except Exception as e:  # pragma: no cover — nunca romper la cascada
        logger.warning("[RECONCILE] recuperación de huecos declinó: %r", e)
        return out


def _flatten_words(wx_segs: list[dict]) -> list[dict]:
    """Concatenate the per-word arrays into one ordered stream. Skips segs
    without word-stamps (whisperX provides them only when align_output=True;
    forced_align doesn't, so reconciliation is whisperX-only)."""
    words: list[dict] = []
    for s in wx_segs or []:
        for w in s.get("words") or []:
            if isinstance(w, dict):
                words.append(w)
    return words


def reconcile(wx_segs: list[dict],
              reference_text: str,
              *, min_coverage: float = 0.5) -> list[dict] | None:
    """Re-bucket whisperX word-stamps into the reference text's line
    structure. Returns segments with reference text + whisperX timing, or
    None if reconciliation can't produce enough lines.

    `min_coverage` — fraction of reference lines that must be assigned
    timing for the result to be trusted. Below the threshold, the caller
    keeps whisperX's own segmentation."""
    words = _flatten_words(wx_segs)
    if len(words) < 8:
        logger.info("[RECONCILE] not enough word stamps (%s) — skip", len(words))
        return None
    lines = [ln.strip() for ln in (reference_text or "").splitlines() if ln.strip()]
    if len(lines) < 4:
        logger.info("[RECONCILE] reference too short (%s lines) — skip", len(lines))
        return None

    # `wordstamps_to_segments` (forced_align.py) is the hardened helper that
    # walks a word stream against a known line structure with fuzzy
    # re-anchoring + drift abort. Returns [] when it gives up.
    from forced_align import wordstamps_to_segments
    out = wordstamps_to_segments(words, lines)
    if not out:
        logger.warning("[RECONCILE] wordstamps_to_segments aborted (drift) — keep whisperX")
        return None
    coverage = len(out) / max(1, len(lines))
    if coverage < min_coverage:
        logger.warning(
            "[RECONCILE] thin coverage %s/%s (%.0f%%) — keep whisperX",
            len(out), len(lines), coverage * 100,
        )
        return None

    # Re-attach per-word stamps to each reconciled line so the editor can
    # still do word-level karaoke. We just bucket the same `words` again by
    # position so the words inside line N align with line N's text.
    line_word_counts = [len(ln.split()) for ln in lines if ln]
    cur = 0
    for i, seg in enumerate(out):
        # `out` may have fewer entries than lines (drop on monotonic clamp);
        # we can't reliably re-attach in that case, so leave words off.
        try:
            wc = line_word_counts[i]
        except IndexError:
            break
        span = words[cur:cur + wc]
        cur += wc
        if span and all(isinstance(w, dict) and "start" in w for w in span):
            seg["words"] = [
                {"word": w.get("word", "").strip(),
                 "start": float(w.get("start", seg["start"])),
                 "end": float(w.get("end", seg["end"]))}
                for w in span
            ]

    logger.info(
        "[RECONCILE] %s/%s lines reconciled (%.0f%% coverage) — adopting reference text + whisperX timing",
        len(out), len(lines), coverage * 100,
    )

    # Gate contra el AUDIO, ANTES de intentar parchear nada. `coverage` sólo
    # dice qué fracción de la REFERENCIA se ancló — y sube cuando la
    # referencia viene recortada. La pregunta que importa es la inversa:
    # ¿esta letra describe esta grabación? Si sólo cubre una minoría de lo
    # cantado, no la describe (otra edición, otro tema, letra recortada) y
    # reconciliar contra ella es peor que quedarse con la segmentación del
    # ASR, que sí oyó el audio. Recuperar huecos acá sería maquillar una
    # referencia que no corresponde.
    audio_cov = _audio_coverage(out, words)
    if audio_cov < _min_audio_coverage():
        logger.warning(
            "[RECONCILE] la letra sólo cubre el %.0f%% de lo cantado "
            "(%d/%d líneas de referencia ancladas) — la referencia no "
            "representa este audio, me quedo con whisperX",
            audio_cov * 100, len(out), len(lines),
        )
        return None
    logger.info("[RECONCILE] cobertura del audio: %.0f%%", audio_cov * 100)

    # Referencia válida pero con zonas sin cubrir (típico: falta el outro o
    # una repetición). Se conserva el texto curado y se rellenan los huecos.
    if _gap_recovery_enabled():
        out = _recover_uncovered_regions(out, wx_segs, words)

    return out


# Umbral de recuperación de coros sub-listados: un segmento no-matcheado
# cuyo texto suena >= esto a UNA línea de la referencia se corrige a esa
# línea (repara misheards de repeticiones que la letra no lista). Alto
# para no reescribir líneas únicas legítimas.
RECOVER_PHON_MIN = 0.72


def text_correct_segments(audio_segs: list[dict],
                          reference_text: str,
                          *, min_match: float = 0.60) -> list[dict]:
    """Audio-as-truth text correction (line-level).

    Keeps each audio segment's timing, order, structure and word-stamps; only
    swaps its TEXT to the best-matching reference line. Segments whose best
    reference match is below `min_match` (e.g. sustained ad-libs like
    "uh uh uh" that lyric sites omit) are returned UNCHANGED.

    Unlike `reconcile()` / `wordstamps_to_segments`, this does NOT flatten the
    word stream or re-bucket it into the reference's line structure — so a
    reference that LACKS the song's ad-lib section can never scatter the
    surrounding chorus words across the ad-lib gap (the smearing bug). It's a
    per-line spell-checker over whisperX's OWN segments, not a re-segmenter.

    Matching: per-segment best reference line via max(token-Jaccard,
    phonetic-ratio) — phonetic rescues mondegreens ("perro de voz" vs "espejo
    de vos") that Jaccard misses. Assignment is a globally-optimal MONOTONIC DP
    so a chorus repeated N times maps to its N reference occurrences in order
    (not all to the first). Returns a NEW list; never mutates the input.
    """
    if not audio_segs:
        return audio_segs
    out_default = [dict(s) for s in audio_segs]
    ref_lines = [ln.strip() for ln in (reference_text or "").splitlines() if ln.strip()]
    if len(ref_lines) < 2:
        return out_default  # nothing to correct against

    from forced_align import _norm, _phonetic_ratio
    from lyrics_cleanup_alignment import _jaccard, _tokens

    seg_txt = [(s.get("text") or "") for s in audio_segs]
    seg_jac = [_tokens(t) for t in seg_txt]
    seg_pho = [[_norm(w) for w in t.split() if _norm(w)] for t in seg_txt]
    ref_jac = [_tokens(l) for l in ref_lines]
    ref_pho = [[_norm(w) for w in l.split() if _norm(w)] for l in ref_lines]

    def score(i: int, j: int) -> float:
        return max(_jaccard(seg_jac[i], ref_jac[j]),
                   _phonetic_ratio(seg_pho[i], ref_pho[j]))

    # Un segmento de whisper puede abarcar DOS líneas de la referencia
    # cantadas de corrido ("…los indicios / de que nada es perfecto" en una
    # sola frase). Con matching 1:1 la segunda línea quedaba skip_ref y sus
    # palabras DESAPARECÍAN del output (caso Amanda Pujó, 03/07: dos frases
    # perdidas). match2 puntúa contra la concatenación de j y j+1.
    def score2(i: int, j: int) -> float:
        if j + 1 >= len(ref_lines):
            return 0.0
        return max(_jaccard(seg_jac[i], ref_jac[j] | ref_jac[j + 1]),
                   _phonetic_ratio(seg_pho[i], ref_pho[j] + ref_pho[j + 1]))

    n, m = len(audio_segs), len(ref_lines)
    EPS = 1e-6  # earliest-ref tie-break, same idea as _match_cleaned_to_synced
    # dp[i][j] = best total match score over segs[i:] using refs[j:].
    # actions: match 1:1 | match2 (1 seg ↔ 2 refs consecutivas) |
    # skip_ref (nunca cantada) | skip_seg (ad-lib).
    dp = [[0.0] * (m + 2) for _ in range(n + 1)]
    choice: list[list[str | None]] = [[None] * (m + 2) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best, act = dp[i + 1][j], "skip_seg"          # seg i unmatched
            if dp[i][j + 1] > best:
                best, act = dp[i][j + 1], "skip_ref"      # ref j unused
            sc = score(i, j)
            if sc >= min_match:
                mv = sc + EPS * (m - j) + dp[i + 1][j + 1]
                if mv > best:
                    best, act = mv, "match"
            sc2 = score2(i, j)
            if sc2 >= min_match:
                # −1e-4: ante empate real gana el match simple (no partir
                # una línea sana en dos por un empate de Jaccard).
                mv2 = sc2 - 1e-4 + EPS * (m - j) + dp[i + 1][j + 2]
                if mv2 > best:
                    best, act = mv2, "match2"
            dp[i][j], choice[i][j] = best, act

    assign: list = [None] * n           # int (1:1) | ("pair", j) | None
    i = j = 0
    while i < n and j < m:
        act = choice[i][j]
        if act == "match":
            assign[i] = j; i += 1; j += 1
        elif act == "match2":
            assign[i] = ("pair", j); i += 1; j += 2
        elif act == "skip_ref":
            j += 1
        else:
            i += 1

    # Build output:
    #   - matched seg → swap text to its reference line.
    #   - unmatched seg with real text (ad-lib "uh") → keep as-heard.
    #   - unmatched BLANK seg (a melisma whisper heard but couldn't transcribe,
    #     e.g. a sustained "Frágil espejo de vos") → name it from the reference
    #     line skipped right here (timing stays the audio segment's, flagged
    #     `review`); drop it if no reference line fits. This is SAFE: it only
    #     names segments the audio already produced — it never fabricates a line
    #     where there's no audio (which would spam phantom chorus repeats).
    matched_refs = set()
    for a in assign:
        if isinstance(a, tuple):
            matched_refs.add(a[1]); matched_refs.add(a[1] + 1)
        elif a is not None:
            matched_refs.add(a)
    out: list[dict] = []
    n_swapped = n_filled = n_split = n_recovered = 0
    last_ref = -1
    for idx, s in enumerate(audio_segs):
        j = assign[idx]
        if isinstance(j, tuple):
            # 1 segmento ↔ 2 líneas de referencia: partirlo en el límite de
            # palabra que mejor separa ambas líneas (word-stamps si hay;
            # proporcional por tokens si no). Las DOS líneas sobreviven.
            ja = j[1]
            a, b = _split_pair_segment(s, ref_lines[ja], ref_lines[ja + 1],
                                       _jaccard, _tokens)
            out.append(a); out.append(b)
            n_swapped += 1; n_split += 1
            last_ref = ja + 1
            continue
        if j is not None:
            seg = dict(s)
            if (seg.get("text") or "").strip() != ref_lines[j]:
                n_swapped += 1
            seg["text"] = ref_lines[j]
            out.append(seg)
            last_ref = j
            continue
        if (s.get("text") or "").strip():
            # Recuperación de coros sub-listados (Amanda Pujó, 05/07): la
            # referencia lista el coro 2× pero la canción lo canta 3×; la
            # 3ª ocurrencia queda sin línea de referencia libre y el DP la
            # deja COMO-OÍDA (mishear "Frágiles vientos de vos" en vez de
            # "frágil espejo de vos", que suena 0.81 fonético). Si el
            # segmento suena casi idéntico a ALGUNA línea de la referencia
            # (usada o no), corregir el TEXTO a esa línea — no fabrica
            # nada, repara un mishear de una línea que SÍ está en la letra.
            # Umbral alto para no pisar líneas únicas legítimas; excluye
            # ad-libs (su fonética contra la letra es baja de por sí).
            best_j, best_sc = None, RECOVER_PHON_MIN
            for rj in range(m):
                sc = _phonetic_ratio(seg_pho[idx], ref_pho[rj])
                if sc > best_sc:
                    best_sc, best_j = sc, rj
            if best_j is not None:
                seg = dict(s)
                seg["text"] = ref_lines[best_j]
                seg["review"] = True
                out.append(seg)
                n_recovered += 1
                continue
            out.append(dict(s))   # ad-lib / unmatched-but-real text → keep as-heard
            continue
        # blank segment: name it from the next genuinely-skipped reference line
        cand = last_ref + 1
        if cand < m and cand not in matched_refs:
            seg = dict(s)
            seg["text"] = ref_lines[cand]
            seg["review"] = True
            out.append(seg)
            last_ref = cand
            n_filled += 1
        # else: drop the empty segment

    logger.info(
        "[TEXT-CORRECT] %s corrected (%s split, %s chorus-recovered), "
        "%s blanks named, %s segs out",
        n_swapped, n_split, n_recovered, n_filled, len(out),
    )
    return out


# Tunables for ad-lib relabeling.
_ADLIB_MIN_TAIL_S = 4.0   # wordless span (segment end − last word end) to treat as ad-lib
_ADLIB_CHUNK_S = 9.0      # target length of each "Uh" line (~ROTOR's 3 lines for a 28 s block)


def _uh_text(seconds: float) -> str:
    """A line of "Uh, uh, uh …" with ~1 'uh' per second (cosmetic, like ROTOR)."""
    n = max(3, min(round(seconds), 12))
    return "Uh" + ", uh" * (n - 1)


def relabel_long_adlibs(segs: list[dict],
                        *, min_tail: float = _ADLIB_MIN_TAIL_S,
                        chunk: float = _ADLIB_CHUNK_S) -> list[dict]:
    """Replace WORDLESS sustained-vocal spans with "Uh".

    Whisper emits a few words for a long ad-lib and leaves the rest of the audio
    WITHOUT word-stamps — e.g. a segment "¿Para qué? Tus santos de papel" whose
    words all end by 69.8 s but whose audio runs to 81 s (an 11 s wordless "uh"
    tail). Left alone, text-correct matches the whole thing to a chorus line and
    the ad-lib is lost.

    For each segment we KEEP the worded part (trimmed to its last word — its real
    text survives so text-correct can name it, e.g. recovering the chorus Whisper
    merged in front of the ad-lib) and relabel only the trailing WORDLESS span
    (>= `min_tail`s) as "Uh" lines of ~`chunk`s. Real lyrics have words spanning
    the whole segment → no wordless tail → untouched. Run BEFORE text-correct so
    the "Uh" lines have no reference match and survive. Returns a NEW list.
    """
    out: list[dict] = []
    n_relabeled = 0
    for s in segs:
        words = [w for w in (s.get("words") or []) if isinstance(w, dict) and "end" in w]
        st = float(s.get("start", 0.0))
        en = float(s.get("end", st))
        if not words:
            out.append(dict(s))
            continue
        last_word_end = max(float(w["end"]) for w in words)
        if en - last_word_end < min_tail:
            out.append(dict(s))            # words span the segment → real lyric, keep
            continue
        # keep the worded head (trimmed to its words), relabel the wordless tail
        out.append({**dict(s), "end": round(last_word_end, 3)})
        n = max(1, round((en - last_word_end) / chunk))
        step = (en - last_word_end) / n
        for k in range(n):
            a = round(last_word_end + k * step, 3)
            b = round(last_word_end + (k + 1) * step, 3)
            out.append({"start": a, "end": b, "text": _uh_text(step)})
        n_relabeled += 1
    if n_relabeled:
        logger.info("[ADLIB] relabeled %s wordless ad-lib tail(s) as 'Uh'", n_relabeled)
    return out


def _split_pair_segment(seg: dict, line_a: str, line_b: str,
                        _jaccard, _tokens) -> tuple[dict, dict]:
    """Parte un segmento que contiene DOS líneas de referencia cantadas de
    corrido. Corte en el límite de palabra que maximiza el match de cada
    mitad con su línea; sin word-stamps, proporcional por cantidad de
    tokens. Ambas mitades conservan el resto de los keys del segmento."""
    words = seg.get("words") or []
    s0, e0 = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
    ta, tb = _tokens(line_a), _tokens(line_b)
    if len(words) >= 2:
        best_k, best_sc = None, -1.0
        for k in range(1, len(words)):
            left = _tokens(" ".join(w.get("word", "") for w in words[:k]))
            right = _tokens(" ".join(w.get("word", "") for w in words[k:]))
            sc = _jaccard(left, ta) + _jaccard(right, tb)
            if sc > best_sc:
                best_sc, best_k = sc, k
        cut_end = float(words[best_k - 1].get("end", s0))
        cut_start = float(words[best_k].get("start", cut_end))
        wa, wb = words[:best_k], words[best_k:]
    else:
        frac = len(line_a.split()) / max(1, len(line_a.split()) + len(line_b.split()))
        cut_end = cut_start = s0 + (e0 - s0) * frac
        wa = wb = None
    a, b = dict(seg), dict(seg)
    a["text"], a["end"] = line_a, round(cut_end, 3)
    b["text"], b["start"] = line_b, round(cut_start, 3)
    if wa is not None:
        a["words"], b["words"] = wa, wb
    else:
        a.pop("words", None); b.pop("words", None)
    return a, b
