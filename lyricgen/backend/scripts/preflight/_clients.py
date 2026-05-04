"""Self-contained API clients for preflight checks.

Preflight should be a black-box validation of deployed infrastructure, not
a white-box import of the pipeline module. We hit Vertex AI and Gemini
Vision directly via REST, exactly the way the production code does — but
without dragging in moviepy/librosa/etc.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests


VEO_FAST_MODEL = "veo-3.1-fast-generate-001"
VEO_DEFAULT_TIMEOUT = 600  # 10 min ceiling per generation


# ---------------------------------------------------------------------------
# Production API client (login + upload + poll)
# ---------------------------------------------------------------------------

class GenLyClient:
    """Tiny client for the deployed FastAPI. Login once, reuse the JWT."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None

    def login(self) -> None:
        r = requests.post(
            f"{self.base}/auth/login",
            json={"username": self._username, "password": self._password},
            timeout=15,
        )
        r.raise_for_status()
        self._token = r.json()["token"]

    def _headers(self) -> dict:
        if not self._token:
            self.login()
        return {"Authorization": f"Bearer {self._token}"}

    def upload(self, mp3_path: str, artist: str = "preflight") -> str:
        with open(mp3_path, "rb") as f:
            files = {"file": (Path(mp3_path).name, f, "audio/mpeg")}
            data = {"artist": artist, "delivery_profile": "youtube"}
            r = requests.post(
                f"{self.base}/upload",
                headers=self._headers(),
                files=files,
                data=data,
                timeout=120,
            )
        r.raise_for_status()
        return r.json()["job_id"]

    def status(self, job_id: str) -> dict:
        r = requests.get(
            f"{self.base}/status/{job_id}",
            headers=self._headers(),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()


def _vertex_token() -> str:
    """Same auth pattern as pipeline._veo_access_token: explicit cloud-platform
    scope + with_quota_project, bypassing google-genai SDK."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    project = os.environ["VERTEX_PROJECT"].strip()
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"].strip()
    creds = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds = creds.with_quota_project(project)
    creds.refresh(Request())
    return creds.token


def generate_veo(prompt: str, output_path: str, model: str = VEO_FAST_MODEL) -> str:
    """Generate one Veo clip, save to output_path, return the path. Mirrors
    pipeline._generate_veo_video minus the cooldown/retry/cache scaffolding —
    the preflight does not need any of that since runs are intentional and
    bounded by the runner's --validator-budget."""
    project = os.environ["VERTEX_PROJECT"].strip()
    location = os.environ.get("VERTEX_LOCATION", "us-central1").strip()

    base = (
        f"https://{location}-aiplatform.googleapis.com/v1"
        f"/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}"
    )
    body = {
        "instances": [{"prompt": prompt}],
        "parameters": {"aspectRatio": "16:9", "sampleCount": 1, "generateAudio": False},
    }
    headers = {
        "Authorization": f"Bearer {_vertex_token()}",
        "Content-Type": "application/json",
        "x-goog-user-project": project,
    }
    r = requests.post(f"{base}:predictLongRunning", headers=headers, json=body, timeout=60)
    r.raise_for_status()
    op_name = r.json()["name"]

    deadline = time.time() + VEO_DEFAULT_TIMEOUT
    while time.time() < deadline:
        time.sleep(10)
        rr = requests.post(
            f"{base}:fetchPredictOperation",
            headers={**headers, "Authorization": f"Bearer {_vertex_token()}"},
            json={"operationName": op_name},
            timeout=30,
        )
        if not rr.ok:
            continue
        op = rr.json()
        if op.get("done"):
            break
    else:
        raise TimeoutError(f"Veo did not finish within {VEO_DEFAULT_TIMEOUT}s")

    if "error" in op:
        raise RuntimeError(f"Veo failed: {op['error']}")

    videos = op.get("response", {}).get("videos") or op.get("response", {}).get("generatedVideos") or []
    if not videos:
        raise RuntimeError(f"Veo response had no videos: {op.get('response')}")
    v = videos[0]
    b64 = v.get("bytesBase64Encoded") or (v.get("video") or {}).get("bytesBase64Encoded")
    uri = v.get("gcsUri") or v.get("videoUri") or (v.get("video") or {}).get("uri")
    if b64:
        Path(output_path).write_bytes(base64.b64decode(b64))
    elif uri:
        dl = requests.get(uri, headers={"Authorization": f"Bearer {_vertex_token()}"}, timeout=120)
        dl.raise_for_status()
        Path(output_path).write_bytes(dl.content)
    else:
        raise RuntimeError(f"Veo video has no uri or bytes: {v}")
    return output_path


def extract_frames(video_path: str, max_frames: int = 5, every_secs: int = 2) -> list[str]:
    """Pull representative frames out of a video via ffmpeg. Skips the first
    second (often black/fade-in)."""
    out_dir = tempfile.mkdtemp(prefix="preflight_frames_")
    try:
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or "8.0")
    except Exception:
        duration = 8.0

    timestamps = []
    t = 1.0
    while t < duration and len(timestamps) < max_frames:
        timestamps.append(t)
        t += every_secs

    paths: list[str] = []
    for i, ts in enumerate(timestamps):
        out = os.path.join(out_dir, f"f{i:03d}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out],
            capture_output=True, timeout=30,
        )
        if os.path.exists(out) and os.path.getsize(out) > 0:
            paths.append(out)
    return paths


