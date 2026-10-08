"""Cobertura de escucha antes de aprobar: matemática y lectura de la base."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import uuid

from auth import create_user
from database import EditorVersion, Job, ProductEvent, SessionLocal

BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "report_listening_coverage", BACKEND / "scripts" / "report_listening_coverage.py",
)
coverage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coverage)


def _seg(start, end, text="hoy te vi pasar"):
    return {"start": start, "end": end, "text": text}


def test_merge_intervals_une_solapados_y_descarta_basura():
    assert coverage.merge_intervals([[5000, 8000], [0, 2000], [1500, 3000], [3000, 4000]]) == [
        (0, 4000), (5000, 8000),
    ]
    assert coverage.merge_intervals([[3, 3], [5, 1], ["0", "1"], [True, 4], [0, float("nan")], [1, 2, 3]]) == []


def test_line_spans_solo_usa_start_end_nunca_el_texto():
    spans = coverage.line_spans([
        _seg(1.0, 2.5), _seg(3, 3), {"text": "sin tiempos"}, _seg("x", 4), "basura", _seg(4.0004, 5),
    ])
    assert spans == [(1000, 2500), (4000, 5000)]


def test_covered_ms_suma_solo_lo_que_cae_dentro_de_la_linea():
    merged = [(0, 1500), (1800, 2200), (9000, 9500)]
    assert coverage.covered_ms((1000, 2000), merged) == 500 + 200
    assert coverage.covered_ms((3000, 4000), merged) == 0


def test_job_coverage_lineas_escuchadas_y_porcentaje_de_cancion():
    segments = [_seg(0, 2), _seg(2, 4), _seg(10, 12), _seg(20, 24)]
    result = coverage.job_coverage(
        segments,
        # Línea 1 completa, línea 2 al 50 % justo, línea 3 al 25 %, línea 4 nada.
        [[0, 2000], [2000, 3000], [11_500, 12_000]],
        audio_duration_ms=30_000,
    )
    assert result["lines"] == 4
    assert result["lines_heard"] == 2
    assert result["lines_touched"] == 3
    assert result["lines_heard_pct"] == 50.0
    assert result["lines_touched_pct"] == 75.0
    assert result["played_ms"] == 3500
    assert result["song_played_pct"] == round(100 * 3500 / 30_000, 1)


def test_job_coverage_repetir_un_tramo_no_infla_la_cobertura():
    segments = [_seg(0, 10)]
    once = coverage.job_coverage(segments, [[0, 4000]], audio_duration_ms=10_000)
    thrice = coverage.job_coverage(segments, [[0, 4000]] * 3, audio_duration_ms=10_000)
    assert once == thrice
    assert once["lines_heard"] == 0  # 40 % < 50 %
    assert coverage.job_coverage(
        segments, [[0, 4000]], audio_duration_ms=10_000, line_min_fraction=0.4,
    )["lines_heard"] == 1


def test_job_coverage_sin_duracion_usa_el_fin_de_la_ultima_linea_y_recorta():
    result = coverage.job_coverage([_seg(0, 5), _seg(5, 10)], [[0, 20_000]])
    assert result["duration_ms"] == 10_000
    assert result["played_ms"] == 10_000
    assert result["song_played_pct"] == 100.0
    assert result["lines_heard_pct"] == 100.0


def test_job_coverage_sin_lineas_ni_escucha():
    result = coverage.job_coverage([], [])
    assert result["lines"] == 0
    assert result["lines_heard_pct"] is None
    assert result["song_played_pct"] is None


def test_summarize_operators_deja_fuera_las_aprobadas_sin_editor():
    rows = [
        {"operator": "ana", "status": "measured", "lines": 10, "lines_heard": 10,
         "lines_heard_pct": 100.0, "song_played_pct": 95.0},
        {"operator": "ana", "status": "no_playback", "lines": 10, "lines_heard": 0,
         "lines_heard_pct": 0.0, "song_played_pct": 0.0},
        {"operator": "ana", "status": "no_editor_activity", "lines": 30, "lines_heard": 0,
         "lines_heard_pct": 0.0, "song_played_pct": 0.0},
    ]
    [ana] = coverage.summarize_operators(rows)
    assert ana["jobs"] == 3
    assert ana["jobs_measured"] == 1
    assert ana["jobs_no_playback"] == 1
    assert ana["jobs_no_editor_activity"] == 1
    assert ana["median_lines_heard_pct"] == 50.0
    assert ana["pooled_lines_heard_pct"] == 50.0
    assert ana["jobs_lines_heard_ge_90"] == 1
    assert ana["jobs_lines_heard_lt_50"] == 1


def test_collect_cuenta_solo_lo_del_aprobador_antes_de_la_primera_aprobacion():
    tenant = f"listen_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    approved_at = now - timedelta(minutes=10)
    with SessionLocal() as db:
        operator = create_user(db, f"listen_op_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        other = create_user(db, f"listen_other_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        silent = create_user(db, f"listen_silent_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        jobs = {key: f"lc_{uuid.uuid4().hex[:9]}" for key in ("heard", "silent", "batch")}
        for job_id in jobs.values():
            db.add(Job(
                job_id=job_id, user_id=operator.id, tenant_id=tenant,
                artist="A", song_title="S", filename="a.wav", style="oscuro",
                status="lyrics_approved", current_step="editing", delivery_profile="youtube",
            ))
        db.flush()
        segments = [_seg(0, 2), _seg(2, 4), _seg(4, 6), _seg(6, 8)]
        for key, approver in (("heard", operator), ("silent", silent), ("batch", operator)):
            db.add(EditorVersion(
                id=str(uuid.uuid4()), job_id=jobs[key], tenant_id=tenant, revision=3,
                segments=segments, created_by=approver.id, created_at=approved_at,
                reason="approve", is_approved=True,
            ))
        # Una re-aprobación posterior no cambia la "primera aprobación".
        db.add(EditorVersion(
            id=str(uuid.uuid4()), job_id=jobs["heard"], tenant_id=tenant, revision=5,
            segments=[_seg(0, 8)], created_by=operator.id, created_at=now,
            reason="approve", is_approved=True,
        ))

        def played(job_id, user, at, ranges):
            db.add(ProductEvent(
                tenant_id=tenant, user_id=user.id, job_id=job_id, name="editor_audio_played",
                occurred_at=at, properties={"ranges": ranges, "audio_duration_ms": 10_000},
            ))

        played(jobs["heard"], operator, approved_at - timedelta(minutes=1), [[0, 2000]])
        played(jobs["heard"], operator, approved_at + timedelta(seconds=2), [[2000, 4000]])  # envío "approve"
        played(jobs["heard"], operator, approved_at + timedelta(minutes=5), [[4000, 8000]])  # después
        played(jobs["heard"], other, approved_at - timedelta(minutes=1), [[4000, 8000]])  # otro usuario
        db.add(ProductEvent(
            tenant_id=tenant, user_id=silent.id, job_id=jobs["silent"],
            name="editor_activity_heartbeat", occurred_at=approved_at - timedelta(minutes=2),
            properties={"task": "text"},
        ))
        db.commit()

        rows = {
            row["job_id"]: row
            for row in coverage.collect(
                db, since=now - timedelta(hours=1), tenant=tenant,
                line_min_fraction=0.5, grace_s=5.0, limit=100,
            )
        }

    heard = rows[jobs["heard"]]
    assert heard["status"] == "measured"
    assert heard["approved_revision"] == 3
    assert heard["lines"] == 4
    assert heard["lines_heard"] == 2
    assert heard["lines_heard_pct"] == 50.0
    assert heard["song_played_pct"] == 40.0
    assert heard["playback_events"] == 2

    assert rows[jobs["silent"]]["status"] == "no_playback"
    assert rows[jobs["silent"]]["lines_heard_pct"] == 0.0
    assert rows[jobs["batch"]]["status"] == "no_editor_activity"
    # Nada de la letra viaja a la salida.
    assert "hoy te vi pasar" not in repr(rows)
