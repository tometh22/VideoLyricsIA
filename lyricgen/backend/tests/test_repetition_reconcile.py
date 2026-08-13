"""Reconciliación de repeticiones: la ocurrencia del estribillo que faltó.

Caso real (job 6f4047db): el audio canta el estribillo 7 veces, la
referencia lo lista 6 → el grupo entero quedó corrido ~5,6 s y la
ocurrencia sin línea apareció como región huérfana (209–217 s). Los
fixtures reproducen esa geometría con texto neutro.
"""
import pytest

import repetition_reconcile as rr


CORO = "estuve girando por alla sin parar"
OTRO = "girando por alla girando por alla"          # grupo distinto, suena parecido a medias
VERSO = "cuando la tarde cae sobre el rio dorado"   # no repite


def _words_de(texto, ini, paso=0.55):
    out, t = [], ini
    for w in texto.split():
        out.append({"word": w, "start": round(t, 2), "end": round(t + 0.45, 2)})
        t += paso
    return out


def _seg(texto, ini, fin, **extra):
    d = {"start": ini, "end": fin, "text": texto,
         "words": _words_de(texto, ini)}
    d.update(extra)
    return d


def _fixture_real():
    """6 miembros del coro (con hueco entre el 4º y el 5º), 1 ocurrencia
    huérfana en 209-217, y palabras ASR para TODO lo cantado."""
    miembros = [
        _seg(CORO, 86.3, 89.7, ctc_lr=-0.212),
        _seg(CORO, 193.6, 197.0, ctc_lr=-0.217),
        _seg(CORO, 199.6, 203.0, ctc_lr=-0.383),
        _seg(CORO, 205.6, 209.0, ctc_lr=-0.078),
        _seg(CORO, 221.6, 225.0, ctc_lr=-0.178),
        _seg(CORO, 227.6, 231.0, ctc_lr=-0.037),
    ]
    relleno = [_seg(VERSO, 30.0, 34.0), _seg(VERSO + " otra vez", 40.0, 45.0)]
    segs = sorted(relleno + miembros, key=lambda s: s["start"])
    # ASR: todo lo que los carteles cubren MÁS la ocurrencia huérfana.
    asr = []
    for s in segs:
        asr.extend(s["words"])
    huerfana = _words_de(CORO, 210.0)          # ~210.0-213.3, sin cartel
    asr.extend(huerfana)
    asr.sort(key=lambda w: w["start"])
    return segs, asr


def test_inserta_la_ocurrencia_faltante_geometria_real():
    segs, asr = _fixture_real()
    out, stats = rr.reconcile(segs, asr)
    assert stats["inserted"] == 1, stats
    nuevos = [s for s in out if s.get("repetition_recovered")]
    assert len(nuevos) == 1
    n = nuevos[0]
    assert 209.0 <= n["start"] <= 210.5 and n["end"] <= 214.5
    assert n["text"] == CORO                      # texto curado del grupo
    assert n["words"], "la insertada lleva las palabras reales del ASR"
    assert "ctc_lr" not in n
    assert [s["start"] for s in out] == sorted(s["start"] for s in out)


def test_no_toca_nada_sin_ocurrencia_huerfana():
    segs, asr = _fixture_real()
    # ASR sin la ocurrencia extra: todo lo cantado ya tiene cartel.
    asr_sin = [w for w in asr if not (209.5 <= w["start"] <= 214.0)]
    out, stats = rr.reconcile(segs, asr_sin)
    assert stats["inserted"] == 0 and stats["reassigned"] == 0
    assert [s["text"] for s in out] == [s["text"] for s in segs]


def test_decline_sin_asr_words():
    segs, _ = _fixture_real()
    out, stats = rr.reconcile(segs, [])
    assert out == segs
    assert ("all", "sin_asr_words") in stats["declined"]


def test_decline_si_la_region_no_suena_al_grupo():
    segs, asr = _fixture_real()
    # La región huérfana canta OTRA cosa (un puente, no el coro).
    asr = [w for w in asr if not (209.5 <= w["start"] <= 214.0)]
    asr.extend(_words_de("un puente instrumental hablado distinto", 210.0))
    asr.sort(key=lambda w: w["start"])
    out, stats = rr.reconcile(segs, asr)
    assert stats["inserted"] == 0


