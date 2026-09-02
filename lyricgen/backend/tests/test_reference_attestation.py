from reference_attestation import (
    assess_reference_attestation,
    reference_gate_action,
)


def _segments(text, end=95):
    return [{"start": 5, "end": end, "text": text}]


def test_correct_catalogue_is_attested_despite_small_asr_mishears():
    reference = (
        "Voy caminando por la noche buscando aquella estrella "
        "Legalícenla para poder cantar esta canción"
    )
    asr = _segments(
        "Voy caminando por la noche buscando aquella estrella "
        "Le realicen la para poder cantar esta cancion"
    )
    result = assess_reference_attestation(
        reference, asr, audio_duration_s=100,
    )
    assert result["allow_vocabulary_reconciliation"] is True
    assert result["allow_global_forced_alignment"] is True


def test_wrong_song_reference_is_fail_closed():
    result = assess_reference_attestation(
        "Bailando bajo la lluvia con un corazón enamorado",
        _segments("Todos estos años de gente historias en la ciudad"),
        audio_duration_s=100,
    )
    assert result["text_status"] == "unsafe_without_witness"
    assert result["allow_vocabulary_reconciliation"] is False
    assert result["allow_global_forced_alignment"] is False


def test_partial_or_live_reference_never_authorizes_global_alignment():
    partial = assess_reference_attestation(
        "Este es el único estribillo",
        _segments(
            "Primera estrofa completamente distinta este es el único estribillo "
            "y luego continúa otra estrofa que falta"
        ),
        audio_duration_s=100,
    )
    assert partial["allow_global_forced_alignment"] is False

    live = assess_reference_attestation(
        "Cantamos juntos esta canción y repetimos todo el estribillo",
        _segments("Cantamos juntos esta cancion y repetimos todo el estribillo"),
        audio_duration_s=100, is_live=True,
    )
    assert live["allow_vocabulary_reconciliation"] is True
    assert live["allow_global_forced_alignment"] is False
    assert "live_structure_requires_local_alignment" in live["reasons"]


def test_trusted_human_reference_allows_local_text_but_incomplete_asr_blocks_global():
    result = assess_reference_attestation(
        "Línea humana aprobada que todavía falta al final de la canción",
        _segments("Línea humana aprobada", end=20),
        reference_source="human_verified", audio_duration_s=100,
    )
    assert result["allow_vocabulary_reconciliation"] is True
    assert result["allow_global_forced_alignment"] is False
    assert "asr_timeline_incomplete" in result["reasons"]


def test_enforce_rejects_unattested_or_incomplete_studio_reference():
    unsafe = assess_reference_attestation(
        "Una canción totalmente equivocada",
        _segments("Otra historia que no se parece en nada"),
        audio_duration_s=100,
    )
    assert reference_gate_action(
        unsafe, mode="enforce", is_live=False,
    ) == "audio_first"

    incomplete = assess_reference_attestation(
        "Esta letra humana correcta continúa durante toda la canción",
        _segments("Esta letra humana correcta", end=15),
        reference_source="human_verified", audio_duration_s=100,
    )
    assert reference_gate_action(
        incomplete, mode="enforce", is_live=False,
    ) == "audio_first"


def test_live_reference_can_only_be_used_as_local_vocabulary():
    result = assess_reference_attestation(
        "Cantamos juntos esta canción en el escenario",
        _segments("Cantamos juntos esta cancion en el escenario"),
        audio_duration_s=100,
        is_live=True,
    )
    assert reference_gate_action(
        result, mode="enforce", is_live=True,
    ) == "local_only"
    assert reference_gate_action(
        result, mode="observe", is_live=True,
    ) == "observe"


def test_attestation_never_emits_plain_sha256_identities(monkeypatch):
    monkeypatch.delenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY", raising=False)
    monkeypatch.delenv("QUALITY_CONTENT_ATTESTATION_KEY", raising=False)
    monkeypatch.delenv("QUALITY_LEARNING_HMAC_KEY", raising=False)
    result = assess_reference_attestation(
        "Texto protegido de catálogo",
        _segments("Texto protegido de catalogo"),
    )
    assert result["identities"] == {
        "reference_fingerprint": None,
        "asr_fingerprint": None,
    }
