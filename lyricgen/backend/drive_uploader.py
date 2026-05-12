"""Worker que transfiere archivos R2 → Google Drive vía rclone.

Por qué rclone subprocess en lugar de google-api-python-client puro:
para archivos de 16 GB necesitamos resume + chunked upload + hash
verify + multi-stream. rclone resuelve todo eso de fábrica con un
comando. Reimplementar en Python serían ~200 líneas frágiles + maint.

Flow del worker:
  1. Levantar refresh_token desde DB (Fernet-decrypt vía drive_oauth).
  2. Refresh para obtener access_token (~1h validity).
  3. Asegurar carpeta destino "GenLy Uploads" en Drive (crear si no
     existe — drive.file scope lo permite porque la creamos nosotros).
  4. Escribir un rclone.conf temporal con creds R2 + tokens Drive.
  5. Spawn subprocess `rclone copyto r2:bucket/<key> "gdrive:GenLy Uploads/<name>"`
     con --use-json-log + --stats 5s. Parsear stdout, actualizar
     DriveTransfer.progress en DB cada update.
  6. Al terminar: get drive_file_id + web_view_link → guardar en DriveTransfer.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("genly.drive_uploader")


GDRIVE_FOLDER_NAME = "GenLy Uploads"
DRIVE_API_FILES = "https://www.googleapis.com/drive/v3/files"


# --- Errors ---

class DriveUploadError(Exception):
    """Falla en cualquier paso de la transferencia R2 → Drive."""
    pass


# --- Drive API: asegurar carpeta destino ---

def ensure_genly_folder(access_token: str) -> str:
    """Devuelve el folder_id de la carpeta "GenLy Uploads" en el Drive
    del user. Si no existe la crea. Idempotente.

    Con scope drive.file solo vemos archivos que la app creó, así que
    el list devuelve solo carpetas que NOSOTROS hicimos antes — no hay
    riesgo de matchear una carpeta del user con el mismo nombre."""
    headers = {"Authorization": f"Bearer {access_token}"}

    # 1. Buscar si ya existe
    q = (
        f"name='{GDRIVE_FOLDER_NAME}' and "
        f"mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res = requests.get(
        DRIVE_API_FILES,
        params={"q": q, "fields": "files(id,name)", "pageSize": 10},
        headers=headers,
        timeout=15,
    )
    if not res.ok:
        raise DriveUploadError(
            f"Drive folder search falló ({res.status_code}): {res.text[:300]}"
        )
    files = res.json().get("files", [])
    if files:
        return files[0]["id"]

    # 2. Crear si no existe
    res = requests.post(
        DRIVE_API_FILES,
        json={
            "name": GDRIVE_FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
        },
        headers={**headers, "Content-Type": "application/json"},
        timeout=15,
    )
    if not res.ok:
        raise DriveUploadError(
            f"Drive folder create falló ({res.status_code}): {res.text[:300]}"
        )
    return res.json()["id"]


def fetch_file_metadata(access_token: str, file_id: str) -> dict:
    """Devuelve {webViewLink, size, name, ...} del archivo subido.
    Usado al final del transfer para guardar el link en DriveTransfer."""
    headers = {"Authorization": f"Bearer {access_token}"}
    res = requests.get(
        f"{DRIVE_API_FILES}/{file_id}",
        params={"fields": "id,name,size,webViewLink,parents,mimeType"},
        headers=headers,
        timeout=10,
    )
    if not res.ok:
        raise DriveUploadError(
            f"Drive file metadata falló ({res.status_code}): {res.text[:200]}"
        )
    return res.json()


# --- rclone config builder ---

def _build_rclone_config(
    r2_access_key: str,
    r2_secret_key: str,
    r2_endpoint: str,
    drive_client_id: str,
    drive_client_secret: str,
    drive_access_token: str,
    drive_refresh_token: str,
    drive_token_expiry: Optional[datetime] = None,
) -> str:
    """Devuelve el contenido de rclone.conf con dos remotes: `r2` y
    `gdrive`. Lo escribimos a un archivo temporal por transfer — NO se
    persiste a disco a largo plazo porque contiene access_token cleartext.

    rclone espera que el campo `token` del remote drive sea un JSON
    string con {access_token, refresh_token, expiry, token_type, scope}.
    """
    # rclone parsea el token como JSON serializado. expiry en RFC3339.
    if drive_token_expiry is None:
        drive_token_expiry = datetime.now(timezone.utc)
    token_json = json.dumps({
        "access_token": drive_access_token,
        "refresh_token": drive_refresh_token,
        "token_type": "Bearer",
        "expiry": drive_token_expiry.isoformat().replace("+00:00", "Z"),
    })

    return f"""[r2]
type = s3
provider = Cloudflare
access_key_id = {r2_access_key}
secret_access_key = {r2_secret_key}
endpoint = {r2_endpoint}
region = auto

