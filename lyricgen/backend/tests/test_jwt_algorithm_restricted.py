"""Premisas de seguridad del JWT de la aplicación.

1. Toda llamada a ``jwt.decode`` restringe ``algorithms`` a la configurada.
2. La configurada es HMAC con secreto simétrico, sin claves públicas.

Nacieron con la excepción de CVE-2026-85394 (python-jose); siguen valiendo
con PyJWT: un decode sin ``algorithms`` es la puerta a la confusión de
algoritmos con cualquier librería.
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


# --- Compatibilidad python-jose → PyJWT (9-oct-2026) ---------------------------

# Token emitido por python-jose 3.5.0 con la forma de un access token real
# (exp/iat con decimales, como los emite `create_access_token`).
_JOSE_SECRET = "test-secret-for-jose-compat-0123456789"
_JOSE_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0MiIsImp0aSI6ImFiYzEyMyIsInR0IjoiYWNjZXNz"
    "IiwiYXYiOjAsImlhdCI6MTc5MTUwMDAwMC4yNSwiZXhwIjo0MTAyNDQ0ODAwLjV9.oI-40Bxo93mT7g3pvJkcVG6BeZxFI4jOiHLChqxU56A"
)


def test_un_token_emitido_por_python_jose_sigue_valiendo(monkeypatch):
    """Las sesiones abiertas antes del cambio no se cortan."""
    import auth

    monkeypatch.setattr(auth, "JWT_SECRET", _JOSE_SECRET)
    payload = auth.decode_token(_JOSE_TOKEN)
    assert payload["sub"] == "42" and payload["jti"] == "abc123"


def test_iat_en_el_futuro_se_acepta_como_antes(monkeypatch):
    """Diferencias de reloj entre réplicas no cortan sesiones recién emitidas."""
    import time

    import jwt
    import auth

    monkeypatch.setattr(auth, "JWT_SECRET", _JOSE_SECRET)
    token = jwt.encode({"sub": "1", "iat": time.time() + 5, "exp": time.time() + 60},
                       _JOSE_SECRET, algorithm="HS256")
    assert auth.decode_token(token)["sub"] == "1"


def test_expirado_none_y_otro_algoritmo_se_rechazan(monkeypatch):
    import time

    import jwt
    import pytest
    from fastapi import HTTPException
    import auth

    monkeypatch.setattr(auth, "JWT_SECRET", _JOSE_SECRET)
    expired = jwt.encode({"sub": "1", "exp": time.time() - 5}, _JOSE_SECRET, algorithm="HS256")
    unsigned = jwt.encode({"sub": "1", "exp": time.time() + 60}, None, algorithm="none")
    other = jwt.encode({"sub": "1", "exp": time.time() + 60}, _JOSE_SECRET, algorithm="HS512")
    for token in (expired, unsigned, other):
        with pytest.raises(HTTPException) as exc:
            auth.decode_token(token)
        assert exc.value.status_code == 401
