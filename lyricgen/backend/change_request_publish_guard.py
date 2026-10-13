"""Publicar contra pedidos de cambio abiertos: dos reglas, una sola fuente.

Medido el 2026-10-07 sobre las campañas UMG:

* 18 publicaciones por lote de septiembre-octubre re-enviaron una versión
  aprobada ANTES de un pedido de cambio que seguía abierto (Barricada,
  job da57c4ad3934, se re-envió 5 veces con la aprobación del 9-sep mientras
  el pedido #141 del 2-oct esperaba). El cliente volvió a ver lo mismo que
  ya había reclamado.
* El pedido #158 se cerró "por publicación" con la letra publicada todavía
  diciendo "que nos da lo mismo" en vez del "pero nos da lo mismo" pedido;
  el cliente tuvo que pedirlo otra vez (#159).

Con ``CHANGE_REQUEST_PUBLISH_GUARD_ENABLED=1``:

1. **Regla de versión (bloquea).** Si la versión aprobada que hay detrás del
   corte a publicar se guardó ANTES del ``submitted_at`` de un pedido abierto
   de esa canción (linaje padre/hijo incluido), la publicación se rechaza con
   ``change_request_newer_than_version``. "Publicar igual" exige un motivo y
   queda auditado. Un pedido sólo visual (fondo, imagen) se compara contra
   el último render, no contra la letra: el arreglo de un fondo no aprueba
   una letra nueva.
2. **Cierre por publicación (nunca bloquea).** Un pedido de TEXTO cuyo texto
   pedido no aparece en la letra publicada queda abierto y se audita
   ``requested_text_not_found`` con las frases que faltan. Los pedidos de
   timing, visuales o de prosa libre sin texto extraíble se cierran como
   hasta ahora: no hay con qué comprobarlos.

"La letra publicada" es la última ``editor_versions`` con ``is_approved``
creada antes de la publicación. ``deliveries.published_revision`` es el
contador del PORTAL (v1, v2…), no la revisión del editor.

El texto pedido sale de un parser determinístico sobre el comentario. El
intérprete con IA (``change_request_interpreter``) sólo persiste su salida
dentro de ``change_request_proposals`` cuando un operador pide analizar el
pedido, atada a una revisión base y como líneas reescritas completas (no la
frase del cliente); el 2026-10-08 no había ninguna fila así en staging ni en
producción, así que no se usa acá.
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

from change_request_workflow import latest_overwrite, timestamp

GUARD_CODE = "change_request_newer_than_version"
TEXT_NOT_FOUND = "requested_text_not_found"
OVERRIDE_ACTION = "delivery.change_request_guard.override"
TEXT_NOT_FOUND_ACTION = "delivery.change_request.requested_text_not_found"
# Tolerancia alrededor del tiempo que cita el cliente: los clientes redondean
# al segundo y suelen citar donde EMPIEZA la frase que escuchan.
NEAR_SECONDS = 3.0
MAX_LINEAGE = 400


def enabled() -> bool:
    return os.environ.get("CHANGE_REQUEST_PUBLISH_GUARD_ENABLED", "0") == "1"


# --------------------------------------------------------------------------
# Texto pedido: extracción determinística
# --------------------------------------------------------------------------

_TIME = r"(?<![\d:])(?:\d{1,2}:)?\d{1,2}:[0-5]\d(?!\d)"
_TIME_RE = re.compile(_TIME)
_QUOTED_RE = re.compile(r'"([^"]+)"|“([^”]+)”|‘([^’]+)’')
_PAREN_RE = re.compile(r"\([^)]*\)")
# Lo que dice HOY la letra, no lo pedido: 'dice "X"', 'cambiar "X" por …'.
_CURRENT_BEFORE = re.compile(r"(?:\bdice|\ben\s+(?:vez|lugar)\s+de|\bcambiar|\bsacar|\bquitar|\bborrar|\beliminar)\s*:?\s*$", re.I)
_TIMING = re.compile(
    r"sincroniz|desincroniz|timing|desfas|entr[ae]\s+(?:tarde|antes)|termina|empieza|arranc"
    r"|m[aá]s\s+(?:de\s+)?tiempo|se\s+lea|aparece\s+la\s+letra\s+sin|acomodar|alargar|adelantar|atrasar|se\s+va\s+antes",
    re.I)
_VISUAL = re.compile(
    r"\b(?:fondo|background|escena|imagen|animaci[oó]n|logo|armas?|banderas?|perrit[oa]s?|colores?|paleta"
    r"|iluminaci[oó]n|cuerpos?|pie\s+que|portada|personas?)\b", re.I)
# Verbos con los que empieza una instrucción que NO es letra.
_INSTRUCTION = re.compile(
    r"^(?:sacar|sacarle|quitar|cambiar|revisar|chequear|verificar|sincroniz|acomodar|alargar|mover|unir|separar"
    r"|dividir|juntar|poner|dejar|mantener|corregir\s+sincron|el\s+estribillo|frase\s+de|que\s+(?:ac[aá]|no\s+aparezcan))",
    re.I)
# Encabezados delante del texto pedido. Los conectores sólo se sacan cuando
# van pegados a un tiempo ("en 1:22", "desde 0:17 hasta 0:21"): "a Enrique
# por toda la ciudad" empieza con "a" y es letra.
_PREFIXES = [re.compile(p, re.I) for p in (
    r"^faltaron\s+las\s+siguientes\s+correcciones",
    r"^corregir\s+(?:la\s+)?letras?(?:\s+en)?",
    r"^correcci[oó]n(?:es)?(?:\s+de\s+letra)?",
    r"^falta(?:n)?\s+(?:la\s+|una\s+|el\s+)?(?:frase|palabra|l[ií]nea|letra)s?",
    r"^no\s+aparece\s+(?:la\s+)?letra(?:\s+en\s+pantalla)?(?:\s+en)?",
    r"^(?:debe|deber[ií]a)\s+(?:decir|ser)",
    r"^la\s+(?:l[ií]nea|frase)\s+(?:correcta\s+)?(?:es|dice)",
    r"^(?:en|desde|hasta|a|y|de)\s*(?=§)",
    r"^§",
    r"^[,:;_\-–—.\s]+",
)]
_COURTESY = re.compile(r"[\s.!]*(?:gracias+|graciaas|porfa(?:vor)?|por\s+favor)[\s.!]*$", re.I)


def fold(text: str) -> str:
    """Comparación: sin mayúsculas, tildes ni puntuación.

    Los clientes marcan en MAYÚSCULAS lo que piden ("lavarteloS"). El
    apóstrofo se borra sin dejar espacio ("engüalicha'o" = "engualichao"), y
    ANTES de NFKD: el acento suelto "´" se descompone en espacio + tilde y
    partía la palabra en dos (pedido #124, "Engüalicha´o").
    """
    text = re.sub(r"['’‘´`ʼ]", "", str(text or ""))
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", text))


def _seconds(raw: str) -> float:
    parts = [int(v) for v in raw.split(":")]
    return float(parts[-1] + 60 * parts[-2] + (3600 * parts[0] if len(parts) == 3 else 0))


def _chunks(comment: str) -> list[str]:
    """Una instrucción por trozo: '//', o una línea que arranca con tiempo,
    con viñeta o después de una línea en blanco. Las demás líneas continúan
    la anterior (letras de varias líneas, citas partidas)."""
    out: list[str] = []
    for part in re.split(r"//", comment or ""):
        current: list[str] = []
        blank = False
        for raw in part.split("\n"):
            line = raw.strip()
            if not line:
                blank = True
                continue
            starts = (bool(re.match(r"(?:[-*•]\s*)?" + _TIME, line)) or line.startswith(("-", "•", "*"))
                      or blank or bool(_INSTRUCTION.match(line)))
            if current and starts and not _open_quote(" ".join(current)):
                out.append(" ".join(current))
                current = []
            current.append(line)
            blank = False
        if current:
            out.append(" ".join(current))
    return [c for c in (c.strip() for c in out) if c]


def _open_quote(text: str) -> bool:
    return text.count('"') % 2 == 1 or text.count("“") > text.count("”")


def _strip_prefixes(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for pattern in _PREFIXES:
            new = pattern.sub("", text, count=1).lstrip()
            if new != text:
                text, changed = new, True
    return text


def extract_requests(comment: str) -> dict[str, Any]:
    """Qué texto pide el cliente y dónde, más el tipo de cada instrucción.

    ``phrases``: [{"text", "times"}]. ``kinds``: conjunto de "text",
    "timing", "visual" y "other" (puntuación, pantalla, prosa sin texto).
    """
    phrases: list[dict] = []
    kinds: set[str] = set()
    for chunk in _chunks(comment):
        times = [_seconds(t) for t in _TIME_RE.findall(chunk)]
        body = _PAREN_RE.sub(" ", chunk)
        body = re.sub(r"^[-*•]\s*", "", body).strip()
        marked = _TIME_RE.sub(" § ", body)
        literal_marker = re.search(r"(?:debe|deber[ií]a)\s+(?:decir|ser)|falta|la\s+(?:l[ií]nea|frase)\s+(?:correcta\s+)?es|corregir\s+letra", marked, re.I)
        quoted = []
        for match in _QUOTED_RE.finditer(body):
            value = next(g for g in match.groups() if g is not None)
            if _CURRENT_BEFORE.search(body[:match.start()]):
                continue
            quoted.append(value)
        if _TIMING.search(marked) and not literal_marker:
            kinds.add("timing")
            continue
        if quoted:
            for value in quoted:
                if fold(value):
                    phrases.append({"text": value.strip(), "times": times})
            kinds.add("text")
            continue
        text = _strip_prefixes(marked.strip())
        # "… y corregir: <letra>", "en 1:47: <letra>": lo pedido va después
        # de los dos puntos de la instrucción.
        if ":" in text:
            head, _, tail = text.rpartition(":")
            if re.search(r"(?:corregir|decir|ser|frase|letra|l[ií]neas?|correcciones|es|§)\s*$", head, re.I) or not tail.strip():
                text = _strip_prefixes(tail.strip()) if tail.strip() else ""
        text = _COURTESY.sub("", text).strip(" .,;:")
        if not text or "§" in text:
            # Un tiempo en medio del texto es una instrucción que no sabemos leer.
            kinds.add("other")
            continue
        if _INSTRUCTION.match(text):
            kinds.add("visual" if _VISUAL.search(text) else "other")
            continue
        if not times and _VISUAL.search(text):
            kinds.add("visual")
            continue
        if not times:
            # Prosa sin tiempo y sin comillas ("Revisar…", comentarios): no
            # hay forma segura de separar letra de explicación.
            kinds.add("other")
            continue
        phrases.append({"text": text, "times": times})
        kinds.add("text")
    return {"phrases": phrases, "kinds": sorted(kinds)}


def visual_only(comment: str) -> bool:
    kinds = set(extract_requests(comment)["kinds"])
    return kinds == {"visual"} or kinds == {"visual", "other"}


def _stream(segments: Iterable[dict]) -> list[tuple[str, int]]:
    tokens: list[tuple[str, int]] = []
    for index, segment in enumerate(segments or []):
        for token in fold((segment or {}).get("text", "")).split():
            tokens.append((token, index))
    return tokens


def check_requested_text(comment: str, segments: list[dict]) -> dict[str, Any]:
    """¿Está el texto pedido en la letra publicada?

    Busca cada frase cerca (±3 s) del tiempo citado y, si no, en cualquier
    parte de la canción: un tiempo mal citado no debe dejar abierto un pedido
    atendido. La frase puede cruzar el corte entre líneas.

    ``status``: "found", "not_found" o "not_checkable" (sin texto extraíble).
    """
    extracted = extract_requests(comment)
    phrases = extracted["phrases"]
    if not phrases:
        return {"status": "not_checkable", "kinds": extracted["kinds"], "found": [], "missing": []}
    segments = [s for s in (segments or []) if isinstance(s, dict)]
    stream = _stream(segments)
    words = [token for token, _ in stream]
    found, missing = [], []
    for phrase in phrases:
        wanted = fold(phrase["text"]).split()
        lines: list[set[int]] = []
        for at in range(len(words) - len(wanted) + 1):
            if words[at:at + len(wanted)] == wanted:
                lines.append({stream[k][1] for k in range(at, at + len(wanted))})
        near = False
        for hit in lines:
            for index in hit:
                start = float(segments[index].get("start") or 0.0)
                end = float(segments[index].get("end") or start)
                if any(start - NEAR_SECONDS <= t <= end + NEAR_SECONDS for t in phrase["times"]):
                    near = True
        entry = {"text": phrase["text"], "times": phrase["times"]}
        if lines:
            found.append({**entry, "where": "near" if near or not phrase["times"] else "elsewhere"})
        else:
            missing.append(entry)
    return {"status": "not_found" if missing else "found", "kinds": extracted["kinds"],
            "found": found, "missing": missing}


# --------------------------------------------------------------------------
# Versión publicada y linaje
# --------------------------------------------------------------------------

def published_version(db, job_id: str, at: datetime | None = None):
    """La última versión aprobada del editor creada antes de ``at``."""
    from database import EditorVersion
    # El corte por fecha se hace acá y no en SQL: SQLite guarda las fechas sin
    # zona y la comparación en la consulta no es confiable entre motores.
    rows = db.query(EditorVersion).filter(
        EditorVersion.job_id == job_id, EditorVersion.is_approved.is_(True),
    ).all()
    at = timestamp(at) if at is not None else None
    rows = [row for row in rows if timestamp(row.created_at) is not None
            and (at is None or timestamp(row.created_at) <= at)]
    return max(rows, key=lambda row: (timestamp(row.created_at), row.revision), default=None)


def _approved_at_by_job(db, job_ids: list[str], at: datetime | None) -> dict[str, tuple[datetime, int]]:
    from database import EditorVersion
    if not job_ids:
        return {}
    query = db.query(EditorVersion.job_id, EditorVersion.created_at, EditorVersion.revision).filter(
        EditorVersion.job_id.in_(job_ids), EditorVersion.is_approved.is_(True),
    )
    at = timestamp(at) if at is not None else None
    best: dict[str, tuple[datetime, int]] = {}
    for job_id, created_at, revision in query.all():
        created_at = timestamp(created_at)
        if created_at is None or (at is not None and created_at > at):
            continue
        if job_id not in best or (created_at, revision) > best[job_id]:
            best[job_id] = (created_at, revision)
    return best


def song_lineages(db, jobs: list) -> dict[str, set[str]]:
    """Todos los jobs de la misma canción: ancestros y descendientes del mismo
    tenant (variantes y regenerados), como ``campaign_pipeline._load_lineage``."""
    from database import Job
    tenant = {job.job_id: job.tenant_id for job in jobs}
    parent = {job.job_id: job.parent_job_id for job in jobs}
    root = {}
    for job in jobs:
        current, seen = job.job_id, {job.job_id}
        while True:
            up = parent.get(current)
            if not up or up in seen or len(seen) > 20:
                break
            if up not in parent:
                row = db.query(Job.job_id, Job.parent_job_id, Job.tenant_id).filter(Job.job_id == up).first()
                if row is None or row.tenant_id != job.tenant_id:
                    break
                parent[up], tenant[up] = row.parent_job_id, row.tenant_id
            if tenant.get(up) != job.tenant_id:
                break
            seen.add(up)
            current = up
        root[job.job_id] = current
    members: dict[str, set[str]] = {}
    for top in set(root.values()):
        group, frontier = {top}, {top}
        tenant_id = tenant.get(top)
        while frontier and len(group) < MAX_LINEAGE:
            rows = db.query(Job.job_id).filter(Job.parent_job_id.in_(list(frontier)), Job.tenant_id == tenant_id).all()
            frontier = {row.job_id for row in rows} - group
            group |= frontier
        members[top] = group
    return {job_id: members[top] | {job_id} for job_id, top in root.items()}


def _brief(request, delivery) -> dict[str, Any]:
    submitted = timestamp(request.submitted_at)
    return {"id": request.id, "delivery_id": delivery.id, "portal_id": delivery.portal_id or "argentina",
            "job_id": delivery.job_id, "submitted_at": submitted.isoformat() if submitted else None,
            "comment": (request.comment or "")[:300]}


def evaluate(db, ddb, jobs: list, at: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Regla de versión para cada job a publicar. No escribe nada.

    ``{job_id: {"blocked", "requests", "version"}}``. Sin versión aprobada del
    editor se usa ``job.approved_at`` (la aprobación del video); sin ninguna de
    las dos no hay con qué comparar y no se bloquea.
    """
    from database import Delivery, DeliveryChangeRequest
    if not jobs:
        return {}
    at = at or datetime.now(timezone.utc)
    lineages = song_lineages(db, jobs)
    all_ids = sorted(set().union(*lineages.values()))
    tenants = sorted({job.tenant_id for job in jobs})
    rows = ddb.query(DeliveryChangeRequest, Delivery).join(
        Delivery, Delivery.id == DeliveryChangeRequest.delivery_id,
    ).filter(
        Delivery.job_id.in_(all_ids), Delivery.tenant_snapshot.in_(tenants),
        Delivery.removed_at.is_(None), DeliveryChangeRequest.resolved_at.is_(None),
    ).all() if all_ids else []
    approved = _approved_at_by_job(db, [job.job_id for job in jobs], at)
    result: dict[str, dict[str, Any]] = {}
    for job in jobs:
        version_at, revision, source = None, None, None
        if job.job_id in approved:
            (version_at, revision), source = approved[job.job_id], "editor_version"
        elif timestamp(job.approved_at) is not None:
            version_at, source = timestamp(job.approved_at), "job_approved_at"
        rendered_at = latest_overwrite(job) or timestamp(job.completed_at)
        lineage = lineages.get(job.job_id, {job.job_id})
        newer = []
        for request, delivery in rows:
            if delivery.job_id not in lineage or delivery.tenant_snapshot != job.tenant_id:
                continue
            submitted = timestamp(request.submitted_at)
            if submitted is None or submitted > at:
                continue
            # Un pedido sólo visual se atiende con un render nuevo, no con
            # una aprobación de letra.
            reference = rendered_at if visual_only(request.comment) else version_at
            if reference is not None and reference < submitted:
                newer.append(_brief(request, delivery))
        newer.sort(key=lambda item: (item["submitted_at"] or "", item["id"]))
        result[job.job_id] = {
            "blocked": bool(newer), "requests": newer,
            "version": {"source": source, "revision": revision,
                        "approved_at": version_at.isoformat() if version_at else None},
        }
    return result


def blocked_detail(job, guard: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    ids = ", ".join(f"#{item['id']}" for item in guard["requests"])
    return {
        "code": GUARD_CODE,
        "message": (f"Hay pedidos de cambio abiertos ({ids}) más nuevos que la versión aprobada de esta canción. "
                    "Corregí y aprobá la letra antes de publicar, o publicá igual indicando el motivo."),
        "job_id": job.job_id, "title": title or job.song_title, "artist": job.artist,
        "version": guard["version"], "requests": guard["requests"],
    }


def override_audit(user_id, job, guard: dict[str, Any], reason: str, source: str, **extra):
    from database import AuditLog
    return AuditLog(user_id=user_id, action=OVERRIDE_ACTION, detail={
        "job_id": job.job_id, "change_request_ids": [item["id"] for item in guard["requests"]],
        "requests": [{"id": item["id"], "submitted_at": item["submitted_at"]} for item in guard["requests"]],
        "version": guard["version"], "reason": reason[:1000], "source": source, **extra,
    })


# --------------------------------------------------------------------------
# Cierre por publicación
# --------------------------------------------------------------------------

def text_gate(db, job, requests: list, at: datetime | None = None) -> tuple[list, list[dict]]:
    """Separa los pedidos que la publicación puede cerrar de los que quedan
    abiertos porque su texto no está en la letra publicada.

    Devuelve (cerrables, retenidos). Cada retenido trae ``id``, ``reason`` y
    ``missing``. Sin versión aprobada no hay letra con qué comparar: se
    mantiene el comportamiento anterior (se cierra).
    """
    if not requests:
        return [], []
    version = published_version(db, job.job_id, at)
    if version is None:
        return list(requests), []
    closable, kept = [], []
    for request in requests:
        check = check_requested_text(request.comment or "", version.segments or [])
        if check["status"] == "not_found":
            kept.append({"id": request.id, "reason": TEXT_NOT_FOUND,
                         "missing": [item["text"] for item in check["missing"]],
                         "editor_revision": version.revision})
        else:
            closable.append(request)
    return closable, kept


def text_not_found_audit(user_id, job, kept: dict[str, Any], source: str, **extra):
    from database import AuditLog
    return AuditLog(user_id=user_id, action=TEXT_NOT_FOUND_ACTION, detail={
        "job_id": job.job_id, "change_request_id": kept["id"], "reason": TEXT_NOT_FOUND,
        "missing": kept["missing"][:20], "editor_revision": kept.get("editor_revision"),
        "source": source, **extra,
    })
