"""Clasificación de los pedidos de cambio del cliente.

Por qué existe
--------------
`delivery_change_requests` es lo que UMG escribe, con sus palabras, cuando algo
está mal en una entrega. Es la única medición directa de calidad que tenemos —
todo lo demás (ediciones del operador, tasa de rechazo) mide trabajo interno,
que es un supuesto sobre lo que el cliente quiere, no el dato.

Estuvo sin mirarse tres meses. La auditoría de ago-2026 arrancó construyendo un
clasificador que comparaba la primera versión de la letra contra la entregada
para inferir qué molestaba al cliente; esta tabla ya lo decía en castellano.
Seis pedidos distintos dicen literalmente que saquemos los puntos finales.

La tabla vive en la base de PROD (el portal es prod-backed) aunque la
producción gestionada corre en staging — usar `get_deliveries_db`, que ya
rutea bien desde los dos entornos.

Sobre la clasificación
----------------------
Son reglas de palabras clave, no un modelo. Un pedido puede caer en varias
categorías a propósito: *"Esta bien el fondo. Solo revisar la sincronización y
quitar los puntos finales de cada frase"* es sincronización Y puntos, y contarlo
una sola vez escondería la mitad del trabajo que generó.

Por eso los porcentajes suman más de 100%: el denominador son los pedidos, no
las etiquetas.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict


def _fold(s: str) -> str:
    """minúsculas sin acentos — los operadores escriben 'sincronizacion' y
    'sincronización' indistintamente."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Orden = orden de reporte. Cada patrón corre sobre el texto plegado.
CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # El pedido más repetido y el más barato de arreglar. Se busca "punto"
    # cerca de "final"/"frase" para no capturar "puntos de luz" en un pedido
    # de fondo.
    ("puntos_finales", "Puntos al final de las frases",
     (r"\bpuntos?\b(?!.*\bde luz\b).{0,40}\b(final|finales|frase|letra)",
      r"\b(sacar|quitar|sin)\b.{0,30}\bpuntos?\b",
      r"\bpuntos?\s+finales?\b")),
    ("sincronizacion", "Sincronización / timing",
     (r"sincroniz", r"\bsync\b", r"\bdesfasad", r"\bantes de que se termine",
      r"\bse van antes\b", r"\bqueda trabad", r"\bmantener por mas tiempo\b",
      r"\btermina(n)? antes\b")),
    ("letra", "Letra incorrecta (transcripción)",
     (r"\bdonde dice\b", r"\bdeberia decir\b", r"\bcorregir\b",
      # La ventana es ancha porque el cliente cita la línea entera entre
      # comillas: *"cambiar 'HACIA EL TREN MIENTRAS SUS SUEÑOS SE ALEJABAN'
      # por ..."*. Con 30 caracteres ese pedido quedaba sin clasificar.
      r"\bcambiar\b.{0,160}\bpor\b",
      r"\bno lo dice\b", r"\bdebe decir\b", r"\bmayuscula\b", r"\bacento\b",
      r"\ben vez de\b", r"\brevisar\b.{0,20}\bletra\b")),
    ("fondo", "Fondo",
     (r"\bfondo\b", r"\bbackground\b", r"\bprompt\b", r"\bestatico\b",
      r"\banimado\b", r"\bloop\b", r"\bpersonas?\b", r"\bescenario\b")),
    ("tipografia", "Tipografía",
     (r"\btipografia\b", r"\bletras? mas grande", r"\btamano\b",
      r"\bfuente\b")),
    ("audio", "Audio equivocado",
     (r"\bel audio\b", r"\baudio\b.{0,20}\b(no|incorrect|mal)\b",
      r"\bno esta correcto el audio\b")),
)

# Filas que no son pedidos reales del cliente.
_NOISE = (r"\bqa test\b", r"\bignorar\b", r"\btest post-release\b")


def is_noise(comment: str) -> bool:
    folded = _fold(comment)
    return any(re.search(p, folded) for p in _NOISE)


def classify(comment: str) -> list[str]:
    """Etiquetas de un pedido. Puede devolver varias, o `[]` si no matchea
    ninguna regla — esos van a `sin_clasificar`, que es la señal de que las
    reglas se quedaron viejas y hay que mirarlos a mano."""
    folded = _fold(comment or "")
    return [key for key, _label, pats in CATEGORIES
            if any(re.search(p, folded) for p in pats)]


LABELS = {key: label for key, label, _ in CATEGORIES}


def summarize(rows: list, deliveries_total: int | None = None) -> dict:
    """Resumen sobre filas (id, comment, submitted_at, resolved_at).

    `deliveries_total` (entregas vigentes del período) habilita la tasa de
    pedidos por entrega, que es EL indicador: dice si el retrabajo baja, y el
    retrabajo es la línea de costo que define el margen del contrato llave en
    mano.
    """
    real, noise = [], []
    for r in rows:
        (noise if is_noise(r.comment or "") else real).append(r)

    by_cat: dict[str, list] = defaultdict(list)
    unclassified = []
    for r in real:
        tags = classify(r.comment or "")
        if not tags:
            unclassified.append(r)
        for t in tags:
            by_cat[t].append(r)

    by_month: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in real:
        if not r.submitted_at:
            continue
        m = r.submitted_at.strftime("%Y-%m")
        by_month[m]["pedidos"] += 1
        for t in classify(r.comment or "") or ["sin_clasificar"]:
            by_month[m][t] += 1

    n = len(real)
    categories = [
        {
            "key": key,
            "label": LABELS[key],
            "count": len(by_cat.get(key, [])),
            "share": round(len(by_cat.get(key, [])) / n, 4) if n else None,
            # Un ejemplo textual: el resumen sin la voz del cliente pierde
            # justo lo que lo hace accionable.
            "sample": next((r.comment for r in by_cat.get(key, [])), None),
        }
        for key, _label, _ in CATEGORIES
    ]
    categories.sort(key=lambda c: -c["count"])

    return {
        "total_rows": len(rows),
        "requests": n,
        "excluded_as_noise": len(noise),
        "unclassified": len(unclassified),
        "unclassified_samples": [r.comment for r in unclassified[:5]],
        "categories": categories,
        "by_month": {m: dict(v) for m, v in sorted(by_month.items())},
        "deliveries": deliveries_total,
        "requests_per_delivery": (
            round(n / deliveries_total, 4)
            if deliveries_total else None
        ),
        "note": (
            "Un pedido puede tener varias categorías (suelen pedir 2-3 cosas "
            "juntas), así que los shares suman más de 100%. El denominador "
            "son los pedidos, no las etiquetas."
        ),
    }
