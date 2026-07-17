#!/usr/bin/env python3
"""Golden renders — regresión visual del pipeline de render (CTO punto 2).

Contexto: en la semana del 2026-06-08 el cliente más grande vio TRES
defectos visuales (fondo stale, editor de sync, tipografía del short)
que 1.700 tests unitarios no detectaron — porque ninguno mira el
ARTEFACTO FINAL. Este script renderiza una matriz fija y determinística
en staging todas las noches y compara frames contra masters dorados:
cualquier cambio visual no intencional rompe el workflow antes de que
un humano lo vea.

Determinismo: segments FIJOS vía /generate-with-segments (sin Whisper),
fondos de BIBLIOTECA (sin Veo, costo cero), fuentes EXPLÍCITAS, audio
sintético fijo. Lo único variable es ruido de encoder → comparación
perceptual con tolerancia.

Uso:
    python3 scripts/golden_render.py --base-url https://api-staging-....up.railway.app
    python3 scripts/golden_render.py --base-url ... --bless   # regenerar goldens

Goldens en tests/golden_frames/{combo}_{artefacto}.png. El modo normal
sale con código != 0 ante cualquier divergencia y deja los diffs en
golden_diffs/ para inspección.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(HERE, "..", "tests", "golden_frames")
DIFF_DIR = os.path.join(os.getcwd(), "golden_diffs")

BOT_USER = "golden_render_bot"
BOT_PASS = os.environ.get("GOLDEN_BOT_PASSWORD", "GoldenRender2026!bot")

# Letras fijas — cubren acentos, ñ, mayúsculas/minúsculas y longitudes
# distintas (las tiers de fontsize dependen del largo).
SEGMENTS = [
    {"start": 0.5, "end": 4.0, "text": "Golden render de regresión visual"},
    {"start": 4.5, "end": 9.0, "text": "La tipografía debe ser idéntica en video y short"},
    {"start": 9.5, "end": 14.0, "text": "Añoranza, corazón y café"},
    {"start": 14.5, "end": 19.0, "text": "Última línea del master dorado"},
]

# Matriz: 3 combos cubren fuente redondeada/condensada, karaoke/pop/none,
# fondo video y fondo imagen, colores custom y contraste fuerte.
COMBOS = [
    {
        "name": "fredoka-karaoke-libvideo",
        "bg": "mp4",
        "params": {
            "font": "fredoka", "lyrics_animation": "karaoke",
            "text_case": "original", "lyric_color": "#FFFFFF",
            "lyric_sung_color": "#FFD700",
        },
    },
    {
        "name": "anton-pop-libimagen",
        "bg": "jpg",
        "params": {
            "font": "anton", "lyrics_animation": "pop",
            "text_case": "title", "line_transition": "slide_up",
        },
    },
    {
        "name": "bebas-none-strong-libvideo",
        "bg": "mp4",
        "params": {
            "font": "bebas-neue", "lyrics_animation": "none",
            "text_case": "upper", "text_contrast": "strong",
        },
    },
]

# Frames a comparar por artefacto: (segundo, sufijo). Elegidos para caer
# en mitad de una línea de letra (no en transiciones).
FRAME_POINTS = {"video": [(2.0, "a"), (11.0, "b")], "short": [(2.0, "a"), (11.0, "b")]}

# Umbral perceptual: diferencia media absoluta por canal (0-255). El ruido
# de encoder entre dos corridas idénticas mide ~1-3; un cambio de fuente,
# peso, color o layout mide >15. 8 da margen sin esconder regresiones.
MEAN_DIFF_THRESHOLD = 8.0


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----goldenboundary7d29a1"
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fname, payload, ctype) in files.items():
        out.write(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
            f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
        )
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _req(base, path, token=None, data=None, headers=None, method=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(base + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _login(base) -> str:
    body = json.dumps({"username": BOT_USER, "password": BOT_PASS}).encode()
    h = {"Content-Type": "application/json"}
    try:
        return _req(base, "/auth/login", data=body, headers=h)["token"]
    except urllib.error.HTTPError:
        reg = json.dumps({
            "username": BOT_USER, "password": BOT_PASS,
            "email": f"{BOT_USER}@golden.local",
        }).encode()
        return _req(base, "/auth/register", data=reg, headers=h)["token"]


def _make_audio(path: str, dur: float = 22.0):
    """Tono fijo determinístico — el contenido del audio no afecta el
    render (segments fijos), solo define la duración."""
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}",
        "-codec:a", "libmp3lame", "-b:a", "96k", path,
    ], check=True)


def _stable_assets(base, token) -> dict:
    """Asset de biblioteca determinístico por tipo: el de menor id."""
    assets = _req(base, "/backgrounds", token=token)
    out = {}
    for ft in ("mp4", "jpg"):
        cands = sorted((a for a in assets if a["file_type"] == ft), key=lambda a: a["id"])
        if not cands:
            raise SystemExit(f"sin assets de biblioteca tipo {ft} en el entorno")
        out[ft] = cands[0]["id"]
    return out


def _download(base, token, job_id, file_type, dest):
    mt = _req(base, f"/media-token/{job_id}/{file_type}", token=token)["token"]
    url = f"{base}/preview/{job_id}/{file_type}?token={mt}"
    with urllib.request.urlopen(url, timeout=180) as r:
        with open(dest, "wb") as f:
            f.write(r.read())


def _extract_frame(video, second, dest):
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(second), "-i", video, "-frames:v", "1", dest,
    ], check=True)


def _mean_abs_diff(path_a, path_b) -> float:
    from PIL import Image, ImageChops, ImageStat
    a = Image.open(path_a).convert("RGB")
    b = Image.open(path_b).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    return sum(ImageStat.Stat(diff).mean) / 3.0


def _cleanup_bot_jobs(base, token):
    """Rechaza los jobs pendientes del bot. Sin esto, el límite de 5
    videos en proceso/revisión por usuario rompe el nocturno a la
    tercera corrida (visto en la validación inicial 2026-06-12)."""
    try:
        jobs = _req(base, "/jobs", token=token)
        items = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
        for j in items:
            if j.get("status") in ("pending_review", "done"):
                try:
                    _req(base, f"/reject/{j['job_id']}", token=token,
                         data=b"{}", headers={"Content-Type": "application/json"},
                         method="POST")
                except Exception:
                    pass
    except Exception as e:
        print(f"[GOLDEN] cleanup de jobs del bot falló (no fatal): {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--bless", action="store_true",
                    help="guardar los frames actuales como nuevos goldens")
    ap.add_argument("--timeout-min", type=int, default=45)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    os.makedirs(GOLDEN_DIR, exist_ok=True)
    os.makedirs(DIFF_DIR, exist_ok=True)
    work = os.path.join(os.getcwd(), "golden_work")
    os.makedirs(work, exist_ok=True)

    token = _login(base)
    _cleanup_bot_jobs(base, token)  # liberar el cupo de 5 pendientes
    assets = _stable_assets(base, token)
    audio_path = os.path.join(work, "golden_tone.mp3")
    _make_audio(audio_path)
    # Lanzar los 3 renders
    jobs = {}
    for combo in COMBOS:
        fields = {
            "artist": "Golden Render", "song_title": combo["name"],
            "language": "es",
            "segments_json": json.dumps(SEGMENTS),
            "background_id": str(assets[combo["bg"]]),
            "background_mode": "as_is",
            **{k: str(v) for k, v in combo["params"].items()},
        }
        # curl para el multipart: el edge de Railway resetea los POST
        # multipart de urllib (broken pipe); curl negocia Expect/100 bien
        # y existe tanto local como en los runners de GitHub.
        # --http1.1: el edge de Railway corta streams multipart por h2
        # (curl exit 92, "stream not closed cleanly").
        cmd = ["curl", "-sS", "--http1.1", "-m", "180", "-X", "POST",
               f"{base}/generate",  # la RUTA es /generate (generate_with_segments es el nombre de la función)
               "-H", f"Authorization: Bearer {token}",
               "-F", f"file=@{audio_path};type=audio/mpeg"]
        for k, v in fields.items():
            cmd += ["-F", f"{k}={v}"]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        res = json.loads(out.stdout)
        if "job_id" not in res:
            raise SystemExit(f"generate falló para {combo['name']}: {out.stdout[:300]}")
        jobs[combo["name"]] = res["job_id"]
        print(f"[GOLDEN] {combo['name']} → {res['job_id']}")

    # Poll a terminal
    deadline = time.time() + args.timeout_min * 60
    pending = dict(jobs)
    failed = {}
    while pending and time.time() < deadline:
        time.sleep(20)
        for name, jid in list(pending.items()):
            st = _req(base, f"/status/{jid}", token=token)
            s = st.get("status")
            if s in ("done", "pending_review"):
                print(f"[GOLDEN] {name}: {s}")
                del pending[name]
            elif s in ("error", "validation_failed", "rejected"):
                failed[name] = f"{s}: {(st.get('error') or '')[:200]}"
                del pending[name]
    # Un job que al deadline sigue SIN ARRANCAR (queued/processing 0%) no es
    # una regresión de render — es la cola canary drenando último detrás de
    # tráfico humano. Marcarlo rojo entrenaría al equipo a ignorar el golden;
    # se rechaza (libera el cupo de 5 pendientes) y se sale NEUTRO.
    starved = {}
    for name, jid in list(pending.items()):
        st = _req(base, f"/status/{jid}", token=token)
        if st.get("status") == "queued":
            starved[name] = jid
            del pending[name]
    for name, jid in pending.items():
        failed[name] = f"timeout tras {args.timeout_min} min"
    if failed:
        for name, why in failed.items():
            print(f"[GOLDEN] RENDER FALLÓ {name}: {why}", file=sys.stderr)
        sys.exit(2)
    if starved:
        # Quedan encolados: corren cuando la cola se vacíe y el cleanup del
        # próximo run los rechaza una vez terminados (pending_review/done).
        for name, jid in starved.items():
            print(f"[GOLDEN] SKIPPED {name} ({jid}): cola ocupada — el render "
                  "nunca arrancó dentro del deadline (no es regresión)",
                  file=sys.stderr)
        sys.exit(0)

    # Frames + comparación
    regressions = []
    for combo in COMBOS:
        name = combo["name"]
        jid = jobs[name]
        for ft, points in FRAME_POINTS.items():
            media = os.path.join(work, f"{name}_{ft}.mp4")
            _download(base, token, jid, ft, media)
            for second, suffix in points:
                frame = os.path.join(work, f"{name}_{ft}_{suffix}.png")
                _extract_frame(media, second, frame)
                golden = os.path.join(GOLDEN_DIR, f"{name}_{ft}_{suffix}.png")
                if args.bless:
                    os.replace(frame, golden)
                    print(f"[GOLDEN] bendecido {os.path.basename(golden)}")
                    continue
                if not os.path.exists(golden):
                    regressions.append((name, ft, suffix, "golden faltante — correr --bless"))
                    continue
                d = _mean_abs_diff(golden, frame)
                status = "OK" if d <= MEAN_DIFF_THRESHOLD else "REGRESIÓN"
                print(f"[GOLDEN] {name} {ft}/{suffix}: diff={d:.2f} ({status})")
                if d > MEAN_DIFF_THRESHOLD:
                    regressions.append((name, ft, suffix, f"diff {d:.2f} > {MEAN_DIFF_THRESHOLD}"))
                    import shutil
                    shutil.copy(frame, os.path.join(DIFF_DIR, f"{name}_{ft}_{suffix}_actual.png"))
                    shutil.copy(golden, os.path.join(DIFF_DIR, f"{name}_{ft}_{suffix}_golden.png"))

    _cleanup_bot_jobs(base, token)  # dejar el cupo libre para la próxima

    if args.bless:
        print("[GOLDEN] goldens regenerados — commitear tests/golden_frames/")
        return
    if regressions:
        print("\n[GOLDEN] ❌ REGRESIONES VISUALES:", file=sys.stderr)
        for name, ft, suffix, why in regressions:
            print(f"  - {name} {ft}/{suffix}: {why}", file=sys.stderr)
        print(f"  frames actuales vs goldens en {DIFF_DIR}/", file=sys.stderr)
        sys.exit(1)
    print("\n[GOLDEN] ✅ sin regresiones visuales")


if __name__ == "__main__":
    main()