def test_decline_por_ambiguedad_entre_grupos():
    """Si la región suena casi igual a dos grupos, no se inserta nada."""
    segs, asr = _fixture_real()
    # Agregar un segundo grupo cuyo texto comparte casi todo con el coro.
    casi = CORO + " ya"
    for ini in (120.0, 130.0, 140.0):
        segs.append(_seg(casi, ini, ini + 4.2))
    segs.sort(key=lambda s: s["start"])
    for s in segs[-3:]:
        asr.extend(s["words"])
    asr.sort(key=lambda w: w["start"])
    out, stats = rr.reconcile(segs, asr)
    assert stats["inserted"] == 0
    assert any(r == "run_ambigua_entre_grupos" for _, r in stats["declined"])


def test_muchas_huerfanas_inserta_hasta_el_tope_por_mejor_ratio():
    """Un outro repetitivo puede tener VARIAS ocurrencias huérfanas legítimas
    (el caso real tiene 4). No se declina: se insertan las de mejor ratio
    hasta _MAX_INSERTS_PER_GROUP, y la salida queda monótona."""
    # 4 miembros cada 20s; el audio canta el coro TAMBIÉN entre medio y
    # después: 6 ocurrencias huérfanas dentro del envolvente (1.5x cadencia).
    miembros = [_seg(CORO, ini, ini + 3.4, ctc_lr=-0.05)
                for ini in (100.0, 120.0, 140.0, 160.0)]
    asr = [w for s in miembros for w in s["words"]]
    for ini in (108.0, 128.0, 148.0, 168.0, 176.0, 184.0):
        asr.extend(_words_de(CORO, ini))
    asr.sort(key=lambda w: w["start"])
    out, stats = rr.reconcile(miembros, asr)
    assert stats["inserted"] == rr._MAX_INSERTS_PER_GROUP
    assert any(r == "huerfanas_extra_recortadas" for _, r in stats["declined"])
    starts = [s["start"] for s in out]
    assert starts == sorted(starts)


def test_grupos_chicos_no_participan():
    """min_group=3: un texto que aparece 2 veces no habilita inserts."""
    segs = [_seg(CORO, 10.0, 13.4), _seg(CORO, 20.0, 23.4),
            _seg(VERSO, 30.0, 34.0)]
    asr = [w for s in segs for w in s["words"]] + _words_de(CORO, 40.0)
    asr.sort(key=lambda w: w["start"])
    out, stats = rr.reconcile(segs, asr)
    assert stats["inserted"] == 0


def test_reassign_mueve_solo_flotantes_con_lr_outlier():
    """Tier 2: un miembro sin onset ASR debajo Y con ctc_lr outlier se mueve
    a la región huérfana; uno flotante pero con ctc_lr sano NO."""
    miembros = [
        _seg(CORO, 100.0, 103.4, ctc_lr=-0.03),
        _seg(CORO, 110.0, 113.4, ctc_lr=-0.02),
        _seg(CORO, 120.0, 123.4, ctc_lr=-0.30),   # flotante + outlier
    ]
    asr = []
    for s in miembros[:2]:
        asr.extend(s["words"])
    # El 3º no tiene palabras debajo (flota); lo cantado real está en 126.
    asr.extend(_words_de(CORO, 126.0))
    asr.sort(key=lambda w: w["start"])
    # Sacarle las words al flotante para que su zona quede huérfana de verdad.
    miembros[2] = dict(miembros[2]); miembros[2].pop("words")
    out, stats = rr.reconcile(miembros, asr)
    assert stats["reassigned"] == 1
    movido = [s for s in out if s.get("repetition_reassigned")]
    assert len(movido) == 1 and 125.5 <= movido[0]["start"] <= 126.5
    assert "ctc_lr" not in movido[0]


def test_lead_y_hold_con_clamp():
    segs, asr = _fixture_real()
    out, stats = rr.reconcile(segs, asr, lead_s=0.15, hold_s=0.25)
    n = [s for s in out if s.get("repetition_recovered")][0]
    assert 209.5 <= n["start"] <= 210.0            # lead aplicado con clamp
    i = out.index(n)
    if i + 1 < len(out):
        assert n["end"] <= out[i + 1]["start"]     # hold nunca pisa al vecino


def test_nunca_levanta_con_basura():
    out, stats = rr.reconcile(
        [{"start": "x"}, None, 42],                # type: ignore[list-item]
        [{"word": "a", "start": 1, "end": 2}] * 10,
    )
    assert isinstance(out, list) and isinstance(stats, dict)
