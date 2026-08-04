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
     (/transcribe-uploaded), genera el video y espera pending_review.
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
import io
import json
import os
import struct
import sys
import time
import wave

import requests

# Tags LATIN-1 (no UTF-8): ó=0xf3, é=0xe9 — el shape exacto del incidente.
_TITLE = "Estrechez de Corazón (smoke)"
_ARTIST = "Los Prisioneros - smoke"  # separador ASCII: el tag va en latin-1


def _accented_wav_bytes(seconds: int = 2) -> bytes:
    """WAV PCM válido + chunk LIST/INFO con metadata latin-1."""
    raw = io.BytesIO()
    with wave.open(raw, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * (8000 * seconds))
    data = bytearray(raw.getvalue())

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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api-url", required=True)
    p.add_argument("--render-timeout", type=int, default=900,
                   help="segundos máximos para cada fase de render")
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
        f"{api}/transcribe-uploaded", headers=headers,
        json={
            "job_id": job_id,
            "language": "es",
            "artist": _ARTIST,
            "title": _TITLE,
        },
        timeout=120,
    )
    if not r.ok:
        return _fail(f"/transcribe-uploaded {r.status_code}: {r.text[:300]}")
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

    # Generar reusando el audio ya persistido y los segmentos aprobados: es
    # exactamente el contrato que usa el wizard después del editor de letra.
    generate_fields = {
        "job_id": job_id,
        "artist": _ARTIST,
        "song_title": _TITLE,
        "segments_json": json.dumps(segments, ensure_ascii=False),
        "delivery_profile": "youtube",
    }
    r = requests.post(
        f"{api}/generate", headers=headers,
        files={key: (None, value) for key, value in generate_fields.items()},
        timeout=120,
    )
    if not r.ok:
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
            if st.get("status") in ("error", "failed", "upload_failed",
                                    "validation_failed", "transcription_failed"):
                raise RuntimeError(f"{phase} terminó en {st.get('status')}: "
                                   f"{st.get('error')}")
            time.sleep(15)
        raise RuntimeError(f"{phase} no terminó en {args.render_timeout}s")

    try:
        wait_for({"pending_review", "done"}, "render")
    except RuntimeError as e:
        return _fail(str(e))

    # 2.5. Autosave del editor — el camino que los operadores reportan como
    # frágil (issue #934). GO/NO-GO: POST /jobs/{id}/save-segments con una
    # corrección de timing debe 200 y persistir el count. Sin esto el gate
    # verde no decía nada sobre el guardado del editor (incidente Seba
    # 21-jul: autosave fallando en prod con smoke verde).
    _segs = [
        {"start": 0.2, "end": 1.4, "text": "estrechez de corazón (smoke)"},
        {"start": 1.5, "end": 2.0, "text": "línea dos"},
    ]
    r = requests.post(
        f"{api}/jobs/{job_id}/save-segments", headers=headers,
        json={"segments": _segs}, timeout=30,
    )
    if not r.ok:
        return _fail(f"/save-segments {r.status_code}: {r.text[:300]}")
    _saved = r.json()
    if _saved.get("count") != len(_segs):
        return _fail(f"/save-segments persistió {_saved.get('count')} != {len(_segs)}")
    print(f"[edit-smoke] save-segments ok (count={_saved['count']})")

    # 3. Edit de metadata — recorre run_edit_pipeline completo (apertura
    # moviepy del source_audio + fallback UTF-8 + re-render) sin costo Veo.
    r = requests.post(
        f"{api}/edit/{job_id}", headers=headers,
        json={"edit_type": "metadata", "song_title": f"{_TITLE} · editado"},
        timeout=30,
    )
    if not r.ok:
        return _fail(f"/edit {r.status_code}: {r.text[:300]}")
    print("[edit-smoke] edit aceptado — esperando re-render…")
    try:
        st = wait_for({"pending_review", "done"}, "edit")
    except RuntimeError as e:
        return _fail(str(e))

    if st.get("error"):
        return _fail(f"edit dejó error residual: {st['error']}")
    print(f"[edit-smoke] GO ✅ — job {job_id}: upload→render→edit→re-render OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
