"""REANCHOR_HOLD_MODE: el re-anclado aplica el mismo hold que la subida.

Fixtures inventados (sin letras ni tiempos de clientes).
"""
import pytest

import main as main_mod
import reanchor_hold
from tests.conftest import auth
from tests.test_reanchor import _db_segments, _make_user, _seed_job


def _line(start, end, text, last_word_end=None, **extra):
    words = text.split()
    last = end if last_word_end is None else last_word_end
    step = (last - start) / len(words)
    seg = {
        "start": start, "end": end, "text": text,
        "words": [{"word": w, "start": round(start + k * step, 3),
                   "end": round(start + (k + 1) * step, 3)} for k, w in enumerate(words)],
    }
    seg.update(extra)
    return seg


@pytest.fixture(autouse=True)
def _timing_env(monkeypatch):
    monkeypatch.delenv("LYRIC_HOLD_S", raising=False)
    monkeypatch.delenv("LYRIC_MIN_GAP_MS", raising=False)
    monkeypatch.delenv("REANCHOR_HOLD_MODE", raising=False)


def test_default_is_off_and_computes_nothing():
    segs = [_line(0.0, 1.0, "uno dos"), _line(3.0, 4.0, "tres cuatro")]
    out, stats = reanchor_hold.run(segs, [])
    assert out is segs and stats is None


def test_invalid_mode_is_off(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "enforce")
    assert reanchor_hold.mode() == "off"


def test_apply_extends_only_lines_ending_on_last_word(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    segs = [
        _line(0.0, 1.0, "uno dos"),                        # termina en la palabra, aire de sobra
        _line(1.2, 2.0, "tres cuatro"),                    # aire 0,2 s: tope = siguiente − 0,01
        _line(2.2, 3.5, "cinco seis", last_word_end=3.1),  # ya trae hold: no se toca
        _line(6.0, 7.0, "siete ocho", locked=True),        # bloqueada
        _line(9.0, 10.0, "nueve diez"),                    # última: apply_hold no la toca
    ]
    out, stats = reanchor_hold.run(segs, [])
    assert [s["end"] for s in out] == [1.19, 2.19, 3.5, 7.0, 10.0]
    assert stats["mode"] == "apply" and stats["applied"] is True
    assert stats["extended"] == 2 and stats["eligible"] == 3
    assert stats["hold_s"] == 0.5
    # Nunca cambia texto, inicio ni palabras.
    for a, b in zip(segs, out):
        assert (a["text"], a["start"], a["words"]) == (b["text"], b["start"], b["words"])


def test_full_hold_when_there_is_air(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    out, _ = reanchor_hold.run([_line(0.0, 1.0, "uno dos"), _line(3.0, 4.0, "tres")], [])
    assert out[0]["end"] == 1.5


def test_lines_not_retimed_by_this_reanchor_are_kept(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    kept = _line(0.0, 1.0, "uno dos")
    segs = [dict(kept), _line(3.0, 4.0, "tres cuatro"), _line(6.0, 7.0, "cinco")]
    out, stats = reanchor_hold.run(segs, [kept])
    assert out[0]["end"] == 1.0          # el revisor ya lo dejó así antes del re-anclado
    assert out[1]["end"] == 4.5
    assert stats["extended"] == 1


def test_observe_reports_without_mutating(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "observe")
    segs = [_line(0.0, 1.0, "uno dos"), _line(3.0, 4.0, "tres")]
    out, stats = reanchor_hold.run(segs, [])
    assert out is segs
    assert stats["extended"] == 1 and stats["applied"] is False
    assert stats["seconds_added"] == 0.5


def test_uses_fleet_hold_value(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    monkeypatch.setenv("LYRIC_HOLD_S", "0.3")
    out, stats = reanchor_hold.run([_line(0.0, 1.0, "uno dos"), _line(3.0, 4.0, "tres")], [])
    assert out[0]["end"] == 1.3 and stats["hold_s"] == 0.3


def test_min_gap_never_shortens(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    monkeypatch.setenv("LYRIC_MIN_GAP_MS", "300")
    segs = [_line(0.0, 1.0, "uno dos"), _line(1.1, 2.0, "tres cuatro"), _line(5.0, 6.0, "cinco")]
    out, _ = reanchor_hold.run(segs, [])
    assert out[0]["end"] == 1.0           # apply_hold recortaría; acá sólo se alarga
    assert out[1]["end"] == 2.5


def test_lines_without_words_are_not_eligible(monkeypatch):
    monkeypatch.setenv("REANCHOR_HOLD_MODE", "apply")
    segs = [{"start": 0.0, "end": 1.0, "text": "uno"}, _line(3.0, 4.0, "dos")]
    out, stats = reanchor_hold.run(segs, [])
    # Sólo cuenta la segunda (con palabras); por ser la última no se alarga.
    assert out[0]["end"] == 1.0 and stats["eligible"] == 1 and stats["extended"] == 0


# ── endpoint ────────────────────────────────────────────────────────────────

PREV = [
    {"start": 0.0, "end": 2.0, "text": "linea uno inventada", "_id": 0},
    {"start": 2.0, "end": 4.0, "text": "linea dos inventada", "_id": 1},
    {"start": 4.0, "end": 6.0, "text": "linea tres inventada", "_id": 2},
]


def _mock_align(monkeypatch):
    async def _fake(result, audio_path, job_id, anchor_lyrics):
        out = dict(result)
        out["segments"] = [
            _line(0.4, 1.6, "linea uno inventada"),
            _line(3.0, 4.2, "linea dos inventada"),
            _line(4.3, 5.5, "linea tres inventada"),
        ]
        out["timing_source"] = "anchor_ctc"
        return out
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)


@pytest.mark.parametrize("mode, ends, has_stats", [
    (None, [1.6, 4.2, 5.5], False),
    ("observe", [1.6, 4.2, 5.5], True),
    ("apply", [2.1, 4.29, 5.5], True),
])
def test_reanchor_endpoint_hold_modes(client, monkeypatch, mode, ends, has_stats):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    if mode:
        monkeypatch.setenv("REANCHOR_HOLD_MODE", mode)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in PREV])
    _mock_align(monkeypatch)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    persisted = _db_segments(job_id)
    assert [s["end"] for s in persisted] == ends
    assert body["segments"] == persisted
    assert ("hold" in body) is has_stats
    if has_stats:
        assert body["hold"]["extended"] == 2
        assert body["hold"]["applied"] is (mode == "apply")
