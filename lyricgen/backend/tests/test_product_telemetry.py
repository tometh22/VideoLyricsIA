import math

from product_telemetry import valid_property


def test_numeric_metrics_cannot_store_lyrics_or_poison_percentiles():
    assert valid_property("text_changes", 3)
    assert not valid_property("text_changes", "Hoy temprano estuve pensando en vos")
    assert not valid_property("duration_ms", -1)
    assert not valid_property("duration_ms", math.nan)
    assert not valid_property("duration_ms", 100_000_000)
    assert not valid_property("active_edit_ms", 14_400_001)
    assert not valid_property("text_changes", 10_001)
    assert not valid_property("status", 600)


def test_session_and_boolean_types_are_strict():
    assert valid_property("session_id", "019abcde-1234-4567-8901-abcdefabcdef")
    assert not valid_property("session_id", "lyric text with spaces")
    assert not valid_property("session_id", "subtitulos-realizados")
    assert valid_property("session_id", "editor-1786674137720-abc123")
    assert valid_property("quality_acknowledged", True)
    assert not valid_property("quality_acknowledged", 1)
    assert valid_property("automatic_recovery_available", True)
    assert not valid_property("automatic_recovery_available", 1)
    assert valid_property("media_error_code", 0)
    assert valid_property("media_error_code", 4)
    assert not valid_property("media_error_code", 5)


def test_categories_are_enums_or_short_machine_codes_only():
    assert valid_property("view", "advanced")
    assert not valid_property("view", "Hoy temprano estuve pensando")
    assert valid_property("reason", "stale_revision")
    assert not valid_property("reason", "letra libre con espacios")
    assert not valid_property("unknown", "anything")


# ---------------------------------------------------------------------------
# Checkpoints de autosave — el enum descartaba eventos enteros
# ---------------------------------------------------------------------------

def test_acepta_los_tres_checkpoints_que_emite_el_cliente():
    """El editor emite draft (800 ms), autosave (5 s) y manual (flush/aprobar).

    El enum sólo tenía {"draft"}, así que `valid_property` fallaba para los
    otros dos y el evento COMPLETO se rechazaba: `autosave_failures` medía un
    subconjunto arbitrario y `avg_time_to_first_edit_ms` (que depende de
    editor_autosave_success) quedaba sesgado.
    """
    for checkpoint in ("draft", "autosave", "manual"):
        assert valid_property("checkpoint", checkpoint) is True, checkpoint


def test_sigue_rechazando_un_checkpoint_inventado():
    assert valid_property("checkpoint", "cualquier-cosa") is False


def test_retry_count_es_una_propiedad_valida():
    # El contador de fallos ahora viaja con el número de reintento para poder
    # distinguir un primer fallo de una racha del backoff.
    assert valid_property("retry_count", 0) is True
    assert valid_property("retry_count", 7) is True


# ---------------------------------------------------------------------------
# Allowlist POR EVENTO — el otro filtro, que los tests no cubrían
# ---------------------------------------------------------------------------

def test_editor_conflict_acepta_lo_que_el_cliente_realmente_manda():
    """`/analytics/events` rechaza el evento ENTERO al primer key desconocido.

    El emisor (handleDurableStatus) manda {checkpoint, reason}; el allowlist
    histórico sólo tenía {server_revision, local_revision, resolution}, así que
    el evento se descartaba al 100% y el contador quedaba clavado en 0 — con el
    CI en verde, porque los tests sólo ejercitaban `valid_property`.
    """
    import main
    allowed = main._PRODUCT_EVENT_PROPERTIES["editor_conflict"]
    for key in ("checkpoint", "reason"):
        assert key in allowed, f"el cliente manda {key} y el backend lo rechaza"


def test_todo_lo_que_emite_autosave_esta_en_su_allowlist():
    import main
    for event, keys in (
        ("editor_autosave_failed", {"checkpoint", "reason", "retry_count"}),
        ("editor_autosave_success", {"duration_ms", "checkpoint", "retry_count"}),
    ):
        assert keys <= main._PRODUCT_EVENT_PROPERTIES[event], event


def test_audio_playback_failure_acepta_lo_que_emite_el_cliente():
    import main
    event = "editor_audio_playback_failed"
    assert event in main._PRODUCT_EVENT_NAMES
    assert {
        "position_ms", "media_error_code", "automatic_recovery_available",
    } <= main._PRODUCT_EVENT_PROPERTIES[event]


def test_auto_repair_undo_telemetry_acepta_la_revision_restaurada():
    import main
    event = "editor_auto_repair_undone"
    assert event in main._PRODUCT_EVENT_NAMES
    assert {"to_revision"} <= main._PRODUCT_EVENT_PROPERTIES[event]
    assert valid_property("to_revision", 1)


