"""Gate texto-vs-audio del fallback best-effort de wordstamps_to_segments.

El caso real (job 6f4047db): la referencia lista la estrofa 2 ANTES de la
segunda tanda de estribillos, pero el audio canta el estribillo. El matcher
no encuentra ancla (best_score < 0.5) y aun así EMITÍA la línea en la
posición del cursor — estrofa pintada sobre audio de estribillo, lugar real
vacío, y la recuperación de huecos lo rellenaba después → texto duplicado.

Con ANCHOR_TEXT_GATE_ENABLED, la línea no-anclada que ni siquiera SUENA a
la ventana donde caería se descarta; su zona queda como hueco recuperable.
"""
import pytest

from forced_align import wordstamps_to_segments


def _stamps(texto, ini, paso=0.5):
    out = []
    t = ini
    for w in texto.split():
        out.append({"word": w, "start": round(t, 2), "end": round(t + 0.4, 2)})
        t += paso
    return out


# El audio canta el estribillo dos veces; la "referencia" mete en el medio
# una línea de estrofa que el audio NO canta ahí.
CORO = "estuve rodando por ahi vagando sin parar"
ESTROFA_FANTASMA = "cuando los meses pasan y mi ropa no cambia"

WORDS = _stamps(CORO, 10.0) + _stamps(CORO, 20.0) + _stamps(CORO, 30.0)
LINEAS = [CORO, ESTROFA_FANTASMA, CORO, CORO]


def test_sin_gate_emite_la_linea_fantasma_mal_ubicada(monkeypatch):
    """Comportamiento histórico (gate off): la línea que no ancla se emite
    igual en la posición del cursor — el bug documentado."""
    monkeypatch.delenv("ANCHOR_TEXT_GATE_ENABLED", raising=False)
    out = wordstamps_to_segments(WORDS, LINEAS)
    textos = [s["text"] for s in out]
    assert ESTROFA_FANTASMA in textos, \
        "el baseline emite la fantasma; si esto cambió, revisar el gate"


def test_con_gate_descarta_la_linea_fantasma(monkeypatch):
    monkeypatch.setenv("ANCHOR_TEXT_GATE_ENABLED", "1")
    out = wordstamps_to_segments(WORDS, LINEAS)
    textos = [s["text"] for s in out]
    assert ESTROFA_FANTASMA not in textos, \
        "la línea que no suena a su ventana debe descartarse"
    # Las líneas legítimas sobreviven.
    assert textos.count(CORO) == 3


def test_con_gate_no_toca_lineas_bien_ancladas(monkeypatch):
    """Con una referencia sana, el gate es no-op exacto."""
    monkeypatch.setenv("ANCHOR_TEXT_GATE_ENABLED", "1")
    lineas = [CORO, CORO, CORO]
    con = wordstamps_to_segments(WORDS, lineas)
    monkeypatch.delenv("ANCHOR_TEXT_GATE_ENABLED", raising=False)
    sin = wordstamps_to_segments(WORDS, lineas)
    assert con == sin
    assert len(con) == 3


def test_gate_respeta_el_drift_abort(monkeypatch):
    """El strike de drift se cuenta ANTES del descarte: 4 líneas fantasma
    seguidas siguen abortando el path completo (return [])."""
    monkeypatch.setenv("ANCHOR_TEXT_GATE_ENABLED", "1")
    fantasmas = [
        "una frase totalmente distinta numero uno",
        "otra frase que tampoco se canta jamas",
        "tercera linea inventada por la referencia",
        "cuarta linea que no existe en el audio",
    ]
    out = wordstamps_to_segments(WORDS, [CORO] + fantasmas + [CORO])
    assert out == [], "4 no-anclas seguidas deben abortar como siempre"


def test_gate_mishear_acustico_sobrevive(monkeypatch):
    """Un mishear que SUENA a la ventana (caso Legalícenla) no se descarta:
    el gate usa ratio fonético, no igualdad de tokens."""
    monkeypatch.setenv("ANCHOR_TEXT_GATE_ENABLED", "1")
    words = (_stamps("le realizan la vida entera con sus manos", 10.0)
             + _stamps("hubo tiempos de guerras y de amores", 20.0))
    lineas = ["legalicenla vida entera con sus manos",
              "hubo tiempos de guerras y de amores"]
    out = wordstamps_to_segments(words, lineas)
    assert len(out) == 2, "el mishear fonéticamente cercano debe emitirse"
