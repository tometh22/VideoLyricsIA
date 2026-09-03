"""Tests del módulo puro de anclas (sin LLM, sin red, sin GPU).

Cubre las tres propiedades de las que depende todo el modo anclado:
  1. una cita inventada se descarta (es lo que hace verificable al paso 1);
  2. la cobertura mide de verdad si el prompt usó la letra (es el gate nuevo);
  3. con el flag apagado nada se activa.
"""

import lyric_anchors as la


LETRA = (
    "Vengo a buscarte a la Plaza de Mayo\n"
    "con una bandera y un megafono roto\n"
    "quedaron papeles y una silla vacia\n"
    "nadie limpia lo que dejamos\n"
)


# ── Flag ───────────────────────────────────────────────────────────────────
def test_modo_default_es_off_y_valores_invalidos_caen_a_off():
    assert la.anchors_mode({}) == "off"
    assert la.anchors_mode({"BG_LYRIC_ANCHORS": "enforce"}) == "off"
    assert la.anchors_mode({"BG_LYRIC_ANCHORS": ""}) == "off"


def test_modos_validos():
    assert la.anchors_mode({"BG_LYRIC_ANCHORS": "ON"}) == "on"
    assert la.anchors_mode({"BG_LYRIC_ANCHORS": " shadow "}) == "shadow"
    assert la.anchors_enabled("on") is True
    assert la.anchors_enabled("shadow") is False
    assert la.anchors_observed("shadow") is True
    assert la.anchors_observed("off") is False


# ── Normalización ──────────────────────────────────────────────────────────
def test_normalize_saca_tildes_y_puntuacion():
    # Es lo que permite que "corazón" en la letra matchee "corazon" en un
    # prompt escrito sin acentos.
    assert la.normalize("¡Corazón, ROTO!") == "corazon roto"


# ── Parseo ─────────────────────────────────────────────────────────────────
def _payload(**over):
    base = {
        "lugar": "Plaza de Mayo",
        "linea_lugar": "Vengo a buscarte a la Plaza de Mayo",
        "objetos": [
            {"objeto": "bandera", "linea": "con una bandera y un megafono roto"},
            {"objeto": "megafono", "linea": "con una bandera y un megafono roto"},
            {"objeto": "papeles", "linea": "quedaron papeles y una silla vacia"},
            {"objeto": "silla", "linea": "quedaron papeles y una silla vacia"},
        ],
        "situacion": "termino una marcha",
        "registro": "bronca",
        "epoca": None,
    }
    base.update(over)
    return base


def test_parse_acepta_json_envuelto_en_markdown():
    import json
    raw = "```json\n" + json.dumps(_payload()) + "\n```"
    parsed = la.parse_anchors(raw)
    assert parsed["lugar"] == "Plaza de Mayo"
    assert len(parsed["objetos"]) == 4


def test_parse_dedupea_objetos_por_forma_normalizada():
    import json
    raw = json.dumps(_payload(objetos=[
        {"objeto": "la bandera", "linea": "x"},
        {"objeto": "Bandera", "linea": "y"},
    ]))
    # "la bandera" y "Bandera" son el mismo ancla; contarlas dos veces inflaría
    # la cobertura y haría pasar un prompt que sólo usó una.
    assert len(la.parse_anchors(raw)["objetos"]) == 1


def test_parse_devuelve_none_sin_lugar_ni_objetos():
    import json
    assert la.parse_anchors(json.dumps(_payload(lugar=None, objetos=[]))) is None


def test_parse_devuelve_none_ante_basura():
    assert la.parse_anchors("no soy json") is None
    assert la.parse_anchors("") is None
    assert la.parse_anchors(None) is None


# ── Verificación de citas (el corazón del diseño) ──────────────────────────
def test_objeto_con_cita_inventada_se_descarta():
    anchors = _payload(objetos=[
        {"objeto": "bandera", "linea": "con una bandera y un megafono roto"},
        {"objeto": "helicoptero", "linea": "un helicoptero sobre la ciudad"},
    ])
    kept = la.verify_anchors(anchors, LETRA)
    nombres = [o["objeto"] for o in kept["objetos"]]
    assert nombres == ["bandera"]


def test_lugar_con_cita_inventada_cae_a_none():
    anchors = _payload(lugar="Machu Picchu", linea_lugar="subiendo la montaña sagrada")
    kept = la.verify_anchors(anchors, LETRA)
    assert kept["lugar"] is None


