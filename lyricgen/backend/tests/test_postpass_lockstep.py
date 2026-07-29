"""Lockstep de los post-pases entre los 3 caminos de transcripción.

LA CLASE DE BUG QUE ESTE TEST MATA (incidente 05/07): el filtro de ad-libs
estaba en los dos endpoints HTTP pero NO en el worker — y el worker es el
camino que usa el frontend real, así que los 'uh' salían fragmentados en
prod mientras los tests de endpoint pasaban verdes.

Regla verificada por AST: toda función que llame `_maybe_adlib_filter`
debe llamar también `_maybe_repetition_reconcile` y `_maybe_phrase_segment`,
DESPUÉS del filtro de ad-libs y ANTES del formatter (`_fmt`). Si alguien
agrega un 4º camino de transcripción, este test lo obliga a incluir la
cadena completa de post-pases.
"""
import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parent.parent

REQUIRED_AFTER_ADLIB = ("_maybe_repetition_reconcile", "_maybe_gap_rescue",
                        "_maybe_phrase_segment")


def _call_sites():
    """(archivo, nombre_de_funcion, {llamada: primera_linea}) para toda
    función que invoque _maybe_adlib_filter."""
    sites = []
    for fname in ("main.py", "transcription_worker.py"):
        tree = ast.parse((BACKEND / fname).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = {}
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    name = (getattr(n.func, "id", None)
                            or getattr(n.func, "attr", None))
                    if name and name not in calls:
                        calls[name] = n.lineno
            if "_maybe_adlib_filter" in calls and node.name != "_maybe_adlib_filter":
                sites.append((fname, node.name, calls))
    return sites


def test_existen_los_tres_caminos():
    sites = _call_sites()
    files = sorted({f for f, _, _ in sites})
    assert "transcription_worker.py" in files, \
        "el camino del worker (el que usa el frontend real) desapareció"
    assert "main.py" in files
    assert len(sites) >= 3, \
        f"se esperaban >=3 caminos de transcripción, hay {len(sites)}: {sites}"


def test_todo_camino_corre_los_postpases_nuevos_en_orden():
    for fname, func, calls in _call_sites():
        for req in REQUIRED_AFTER_ADLIB:
            assert req in calls, (
                f"{fname}:{func} llama _maybe_adlib_filter pero NO {req} — "
                f"un camino sin el post-pass es el bug del 05/07 de nuevo")
            assert calls[req] > calls["_maybe_adlib_filter"], (
                f"{fname}:{func}: {req} debe correr DESPUÉS del filtro de "
                f"ad-libs (necesita la membresía final de los grupos)")
        assert calls["_maybe_phrase_segment"] > calls["_maybe_repetition_reconcile"], (
            f"{fname}:{func}: el segmentador debe correr después del "
            f"reconciliador (parte líneas; el reconciliador las necesita enteras)")
        assert calls["_maybe_phrase_segment"] > calls["_maybe_gap_rescue"], (
            f"{fname}:{func}: el segmentador debe correr después del rescate "
            f"(las líneas rescatadas también se cortan en frases)")
        if "_fmt" in calls:
            for req in REQUIRED_AFTER_ADLIB:
                assert calls[req] < calls["_fmt"], (
                    f"{fname}:{func}: {req} debe correr ANTES del formatter "
                    f"(el formatter debe ver los carteles finales)")
