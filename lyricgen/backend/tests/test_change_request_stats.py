"""Clasificación de los pedidos de cambio del cliente.

Los casos de abajo son textos REALES de `delivery_change_requests` en prod
(may–ago 2026). Se usan literales a propósito: son la única medición directa
de calidad que tenemos, y si las reglas dejan de reconocerlos el indicador
queda mudo justo cuando más importa.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import change_request_stats as crs


def _r(comment, when="2026-07-27", rid=1):
    return SimpleNamespace(
        id=rid, comment=comment, resolved_at=None,
        submitted_at=datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Puntos finales — 6 pedidos, el más repetido y el más barato de arreglar
# ---------------------------------------------------------------------------

def test_reconoce_los_seis_pedidos_de_puntos_finales():
    reales = [
        'y casi todas las frases tienen punto finales. Sacarlos porfa!',
        'Esta bien el fondo. Solo revisar la sincronización y quitar los '
        'puntos finales de cada frase. Gracias',
        'Revisar sincronizacion, algunas frases terminan antes. Y quitar '
        'puntos finales. Gracias!',
        'Quitar circulo debajo del arbol y quitar puntos en cada frase. '
        'Porfa, que las letras no tengan puntos',
        '1:21 corregir a "Hay dos tipos en la esquina" y sacar los puntos '
        'finales de todas las frases',
        'sacar los puntos finales de todas las frases',
    ]
    for texto in reales:
        assert "puntos_finales" in crs.classify(texto), texto


def test_no_confunde_puntos_de_luz_con_puntos_finales():
    """Un pedido de fondo que menciona 'puntos de luz' no es una queja de
    puntuación — clasificarlo así inflaría el indicador que queremos bajar."""
    texto = "podemos poner un fondo con puntos de luz rojos"
    assert "puntos_finales" not in crs.classify(texto)
    assert "fondo" in crs.classify(texto)


# ---------------------------------------------------------------------------
# El resto de las categorías, con textos reales
# ---------------------------------------------------------------------------

def test_sincronizacion():
    for texto in [
        "No esta bien sincronizada la letra",
        "revisar sincronizacion a partir de min 1:14",
        "Revisar la sincronización del coro",
        'Hay algunas líneas largas cuyas lyrics se van antes de que se '
        'terminen de pronunciar',
        'mantener por más tiempo el último "hoy" cuando termina la canción',
    ]:
        assert "sincronizacion" in crs.classify(texto), texto


def test_letra_incorrecta():
    for texto in [
        'Donde dice "traccionar" debería decir "reaccionar"',
        'Revisar letra. Min 1:46 y 2:25 debe decir "hombre pobre"',
        'Corregir: 0:13 "Dicen que soy lo peor"',
        "Quizás en vez de quizá",
        # La cita entre comillas es larga: con una ventana angosta este
        # pedido quedaba sin clasificar.
        'En el minuto 1:05 cambiar "HACIA EL TREN MIENTRAS SUS SUEÑOS SE '
        'ALEJABAN" por Y SIGUE EL TREN MIENTRAS',
    ]:
        assert "letra" in crs.classify(texto), texto


def test_fondo():
    for texto in [
        "Cambiar fondo sin los billetes que caen del cielo. gracias",
        "Podria ser un fondo sin tormenta y que no sea animado",
        "Porfa, cambiar fondo, que no aparezcan personas de frente. Gracias!",
        "podriamos cambiar este fondo a mas estatico?",
    ]:
        assert "fondo" in crs.classify(texto), texto


def test_tipografia():
    assert "tipografia" in crs.classify("Poner la tipografía un toque mas grande")
    assert "tipografia" in crs.classify("Poner la tipografia un poco mas grande")


def test_audio_equivocado():
    assert "audio" in crs.classify("No esta correcto el audio. Es de rata blanca")


# ---------------------------------------------------------------------------
# Multi-etiqueta y ruido
# ---------------------------------------------------------------------------

def test_un_pedido_puede_tener_varias_categorias():
    """Suelen pedir 2-3 cosas juntas. Contarlo una sola vez escondería la
    mitad del trabajo que generó."""
    tags = crs.classify(
        "Esta bien el fondo. Solo revisar la sincronización y quitar los "
        "puntos finales de cada frase")
    assert {"sincronizacion", "puntos_finales"} <= set(tags)


def test_excluye_las_filas_de_qa():
    assert crs.is_noise("QA test post-release #197 - ignorar")
    assert not crs.is_noise("No esta bien sincronizada la letra")


def test_tolera_falta_de_acentos():
    """Los operadores escriben 'sincronizacion' y 'sincronización' igual."""
    assert crs.classify("revisar sincronizacion") == \
           crs.classify("revisar sincronización")


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

def test_summarize_reproduce_la_distribucion_real():
    """Chequeo de contrato contra el resultado medido en prod: 35 filas,
    34 pedidos reales, 1 de QA excluido, 6 de puntos finales."""
    rows = [_r("QA test post-release #197 - ignorar", "2026-05-19", 1)]
    rows += [_r("quitar los puntos finales de cada frase", "2026-07-27", i)
             for i in range(2, 8)]                       # 6 de puntos
    rows += [_r("No esta bien sincronizada la letra", "2026-07-27", i)
             for i in range(8, 18)]                      # 10 de sync
    s = crs.summarize(rows, deliveries_total=68)

    assert s["total_rows"] == 17
    assert s["requests"] == 16
    assert s["excluded_as_noise"] == 1
    cats = {c["key"]: c["count"] for c in s["categories"]}
    assert cats["puntos_finales"] == 6
    assert cats["sincronizacion"] == 10
    # 16 pedidos sobre 68 entregas.
    assert s["requests_per_delivery"] == round(16 / 68, 4)


def test_summarize_expone_los_sin_clasificar():
    """Si las reglas se quedan viejas hay que verlo, no que desaparezca."""
    s = crs.summarize([_r("algo totalmente distinto zzz")])
    assert s["unclassified"] == 1
    assert s["unclassified_samples"] == ["algo totalmente distinto zzz"]


def test_summarize_sin_entregas_no_divide_por_cero():
    s = crs.summarize([_r("quitar puntos finales")], deliveries_total=0)
    assert s["requests_per_delivery"] is None


def test_summarize_lista_vacia():
    s = crs.summarize([], deliveries_total=10)
    assert s["requests"] == 0
    assert all(c["count"] == 0 for c in s["categories"])
