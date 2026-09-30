"""Revisión rápida: los casos que encontró la revisión adversarial (30-09-2026)."""
import time
import unicodedata
import uuid
from datetime import datetime, timezone

from lyric_review import apply_punctuation, build_review, replace_words
from lyric_review_sources import _memory_cache, memory_pairs


def _line(text, start, end, segment_id=None, words=None):
    row = {"text": text, "start": start, "end": end}
    if segment_id:
        row["segment_id"] = segment_id
    if words is not None:
        row["words"] = words
    return row


def _words(text, start, step=0.3, score=0.9):
    out, t = [], start
    for token in text.split():
        out.append({"word": token, "start": round(t, 2), "end": round(t + step - 0.05, 2), "score": score})
        t += step
    return out


def _ev(witness=None, gemini=None):
    hyps = []
    if witness:
        hyps.append({"role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
                     "transformation": "live_independent_verify_raw",
                     "events": [{k: w[k] for k in ("word", "start", "end")} for w in witness]})
    if gemini:
        hyps.append({"role": "primary", "family": "google/gemini-2.5-flash-audio", "kind": "text",
                     "transformation": "gemini_reference_hypothesis_raw", "events": [{"text": gemini}]})
    return {"hypotheses_by_family": hyps}


def _fixes(review):
    return {o["line_segment_id"]: (i["kind"], i["required"], o["after"])
            for i in review["items"] for o in i["occurrences"]}


def test_correcting_one_repetition_does_not_touch_the_other():
    text = "Te quiero, te quiero, mi amor"
    assert replace_words(text, "quiero,", "extraño,", at_word=3, scope="one") == "Te quiero, te extraño, mi amor"
    assert replace_words(text, "quiero,", "extraño,", scope="all") == "Te extraño, te extraño, mi amor"
    # Nunca reemplaza adentro de otra palabra.
    assert replace_words("Estuve pensando en vos", "tu", "tú") is None


def test_question_marks_only_touch_the_marked_clause():
    assert apply_punctuation("¿Cómo estás? ¿Que te pasa?", "accent_question", "¿Que te pasa?") \
        == "¿Cómo estás? ¿Qué te pasa?"
    segs = [_line("¿Que hora es?", 1, 2, "a"), _line("¿Qué mal te hice yo?", 3, 4, "b"),
            _line("¿Donde estabas anoche?", 5, 6, "c")]
    fixes = _fixes(build_review(segs, title=""))
    assert fixes["a"] == ("question_marks", False, "¿Qué hora es?")
    assert "b" not in fixes  # "¿Qué mal…?" es una pregunta real
    assert fixes["c"][2] == "Donde estabas anoche"


def test_unrelated_question_lines_are_separate_points():
    segs = [_line("¿Cuando vuelvas?", 1, 2, "a"), _line("¿Como te llamas?", 3, 4, "b")]
    review = build_review(segs, title="")
    assert len([i for i in review["items"] if i["kind"] == "question_marks"]) == 2


def test_legitimate_accent_pairs_are_not_flagged():
    segs = [_line("Estas noches sin vos", 1, 2, "a"), _line("¿Dónde estás?", 3, 4, "b"),
            _line("Hacia el mar", 5, 6, "c"), _line("Me hacía falta", 7, 8, "d"),
            _line("Una mujer seria", 9, 10, "e"), _line("Todo sería mejor", 11, 12, "f")]
    kinds = [i["kind"] for i in build_review(segs, title="")["items"]]
    assert "accent_inconsistent" not in kinds


def test_missing_phrase_keeps_every_word():
    # "ya" aparece en la línea siguiente: igual es parte de "dormite ya".
    machine = [_line("Cierra los ojos dormite ya", 10, 13, "a", _words("Cierra los ojos dormite ya", 10)),
               _line("Ya me voy de aquí", 14, 16, "b", _words("Ya me voy de aquí", 14))]
    screen = [_line("Cierra los ojos", 10, 13, "a"), _line("Ya me voy de aquí", 14, 16, "b")]
    witness = _words("Cierra los ojos dormite ya", 10) + _words("Ya me voy de aquí", 14)
    review = build_review(screen, original_segments=machine, machine_evidence=_ev(witness))
    missing = [i for i in review["items"] if i["kind"] == "missing"]
    assert len(missing) == 1
    assert missing[0]["occurrences"][0]["after"] == "Cierra los ojos dormite ya"