def validate_frame_with_gemini(image_path: str) -> dict:
    """Mirror content_validator._check_frame_with_gemini, but without the
    pipeline-side imports. Returns {"safe": bool, "issues": [str]}."""
    from google import genai

    project = os.environ["VERTEX_PROJECT"].strip()
    location = os.environ.get("VERTEX_LOCATION", "us-central1").strip()
    client = genai.Client(vertexai=True, project=project, location=location)

    image_bytes = Path(image_path).read_bytes()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            (
                "You are auditing a frame from a music-video background "
                "for risks where AI image generation typically fails. "
                "Flag ONLY actual failures, not benign content.\n\n"
                "FLAG (safe=false) if you can clearly see ANY of these:\n"
                "  - A LARGE, FOREGROUND, or IDENTIFIABLE human face "
                "(eyes/nose/mouth clearly visible on a recognizable "
                "individual). One small face in a crowd does NOT count.\n"
                "  - Visible hands or individual fingers in foreground.\n"
                "  - Text matching a REAL brand or company name "
                "(Nike, Coca-Cola, McDonald's, Apple, etc.).\n"
                "  - Real-world brand logos or trademarks.\n\n"
                "DO NOT FLAG (safe=true) any of these — they are "
                "acceptable in music-video backgrounds:\n"
                "  - Silhouettes, audiences, or distant crowds where "
                "individual identities cannot be made out, even if "
                "small partial faces are visible in the background.\n"
                "  - Invented / gibberish / stylized text strings that "
                "do NOT match any real brand. Fake words on signage "
                "are fine.\n"
                "  - Heavily blurred / motion-blurred / rain-distorted "
                "signage.\n"
                "  - Abstract glowing shapes, smoke, particles, "
                "weather effects, lighting effects.\n"
                "  - Generic pattern textures or non-brand graphic "
                "elements.\n\n"
                "Rule of thumb: flag ONLY if a viewer would recognize "
                "a specific real-world person, hand, brand name, or "
                "logo. Otherwise mark safe. "
                "Respond ONLY with JSON: "
                '{"safe":true/false,"issues":["specific reason"]}'
            ),
        ],
        config=genai.types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=300,
            thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
        ),
    )
    text = (response.text or "").strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"safe": True, "issues": [], "_raw": text[:200]}
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return {"safe": True, "issues": [], "_raw": text[:200]}
    return {
        "safe": bool(data.get("safe", True)),
        "issues": list(data.get("issues", [])),
    }


def validate_video(video_path: str, max_frames: int = 5) -> dict:
    """Run validate_frame_with_gemini across N frames. The video is unsafe if
    any frame is unsafe — same semantics as content_validator.validate_video."""
    frames = extract_frames(video_path, max_frames=max_frames)
    issues: list[dict] = []
    for i, frame in enumerate(frames):
        try:
            r = validate_frame_with_gemini(frame)
        except Exception as e:
            r = {"safe": True, "issues": [f"<gemini error {type(e).__name__}: {e}>"]}
        if not r["safe"]:
            for issue in r["issues"]:
                issues.append({"frame": i, "type": issue})
    for f in frames:
        try:
            os.unlink(f)
        except OSError:
            pass
    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "frames_checked": len(frames),
    }
