"""Registro de rol por canción: train / val / eval_holdout.

Por qué existe: el 2026-09-02 el canary de 30 canciones que se usó para evaluar
LoRA v1, el router de dificultad y el semáforo resultó ser el propio golden set
—15 de esas canciones eran audio de entrenamiento del adaptador—, y ningún
código lo impidió ni lo declaró. Lo mismo pasó en el piloto del router: 13 de
las 18 canciones "reconstructed" estaban en el train set.

Este módulo es la única fuente de verdad sobre qué canción puede entrenar y cuál
puede juzgar. La clave es el SHA-256 del audio, que es estable entre entornos
(el mismo master sube a staging y a producción con job_id distinto). Cuando
todavía no conocemos el hash, la entrada queda anclada al job_id con
``needs_sha256`` en true: preferimos una entrada incompleta y visible antes que
un hash inventado.

Contrato:

* ``assert_evaluable`` levanta si la canción es de entrenamiento. Lo llama todo
  lo que puntúa (evaluaciones, pilotos, gates).
* ``assert_trainable`` levanta si la canción es holdout. Lo llama todo lo que
  arma datasets.
* ``assign_role`` nunca pisa en silencio: cambiar un rol exige motivo y deja
  ``history`` con el rol previo y la fecha.
"""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterable

SCHEMA = "song-role-registry-v1"
ROLES = ("train", "val", "eval_holdout")

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("data") / "song_roles.json"

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_CACHE_PATH: Path | None = None


class SongRoleViolation(RuntimeError):
    """Se intentó entrenar con holdout o evaluar con audio de entrenamiento."""


class SongRoleUnknown(LookupError):
    """La canción no está en el registro. Nunca se asume un rol por defecto."""