def test_decomposed_unicode_does_not_corrupt_fixes():
    nfd = unicodedata.normalize("NFD", "Mío en la noche")
    segs = [_line("Mío, mío", 1, 2, "a"), _line(unicodedata.normalize("NFD", "Mio en la noche"), 3, 4, "b"),
            _line(nfd, 5, 6, "c"), _line("Mío otra vez", 7, 8, "d")]
    fixes = _fixes(build_review(segs, title=""))
    assert fixes["b"][2] == "Mío en la noche"


def test_no_lines_does_not_crash():
    review = build_review([], original_segments=[_line("hola", 1, 2)])
    assert review["items"] == [] and review["required_count"] == 0


def test_repetitive_long_song_stays_fast():
    lines = [("La la la la la la la la" if i % 2 else "Oh oh oh oh oh eh oh oh", 5.0 + 3 * i) for i in range(400)]
    screen = [_line(t, s, s + 2.6, f"s{k}", _words(t, s)) for k, (t, s) in enumerate(lines)]
    witness = [w for t, s in lines for w in _words(t.replace("la", "na"), s)]
    gemini = " ".join(t for t, _ in lines)
    started = time.monotonic()
    build_review(screen, original_segments=screen, machine_evidence=_ev(witness, gemini),
                 official_text=gemini)
    assert time.monotonic() - started < 5.0


def test_official_lyrics_veto_sound_alike_ears():
    # "¡Y suéltala que ya camina!": Gemini y el testigo oyen "ella" (yeísmo),
    # la letra oficial confirma "ya": no se propone nada.
    lines = [("Y suéltala que ya camina compa Luis", 0.0), ("Te recuerdo más cada día", 4.0),
             ("Hace un año atrás y siempre tú", 8.0), ("Y pienso en ti mi amor", 12.0)]
    screen = [_line(t, s, s + 3.5, f"s{k}") for k, (t, s) in enumerate(lines)]
    heard = [(t.replace("que ya", "que ella"), s) for t, s in lines]
    witness = [w for t, s in heard for w in _words(t, s)]
    gemini = " ".join(t for t, _ in heard)
    official = "\n".join(t for t, _ in lines)
    review = build_review(screen, original_segments=screen, machine_evidence=_ev(witness, gemini),
                          official_text=official)
    assert [i for i in review["items"] if i["kind"] == "heard_different"] == []


def test_official_spelling_with_a_matching_ear_is_required():
    # "Urgo" en pantalla; la letra oficial y el testigo dicen "hurgo" (#123).
    lines = [("A veces en la noche de mal sueño", 0.0), ("Urgo en los placares encerrados", 3.0),
             ("Entre prendas y papeles olvidados", 6.0), ("Y descubro de pronto en mi guarida", 9.0)]
    screen = [_line(t, s, s + 2.8, f"s{k}") for k, (t, s) in enumerate(lines)]
    fixed = [(t.replace("Urgo", "Hurgo"), s) for t, s in lines]
    witness = [w for t, s in fixed for w in _words(t, s)]
    official = "\n".join(t for t, _ in fixed)
    fixes = _fixes(build_review(screen, original_segments=screen, machine_evidence=_ev(witness),
                                official_text=official))
    assert fixes["s1"] == ("heard_different", True, "Hurgo en los placares encerrados")


def test_proposal_keeps_mid_line_case():
    lines = [("Vos sos Dios", 0.0), ("Vos sos Gardel", 2.0), ("Vos sos Román", 4.0), ("Vos hablando y yo", 6.0)]
    screen = [_line(t, s, s + 1.8, f"s{k}") for k, (t, s) in enumerate(lines)]
    official = "\n".join(t.replace("Román", "lo más") for t, _ in lines)
    witness = [w for t, s in lines for w in _words(t.replace("Román", "lo más"), s)]
    fixes = _fixes(build_review(screen, original_segments=screen, machine_evidence=_ev(witness),
                                official_text=official))
    assert fixes["s2"][2] == "Vos sos lo más"


