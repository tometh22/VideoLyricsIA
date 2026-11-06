#!/usr/bin/env python3
"""Fail when the services of one Railway environment disagree on the pipeline config.

Every service computes a runtime token from the pipeline/calibration environment
(`transcription_quality._PIPELINE_CONFIG_KEYS` plus the calibration keys). A quality job
enqueued by one service is silently discarded by another whose token differs
(`runtime_identity_mismatch`), and /health reports `fleet_runtime_token_mismatch`.
Run this before a promotion and after touching variables:

    PYTHONPATH=lyricgen/backend python lyricgen/backend/scripts/check_fleet_config_parity.py production
    PYTHONPATH=lyricgen/backend python lyricgen/backend/scripts/check_fleet_config_parity.py staging api Worker

Exit code 0 = identical, 1 = drift (the differing keys are listed), 2 = could not read variables.
Values are only printed for non-secret-looking keys.
"""
from __future__ import annotations

import subprocess
import sys

DEFAULT_SERVICES = ("api", "Worker", "ShortWorker", "quality-worker")
CALIBRATION_KEYS = (
    "RELEASE",
    "TRANSCRIPTION_QUALITY_CALIBRATION_ID",
    "TRANSCRIPTION_QUALITY_CALIBRATION_POLICY",
    "TRANSCRIPTION_QUALITY_CALIBRATION_CONFIG_FINGERPRINT",
    "TRANSCRIPTION_QUALITY_CALIBRATED",
    "TRANSCRIPTION_QUALITY_RELEASE_REPORT_PATH",
    "TRANSCRIPTION_QUALITY_RELEASE_REPORT_SHA256",
    "TRANSCRIPTION_QUALITY_BENCHMARK_MANIFEST_PATH",
)
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "SHA256")


def config_keys() -> tuple[str, ...]:
    from transcription_quality import _PIPELINE_CONFIG_KEYS
    return tuple(dict.fromkeys((*_PIPELINE_CONFIG_KEYS, *CALIBRATION_KEYS)))


def diff_services(envs: dict[str, dict[str, str]], keys) -> dict[str, dict[str, str]]:
    """Keys whose (possibly empty) value is not the same in every service."""
    drift = {}
    for key in keys:
        values = {service: envs[service].get(key, "") for service in envs}
        if len(set(values.values())) > 1:
            drift[key] = values
    return drift


def load_variables(service: str, environment: str) -> dict[str, str]:
    out = subprocess.run(
        ["railway", "variables", "--service", service, "--environment", environment, "--kv"],
        capture_output=True, text=True, check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def render(drift: dict[str, dict[str, str]]) -> str:
    lines = []
    for key, values in sorted(drift.items()):
        hidden = any(hint in key for hint in _SECRET_HINTS)
        cells = ", ".join(
            f"{service}={'<oculto>' if hidden and value else (value or '∅')}"
            for service, value in values.items()
        )
        lines.append(f"  {key}: {cells}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    environment = argv[1] if len(argv) > 1 else "production"
    services = tuple(argv[2:]) or DEFAULT_SERVICES
    try:
        envs = {service: load_variables(service, environment) for service in services}
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"No se pudieron leer las variables de Railway: {exc}", file=sys.stderr)
        return 2
    drift = diff_services(envs, config_keys())
    if not drift:
        print(f"OK: {len(config_keys())} claves idénticas en {', '.join(services)} ({environment}).")
        return 0
    print(f"DERIVA en {environment}: {len(drift)} clave(s) distintas entre {', '.join(services)}:")
    print(render(drift))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
