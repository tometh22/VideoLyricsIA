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
