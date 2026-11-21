"""Timing no confiable a nivel canción: detector, traspaso de tiempo y
re-alineado del worker. Fixtures inventadas (ninguna letra real)."""
import asyncio
import copy

import pytest

import song_timing_trust as stt
from lyric_review import build_review
from transcription_worker import _maybe_song_timing_trust

VERSE = [
    "la luna sale temprano sobre el puerto",
    "camino solo por la calle del mercado",
    "nadie me espera cuando vuelvo de noche",
    "el viento trae canciones de otro tiempo",
    "las luces bajan lentas hasta el agua",
    "y la ciudad se duerme sin saberlo",
]


def words_for(text, start, step=0.4, score=0.9):
    out = []
    for i, w in enumerate(text.split()):
        out.append({"word": w, "start": round(start + i * step, 3),
                    "end": round(start + i * step + step * 0.8, 3), "score": score})
    return out


def healthy_song(n_rep=3):
    segs, t = [], 5.0
    for _ in range(n_rep):
        for line in VERSE:
            w = words_for(line, t)
            segs.append({"text": line, "start": w[0]["start"], "end": w[-1]["end"] + 0.3,
                         "words": w, "line_signals_v2": {"voiced_gap_after_s": 0.0}})
            t = w[-1]["end"] + 0.8
    return segs


def witness_of(segs, shift=0.0):
    return [{"word": w["word"], "start": w["start"] + shift, "end": w["end"] + shift}
            for s in segs for w in s["words"]]


def broken_song():
    """Palabras estiradas, score bajo, canto sin cartel y control distinto."""
    segs = []
    t = 5.0
    for i, line in enumerate(VERSE * 3):
        n = len(line.split())
        w = words_for(line, t, step=1.6, score=0.35)
        segs.append({"text": line, "start": w[0]["start"], "end": w[-1]["end"],
                     "words": w, "line_signals_v2": {"voiced_gap_after_s": 3.0 if i % 3 == 0 else 0.0}})
        t = w[-1]["end"] + 12.0
        assert n > 1
    return segs


def heard_elsewhere(segs):
    # el testigo oye letra en los huecos largos entre carteles
    out = []
    for s in segs:
        for k, w in enumerate(s["text"].split()):
            out.append({"word": w, "start": s["end"] + 2 + k * 0.4, "end": s["end"] + 2.3 + k * 0.4})
    return out


CONTROL_OTHER = " ".join(["otra letra distinta que no coincide con nada de lo que dice la pantalla"] * 3)


def test_healthy_song_is_trusted():
    segs = healthy_song()
    verdict = stt.assess(segs, witness=witness_of(segs), control_text=" ".join(s["text"] for s in segs))
    assert verdict["unreliable"] is False
    assert verdict["lines"] == []
    assert verdict["score"] < stt.THRESHOLD


def test_broken_song_is_flagged_with_suspicious_lines_first():
    segs = broken_song()
    verdict = stt.assess(segs, witness=heard_elsewhere(segs), control_text=CONTROL_OTHER)
    assert verdict["unreliable"] is True
    assert set(verdict["reasons"]) >= {"word_score_median", "slow_line_share"}
    assert verdict["lines"], "las líneas sospechosas se listan"
    scores = [row["score"] for row in verdict["lines"]]
    assert scores == sorted(scores, reverse=True)
    assert len(verdict["lines"]) <= stt.MAX_SUSPICIOUS_LINES
    assert "stretched_words" in verdict["lines"][0]["reasons"]


def test_missing_signals_are_neutral():
    segs = healthy_song()
    for s in segs:
        s.pop("line_signals_v2")
        for w in s["words"]:
            w.pop("score")
    values = stt.signals(segs, witness=[], control_text="")
    assert values["word_score_median"] is None and values["witness_outside_share"] is None
    total, parts = stt.score(values)
    assert parts["word_score_median"] == pytest.approx(-0.526 * (0.782 - 0.7678) / 0.1336)
    assert stt.assess(segs, witness=[], control_text="")["unreliable"] is False


def test_assess_empty_never_raises():
    assert stt.assess([], witness=[], control_text="")["unreliable"] is False
    assert stt.assess(None, None)["score"] is None


def test_mode_env(monkeypatch):
    monkeypatch.delenv("SONG_TIMING_TRUST_MODE", raising=False)
    assert stt.mode() == "observe"
    monkeypatch.setenv("SONG_TIMING_TRUST_MODE", "APPLY")
    assert stt.mode() == "apply"
    monkeypatch.setenv("SONG_TIMING_TRUST_MODE", "nonsense")
    assert stt.mode() == "observe"


# --------------------------------------------------------------------------
# Traspaso de tiempo
# --------------------------------------------------------------------------

