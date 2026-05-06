"""A/B test: generate the same Veo prompt with veo-3.1-generate-001 and
veo-3.1-fast-generate-001 to compare visual quality before swapping models
in production.

Reads VERTEX_PROJECT, VERTEX_LOCATION, GOOGLE_APPLICATION_CREDENTIALS from
.env in the backend dir. Costs roughly $4 (8s × $0.40 standard + 8s × $0.10
fast = $4). Output MP4s land in lyricgen/outputs/ab_test_veo/.

Run from repo root or from backend/:
    cd lyricgen/backend && python3 scripts/ab_test_veo.py
"""

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

import requests

VERTEX_PROJECT = os.environ["VERTEX_PROJECT"].strip()
VERTEX_LOCATION = os.environ["VERTEX_LOCATION"].strip()
CREDS_PATH = os.environ["GOOGLE_APPLICATION_CREDENTIALS"].strip()

OUTPUT_DIR = BACKEND.parent / "outputs" / "ab_test_veo"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PROMPT = (
    "Cinematic shot of a stormy ocean at dusk, dramatic dark clouds, "
    "powerful waves crashing against rocky cliffs, moody atmosphere, "
    "deep blue and orange tones from the setting sun. "
    "Photorealistic, filmed with cinema camera, real footage. "
    "No text, no words, no letters, no people, no faces, no hands, "
    "no CGI, no animation."
)

MODELS = [
    ("veo-3.1-generate-001", "standard"),
    ("veo-3.1-fast-generate-001", "fast"),
]

# Allow retrying just one variant: `python3 scripts/ab_test_veo.py standard`
if len(sys.argv) > 1:
    wanted = sys.argv[1]
    MODELS = [(m, l) for (m, l) in MODELS if l == wanted]
    if not MODELS:
        sys.exit(f"unknown variant {wanted!r} (use: standard|fast)")


def access_token() -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_file(
        CREDS_PATH,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds = creds.with_quota_project(VERTEX_PROJECT)
    creds.refresh(Request())
    return creds.token


def generate(model: str, label: str) -> Path:
    out_path = OUTPUT_DIR / f"{label}_{model}.mp4"
    base = (
        f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1"
        f"/projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}"
        f"/publishers/google/models/{model}"
    )
    body = {
        "instances": [{"prompt": PROMPT}],
        "parameters": {
            "aspectRatio": "16:9",
            "sampleCount": 1,
            "generateAudio": False,
        },
    }
    print(f"\n[{label}] submitting to {model}...")
    t0 = time.time()
    token = access_token()
    r = requests.post(
        f"{base}:predictLongRunning",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-goog-user-project": VERTEX_PROJECT,
        },
        json=body,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"submit HTTP {r.status_code}: {r.text[:500]}")
    op_name = r.json()["name"]
    print(f"[{label}] operation: {op_name}")

    fetch_url = f"{base}:fetchPredictOperation"
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(10)
        token = access_token()
        rr = requests.post(
            fetch_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-goog-user-project": VERTEX_PROJECT,
            },
            json={"operationName": op_name},
            timeout=30,
        )
        if not rr.ok:
            print(f"[{label}] poll HTTP {rr.status_code}: {rr.text[:200]}")
            continue
        op = rr.json()
        if op.get("done"):
            break
        print(f"[{label}] still running... ({int(time.time() - t0)}s)")
    else:
        raise TimeoutError(f"{label} timed out")

    if "error" in op:
        raise RuntimeError(f"{label} failed: {op['error']}")

    resp = op.get("response", {})
    videos = resp.get("videos") or resp.get("generatedVideos") or []
    if not videos:
        raise RuntimeError(f"{label} no videos in response: {resp}")
    v = videos[0]
    b64 = v.get("bytesBase64Encoded") or (v.get("video") or {}).get("bytesBase64Encoded")
    uri = v.get("gcsUri") or v.get("videoUri") or (v.get("video") or {}).get("uri")
    if b64:
        import base64
        out_path.write_bytes(base64.b64decode(b64))
    elif uri:
        token = access_token()
        dl = requests.get(uri, headers={"Authorization": f"Bearer {token}"}, timeout=120)
        dl.raise_for_status()
        out_path.write_bytes(dl.content)
    else:
        raise RuntimeError(f"{label} no uri/bytes: {v}")

    elapsed = time.time() - t0
    size = out_path.stat().st_size / 1024 / 1024
    print(f"[{label}] DONE in {elapsed:.0f}s, {size:.1f} MB -> {out_path}")
    return out_path


def main():
    print(f"Output dir: {OUTPUT_DIR}")
    print(f"Prompt: {PROMPT[:80]}...")
    results = []
    for model, label in MODELS:
        try:
            p = generate(model, label)
            results.append((label, model, p, None))
        except Exception as e:
            results.append((label, model, None, str(e)))
            print(f"[{label}] ERROR: {e}")

    print("\n=== RESULTS ===")
    for label, model, path, err in results:
        if path:
            print(f"  {label:10} {model:30} -> {path}")
        else:
            print(f"  {label:10} {model:30} -> FAILED: {err}")
    print(f"\nOpen both files to compare visually:")
    for label, model, path, err in results:
        if path:
            print(f"  open '{path}'")


if __name__ == "__main__":
    main()