def registry_path() -> Path:
    configured = os.environ.get("SONG_ROLE_REGISTRY_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_REGISTRY_PATH


def load_registry(path: str | os.PathLike[str] | None = None,
                  *, refresh: bool = False) -> dict[str, Any]:
    """Devuelve el registro completo, cacheado por proceso."""
    global _CACHE, _CACHE_PATH
    target = Path(path) if path else registry_path()
    with _LOCK:
        if not refresh and _CACHE is not None and _CACHE_PATH == target:
            return _CACHE
        if not target.is_file():
            registry: dict[str, Any] = {"schema": SCHEMA, "entries": []}
        else:
            registry = json.loads(target.read_text(encoding="utf-8"))
        if registry.get("schema") != SCHEMA:
            raise ValueError(f"registro de roles con schema inesperado: {registry.get('schema')}")
        _CACHE, _CACHE_PATH = registry, target
        return registry


def _entries(path=None) -> list[dict[str, Any]]:
    return [e for e in (load_registry(path).get("entries") or []) if isinstance(e, dict)]


def _normalise_sha(value: str | None) -> str:
    return str(value or "").strip().lower()


def entry_for_sha(sha256: str, path=None) -> dict[str, Any] | None:
    sha = _normalise_sha(sha256)
    if not sha:
        return None
    for entry in _entries(path):
        if _normalise_sha(entry.get("sha256")) == sha:
            return entry
    return None


def entry_for_job(job_id: str, path=None) -> dict[str, Any] | None:
    job = str(job_id or "").strip()
    if not job:
        return None
    for entry in _entries(path):
        for known in entry.get("known_job_ids") or []:
            if isinstance(known, dict) and str(known.get("job_id") or "") == job:
                return entry
    return None


def _entry_for_any(identifier: str, path=None) -> dict[str, Any] | None:
    # Un SHA-256 tiene 64 hex; los job_id del producto tienen 12. Probamos
    # ambos índices igual, porque un identificador corto nunca es un hash.
    return entry_for_sha(identifier, path) or entry_for_job(identifier, path)


def role_for_sha(sha256: str, path=None) -> str | None:
    entry = entry_for_sha(sha256, path)
    return str(entry.get("role")) if entry else None


def role_for_job(job_id: str, path=None) -> str | None:
    entry = entry_for_job(job_id, path)
    return str(entry.get("role")) if entry else None


def role_for(identifier: str, path=None) -> str | None:
    entry = _entry_for_any(identifier, path)
    return str(entry.get("role")) if entry else None


def _describe(entry: dict[str, Any], identifier: str) -> str:
    title = str(entry.get("title") or "?")
    artist = str(entry.get("artist") or "?")
    return f"{identifier} ({title} — {artist})"


def assert_evaluable(identifier: str, *, path=None, strict_unknown: bool = False) -> str:
    """Levanta si la canción es de entrenamiento. Devuelve el rol."""
    entry = _entry_for_any(identifier, path)
    if entry is None:
        if strict_unknown:
            raise SongRoleUnknown(
                f"{identifier} no está en el registro de roles; asignale un rol antes de evaluar",
            )
        return "unknown"
    role = str(entry.get("role"))
    if role == "train":
        raise SongRoleViolation(
            f"no se puede evaluar con audio de entrenamiento: {_describe(entry, identifier)}",
        )
    return role


def assert_trainable(identifier: str, *, path=None, strict_unknown: bool = False) -> str:
    """Levanta si la canción es holdout de evaluación. Devuelve el rol."""
    entry = _entry_for_any(identifier, path)
    if entry is None:
        if strict_unknown:
            raise SongRoleUnknown(
                f"{identifier} no está en el registro de roles; asignale un rol antes de entrenar",
            )
        return "unknown"
    role = str(entry.get("role"))
    if role == "eval_holdout":
        raise SongRoleViolation(
            f"no se puede entrenar con holdout de evaluación: {_describe(entry, identifier)}",
        )
    return role


def filter_evaluable(identifiers: Iterable[str], *, path=None) -> tuple[list[str], list[str]]:
    """Separa (evaluables, rechazadas por ser train). No levanta."""
    ok, rejected = [], []
    for identifier in identifiers:
        try:
            assert_evaluable(identifier, path=path)
            ok.append(identifier)
        except SongRoleViolation:
            rejected.append(identifier)
    return ok, rejected


def role_split(identifiers: Iterable[str], *, path=None) -> dict[str, int]:
    """Resumen de roles para declarar la cohorte en cualquier reporte."""
    counts = {"train": 0, "val": 0, "eval_holdout": 0, "unknown": 0}
    for identifier in identifiers:
        role = role_for(identifier, path) or "unknown"
        counts[role] = counts.get(role, 0) + 1
    return counts


def assign_role(sha256: str | None, role: str, reason: str, *,
                title: str = "", artist: str = "",
                job_ids: Iterable[dict[str, str]] | None = None,
                path: str | os.PathLike[str] | None = None,
                assigned_at: str | None = None) -> dict[str, Any]:
    """Crea o cambia el rol de una canción dejando rastro.

    Cambiar un rol existente exige motivo y guarda el rol previo en ``history``.
    Nunca se pisa una entrada en silencio: esa es toda la razón de este módulo.
    """
    if role not in ROLES:
        raise ValueError(f"rol inválido: {role!r} (esperado uno de {ROLES})")
    if not str(reason or "").strip():
        raise ValueError("todo cambio de rol necesita un motivo")
    target = Path(path) if path else registry_path()
    registry = load_registry(target, refresh=True)
    entries = registry.setdefault("entries", [])
    sha = _normalise_sha(sha256)
    known = [dict(item) for item in (job_ids or []) if isinstance(item, dict)]
    today = str(assigned_at or date.today().isoformat())

    existing = None
    if sha:
        existing = next((e for e in entries if _normalise_sha(e.get("sha256")) == sha), None)
    if existing is None:
        for candidate in known:
            existing = next(
                (
                    e for e in entries
                    for row in (e.get("known_job_ids") or [])
                    if isinstance(row, dict)
                    and str(row.get("job_id")) == str(candidate.get("job_id"))
                ),
                None,
            )
            if existing is not None:
                break

    if existing is None:
        entry = {
            "sha256": sha or None,
            "needs_sha256": not bool(sha),
            "role": role,
            "title": title,
            "artist": artist,
            "known_job_ids": known,
            "assigned_at": today,
            "reason": reason,
            "history": [],
        }
        entries.append(entry)
    else:
        entry = existing
        if str(entry.get("role")) != role:
            entry.setdefault("history", []).append({
                "previous_role": entry.get("role"),
                "changed_at": today,
                "reason": reason,
            })
            entry["role"] = role
            entry["assigned_at"] = today
            entry["reason"] = reason
        if sha and not _normalise_sha(entry.get("sha256")):
            entry["sha256"] = sha
            entry["needs_sha256"] = False
        entry["title"] = entry.get("title") or title
        entry["artist"] = entry.get("artist") or artist
        seen = {str(r.get("job_id")) for r in entry.get("known_job_ids") or []}
        for row in known:
            if str(row.get("job_id")) not in seen:
                entry.setdefault("known_job_ids", []).append(row)

    entries.sort(key=lambda e: (str(e.get("artist") or ""), str(e.get("title") or "")))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(registry, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    load_registry(target, refresh=True)
    return entry


def summary(path=None) -> dict[str, Any]:
    entries = _entries(path)
    counts = {role: 0 for role in ROLES}
    for entry in entries:
        counts[str(entry.get("role"))] = counts.get(str(entry.get("role")), 0) + 1
    return {
        "schema": SCHEMA,
        "songs": len(entries),
        "by_role": counts,
        "needs_sha256": sum(1 for e in entries if e.get("needs_sha256")),
    }
