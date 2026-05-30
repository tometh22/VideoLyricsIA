#!/usr/bin/env python3
"""Migrar/duplicar UN job de PROD a STAGING (fila de DB + audio + fondo en R2),
para re-renderizarlo en staging y validar el fix de karaoke.

Dos fases (corré cada una con las credenciales de SU entorno; nunca mezclás):

  # FASE 1 — EXPORT (credenciales de PROD): lee la fila + baja audio/fondo
  PHASE=export \
  DATABASE_URL='<PROD_DATABASE_URL>' \
  R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=<prod-bucket> \
  JOB_ID=5e677b09f4a2 \
  python3 /tmp/migrate_job.py

  # FASE 2 — IMPORT (credenciales de STAGING): sube audio/fondo + inserta la fila
  PHASE=import \
  DATABASE_URL='<STAGING_DATABASE_URL>' \
  R2_ENDPOINT_URL=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=<staging-bucket> \
  STAGING_USERNAME='<tu-usuario-en-staging>' \
  python3 /tmp/migrate_job.py

Deja el job en staging con status='done' (editable) → abrís el video en staging y
hacés "Editar y re-renderizar" (karaoke) → con FORCED_ALIGNER_ENABLED=1 corre el
forced-align y el relleno sincroniza. Reusa el fondo cacheado (no regenera Veo).
"""
import json
import os

import boto3
from sqlalchemy import create_engine, text

BUNDLE = os.environ.get("BUNDLE_DIR", "/tmp/job_bundle")
PHASE = os.environ["PHASE"]
COLS = ["job_id", "artist", "song_title", "style", "filename", "delivery_profile",
        "segments_json", "render_params", "input_r2_key", "bg_r2_key_cached",
        "s3_keys", "umg_spec"]


def _r2():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )


def export():
    os.makedirs(BUNDLE, exist_ok=True)
    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.connect() as c:
        row = c.execute(
            text(f"SELECT {', '.join(COLS)} FROM jobs WHERE job_id = :j"),
            {"j": os.environ["JOB_ID"]},
        ).mappings().first()
    if not row:
        raise SystemExit(f"job {os.environ['JOB_ID']} no encontrado en esta DB")
    data = dict(row)
    json.dump(data, open(os.path.join(BUNDLE, "job.json"), "w"), default=str)
    print("[export] fila leída:", data["job_id"], "|", data.get("artist"), "-", data.get("song_title"))

    s3, bucket = _r2(), os.environ["R2_BUCKET"]
    for key_col in ("input_r2_key", "bg_r2_key_cached"):
        key = data.get(key_col)
        if not key:
            print(f"[export] {key_col}: vacío — se salta")
            continue
        dest = os.path.join(BUNDLE, key_col + os.path.splitext(key)[1])
        s3.download_file(bucket, key, dest)
        print(f"[export] {key_col} ↓ {key} → {dest} ({os.path.getsize(dest)/1e6:.1f} MB)")
    print(f"[export] OK → bundle en {BUNDLE}. Corré la FASE import con credenciales de staging.")


def _import():
    data = json.load(open(os.path.join(BUNDLE, "job.json")))
    eng = create_engine(os.environ["DATABASE_URL"])

    # Resolver el usuario/tenant de staging al que pertenecerá el job (para que
    # lo puedas ver y editar desde la UI de staging).
    with eng.connect() as c:
        u = c.execute(
            text("SELECT id, tenant_id FROM users WHERE username = :u OR email = :u"),
            {"u": os.environ["STAGING_USERNAME"]},
        ).mappings().first()
    if not u:
        raise SystemExit(f"usuario '{os.environ['STAGING_USERNAME']}' no existe en staging")
    user_id, tenant_id = u["id"], u["tenant_id"]

    # Subir audio + fondo al R2 de staging, a las MISMAS keys (input_r2_key /
    # bg_r2_key_cached apuntan a esos strings).
    s3, bucket = _r2(), os.environ["R2_BUCKET"]
    for key_col in ("input_r2_key", "bg_r2_key_cached"):
        key = data.get(key_col)
        if not key:
            continue
        src = os.path.join(BUNDLE, key_col + os.path.splitext(key)[1])
        if not os.path.exists(src):
            print(f"[import] {key_col}: no hay archivo local ({src}) — se salta")
            continue
        s3.upload_file(src, bucket, key)
        print(f"[import] {key_col} ↑ {src} → {key}")
        # Hotfix 2026-05-30: verificar que el upload landed en R2 antes de
        # seguir. La corrida del 2026-05-29 del job Bersuit (5e677b09f4a2)
        # silenciosamente terminó con input_r2_key=null en la DB de staging
        # — el editor lyric montaba sin audioUrl porque /source-audio-url
        # devolvía 404, y el toggle Lista/Timeline + scrub bar desaparecían.
        # No vimos el problema hasta que un operador entró al editor en
        # staging. Esta verificación post-upload aborta la corrida si el
        # archivo no quedó (config R2 wrong, perm error, etc.).
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"[import] FATAL: {key_col} ('{key}') no quedó en R2 staging "
                f"tras el upload ({type(exc).__name__}). Abortando antes del "
                f"INSERT para no producir un job con audio o fondo huérfano."
            ) from exc

    new_job_id = os.environ.get("NEW_JOB_ID", data["job_id"])
    with eng.begin() as c:
        exists = c.execute(text("SELECT 1 FROM jobs WHERE job_id = :j"),
                           {"j": new_job_id}).first()
        if exists:
            raise SystemExit(f"job_id {new_job_id} YA existe en staging — pasá NEW_JOB_ID=<otro>")
        c.execute(text(
            "INSERT INTO jobs (job_id, user_id, tenant_id, artist, song_title, style, "
            "filename, status, current_step, progress, delivery_profile, segments_json, "
            "render_params, input_r2_key, bg_r2_key_cached, s3_keys, umg_spec, edit_count, created_at) "
            "VALUES (:job_id, :user_id, :tenant_id, :artist, :song_title, :style, :filename, "
            "'done', 'done', 100, :delivery_profile, CAST(:segments_json AS jsonb), "
            "CAST(:render_params AS jsonb), :input_r2_key, :bg_r2_key_cached, "
            "CAST(:s3_keys AS jsonb), CAST(:umg_spec AS jsonb), 0, now())"
        ), {
            "job_id": new_job_id, "user_id": user_id, "tenant_id": tenant_id,
            "artist": data["artist"], "song_title": data.get("song_title"),
            "style": data.get("style") or "oscuro",
            "filename": data.get("filename") or "perro_amor.mp3",
            "delivery_profile": data.get("delivery_profile") or "youtube",
            "segments_json": json.dumps(data.get("segments_json")),
            "render_params": json.dumps(data.get("render_params")),
            "input_r2_key": data.get("input_r2_key"),
            "bg_r2_key_cached": data.get("bg_r2_key_cached"),
            "s3_keys": json.dumps(data.get("s3_keys")),
            "umg_spec": json.dumps(data.get("umg_spec")),
        })
    print(f"[import] OK → job {new_job_id} creado en staging (tenant {tenant_id}, user {user_id}).")
    print("[import] Abrí /videos/%s en staging → 'Editar y re-renderizar' → karaoke." % new_job_id)


if PHASE == "export":
    export()
elif PHASE == "import":
    _import()
else:
    raise SystemExit("PHASE debe ser 'export' o 'import'")