def test_objeto_sin_cita_se_conserva():
    # Sin cita no se puede refutar; el modo de falla que importa es la cita
    # INVENTADA, no la omitida.
    anchors = _payload(objetos=[{"objeto": "bandera", "linea": ""}])
    assert len(la.verify_anchors(anchors, LETRA)["objetos"]) == 1


def test_verify_devuelve_none_si_no_queda_nada():
    anchors = _payload(lugar=None, linea_lugar=None,
                       objetos=[{"objeto": "tren", "linea": "un tren al sur"}])
    assert la.verify_anchors(anchors, LETRA) is None


def test_verify_es_insensible_a_tildes_y_mayusculas():
    anchors = _payload(objetos=[
        {"objeto": "megáfono", "linea": "CON UNA BANDERA Y UN MEGÁFONO ROTO"},
    ])
    assert len(la.verify_anchors(anchors, LETRA)["objetos"]) == 1


# ── Cobertura ──────────────────────────────────────────────────────────────
def test_cobertura_cuenta_lugar_y_objetos():
    anchors = _payload()
    prompt = ("Plaza de Mayo vacia al mediodia, una bandera argentina, papeles "
              "arrugados y una silla plastica volcada")
    cov = la.anchor_coverage(prompt, anchors)
    assert cov["total"] == 5              # lugar + 4 objetos
    assert cov["covered"] == 4            # falta megafono
    assert "megafono" in cov["misses"]


def test_cobertura_matchea_con_palabras_intercaladas():
    anchors = _payload(lugar=None, linea_lugar=None,
                       objetos=[{"objeto": "botella", "linea": ""}])
    cov = la.anchor_coverage("una botella de vidrio vacia sobre la barra", anchors)
    assert cov["covered"] == 1


def test_cobertura_ignora_articulos():
    # Si el ancla es "la ruta", el match tiene que venir de "ruta" y no del
    # artículo, que aparece en cualquier prompt en español.
    anchors = _payload(lugar=None, linea_lugar=None,
                       objetos=[{"objeto": "la ruta", "linea": ""}])
    assert la.anchor_coverage("la casa y el arbol", anchors)["covered"] == 0
    assert la.anchor_coverage("una ruta de tierra", anchors)["covered"] == 1


def test_cobertura_insuficiente_dispara_reroll():
    cov = {"covered": 1, "total": 5}
    assert la.coverage_is_sufficient(cov) is False
    assert la.coverage_is_sufficient({"covered": 4, "total": 5}) is True


def test_cancion_abstracta_con_pocas_anclas_no_loopea():
    # Pedir 4 de 2 haría re-rollear para siempre: con menos anclas que el
    # mínimo se exige que estén todas, no el mínimo absoluto.
    assert la.coverage_is_sufficient({"covered": 2, "total": 2}) is True
    assert la.coverage_is_sufficient({"covered": 1, "total": 2}) is False
    assert la.coverage_is_sufficient({"covered": 0, "total": 0}) is True
    assert la.coverage_is_sufficient(None) is True


# ── Gate urbano del riel anti-callejón ─────────────────────────────────────
def test_ancla_urbana_detectada():
    assert la.has_urban_anchor(_payload()) is True          # "Plaza de Mayo"


def test_ancla_no_urbana():
    anchors = _payload(lugar="un campo de trigo", linea_lugar="",
                       objetos=[{"objeto": "molino", "linea": ""}],
                       situacion="amanece en el campo")
    assert la.has_urban_anchor(anchors) is False


def test_sin_anclas_no_hay_ancla_urbana():
    assert la.has_urban_anchor(None) is False


# ── Bloque de restricción ──────────────────────────────────────────────────
def test_bloque_lista_lugar_y_objetos_y_manda_sobre_genero():
    block = la.anchors_constraint_block(_payload())
    assert "Plaza de Mayo" in block
    assert "megafono" in block
    assert "RESTRICCIÓN" in block
    assert "mandan sobre género" in block


def test_bloque_prohibe_el_atardecer_por_descarte_cuando_no_hay_epoca():
    # El 59% de los fondos medidos en staging terminaban al atardecer, la misma
    # tasa que en modo Auto (que ni mira la letra).
    block = la.anchors_constraint_block(_payload(epoca=None))
    assert "golden hour" in block
    assert "no la da" in block


def test_bloque_usa_la_epoca_cuando_la_letra_la_da():
    block = la.anchors_constraint_block(_payload(epoca="de madrugada"))
    assert "de madrugada" in block
    assert "golden hour" not in block


def test_bloque_vacio_sin_anclas():
    assert la.anchors_constraint_block(None) == ""