def official_aligned(lines, start=10.0, step=0.5, gap=1.0):
    out, t = [], start
    for line in lines:
        w = words_for(line, t, step=step, score=0.8)
        out.append({"text": line, "start": w[0]["start"], "end": w[-1]["end"], "words": w})
        t = w[-1]["end"] + gap
    return out


def test_transfer_copies_time_and_keeps_machine_text():
    machine = [dict(s) for s in healthy_song(1)]
    # la máquina oyó mal una palabra del medio de la línea 2
    machine[1]["text"] = "camino solo por la calle del mercurio"
    official = official_aligned(VERSE)
    new, stats = stt.transfer_timing(machine, official)
    assert stats["applied"] and stats["retimed"] == len(VERSE)
    assert [s["text"] for s in new] == [s["text"] for s in machine]
    assert new[0]["start"] == official[0]["start"]
    assert new[0]["official_timing"] is True
    # la palabra mal oída conserva su texto y toma el tiempo de su lugar
    assert new[1]["words"][-1]["word"] == "mercurio" and "score" not in new[1]["words"][-1]
    assert all(b["start"] >= a["start"] for a, b in zip(new, new[1:]))
    assert [w["word"] for w in new[2]["words"]] == VERSE[2].split()


def test_transfer_interpolates_unanchored_middle_word():
    machine = healthy_song(1)
    machine[3]["text"] = "el viento trae melodías de otro tiempo"
    official = official_aligned(VERSE)
    new, _ = stt.transfer_timing(machine, official)
    words = new[3]["words"]
    assert new[3]["official_timing"] is True
    assert words[3]["word"] == "melodías" and "score" not in words[3]
    assert words[2]["end"] <= words[3]["start"] <= words[4]["start"]


def test_transfer_declines_other_version():
    machine = healthy_song(1)
    official = official_aligned(["una letra que no tiene nada que ver"] * 6)
    new, stats = stt.transfer_timing(machine, official)
    assert not stats["applied"] and stats["reason"] == "official_text_does_not_cover_lines"
    assert new == machine


def test_transfer_never_overlaps_neighbours():
    machine = healthy_song(1)
    official = official_aligned(VERSE, step=0.5, gap=-1.5)  # alineado que se pisa
    new, stats = stt.transfer_timing(machine, official)
    for a, b in zip(new, new[1:]):
        assert float(a["end"]) <= float(b["start"]) + 1e-6 or not (
            a.get("official_timing") and b.get("official_timing"))


def test_transfer_holds_end_after_last_word():
    machine = healthy_song(1)
    official = official_aligned(VERSE, gap=2.0)
    new, _ = stt.transfer_timing(machine, official)
    last_word_end = new[0]["words"][-1]["end"]
    assert new[0]["end"] == pytest.approx(last_word_end + stt.HOLD_S)
    assert new[0]["end"] < new[1]["start"]


@pytest.mark.parametrize("report,live,ok,reason", [
    ({"allow_vocabulary_reconciliation": True, "metrics": {"reference_token_coverage": 0.7}}, False, True, "vocabulary_attested"),
    ({"allow_vocabulary_reconciliation": True, "metrics": {"reference_token_coverage": 0.3}}, False, False, "reference_mostly_unheard"),
    ({"allow_vocabulary_reconciliation": False, "metrics": {"reference_token_coverage": 0.9}}, False, False, "vocabulary_not_attested"),
    ({"allow_vocabulary_reconciliation": True, "metrics": {"reference_token_coverage": 0.9}}, True, False, "live"),
    (None, False, False, "no_report"),
])
def test_timing_reference_certification(report, live, ok, reason):
    assert stt.timing_reference_certified(report, is_live=live) == (ok, reason)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def broken_result():
    segs = broken_song()
    return {"segments": copy.deepcopy(segs), "timing_source": "ctc_alignment"}


@pytest.fixture
def force_unreliable(monkeypatch):
    def assess(segments, evidence=None, **kwargs):
        return {"schema": stt.SCHEMA, "unreliable": True, "score": 2.0, "threshold": stt.THRESHOLD,
                "signals": {}, "reasons": ["word_score_median"], "lines": [{"line": 0}]}
    monkeypatch.setattr(stt, "assess", assess)


async def never(*args, **kwargs):
    raise AssertionError("no debe alinear")


def run(result, monkeypatch, mode, aligner=never, lookup=lambda job_id: (" \n".join(VERSE * 3), "sheet"), live=False):
    monkeypatch.setenv("SONG_TIMING_TRUST_MODE", mode)
    return asyncio.run(_maybe_song_timing_trust(result, "/no/audio.wav", "job1", live=live,
                                                aligner=aligner, official_lookup=lookup))


def test_worker_off_is_inert(monkeypatch):
    result = broken_result()
    assert run(result, monkeypatch, "off") is result


