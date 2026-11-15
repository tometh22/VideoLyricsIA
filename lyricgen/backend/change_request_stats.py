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
from datetime import datetime, timedelta, timezone


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
      r"\ben vez de\b", r"\brevisar\b.{0,20}\bletra\b",
      # La tanda de sep-2026 llegó como listas de timecodes + texto citado,
      # sin repetir "corregir" en cada renglón.
      r"\b\d{1,2}:\d{2}\b.{0,120}[\"'“”]",
      r"\b\d{1,2}:\d{2}\b[\s\S]{1,220}\b\d{1,2}:\d{2}\b")),
    ("fondo", "Fondo",
     (r"\bfondo\b", r"\bbackground\b", r"\bprompt\b", r"\bestatico\b",
      r"\banimado\b", r"\bloop\b", r"\bpersonas?\b", r"\bescenario\b")),
    ("tipografia", "Tipografía",
     (r"\btipografia\b", r"\bletras? mas grande", r"\btamano\b",
      r"\bfuente\b")),
    ("audio", "Audio equivocado",
     (r"\bel audio\b", r"\baudio\b.{0,20}\b(no|incorrect|mal)\b",
      r"\bno esta correcto el audio\b")),
    ("estructura", "Estructura / layout de letra",
     (r"\b(unir|separar|dividir|juntar)\b.{0,35}\b(linea|lineas|frase)",
      r"\bfrase completa\b", r"\bmisma pantalla\b", r"\buna sola linea\b")),
    ("repeticiones", "Corrección en ocurrencias repetidas",
     (r"\btodas? las? (apariciones|veces)\b", r"\bcada vez\b",
      r"\btodos? los? (coros?|estribillos?)\b")),
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
        # Advertencia explícita, no una nota al pie: el numerador y el
        # denominador NO son la misma cohorte. Un reclamo que entra el mes
        # siguiente al de su entrega suma arriba y no abajo; una entrega
        # recién cargada que todavía no recibió feedback hace lo contrario.
        # Cuál es la cohorte correcta ("cuántos reclamos entraron este mes"
        # vs "qué tan buena fue la tanda de este mes") es una decisión de
        # negocio, así que el número se publica con la salvedad al lado en
        # vez de elegirla el código en silencio.
        "requests_per_delivery_caveat": (
            "cohortes mezcladas: los pedidos se cuentan por su propia fecha "
            "y las entregas por la suya. Un pedido de una entrega del mes "
            "anterior infla el ratio; una entrega sin feedback todavía lo "
            "diluye. Sirve para ver tendencia, no como tasa exacta."
        ),
        "note": (
            "Un pedido puede tener varias categorías (suelen pedir 2-3 cosas "
            "juntas), así que los shares suman más de 100%. El denominador "
            "son los pedidos, no las etiquetas."
        ),
    }


def summarize_delivery_cohort(deliveries, requests, publication_events, *,
                             as_of: datetime | None = None,
                             followup_days: int = 30) -> dict:
    """Measure client lyric/timing requests against their first published cut.

    ``deliveries`` are (job_id, portal_id) pairs. ``requests`` are
    (job_id, portal_id, submitted_at, comment) rows. ``publication_events``
    are (job_id, portal_id, created_at, action, detail) rows from AuditLog.
    The append-only ``delivery.create`` audit event is used because the live
    Delivery row's ``added_at`` is refreshed by later republishes.
    """
    now = as_of or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=followup_days)
    first_publish = {}
    for job_id, portal_id, created_at, action, detail in publication_events:
        detail = detail if isinstance(detail, dict) else {}
        job = detail.get("job_id")
        portal = detail.get("portal_id") or "argentina"
        if not job or action != "delivery.create" or not created_at:
            continue
        key = (str(job), str(portal))
        stamp = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        if key not in first_publish or stamp < first_publish[key]:
            first_publish[key] = stamp

    delivered = {(str(job), str(portal or "argentina")) for job, portal in deliveries}
    cohort = {key: stamp for key, stamp in first_publish.items()
              if key in delivered and stamp <= cutoff}
    affected = defaultdict(set)
    request_count = defaultdict(int)
    category_videos = defaultdict(set)
    category_requests = defaultdict(int)
    total_requests = 0
    for job, portal, submitted_at, comment in requests:
        key = (str(job), str(portal or "argentina"))
        published_at = cohort.get(key)
        if not published_at or not submitted_at:
            continue
        submitted = submitted_at if submitted_at.tzinfo else submitted_at.replace(tzinfo=timezone.utc)
        if not (published_at <= submitted < published_at + timedelta(days=followup_days)):
            continue
        tags = set(classify(comment or ""))
        relevant = tags & {"letra", "sincronizacion", "estructura", "repeticiones"}
        lyric_or_timing = tags & {"letra", "sincronizacion"}
        if not relevant or is_noise(comment or ""):
            continue
        if lyric_or_timing:
            total_requests += 1
            affected[key].update(lyric_or_timing)
            request_count[key] += 1
        for tag in relevant:
            category_videos[tag].add(key)
            category_requests[tag] += 1

    return {
        "followup_days": followup_days,
        "as_of": now.isoformat(),
        "cohort_start": min(cohort.values()).isoformat() if cohort else None,
        "cohort_end": max(cohort.values()).isoformat() if cohort else None,
        "delivered_videos": len(delivered),
        "eligible_videos": len(cohort),
        "videos_without_reconstructable_first_publish": len(delivered - set(first_publish)),
        "videos_with_lyric_or_timing_request": len(affected),
        "lyric_or_timing_request_rate": round(len(affected) / len(cohort), 4) if cohort else None,
        "request_count": total_requests,
        "requests_per_affected_video": round(total_requests / len(affected), 2) if affected else None,
        "videos_by_category": {tag: len(keys) for tag, keys in category_videos.items()},
        "requests_by_category": dict(category_requests),
        "categories_are_nonexclusive": True,
        "note": "La cohorte solo incluye videos con evento de primera publicación reconstruible y seguimiento completo.",
    }
