"""Pure guardrails from anchor_structural_guard (incidente 2026-09-13).

Calibration data (campaign snapshot, .context/campaign-draft-audit-20260913):
Color Esperanza re-anchored over Diego Torres' full lyric → run of 7 (31 %);
Zi Zi Zi → run of 6; Buseca's Whisper-DP padding → run of 5. Among 311 live
jobs no healthy one has a run above 3; hand-edited songs with many short
lines reach a crammed fraction of 0.25-0.28, hence the 0.4 backstop.
"""

import uuid
from datetime import datetime, timedelta, timezone

from anchor_structural_guard import (
    crammed_line_verdict,
    segment_write_velocity,
    segment_write_velocity_exceeded,
)


def _line(start, end, text, score):
    words = text.split()
    step = (end - start) / max(1, len(words))
    return {
        "start": start, "end": end, "text": text,
        "words": [
            {"word": w, "start": round(start + i * step, 3),
             "end": round(start + (i + 1) * step, 3), "score": score}
            for i, w in enumerate(words)
        ],
    }


def _healthy(n=20):
    return [_line(i * 3.0, i * 3.0 + 2.4, f"linea cantada numero {i}", 0.7) for i in range(n)]


def test_healthy_alignment_is_not_a_mismatch():
    verdict = crammed_line_verdict(_healthy())
    assert verdict["mismatch"] is False
    assert verdict["crammed_lines"] == 0
    assert verdict["scored_lines"] == 20


def test_color_esperanza_shape_run_of_crammed_lines_trips():
    segs = _healthy(10)
    t = segs[-1]["end"]
    # 8 unsung chorus lines squeezed into ~4 s with score ≈ 0 (forced_align).
    for i in range(8):
        segs.append(_line(t + i * 0.5, t + i * 0.5 + 0.45, "saber que se puede", 0.01))
    segs += [_line(t + 10 + i * 3.0, t + 10 + i * 3.0 + 2.4, "linea final cantada", 0.6) for i in range(3)]
    verdict = crammed_line_verdict(segs)
    assert verdict["mismatch"] is True
    assert verdict["crammed_run"] == 8
    assert verdict["crammed_lines"] == 8


def test_isolated_short_low_score_lines_are_tolerated():
    """Ad-libs ('oh oh oh') come back short and low-scored on healthy songs
    too; three scattered ones are far below both thresholds."""
    segs = _healthy(30)
    for idx in (5, 14, 23):
        segs[idx] = _line(idx * 3.0, idx * 3.0 + 0.4, "oh oh oh", 0.02)
    verdict = crammed_line_verdict(segs)
    assert verdict["mismatch"] is False
    assert verdict["crammed_lines"] == 3
    assert verdict["crammed_run"] == 1


def test_fraction_threshold_trips_without_a_long_run():
    segs = []
    for i in range(12):
        good = _line(i * 3.0, i * 3.0 + 2.4, f"linea cantada {i}", 0.8)
        bad = _line(i * 3.0 + 2.45, i * 3.0 + 2.9, "estrofa que no existe", 0.0)
        segs += [good, bad]
    verdict = crammed_line_verdict(segs)
    assert verdict["crammed_run"] == 1
    assert verdict["crammed_fraction"] == 0.5
    assert verdict["mismatch"] is True


def test_whisper_dp_interpolation_pads_trip_without_scores():
    """Buseca y Vino Tinto (80af47d18113, 2026-09-14): CTC declined (short
    repeated motif), hosted align was unsafe, Whisper-DP anchored 29/51 lines
    and padded the rest to 0.6 s with a constant 0.6 word score. Five words in
    0.6 s is not singing; the verdict must not depend on a low score."""
    segs = _healthy(8)
    t = segs[-1]["end"]
    for i in range(5):
        segs.append(_line(t + i * 0.6, t + i * 0.6 + 0.6, "y yo me quemo hasta los dientes", 0.6))
    segs += [_line(t + 4 + i * 3.0, t + 4 + i * 3.0 + 2.4, "buseca y vino tinto", 0.6) for i in range(4)]
    verdict = crammed_line_verdict(segs)
    assert verdict["mismatch"] is True
    assert verdict["crammed_run"] == 5


