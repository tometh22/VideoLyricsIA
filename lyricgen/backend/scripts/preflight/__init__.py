"""Preflight test suite — pre-launch verification for production readiness.

Each `check_*.py` module exports a `Check` subclass that performs one focused
verification (e.g. UMG master codec, volume caps, validator quality) and
returns a structured `CheckResult`.

Run from the backend dir:

    python3 -m scripts.preflight.run [--only NAME] [--skip NAME] [--json]

Reports land in scripts/preflight/reports/ (gitignored).
"""
