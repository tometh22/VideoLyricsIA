"""Tarifas por llamada derivadas de la factura en vez de estimadas.

Ningún proveedor de IA devuelve el costo en la respuesta, así que la única
fuente real es la factura. Medido contra dos meses cerrados: Veo estaba
cargado a $0,80 de lista y la factura da ~$0,62, o sea que el panel
sobreestimaba ~25% — y ese error se propagaba al costo por canción, al
margen por tenant y al tamaño del desperdicio.
"""

from types import SimpleNamespace

import pytest

import rate_calibration as rc
from billing_sources import SourceCost, gcp_cost_by_tool


# ---------------------------------------------------------------------------
# Mapear SKUs de la factura a herramientas
# ---------------------------------------------------------------------------

def test_separa_veo_de_gemini_dentro_de_vertex():
    """"Vertex AI" como servicio mezcla Veo, Imagen y Gemini, y sus costos
    unitarios difieren ~50x. Sólo la línea de SKU los separa."""
    src = SourceCost("gcp", "2026-07", breakdown=[
        {"service": "Vertex AI", "sku": "Veo 3.1 Fast Video Generation", "cost": 303.0},
        {"service": "Vertex AI", "sku": "Gemini 2.5 Flash Input Tokens", "cost": 6.0},
        {"service": "Vertex AI", "sku": "Imagen 4 Ultra Image", "cost": 1.5},
        {"service": "Cloud Storage", "sku": "Standard Storage", "cost": 2.5},
    ])
    assert gcp_cost_by_tool(src) == {
        "veo": 303.0, "gemini": 6.0, "imagen": 1.5, "otros": 2.5}


def test_sku_desconocido_va_a_otros_no_se_pierde():
    """Un SKU que no reconocemos es plata que igual se gastó. Perderlo haría
    que las tarifas derivadas parezcan más baratas de lo que son."""
    src = SourceCost("gcp", "2026-07", breakdown=[
        {"service": "X", "sku": "Servicio Nuevo Que No Conocemos", "cost": 40.0},
    ])
    assert gcp_cost_by_tool(src) == {"otros": 40.0}


# ---------------------------------------------------------------------------
# Derivar la tarifa
# ---------------------------------------------------------------------------

def _fake_sessions(calls_por_entorno):
    """Sesiones falsas que devuelven (tool_name, n) por entorno."""
    class _Q:
        def __init__(self, rows): self.rows = rows
        def filter(self, *a, **k): return self
        def group_by(self, *a, **k): return self
        def all(self): return self.rows

    class _S:
        def __init__(self, rows): self.rows = rows
        def query(self, *a, **k): return _Q(self.rows)

    return {name: _S(rows) for name, rows in calls_por_entorno.items()}


def test_reproduce_la_tarifa_real_de_julio():
    """Datos reales: 487 llamadas Veo entre los dos entornos, $313 de Google
    de los cuales ~$303 son el SKU de Veo → ~$0,62 por llamada, no $0,80."""
    sessions = _fake_sessions({
        "prod": [("veo-3.1-fast-generate-001", 208)],
        "staging": [("veo-3.1-fast-generate-001", 279)],
    })
    out = rc.derive_rates(sessions, {"veo": 303.0}, "2026-07")
    veo = next(r for r in out["rates"] if r["tool"] == "veo")

    assert veo["calls"] == 487
    assert veo["derived_rate"] == pytest.approx(0.622, abs=0.005)
    assert veo["status"] == "ok"
    # La estimada de la tabla era $0,80 → el panel iba ~25% alto.
    assert veo["estimated_rate"] == 0.80
    assert veo["drift"] == pytest.approx(0.78, abs=0.02)
    assert out["applied"]["veo"] == pytest.approx(0.622, abs=0.005)


def test_contar_un_solo_entorno_duplicaria_la_tarifa():
    """Staging y prod comparten proyecto de GCP: la factura cubre ambos. Es
    el error que hace que este cálculo tenga que exigir los dos."""
    solo_prod = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 208)]}),
        {"veo": 303.0}, "2026-07")
    ambos = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 208)],
                        "staging": [("veo-3.1-fast-generate-001", 279)]}),
        {"veo": 303.0}, "2026-07")
    r1 = next(r for r in solo_prod["rates"] if r["tool"] == "veo")["derived_rate"]
    r2 = next(r for r in ambos["rates"] if r["tool"] == "veo")["derived_rate"]
    assert r1 > r2 * 2


def test_muestra_chica_no_calibra():
    """Un mes con 3 llamadas y un cargo mínimo daría una tarifa absurda que
    después se aplica a miles."""
    out = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 3)]}),
        {"veo": 30.0}, "2026-07")
    veo = next(r for r in out["rates"] if r["tool"] == "veo")
    assert veo["status"] == "muestra_chica"
    assert "veo" not in out["applied"]


def test_tarifa_implausible_se_reporta_pero_no_se_aplica():
    """Si la derivada se dispara respecto de la estimada, casi seguro el SKU
    está mal mapeado (la línea de Veo capturando almacenamiento, por
    ejemplo). Mejor reportarlo que envenenar toda la atribución."""
    out = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 100)]}),
        {"veo": 5000.0}, "2026-07")          # $50/llamada
    veo = next(r for r in out["rates"] if r["tool"] == "veo")
    assert veo["status"] == "implausible"
    assert "veo" not in out["applied"]
    assert "SKU" in veo["reason"]


