"""Los tres puntos de entrada de transcripción corren los MISMOS post-pases.

Bug 05/07: el filtro de ad-libs (_maybe_adlib_filter) estaba cableado en los
dos endpoints HTTP (/transcribe, /transcribe-uploaded) pero NO en el worker
encolado (transcription_worker.run_transcription_job) — que es el camino que
usa el frontend real (enqueue → ShortWorker). Resultado: en producción los
'uh' salían fragmentados y los fantasmas sin filtrar, aunque los endpoints
sí lo aplicaban.

Guard AST: el cuerpo de run_transcription_job debe llamar a los mismos
wrappers post-cascada que los endpoints. Si alguien agrega un post-pase a un
endpoint y se olvida del worker (o viceversa), este test falla.
"""
import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent

# Wrappers post-cascada que TODOS los caminos deben aplicar, en orden.
_REQUIRED_POST_PASSES = ["_maybe_ctc_retime", "_maybe_adlib_filter"]


def _calls_in(source: str, func_name: str) -> list[str]:
    """Nombres de funciones llamadas dentro de `func_name` (recursivo)."""
    tree = ast.parse(source)
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == func_name), None)
    assert target is not None, f"no se encontró {func_name}"
    calls = []
    for node in ast.walk(target):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.append(node.func.id)
    return calls


def test_worker_applies_all_post_passes():
    src = (_BACKEND / "transcription_worker.py").read_text()
    # run_transcription_job define una coroutine interna (_run_with_retime);
    # buscamos las llamadas en TODO el módulo para no depender del nombre.
    module_calls = [
        node.func.id
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    for wrapper in _REQUIRED_POST_PASSES:
        assert wrapper in module_calls, (
            f"el worker de transcripción NO llama a {wrapper} — el camino "
            f"encolado (frontend real) se saltea ese post-pase")


def test_endpoints_apply_all_post_passes():
    src = (_BACKEND / "main.py").read_text()
    calls = [
        node.func.id
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    for wrapper in _REQUIRED_POST_PASSES:
        assert calls.count(wrapper) >= 2, (
            f"{wrapper} debería llamarse en los 2 endpoints HTTP "
            f"(/transcribe y /transcribe-uploaded)")