def test_line_structure_event_acepta_lo_que_emite_el_editor():
    """`editor_line_structure_changed` mide partir/unir: sólo contadores y enums."""
    import main
    event = "editor_line_structure_changed"
    assert event in main._PRODUCT_EVENT_NAMES
    sample = {
        "structure_op": "split", "trigger": "caret", "count": 1, "words_count": 1,
        "stale_words_count": 0, "one_side_count": 0, "no_words_count": 0, "duration_ms": 3200,
    }
    assert set(sample) <= main._PRODUCT_EVENT_PROPERTIES[event]
    for key, value in sample.items():
        assert valid_property(key, value), key
    for trigger in ("caret", "button", "bulk", "wrap_dialog", "reference", "backspace_empty"):
        assert valid_property("trigger", trigger), trigger
    assert valid_property("structure_op", "merge")
    assert not valid_property("structure_op", "hoy te vi pasar")
    assert not valid_property("trigger", "letra libre")
    assert not valid_property("words_count", "hoy te vi pasar")
    assert not valid_property("no_words_count", -1)


# ---------------------------------------------------------------------------
# editor_audio_played — tramos reproducidos, sólo números
# ---------------------------------------------------------------------------

def test_audio_played_acepta_lo_que_emite_el_editor():
    import main
    event = "editor_audio_played"
    assert event in main._PRODUCT_EVENT_NAMES
    sample = {
        "ranges": [[0, 12_000], [60_000, 64_250]],
        "played_ms": 16_250,
        "playback_rate": 1,
        "audio_duration_ms": 215_400,
        "flush_reason": "interval",
    }
    assert set(sample) <= main._PRODUCT_EVENT_PROPERTIES[event]
    for key, value in sample.items():
        assert valid_property(key, value), key
    for reason in ("interval", "pause", "ended", "full", "approve", "hidden", "unmount"):
        assert valid_property("flush_reason", reason), reason
    assert valid_property("playback_rate", 0.75)
    assert not valid_property("playback_rate", 0)
    assert not valid_property("playback_rate", 32)
    assert not valid_property("playback_rate", True)
    assert not valid_property("flush_reason", "letra libre")


def test_rangos_reproducidos_validador_dedicado():
    from product_telemetry import MAX_PLAYED_RANGES, valid_played_ranges

    assert valid_played_ranges([[0, 1]])
    assert valid_played_ranges([[0, 86_400_000]])
    assert valid_played_ranges([[i * 10, i * 10 + 5] for i in range(MAX_PLAYED_RANGES)])
    # Acotado: ni vacío ni más de 64 pares.
    assert not valid_played_ranges([])
    assert not valid_played_ranges([[i * 10, i * 10 + 5] for i in range(MAX_PLAYED_RANGES + 1)])
    # Enteros dentro del audio, inicio < fin.
    assert not valid_played_ranges([[5, 5]])
    assert not valid_played_ranges([[6, 5]])
    assert not valid_played_ranges([[-1, 5]])
    assert not valid_played_ranges([[0, 86_400_001]])
    assert not valid_played_ranges([[0.5, 5]])
    assert not valid_played_ranges([[True, 5]])
    assert not valid_played_ranges([[0, math.inf]])
    # Forma: pares, nada de texto ni dicts.
    assert not valid_played_ranges([[0, 1, 2]])
    assert not valid_played_ranges([[0]])
    assert not valid_played_ranges([["0", "1"]])
    assert not valid_played_ranges([{"start": 0, "end": 1}])
    assert not valid_played_ranges("hoy te vi pasar")
    assert not valid_played_ranges({"ranges": [[0, 1]]})
    # valid_property enruta la clave al validador dedicado.
    assert valid_property("ranges", [[0, 1000]])
    assert not valid_property("ranges", [[0, "hoy te vi pasar"]])


def test_64_rangos_maximos_entran_en_el_tope_de_2000_caracteres():
    import json

    from product_telemetry import MAX_MEDIA_MS, MAX_PLAYED_RANGES

    properties = {
        "ranges": [[MAX_MEDIA_MS - 1, MAX_MEDIA_MS]] * MAX_PLAYED_RANGES,
        "played_ms": MAX_MEDIA_MS, "playback_rate": 0.0625,
        "audio_duration_ms": MAX_MEDIA_MS, "flush_reason": "interval",
        "session_id": "019abcde-1234-4567-8901-abcdefabcdef",
    }
    assert len(json.dumps(properties, ensure_ascii=False)) <= 2000


# ---------------------------------------------------------------------------
# occurred_at — el cliente nunca lo mandaba y quedaba NULL
# ---------------------------------------------------------------------------

def test_occurred_at_usa_la_hora_del_cliente_si_es_plausible():
    from datetime import datetime, timedelta, timezone

    from product_telemetry import resolve_occurred_at

    received = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
    assert resolve_occurred_at("2026-10-08T14:59:58.250Z", received) == (
        received - timedelta(seconds=1, milliseconds=750)
    )
    # Sin zona horaria se interpreta UTC.
    assert resolve_occurred_at("2026-10-08T14:59:00", received) == received - timedelta(minutes=1)


def test_occurred_at_cae_a_la_hora_de_llegada():
    from datetime import datetime, timedelta, timezone

    from product_telemetry import resolve_occurred_at

    received = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
    assert resolve_occurred_at(None, received) == received
    assert resolve_occurred_at("", received) == received
    assert resolve_occurred_at("ayer a la tarde", received) == received
    # Un evento no ocurre después de llegar ni días antes: reloj corrido.
    assert resolve_occurred_at((received + timedelta(seconds=3)).isoformat(), received) == received
    assert resolve_occurred_at((received - timedelta(days=2)).isoformat(), received) == received