[gdrive]
type = drive
client_id = {drive_client_id}
client_secret = {drive_client_secret}
scope = drive.file
token = {token_json}
"""


# --- Main entrypoint ---

# Map de file_type → filename en R2/Drive. Espejo de prores.FILE_MAP_PRORES
# extendido con MP4 también porque la app sirve los 4 formatos.
FILE_TYPE_TO_DRIVE_NAME = {
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
    "video": "lyric_video.mp4",
    "short": "short.mp4",
}


def upload_via_rclone(
    transfer_id: str,
    user_id: int,
    job_id: str,
    file_type: str,
    progress_callback=None,
) -> dict:
    """Ejecuta la transferencia R2 → Drive para una row de DriveTransfer.

    Args:
        transfer_id: PK de drive_transfers, para updates incrementales.
        user_id: dueño de los tokens Drive.
        job_id: que se sube.
        file_type: 'umg_master' | 'umg_short' | 'video' | 'short'.
        progress_callback: opcional, llamado cada update de progress.
            Recibe {bytes_transferred, bytes_total, percent}.

    Returns:
        dict con {drive_file_id, web_view_link, bytes_transferred}.

    Raises:
        DriveUploadError si Drive folder ops fallan, rclone falla, o
        no podemos parsear el output.
    """
    from database import SessionLocal, UserDriveTokens, Job
    from drive_oauth import (
        decrypt_token, refresh_access_token,
        GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
        DriveOAuthError,
    )
    from storage import _object_key, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT_URL

    if file_type not in FILE_TYPE_TO_DRIVE_NAME:
        raise DriveUploadError(f"file_type inválido: {file_type!r}")

    db = SessionLocal()
    try:
        tokens_row = db.query(UserDriveTokens).filter(
            UserDriveTokens.user_id == user_id
        ).first()
        if tokens_row is None:
            raise DriveUploadError(
                f"User {user_id} no tiene Drive conectado."
            )

        refresh_token = decrypt_token(tokens_row.encrypted_refresh_token)

        job = db.query(Job).filter(Job.job_id == job_id).first()
        if job is None:
            raise DriveUploadError(f"Job {job_id} no existe.")
        tenant_id = job.tenant_id
    finally:
        db.close()

    # Refresh para access_token fresh — rclone va a refrescar también si
    # expira mid-transfer, pero damos uno limpio para evitar latencia inicial.
    try:
        token_data = refresh_access_token(refresh_token)
    except DriveOAuthError as e:
        raise DriveUploadError(
            f"refresh_access_token falló (probable revoke en Google): {e}"
        )
    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    token_expiry = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc)

    # Asegurar carpeta destino. Idempotente.
    folder_id = ensure_genly_folder(access_token)

    # Filename en Drive. Para evitar colisiones cuando varios jobs tienen
    # el mismo umg_master.mov, prefijamos con job_id + song_title.
    drive_filename = _build_drive_filename(job_id, file_type)
    r2_filename = FILE_TYPE_TO_DRIVE_NAME[file_type]
    r2_key = _object_key(tenant_id, job_id, r2_filename)

    # Escribir rclone config temporal (creds sensibles → tempfile,
    # cleanup garantizado).
    config_content = _build_rclone_config(
        r2_access_key=R2_ACCESS_KEY_ID,
        r2_secret_key=R2_SECRET_ACCESS_KEY,
        r2_endpoint=R2_ENDPOINT_URL,
        drive_client_id=GOOGLE_OAUTH_CLIENT_ID,
        drive_client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        drive_access_token=access_token,
        drive_refresh_token=refresh_token,
        drive_token_expiry=token_expiry,
    )

    # Usamos NamedTemporaryFile con delete=False para garantizar que el
    # archivo exista mientras corre rclone, y lo borramos en finally.
    config_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".conf", delete=False
    )
    try:
        config_file.write(config_content)
        config_file.close()
        os.chmod(config_file.name, 0o600)

        # rclone copyto sube a un destino específico (no a una carpeta).
        # Usamos drive_filename como nombre explícito y --drive-root-folder-id
        # para que rclone resuelva la carpeta correcta sin necesitar
        # listar el Drive entero.
        cmd = [
            "rclone", "copyto",
            f"r2:{R2_BUCKET}/{r2_key}",
            f"gdrive:{drive_filename}",
            "--config", config_file.name,
            "--drive-root-folder-id", folder_id,
            "--use-json-log",
            "--stats", "5s",
            "--stats-one-line",
            "--transfers", "4",
            "--checkers", "4",
            "--retries", "3",
            "--low-level-retries", "10",
            "--multi-thread-streams", "4",
        ]

        logger.info(
            "[drive_uploader] starting rclone for transfer=%s job=%s file=%s",
            transfer_id, job_id, file_type,
        )

        # Spawn + stream stdout. rclone con --use-json-log emite líneas
        # JSON al stderr (no stdout). Parseamos stats messages para
        # actualizar progress.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # mergeamos para parser único
            text=True,
            bufsize=1,  # line-buffered
        )

        bytes_transferred = 0
        bytes_total = 0

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            # rclone JSON log: {"level":"info","msg":"Transferred: ...","stats":{...}}
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            stats = entry.get("stats")
            if stats:
                bytes_transferred = stats.get("bytes", 0)
                bytes_total = stats.get("totalBytes", bytes_total)
                if progress_callback and bytes_total > 0:
                    percent = int((bytes_transferred / bytes_total) * 100)
                    progress_callback({
                        "bytes_transferred": bytes_transferred,
                        "bytes_total": bytes_total,
                        "percent": percent,
                    })

        proc.wait()
        if proc.returncode != 0:
            raise DriveUploadError(
                f"rclone exit code {proc.returncode}. "
                f"Ver logs del worker para detalle del stderr."
            )

        # Resolver drive_file_id. rclone NO emite el ID en su output JSON.
        # Hacemos un list en la carpeta filtrado por nombre.
        files_res = requests.get(
            DRIVE_API_FILES,
            params={
                "q": f"name='{drive_filename}' and '{folder_id}' in parents and trashed=false",
                "fields": "files(id,name,size,webViewLink)",
                "pageSize": 5,
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if not files_res.ok:
            raise DriveUploadError(
                f"Drive file lookup post-upload falló: {files_res.status_code} {files_res.text[:200]}"
            )
        files = files_res.json().get("files", [])
        if not files:
            raise DriveUploadError(
                "rclone reportó éxito pero el archivo no aparece en Drive. "
                "Posible problema de scope drive.file o el archivo está en "
                "otra carpeta."
            )
        meta = files[0]

        return {
            "drive_file_id": meta["id"],
            "web_view_link": meta.get("webViewLink"),
            "bytes_transferred": int(meta.get("size") or bytes_transferred),
        }

    finally:
        try:
            os.unlink(config_file.name)
        except OSError:
            pass


def _build_drive_filename(job_id: str, file_type: str) -> str:
    """Nombre que va a aparecer en Drive. Incluye job_id para evitar
    colisiones cuando varios jobs tienen el mismo umg_master.mov.

    Patrón: "<job_id>__<filename>"
    Ej: "abc123def456__umg_master.mov"
    """
    base = FILE_TYPE_TO_DRIVE_NAME[file_type]
    return f"{job_id}__{base}"


# --- Worker entrypoint (llamado por RQ) ---

def run_drive_delivery(transfer_id: str) -> None:
    """RQ worker entrypoint. Lee la row drive_transfers, actualiza
    status mientras corre, escribe el resultado al terminar.

    Hace todo el manejo de estado en DB para que el endpoint
    `GET /drive/transfers/{id}` muestre progreso al frontend.
    """
    from database import SessionLocal, DriveTransfer

    db = SessionLocal()
    try:
        transfer = db.query(DriveTransfer).filter(DriveTransfer.id == transfer_id).first()
        if transfer is None:
            logger.error("[drive_uploader] transfer %s no existe", transfer_id)
            return
        transfer.status = "running"
        db.commit()
        user_id = transfer.user_id
        job_id = transfer.job_id
        file_type = transfer.file_type
    finally:
        db.close()

    def _progress_cb(p: dict) -> None:
        """Update DriveTransfer.progress_pct cada 5s. Abrimos sesión corta
        por update para no mantener una conexión abierta toda la transferencia."""
        s = SessionLocal()
        try:
            row = s.query(DriveTransfer).filter(DriveTransfer.id == transfer_id).first()
            if row is None:
                return
            row.bytes_transferred = p["bytes_transferred"]
            row.bytes_total = p["bytes_total"]
            row.progress_pct = p["percent"]
            s.commit()
        finally:
            s.close()

    try:
        result = upload_via_rclone(
            transfer_id=transfer_id,
            user_id=user_id,
            job_id=job_id,
            file_type=file_type,
            progress_callback=_progress_cb,
        )
        # Marcar como done
        s = SessionLocal()
        try:
            row = s.query(DriveTransfer).filter(DriveTransfer.id == transfer_id).first()
            if row is not None:
                row.status = "done"
                row.progress_pct = 100
                row.drive_file_id = result["drive_file_id"]
                row.web_view_link = result["web_view_link"]
                row.bytes_transferred = result["bytes_transferred"]
                row.completed_at = datetime.now(timezone.utc)
                s.commit()
        finally:
            s.close()
        logger.info(
            "[drive_uploader] transfer %s done. file_id=%s",
            transfer_id, result["drive_file_id"],
        )

    except Exception as e:
        # Cualquier error → status=error con mensaje. Frontend lo muestra.
        logger.exception("[drive_uploader] transfer %s FAILED", transfer_id)
        s = SessionLocal()
        try:
            row = s.query(DriveTransfer).filter(DriveTransfer.id == transfer_id).first()
            if row is not None:
                row.status = "error"
                row.error = str(e)[:1000]
                row.completed_at = datetime.now(timezone.utc)
                s.commit()
        finally:
            s.close()
        raise