def test_lines_without_word_scores_count_only_when_too_dense():
    slow = [{"start": i * 2.0, "end": i * 2.0 + 0.9, "text": "dos palabras", "words": []}
            for i in range(20)]
    assert crammed_line_verdict(slow)["mismatch"] is False  # 2.2 words/s
    dense = [{"start": i * 0.5, "end": i * 0.5 + 0.3, "text": "cinco palabras en tres decimas", "words": []}
             for i in range(20)]
    verdict = crammed_line_verdict(dense)
    assert verdict["mismatch"] is True
    assert verdict["scored_lines"] == 20


def test_fast_rap_lines_with_real_scores_are_fine():
    """Two fast-but-real lines (4 words in 0.95 s, score 0.8) between sung
    lines: under the run threshold and well scored."""
    segs = _healthy(10)
    segs[4] = _line(12.0, 12.95, "rapeo muy rapido aca", 0.8)
    segs[5] = _line(13.0, 13.9, "otra linea veloz igual", 0.8)
    assert crammed_line_verdict(segs)["mismatch"] is False


def test_single_word_lines_never_count():
    segs = [_line(i * 0.5, i * 0.5 + 0.3, "oh", 0.0) for i in range(20)]
    assert crammed_line_verdict(segs)["mismatch"] is False


def test_verdict_survives_garbage_rows():
    segs = [None, "x", {"start": "a", "end": None, "text": "b c", "words": [{"score": 0.0}]}]
    verdict = crammed_line_verdict(segs)
    assert verdict["mismatch"] is False


def test_thresholds_are_env_tunable(monkeypatch):
    segs = _healthy(10)
    t = segs[-1]["end"]
    for i in range(3):
        segs.append(_line(t + i * 0.5, t + i * 0.5 + 0.45, "no cantada", 0.01))
    assert crammed_line_verdict(segs)["mismatch"] is False  # run 3 < default 4
    monkeypatch.setenv("REANCHOR_CRAMMED_MIN_RUN", "3")
    assert crammed_line_verdict(segs)["mismatch"] is True


# ---------------------------------------------------------------------------
# Velocity of segment writes across distinct jobs
# ---------------------------------------------------------------------------


def _seed_diffs(user_id, n_jobs, *, age_s=0):
    from database import AuditLog, SessionLocal
    s = SessionLocal()
    try:
        when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        for _ in range(n_jobs):
            s.add(AuditLog(
                user_id=user_id, action="lyrics.segments_diff",
                detail={"job_id": uuid.uuid4().hex[:12], "schema": "editor-line-delta-v2"},
                created_at=when,
            ))
        s.commit()
    finally:
        s.close()


def _user_id(client):
    from database import SessionLocal, User
    username = f"velocity_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    s = SessionLocal()
    try:
        return s.query(User).filter(User.username == username).first().id
    finally:
        s.close()


def test_velocity_counts_distinct_jobs_in_window(client):
    from database import SessionLocal
    uid = _user_id(client)
    _seed_diffs(uid, 5)
    _seed_diffs(uid, 40, age_s=3600)  # outside a 10-minute window
    s = SessionLocal()
    try:
        assert segment_write_velocity(s, uid, "newjob000001", window_s=600) == 6
        assert segment_write_velocity(s, uid, "newjob000001", window_s=7200) == 46
    finally:
        s.close()


def test_velocity_exceeded_uses_env_limits(client, monkeypatch):
    from database import SessionLocal
    uid = _user_id(client)
    _seed_diffs(uid, 30)
    s = SessionLocal()
    try:
        monkeypatch.setenv("SEGMENT_WRITE_MAX_DISTINCT_JOBS", "25")
        exceeded, count, max_jobs = segment_write_velocity_exceeded(s, uid, "x")
        assert (exceeded, count, max_jobs) == (True, 31, 25)
        monkeypatch.setenv("SEGMENT_WRITE_MAX_DISTINCT_JOBS", "0")
        assert segment_write_velocity_exceeded(s, uid, "x")[0] is False
    finally:
        s.close()


def test_velocity_same_job_repeated_never_accumulates(client):
    from database import AuditLog, SessionLocal
    uid = _user_id(client)
    s = SessionLocal()
    try:
        for _ in range(60):
            s.add(AuditLog(user_id=uid, action="lyrics.segments_diff",
                           detail={"job_id": "samejob00001"}))
        s.commit()
        assert segment_write_velocity(s, uid, "samejob00001", window_s=600) == 1
    finally:
        s.close()