def test_worker_observe_records_without_aligning(monkeypatch, force_unreliable):
    result = broken_result()
    out = run(result, monkeypatch, "observe")
    trust = out["song_timing_trust"]
    assert trust["unreliable"] and trust["mode"] == "observe"
    assert trust["realign"]["status"] == "would_realign" and trust["realign"]["certified"]
    assert out["segments"] == result["segments"]
    assert "text" not in trust["realign"]  # nunca se guarda la letra


@pytest.mark.parametrize("lookup,status", [
    (lambda job_id: ("", ""), "no_official_text"),
    (lambda job_id: ("otra cancion completamente distinta sin nada en comun con lo que se canta aqui", "lrclib"), "not_certified"),
])
def test_worker_without_certified_reference_keeps_output(monkeypatch, force_unreliable, lookup, status):
    result = broken_result()
    out = run(result, monkeypatch, "apply", lookup=lookup)
    assert out["song_timing_trust"]["realign"]["status"] == status
    assert out["segments"] == result["segments"]


def test_worker_skips_live_and_official_timing(monkeypatch, force_unreliable):
    assert run(broken_result(), monkeypatch, "apply", live=True)["song_timing_trust"]["realign"]["status"] == "live_not_supported"
    official = {**broken_result(), "timing_source": "anchor_ctc"}
    assert run(official, monkeypatch, "apply")["song_timing_trust"]["realign"]["status"] == "already_official"


def aligner_for(lines):
    async def aligner(base, path, job_id, text, **kwargs):
        assert kwargs == {"content_source": "catalog_reference", "enabled": True}
        base["segments"] = official_aligned(text.splitlines(), start=3.0)
        base["anchor_alignment"] = {"status": "applied", "timing_source": "ctc_timing_only"}
        base["timing_source"] = "anchor_ctc"
        return base
    return aligner


def test_worker_shadow_measures_but_does_not_apply(monkeypatch, force_unreliable):
    result = broken_result()
    out = run(result, monkeypatch, "shadow", aligner=aligner_for(VERSE * 3),
              lookup=lambda job_id: ("\n".join(VERSE * 3), "sheet"))
    realign = out["song_timing_trust"]["realign"]
    assert realign["status"] == "shadow" and realign["transfer"]["applied"]
    assert out["segments"] == result["segments"]
    assert out["timing_source"] == "ctc_alignment"


def test_worker_apply_changes_only_timing(monkeypatch, force_unreliable):
    result = broken_result()
    out = run(result, monkeypatch, "apply", aligner=aligner_for(VERSE * 3),
              lookup=lambda job_id: ("\n".join(VERSE * 3), "sheet"))
    assert out["song_timing_trust"]["realign"]["status"] == "applied"
    assert [s["text"] for s in out["segments"]] == [s["text"] for s in result["segments"]]
    assert out["segments"][0]["start"] == 3.0 != result["segments"][0]["start"]
    assert out["timing_source"] == result["timing_source"]
    assert "_segments" not in out["song_timing_trust"]["realign"]


def test_worker_alignment_decline_keeps_output(monkeypatch, force_unreliable):
    async def declined(base, *args, **kwargs):
        base["anchor_alignment"] = {"status": "declined", "reason": "structural_mismatch"}
        return base
    result = broken_result()
    out = run(result, monkeypatch, "apply", aligner=declined,
              lookup=lambda job_id: ("\n".join(VERSE * 3), "sheet"))
    assert out["song_timing_trust"]["realign"]["status"] == "alignment_declined"
    assert out["segments"] == result["segments"]


def test_worker_assess_failure_never_breaks(monkeypatch):
    monkeypatch.setattr(stt, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = broken_result()
    assert run(result, monkeypatch, "apply") is result


# --------------------------------------------------------------------------
# Revisión rápida
# --------------------------------------------------------------------------

def test_review_exposes_song_warning_on_current_lines(monkeypatch):
    monkeypatch.delenv("SONG_TIMING_TRUST_MODE", raising=False)
    original = broken_song()
    current = copy.deepcopy(original)
    for i, s in enumerate(current):
        s["segment_id"] = f"seg-{i}"
    review = build_review(current, original_segments=original, machine_evidence=None)
    trust = review["timing_trust"]
    assert trust["unreliable"] is True
    assert trust["reasons"] and all(isinstance(r, str) for r in trust["reasons"])
    assert trust["lines"] and trust["lines"][0]["line_segment_id"].startswith("seg-")
    # nunca bloquea: el aviso no agrega puntos obligatorios
    assert review["required_count"] == build_review(current, original_segments=None)["required_count"]


def test_review_without_original_or_off(monkeypatch):
    segs = healthy_song(1)
    assert build_review(segs, original_segments=None)["timing_trust"] is None
    monkeypatch.setenv("SONG_TIMING_TRUST_MODE", "off")
    assert build_review(segs, original_segments=broken_song())["timing_trust"] is None
