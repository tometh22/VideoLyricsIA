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