# ── Petición de extracción ─────────────────────────────────────────────────
def test_request_incluye_metadata_y_letra_completa():
    req = la.build_extraction_request("Bersuit", "Sr. Cobranza", LETRA)
    assert "Bersuit" in req and "Sr. Cobranza" in req
    assert "Plaza de Mayo" in req


def test_request_trunca_pero_muy_por_encima_del_compositor():
    # El compositor viejo veía 1800 caracteres; acá la letra no compite con
    # nada, así que entra completa hasta 4000.
    req = la.build_extraction_request("a", "b", "x" * 9000)
    assert req.count("x") == 4000


def test_request_marca_la_ausencia_de_letra():
    assert "[sin letra disponible]" in la.build_extraction_request("a", "b", "  ")


# ── Anclas irrenderizables ─────────────────────────────────────────────────
# Medido sobre 12 canciones reales de staging: el 24% de las anclas extraídas
# eran cosas que los rieles del propio pipeline prohíben dibujar. Contarlas
# hacía que el compositor "fallara" por obedecer compliance.
def test_partes_del_cuerpo_y_abstracciones_no_son_anclas():
    for term in ("piel", "ojos", "las manos", "el vacío", "la herida",
                 "las estrellas", "un ángel", "el alma", "gente"):
        assert la.is_renderable(term) is False, term


def test_objetos_filmables_si_son_anclas():
    for term in ("una botella", "vereda", "farol oxidado", "colectivo",
                 "estrella de mar", "silla plástica"):
        assert la.is_renderable(term) is True, term


def test_verify_descarta_las_irrenderizables():
    anchors = _payload(objetos=[
        {"objeto": "bandera", "linea": "con una bandera y un megafono roto"},
        {"objeto": "piel", "linea": "quedaron papeles y una silla vacia"},
        {"objeto": "el vacío", "linea": "nadie limpia lo que dejamos"},
    ])
    kept = la.verify_anchors(anchors, LETRA)
    assert [o["objeto"] for o in kept["objetos"]] == ["bandera"]


def test_la_cobertura_no_castiga_por_obedecer_compliance():
    """Caso testigo Luciano Pereyra "Eres Perfecta": 6 de 7 anclas eran piel,
    estrellas, Instagram, perfil, Cristóbal Colón y doctor. El compositor no
    podía dibujar casi ninguna, y la cobertura daba 1/7 → re-roll inútil."""
    anchors = _payload(lugar="el barrio", linea_lugar="Vengo a buscarte a la Plaza de Mayo",
                       objetos=[
                           {"objeto": "piel", "linea": "quedaron papeles y una silla vacia"},
                           {"objeto": "estrellas", "linea": "nadie limpia lo que dejamos"},
                           {"objeto": "silla", "linea": "quedaron papeles y una silla vacia"},
                       ])
    kept = la.verify_anchors(anchors, LETRA)
    cov = la.anchor_coverage("el barrio con una silla vacía en la vereda", kept)
    # Sólo cuentan lugar + silla; piel y estrellas ya no inflan el denominador.
    assert cov["total"] == 2 and cov["covered"] == 2
    assert la.coverage_is_sufficient(cov) is True


# ── Métrica de lugar concreto (la que mira el sello) ───────────────────────
def test_nombra_el_lugar_de_la_letra():
    anchors = _payload()          # lugar = "Plaza de Mayo", con su línea
    r = la.names_place_from_lyrics(
        "Plaza de Mayo vacía al mediodía, papeles en la vereda", anchors)
    assert r["names_place"] is True
    assert r["citado"] is True
    assert r["lugar"] == "Plaza de Mayo"


def test_detecta_que_el_compositor_ignoro_el_lugar():
    """El modo de falla que importa: se extrajo un lugar de la letra y la escena
    terminó en otro lado."""
    r = la.names_place_from_lyrics(
        "un valle de montaña con niebla al amanecer", _payload())
    assert r["names_place"] is False
    assert "no lo usó" in r["reason"]


def test_distingue_no_haber_lugar_de_haberlo_ignorado():
    """Una canción abstracta sin lugar no es un incumplimiento; ignorar un lugar
    que la letra sí daba, sí. La métrica los separa para poder diagnosticar."""
    r = la.names_place_from_lyrics("cualquier escena", _payload(lugar=None))
    assert r["names_place"] is False
    assert "no ancla en un lugar" in r["reason"]


def test_el_lugar_sin_cita_se_marca_como_no_rastreable():
    r = la.names_place_from_lyrics("Plaza de Mayo vacía",
                                   _payload(linea_lugar=None))
    assert r["names_place"] is True
    assert r["citado"] is False
