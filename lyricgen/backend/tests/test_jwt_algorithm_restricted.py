"""La excepción de CVE-2026-85394 (python-jose) depende de dos premisas.

1. Toda llamada a ``jwt.decode`` restringe ``algorithms`` a la configurada.
2. La configurada es HMAC con secreto simétrico, sin claves públicas.

La vulnerabilidad sólo se explota si ``algorithms`` no se restringe. Si
alguna de las dos premisas cambia, la excepción deja de valer y este test
tiene que romper.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _decode_calls():
    for path in sorted(BACKEND.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "decode"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"jwt", "_jwt"}
            ):
                yield path.name, node


def test_every_jwt_decode_restricts_algorithms():
    calls = list(_decode_calls())
    assert calls, "no se encontró ningún jwt.decode: revisar el test"
    unrestricted = [
        f"{name}:{node.lineno}"
        for name, node in calls
        if not any(kw.arg == "algorithms" and isinstance(kw.value, ast.List) for kw in node.keywords)
    ]
    assert unrestricted == []


def test_default_jwt_algorithm_is_hmac():
    import auth

    assert auth.JWT_ALGORITHM.startswith("HS")
