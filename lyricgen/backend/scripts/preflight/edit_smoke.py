"""Post-deploy edit smoke — GO/NO-GO del golden path upload→generate→edit.

Motivación (incidente UMG 2026-07-09): el fix #853 deployó una regresión
(ffmpeg exit 234) que rompía TODOS los edits de WAVs con metadata acentuada.
Estuvo en Sentry a los minutos, pero recién se investigó 8 horas después,
cuando un cliente escribió por WhatsApp. Este smoke ejecuta ese camino
exacto tras cada deploy y falla el workflow si se rompió. En su primera
corrida real (2026-07-10, staging) atrapó un consumidor stale de la flota
de workers (TypeError frame_format) — exactamente la clase de problema que
existe para cazar.

  1. Craftea un WAV PCM de 2 s con tags INFO en LATIN-1 ("Estrechez de
     Corazón", byte 0xf3) — el disparador real del UnicodeDecodeError que
     activó el 234.
  2. Lo sube directo a R2 con el flujo vigente (/upload-url), lo transcribe
     (/transcribe-uploaded), guarda una corrección de timing pre-aprobación,
     genera el video y espera pending_review.
  3. Pide un edit de metadata (/edit) — recorre run_edit_pipeline: la
     apertura moviepy del source_audio, el fallback UTF-8, el re-render y
     el re-upload de deliverables.
  4. GO si el edit vuelve a pending_review; NO-GO (exit 1) si algo erró.

Costo por corrida: un render corto (audio de 2 s) + 1 background Veo Fast
(~USD 0.10-0.80) + un re-render sin Veo. Corre tras cada deploy a staging;
contra prod solo por workflow_dispatch explícito.

Uso:
    PREFLIGHT_USERNAME=... PREFLIGHT_PASSWORD=... \
    python3 -m scripts.preflight.edit_smoke --api-url https://api-staging-9b82.up.railway.app
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

import requests

# Tags LATIN-1 (no UTF-8): ó=0xf3, é=0xe9 — el shape exacto del incidente.
_TITLE = "Estrechez de Corazón (smoke)"
_ARTIST = "Los Prisioneros - smoke"  # separador ASCII: el tag va en latin-1


def _accented_wav_bytes() -> bytes:
    """Voiced WAV fixture + LIST/INFO metadata encoded as latin-1."""
    fixture = (
        Path(__file__).with_name("fixtures")
        / "voiced_smoke.wav.zlib.b64"
    )
    data = bytearray(zlib.decompress(base64.b64decode(fixture.read_bytes())))
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise RuntimeError("voiced smoke fixture is not a RIFF/WAVE file")

    def sub(cid: bytes, text: str) -> bytes:
        b = text.encode("latin-1") + b"\x00"
        if len(b) % 2:
            b += b"\x00"
        return cid + struct.pack("<I", len(b)) + b

    info = b"INFO" + sub(b"INAM", _TITLE) + sub(b"IART", _ARTIST)
    out = bytes(data) + b"LIST" + struct.pack("<I", len(info)) + info
    return out[:4] + struct.pack("<I", len(out) - 8) + out[8:]


def _fail(msg: str) -> int:
    print(f"[edit-smoke] NO-GO: {msg}", file=sys.stderr)
    return 1


def _timing_only_edit(segments: list[dict]) -> list[dict]:
    """Move one real machine line while preserving every other line."""
    if not segments or not all(isinstance(row, dict) for row in segments):
        raise ValueError("la transcripción no produjo líneas de máquina")
    first = segments[0]
    if not str(first.get("text") or "").strip():
        raise ValueError("la primera línea de máquina no contiene texto")
    try:
        start = float(first["start"])
        end = float(first["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("la primera línea no tiene timing válido") from exc
    shifted_start = round(start + 0.1, 4)
    if shifted_start >= end:
        shifted_start = round(max(0.0, start - 0.1), 4)
    if shifted_start == start or shifted_start >= end:
        raise ValueError("la primera línea es demasiado corta para el delta")

    edited = [dict(row) for row in segments]
    edited[0]["start"] = shifted_start
    return edited


_QUALITY_GATE_CODES = {
    "transcription_quality_unavailable",
    "transcription_quality_analysis_incomplete",
    "transcription_quality_review_required",
}


def _quality_gate_code(response) -> str | None:
    """Return a fail-closed quality code without trusting free-form text."""
    if getattr(response, "status_code", None) != 409:
        return None
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    code = str(payload.get("code") or "")
    return code if code in _QUALITY_GATE_CODES else None


def _status_quality_gate_code(status_payload: dict) -> str | None:
    error = str(status_payload.get("error") or "")
    return next((code for code in _QUALITY_GATE_CODES if code in error), None)


def _quality_gate_go(job_id: str, phase: str, code: str) -> int:
    print(
        f"[edit-smoke] GO ✅ — job {job_id}: gate v6 bloqueó el fixture "
        f"acústicamente inválido en {phase} ({code}); "
        "el render/edit completo se conserva para entornos sin enforcement."
    )
    return 0


def _delivery_qc_contract_error(status_payload: dict) -> str | None:
    """Validate that /status exposes a fresh report for the current render."""
    report = status_payload.get("delivery_qc")
    if not isinstance(report, dict):
        return "delivery_qc ausente en /status"
    if report.get("status") != "COMPLETE":
        return f"delivery_qc no está fresco (status={report.get('status')})"
    if report.get("mode") not in {"observe", "enforce"}:
        return f"delivery_qc mode inválido: {report.get('mode')}"
    if not report.get("generated_at") or not report.get("segments_hash"):
        return "delivery_qc no tiene identidad temporal/de segmentos"
    expected_revision = int(status_payload.get("segments_revision") or 0)
    if int(report.get("segments_revision") or 0) != expected_revision:
        return (
            "delivery_qc corresponde a otra revisión "
            f"({report.get('segments_revision')} != {expected_revision})"
        )
    identity = report.get("render_identity") or {}
    expected_edit_count = int(status_payload.get("edit_count") or 0)
    if int(identity.get("edit_count") or 0) != expected_edit_count:
        return (
            "delivery_qc corresponde a otro render/edit "
            f"({identity.get('edit_count')} != {expected_edit_count})"
        )
    technical = report.get("technical") or {}
    video = technical.get("video") or {}
    if not video.get("codec") or int(technical.get("audio_streams") or 0) < 1:
        return "delivery_qc no certificó streams de audio/video"
    approval = report.get("approval") or {}
    if report.get("mode") == "observe" and approval.get("blocked") is not False:
        return "delivery_qc observe bloqueó la aprobación"
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api-url", required=True)
    p.add_argument("--render-timeout", type=int, default=900,
                   help="segundos máximos para cada fase de render")
    p.add_argument(
        "--allow-quality-gate-block", action="store_true",
        help=(
            "acepta un 409 fail-closed del gate v6 para este fixture de silencio; "
            "staging lo usa con enforcement, producción conserva el smoke completo"
        ),
    )
    p.add_argument(
        "--require-delivery-qc", action="store_true",
        help=(
            "exige que cada render publique en /status un preflight fresco, "
            "ligado a la revisión y al edit_count actuales"
        ),
    )
    args = p.parse_args()
    api = args.api_url.rstrip("/")

    user = os.environ.get("PREFLIGHT_USERNAME")
    pw = os.environ.get("PREFLIGHT_PASSWORD")
    if not (user and pw):
        return _fail("PREFLIGHT_USERNAME / PREFLIGHT_PASSWORD sin setear")

    # 1. Login
    r = requests.post(f"{api}/auth/login",
                      json={"username": user, "password": pw}, timeout=20)
    if not r.ok:
        return _fail(f"login {r.status_code}: {r.text[:200]}")
    headers = {"Authorization": f"Bearer {r.json()['token']}"}

    # 2. Upload vigente: API crea el job + firma una URL, luego el WAV viaja
    # directo a R2. El endpoint multipart legado /upload fue retirado el
    # 2026-08-01 y además sostenía una sesión DB durante todo el I/O a R2,
    # por lo que dejó de representar el camino real del frontend.
    wav = _accented_wav_bytes()
    r = requests.post(
        f"{api}/upload-url", headers=headers,
        json={
            "filename": "estrechez_smoke.wav",
            "content_type": "audio/wav",
            "size_bytes": len(wav),
            "artist": _ARTIST,
            "title": _TITLE,
        },
        timeout=30,
    )
    if not r.ok:
        return _fail(f"/upload-url {r.status_code}: {r.text[:300]}")
    ticket = r.json()
    if ticket.get("use_multipart") or not ticket.get("upload_url"):
        return _fail("/upload-url devolvió multipart para el WAV mínimo")
    job_id = ticket["job_id"]

    r = requests.put(
        ticket["upload_url"], data=wav,
        headers={"Content-Type": "audio/wav"}, timeout=120,
    )
    if not r.ok:
        return _fail(f"R2 PUT {r.status_code}: {r.text[:300]}")

    r = requests.post(
        f"{api}/transcribe-uploaded",
        headers={
            **headers,
            "Idempotency-Key": f"edit-smoke-transcribe-{job_id}",
        },
        json={
            "job_id": job_id,
            "language": "es",
            "artist": _ARTIST,
            "title": _TITLE,
        },
        timeout=15,
    )
    if not r.ok:
        return _fail(f"/transcribe-uploaded {r.status_code}: {r.text[:300]}")
    if r.status_code != 202 or not r.headers.get("Location"):
        return _fail(
            "/transcribe-uploaded no devolvió el contrato 202+Location: "
            f"{r.status_code} {r.text[:300]}"
        )
    _transcription_acceptance = r.json()
    if _transcription_acceptance.get("status_url") != f"/transcription-status/{job_id}":
        return _fail("/transcribe-uploaded status_url inconsistente")
    # Exercise the lost-response retry contract before polling. The same key
    # must return the same durable event and must not enqueue a second job.
    duplicate = requests.post(
        f"{api}/transcribe-uploaded",
        headers={
            **headers,
            "Idempotency-Key": f"edit-smoke-transcribe-{job_id}",
        },
        json={
            "job_id": job_id,
            "language": "es",
            "artist": _ARTIST,
            "title": _TITLE,
        },
        timeout=15,
    )
    if not duplicate.ok or duplicate.status_code != 202:
        return _fail(f"/transcribe-uploaded retry {duplicate.status_code}: {duplicate.text[:300]}")
    if (
        not duplicate.json().get("deduplicated")
        or duplicate.json().get("outbox_event_id") != _transcription_acceptance.get("outbox_event_id")
    ):
        return _fail("/transcribe-uploaded retry no reutilizó el evento durable")
    print(f"[edit-smoke] job {job_id} subido — esperando transcripción…")

    transcription_deadline = time.time() + args.render_timeout
    transcription_last = ""
    segments = []
    while time.time() < transcription_deadline:
        r = requests.get(
            f"{api}/transcription-status/{job_id}", headers=headers,
            timeout=20,
        )
        if not r.ok:
            return _fail(f"/transcription-status {r.status_code}: {r.text[:300]}")
        transcription = r.json()
        transcription_status = transcription.get("status")
        if transcription_status != transcription_last:
            print(f"[edit-smoke]   transcripción: {transcription_status}")
            transcription_last = transcription_status
        if transcription_status == "transcribed":
            segments = transcription.get("segments") or []
            break
        if transcription_status in (
            "error", "failed", "transcription_failed", "validation_failed",
        ):
            return _fail(
                "transcripción terminó en "
                f"{transcription_status}: {transcription.get('error')}"
            )
        time.sleep(10)
    else:
        return _fail(f"transcripción no terminó en {args.render_timeout}s")

    # 2.5. Autosave pre-aprobación — además de probar el endpoint del editor,
    # deja un delta de timing real entre la hipótesis de máquina y la versión
    # que /generate congela como aprobada. Guardarlo después de /generate no
    # sirve para entrenamiento: sería contaminación posterior a la aprobación.
    try:
        edited_segments = _timing_only_edit(segments)
    except ValueError as exc:
        return _fail(f"fixture no apto para delta de timing: {exc}")
    r = requests.post(
        f"{api}/jobs/{job_id}/save-segments", headers=headers,
        json={"segments": edited_segments}, timeout=30,
    )
    if not r.ok:
        return _fail(f"/save-segments {r.status_code}: {r.text[:300]}")
    saved = r.json()
    if saved.get("count") != len(edited_segments):
        return _fail(
            "/save-segments persistió "
            f"{saved.get('count')} != {len(edited_segments)}"
        )
    saved_revision = saved.get("revision")
    if not isinstance(saved_revision, int) or saved_revision < 1:
        return _fail(
            "/save-segments no devolvió una revisión durable positiva"
        )
    segments = edited_segments
    print(f"[edit-smoke] save-segments pre-aprobación ok (count={saved['count']})")

    # Generar reusando el audio ya persistido y los segmentos aprobados: es
    # exactamente el contrato que usa el wizard después del editor de letra.
    generate_fields = {
        "job_id": job_id,
        "artist": _ARTIST,
        "song_title": _TITLE,
        "segments_json": json.dumps(segments, ensure_ascii=False),
        "base_revision": str(saved_revision),
        "delivery_profile": "youtube",
    }
    r = requests.post(
        f"{api}/generate", headers=headers,
        files={key: (None, value) for key, value in generate_fields.items()},
        timeout=120,
    )
    if not r.ok:
        gate_code = _quality_gate_code(r)
        if args.allow_quality_gate_block and gate_code:
            return _quality_gate_go(job_id, "generate", gate_code)
        return _fail(f"/generate {r.status_code}: {r.text[:300]}")
    print("[edit-smoke] generación aceptada — esperando render inicial…")

    def wait_for(target: set[str], phase: str) -> dict:
        deadline = time.time() + args.render_timeout
        last = ""
        while time.time() < deadline:
            st = requests.get(f"{api}/status/{job_id}", headers=headers,
                              timeout=20).json()
            cur = f"{st.get('status')}|{st.get('current_step')}|{st.get('progress')}"
            if cur != last:
                print(f"[edit-smoke]   {phase}: {cur}")
                last = cur
            if st.get("status") in target:
                return st
            # El gate de calidad puede aceptar /generate y bloquear el render
            # de forma asíncrona. En staging este fixture de silencio debe dar
            # GO en ese punto, sin esperar los 15 minutos del timeout.
            if (
                args.allow_quality_gate_block
                and _status_quality_gate_code(st)
            ):
                return st
            if st.get("status") in ("error", "failed", "upload_failed",
                                    "validation_failed", "transcription_failed"):
                raise RuntimeError(f"{phase} terminó en {st.get('status')}: "
                                   f"{st.get('error')}")
            time.sleep(15)
        raise RuntimeError(f"{phase} no terminó en {args.render_timeout}s")

    try:
        st = wait_for({"pending_review", "done"}, "render")
    except RuntimeError as e:
        return _fail(str(e))
    gate_code = _status_quality_gate_code(st)
    if args.allow_quality_gate_block and gate_code:
        return _quality_gate_go(job_id, "render", gate_code)
    _initial_qc_generated_at = None
    if args.require_delivery_qc:
        contract_error = _delivery_qc_contract_error(st)
        if contract_error:
            return _fail(f"render inicial: {contract_error}")
        _initial_qc_generated_at = st["delivery_qc"]["generated_at"]
        print("[edit-smoke] delivery_qc inicial fresco y ligado al render")

    # 3. Edit de metadata — recorre run_edit_pipeline completo (apertura
    # moviepy del source_audio + fallback UTF-8 + re-render) sin costo Veo.
    r = requests.post(
        f"{api}/edit/{job_id}",
        headers={
            **headers,
            "Idempotency-Key": (
                "edit-smoke-edit-"
                + hashlib.sha256(
                    f"{job_id}:{_TITLE}:metadata".encode("utf-8"),
                ).hexdigest()
            ),
        },
        json={"edit_type": "metadata", "song_title": f"{_TITLE} · editado"},
        timeout=15,
    )
    if not r.ok:
        return _fail(f"/edit {r.status_code}: {r.text[:300]}")
    if r.status_code != 202 or not r.headers.get("Location"):
        return _fail(f"/edit no devolvió el contrato 202+Location: {r.status_code}")
    _edit_acceptance = r.json()
    if _edit_acceptance.get("status_url") != f"/status/{job_id}":
        return _fail("/edit status_url inconsistente")
    duplicate = requests.post(
        f"{api}/edit/{job_id}",
        headers={
            **headers,
            "Idempotency-Key": (
                "edit-smoke-edit-"
                + hashlib.sha256(
                    f"{job_id}:{_TITLE}:metadata".encode("utf-8"),
                ).hexdigest()
            ),
        },
        json={"edit_type": "metadata", "song_title": f"{_TITLE} · editado"},
        timeout=15,
    )
    if not duplicate.ok or duplicate.status_code != 202:
        return _fail(f"/edit retry {duplicate.status_code}: {duplicate.text[:300]}")
    if (
        not duplicate.json().get("deduplicated")
        or duplicate.json().get("outbox_event_id") != _edit_acceptance.get("outbox_event_id")
    ):
        return _fail("/edit retry no reutilizó el evento durable")
    print("[edit-smoke] edit aceptado — esperando re-render…")
    try:
        st = wait_for({"pending_review", "done"}, "edit")
    except RuntimeError as e:
        return _fail(str(e))

    if st.get("error"):
        gate_code = _status_quality_gate_code(st)
        if args.allow_quality_gate_block and gate_code:
            return _quality_gate_go(job_id, "edit", gate_code)
        return _fail(f"edit dejó error residual: {st['error']}")
    if args.require_delivery_qc:
        contract_error = _delivery_qc_contract_error(st)
        if contract_error:
            return _fail(f"re-render editado: {contract_error}")
        if st["delivery_qc"]["generated_at"] == _initial_qc_generated_at:
            return _fail("el edit reutilizó el delivery_qc del render anterior")
        print("[edit-smoke] delivery_qc regenerado y ligado al edit actual")
    print(f"[edit-smoke] GO ✅ — job {job_id}: upload→render→edit→re-render OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