def test_credito_de_factura_no_genera_tarifa_negativa():
    """Un ajuste neto negativo se informa, pero no contamina costos futuros."""
    out = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 100)]}),
        {"veo": -62.0}, "2026-07")
    veo = next(r for r in out["rates"] if r["tool"] == "veo")
    assert veo["invoiced_usd"] == -62.0
    assert veo["derived_rate"] is None
    assert veo["drift"] is None
    assert veo["status"] == "ajuste_no_positivo"
    assert "veo" not in out["applied"]


def test_sin_factura_no_inventa_tarifa():
    out = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 300)]}),
        {}, "2026-07")
    veo = next(r for r in out["rates"] if r["tool"] == "veo")
    assert veo["status"] == "sin_factura"
    assert veo["derived_rate"] is None
    assert out["applied"] == {}


def test_cada_herramienta_calibra_por_separado():
    out = rc.derive_rates(
        _fake_sessions({"prod": [("veo-3.1-fast-generate-001", 400),
                                 ("gemini-2.5-flash", 500)]}),
        {"veo": 248.0, "gemini": 5.0}, "2026-07")
    rates = {r["tool"]: r for r in out["rates"]}
    assert rates["veo"]["derived_rate"] == pytest.approx(0.62, abs=0.01)
    assert rates["gemini"]["derived_rate"] == pytest.approx(0.01, abs=0.001)


# ---------------------------------------------------------------------------
# Lectura de tarifas
# ---------------------------------------------------------------------------

def test_rate_for_tool_matchea_por_prefijo():
    cal = {"veo": 0.62, "gemini": 0.008}
    assert rc.rate_for_tool("veo-3.1-fast-generate-001", cal) == 0.62
    assert rc.rate_for_tool("gemini-2.5-flash-vision", cal) == 0.008
    assert rc.rate_for_tool("whisper-1", cal) is None


def test_load_applied_rates_degrada_a_vacio_si_falla(monkeypatch):
    """Una calibración ausente o rota debe caer a la tabla estimada, nunca
    romper el panel."""
    class _Boom:
        def query(self, *a, **k): raise RuntimeError("tabla no existe")
    assert rc.load_applied_rates(_Boom(), "2026-07") == {}


def test_load_applied_rates_ignora_las_no_ok():
    row = SimpleNamespace(breakdown=[
        {"tool": "veo", "derived_rate": 0.62, "status": "ok"},
        {"tool": "imagen", "derived_rate": 9.9, "status": "implausible"},
        {"tool": "gemini", "derived_rate": None, "status": "sin_factura"},
    ])

    class _Q:
        def filter(self, *a, **k): return self
        def one_or_none(self): return row

    class _S:
        def query(self, *a, **k): return _Q()

    assert rc.load_applied_rates(_S(), "2026-07") == {"veo": 0.62}


# ---------------------------------------------------------------------------
# Guarda contra snapshot rancio
# ---------------------------------------------------------------------------
#
# `rate_for_tool` aplica la calibración como MULTIPLICADOR sobre la lista
# viva: `lista × derivada/estimada`. Eso conserva el precio relativo dentro
# de una familia (un Veo Fast y un Standard no valen igual), pero sólo
# funciona si `estimada` —congelada al calibrar— sigue siendo del mismo
# orden que la lista de hoy.
#
# Pasó de verdad: la calibración de jul-2026 se derivó en un proceso sin
# `VEO_CLIP_SECONDS` (estimada $0,80) mientras la app corre con 4 (lista
# $0,40). El panel valuó Veo a $0,146058 contra una factura de $0,292116 —
# la MITAD, y en la dirección que infla la utilidad repartible. Nada avisó,
# y la única defensa era acordarse de calibrar desde el proceso correcto.

def test_un_snapshot_derivado_con_otra_lista_no_parte_la_tarifa():
    import rate_calibration as rc
    from provenance import COST_PER_CALL

    MODELO = "veo-3.1-fast-generate-001"
    lista = COST_PER_CALL[(MODELO, "google_vertex")]
    derivada = 0.292116
    est = lista * 1.25          # promedio de familia: difiere de la lista, y está bien

    base = f"{rc.BASE_KEY_PREFIX}{MODELO}"

    # Snapshot COHERENTE: se calibró con la misma lista que hay hoy.
    ok = rc.rate_for_tool(MODELO, {
        "veo": derivada, "veo::estimada": est, base: lista})
    assert ok == pytest.approx(round(lista * (derivada / est), 6), abs=1e-6)

    # Snapshot RANCIO: se calibró cuando la lista de ESE modelo valía el
    # doble (es lo que pasa al cambiar VEO_CLIP_SECONDS). No puede aplicar
    # el multiplicador: cae a la derivada plana, que sigue cuadrando contra
    # la factura.
    rancio = rc.rate_for_tool(MODELO, {
        "veo": derivada, "veo::estimada": est, base: lista * 2})
    assert rancio == pytest.approx(derivada, abs=1e-6), (
        f"aplicó un multiplicador rancio: {rancio}")


def test_sin_base_guardada_se_comporta_como_antes():
    """Compatibilidad: los snapshots viejos no tienen `list_basis`.

    Sin esa clave no hay con qué comparar, así que se aplica el
    multiplicador como siempre — degradar a plano ahí sería romper meses ya
    calibrados por una protección que no puede evaluar.
    """
    import rate_calibration as rc
    from provenance import COST_PER_CALL

    MODELO = "veo-3.1-fast-generate-001"
    lista = COST_PER_CALL[(MODELO, "google_vertex")]
    r = rc.rate_for_tool(MODELO, {"veo": 0.8, "veo::estimada": 1.0})
    assert r == pytest.approx(round(lista * 0.8, 6), abs=1e-6)
