"""El registro de roles es lo único que separa entrenamiento de evaluación.

Contexto: el canary del 2026-09-02 usó como "cohorte nueva" el propio golden
set; 15 de sus 30 canciones eran audio de entrenamiento del LoRA v1 y nada en el
código lo impidió. Estos tests cubren las dos direcciones del contrato.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import song_roles  # noqa: E402


def _registry(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "song_roles.json"
    path.write_text(
        json.dumps({"schema": song_roles.SCHEMA, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    song_roles.load_registry(path, refresh=True)
    return path


def _entry(sha: str, role: str, job_id: str, **extra) -> dict:
    return {
        "sha256": sha, "role": role, "title": extra.get("title", "T"),
        "artist": extra.get("artist", "A"),
        "known_job_ids": [{"env": "staging", "job_id": job_id}],
        "assigned_at": "2026-09-03", "reason": "test", "history": [],
    }


def test_lookup_by_sha_and_by_job(tmp_path):
    path = _registry(tmp_path, [_entry("a" * 64, "train", "job000000001")])
    assert song_roles.role_for_sha("A" * 64, path) == "train"
    assert song_roles.role_for_job("job000000001", path) == "train"
    assert song_roles.role_for("job000000001", path) == "train"
    assert song_roles.role_for_job("desconocido", path) is None


def test_assert_evaluable_rejects_training_audio(tmp_path):
    path = _registry(tmp_path, [_entry("b" * 64, "train", "job000000002", title="Perra")])
    with pytest.raises(song_roles.SongRoleViolation) as excinfo:
        song_roles.assert_evaluable("job000000002", path=path)
    assert "Perra" in str(excinfo.value)


def test_assert_trainable_rejects_holdout(tmp_path):
    path = _registry(tmp_path, [_entry("c" * 64, "eval_holdout", "job000000003")])
    with pytest.raises(song_roles.SongRoleViolation):
        song_roles.assert_trainable("c" * 64, path=path)
    # La dirección opuesta sí está permitida.
    assert song_roles.assert_evaluable("c" * 64, path=path) == "eval_holdout"


def test_val_songs_can_be_evaluated_and_trained(tmp_path):
    path = _registry(tmp_path, [_entry("d" * 64, "val", "job000000004")])
    assert song_roles.assert_evaluable("d" * 64, path=path) == "val"
    assert song_roles.assert_trainable("d" * 64, path=path) == "val"


def test_unknown_song_abstains_or_fails_when_strict(tmp_path):
    path = _registry(tmp_path, [])
    assert song_roles.assert_evaluable("nadie", path=path) == "unknown"
    with pytest.raises(song_roles.SongRoleUnknown):
        song_roles.assert_evaluable("nadie", path=path, strict_unknown=True)


def test_filter_and_split_report_the_cohort(tmp_path):
    path = _registry(tmp_path, [
        _entry("e" * 64, "train", "job000000005"),
        _entry("f" * 64, "eval_holdout", "job000000006"),
    ])
    ok, rejected = song_roles.filter_evaluable(
        ["job000000005", "job000000006", "job000000007"], path=path,
    )
    assert ok == ["job000000006", "job000000007"]
    assert rejected == ["job000000005"]
    assert song_roles.role_split(
        ["job000000005", "job000000006", "job000000007"], path=path,
    ) == {"train": 1, "val": 0, "eval_holdout": 1, "unknown": 1}


def test_role_change_requires_reason_and_keeps_history(tmp_path):
    path = _registry(tmp_path, [_entry("1" * 64, "train", "job000000008")])
    with pytest.raises(ValueError):
        song_roles.assign_role("1" * 64, "eval_holdout", "", path=path)
    with pytest.raises(ValueError):
        song_roles.assign_role("1" * 64, "inventado", "motivo", path=path)

    entry = song_roles.assign_role(
        "1" * 64, "eval_holdout", "reservada para el gate de septiembre",
        path=path, assigned_at="2026-09-10",
    )
    assert entry["role"] == "eval_holdout"
    assert entry["history"] == [{
        "previous_role": "train", "changed_at": "2026-09-10",
        "reason": "reservada para el gate de septiembre",
    }]
    # Persistió en disco, no sólo en memoria.
    saved = json.loads(path.read_text(encoding="utf-8"))["entries"][0]
    assert saved["role"] == "eval_holdout" and saved["history"]


def test_assign_role_without_sha_marks_needs_sha256(tmp_path):
    path = _registry(tmp_path, [])
    entry = song_roles.assign_role(
        None, "eval_holdout", "sólo conocemos el job",
        job_ids=[{"env": "staging", "job_id": "job000000009"}], path=path,
    )
    assert entry["sha256"] is None and entry["needs_sha256"] is True
    assert song_roles.role_for_job("job000000009", path) == "eval_holdout"
    # Cuando aparece el hash, se completa sin duplicar la entrada.
    song_roles.assign_role(
        "9" * 64, "eval_holdout", "hash calculado",
        job_ids=[{"env": "staging", "job_id": "job000000009"}], path=path,
    )
    entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 1
    assert entries[0]["sha256"] == "9" * 64 and entries[0]["needs_sha256"] is False


def test_shipped_registry_has_the_canary_split(tmp_path):
    """El registro versionado debe reflejar lo que encontró el sprint."""
    shipped = song_roles.summary()
    assert shipped["songs"] >= 60
    assert shipped["by_role"]["train"] > 0
    assert shipped["by_role"]["eval_holdout"] >= 23
    # La #1 de la lista original del Rol 1 era audio de entrenamiento.
    assert song_roles.role_for_job("ec5bd53c1dbd") == "train"
    # Las que quedaron en el holdout del sprint no pueden entrenar.
    with pytest.raises(song_roles.SongRoleViolation):
        song_roles.assert_trainable("6bd2142c0f6d")