def test_layout_follows_the_official_line_breaks():
    lines = [("Cuando vuelvas quiero", 0.0), ("Verte a solas otra vez", 2.0),
             ("Y contarte todas estas cosas", 5.0), ("Que me pasan sin vos acá", 8.0)]
    screen = [_line(t, s, s + 1.9, f"s{k}") for k, (t, s) in enumerate(lines)]
    official = "Cuando vuelvas\nQuiero verte a solas otra vez\nY contarte todas estas cosas\nQue me pasan sin vos acá"
    review = build_review(screen, original_segments=screen, official_text=official)
    [item] = [i for i in review["items"] if i["kind"] == "layout_official"]
    assert item["occurrences"][0]["fix"]["lines"][0]["text"] == "Cuando vuelvas"
    assert item["occurrences"][0]["fix"]["lines"][1]["text"] == "Quiero verte a solas otra vez"


def test_line_that_leaves_before_the_last_word_suggests_a_longer_end():
    words = _words("Debo cuidarme de caer en el brasero", 60.0, step=0.4)
    screen = [_line("Debo cuidarme de caer en el brasero", 60.0, 61.8, "a"),
              _line("Otra línea", 66.0, 68.0, "b")]
    machine = [dict(screen[0], words=words), screen[1]]
    review = build_review(screen, original_segments=machine, machine_evidence=_ev(words))
    [item] = [i for i in review["items"] if i["kind"] == "timing"]
    assert item["required"] is False
    assert item["occurrences"][0]["fix"]["end"] > 62.5


def test_chorus_propagation_keeps_the_corrected_punctuation():
    original = [_line("no me dejes solo nunca", t, t + 2, f"c{t}") for t in (10, 40)]
    screen = [dict(original[0], text="¡No me dejes sola nunca!"), original[1]]
    [item] = [i for i in build_review(screen, original_segments=original, title="")["items"]
              if i["kind"] == "chorus_propagate"]
    assert item["occurrences"][0]["fix"] == {"type": "set_text", "text": "¡No me dejes sola nunca!",
                                             "expect": "no me dejes solo nunca"}


def test_correction_memory_never_crosses_tenants():
    from auth import create_user
    from database import EditorDocument, EditorVersion, Job, SessionLocal
    from editor import ensure_document

    db = SessionLocal()
    try:
        artist = f"Artista {uuid.uuid4().hex[:6]}"
        ids, users = {}, {}
        for tenant in ("tenant_a", "tenant_b"):
            users[tenant] = create_user(db, f"lr_{uuid.uuid4().hex[:6]}", "testpass12345", None,
                                        tenant_id=tenant).id
            job_id = f"lr_{uuid.uuid4().hex[:9]}"
            ids[tenant] = job_id
            db.add(Job(job_id=job_id, user_id=users[tenant], tenant_id=tenant, artist=artist,
                       song_title="Canción",
                       filename="x.wav", style="oscuro", status="done", current_step="done",
                       delivery_profile="youtube"))
            db.commit()
            ensure_document(db, job_id, tenant, [_line("Es un mambo de chuchu", 1, 2)])
            db.commit()
        db.add(EditorVersion(
            id=str(uuid.uuid4()), job_id=ids["tenant_b"], tenant_id="tenant_b", revision=9,
            segments=[_line("Es un mambo de Xuxú", 1, 2)], created_at=datetime.now(timezone.utc),
            reason="approve", is_approved=True,
        ))
        db.commit()
        _memory_cache.clear()
        probe_a = db.query(Job).filter(Job.job_id == ids["tenant_a"]).one()
        assert memory_pairs(db, probe_a) == {}
        other = f"lr_{uuid.uuid4().hex[:9]}"
        db.add(Job(job_id=other, user_id=users["tenant_b"], tenant_id="tenant_b", artist=artist,
                   song_title="Otra",
                   filename="y.wav", style="oscuro", status="done", current_step="done",
                   delivery_profile="youtube"))
        db.commit()
        _memory_cache.clear()
        probe_b = db.query(Job).filter(Job.job_id == other).one()
        assert memory_pairs(db, probe_b) == {"chuchu": "Xuxú"}
        assert db.query(EditorDocument).count() >= 2
    finally:
        db.close()
