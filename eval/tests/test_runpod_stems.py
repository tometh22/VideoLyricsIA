import io
import json
import tarfile
from pathlib import Path

import soundfile as sf
import numpy as np
import pytest

from eval.runpod_stems import _sha256_file, import_results, package


def _fixture(tmp_path: Path):
    golden, cache = tmp_path / "golden", tmp_path / "cache"
    case = golden / "case-a"
    case.mkdir(parents=True)
    audio = case / "audio.wav"
    sf.write(audio, np.zeros(16000, dtype=np.float32), 16000)
    sha = _sha256_file(audio)
    (case / "meta.json").write_text(json.dumps({
        "raw_quality": "exact", "duration_s": 1.0,
        "audio": {"filename": "audio.wav", "sha256": sha},
    }))
    (golden / "manifest.json").write_text(json.dumps({"cases": [{
        "song_id": "a", "path": "case-a", "raw_quality": "exact",
    }]}))
    cache.mkdir()
    (cache / "manifest.json").write_text(json.dumps({
        "cases": [{"song_id": "a", "status": "cache_miss"}],
        "downloaded": 0, "cache_misses": 1,
    }))
    return golden, cache, sha


def test_package_is_credential_free_and_records_source_identity(tmp_path: Path):
    golden, cache, sha = _fixture(tmp_path)
    archive = tmp_path / "bundle.tar.gz"
    result = package(golden, cache, archive)
    assert result["contains_credentials"] is False
    assert result["cases"][0]["source_audio_sha256"] == sha
    with tarfile.open(archive, "r:gz") as handle:
        names = handle.getnames()
        manifest = json.load(handle.extractfile("RUNPOD_BUNDLE_MANIFEST.json"))
        runner = handle.extractfile("run_job.sh").read()
    assert names == sorted(names)
    assert b"RUNPOD_API_KEY" not in runner
    assert not any(".env" in name for name in names)
    assert manifest["model"] == "mdx_extra"


def test_import_rejects_tampered_stem(tmp_path: Path):
    golden, cache, source_sha = _fixture(tmp_path)
    archive = tmp_path / "result.tar.gz"
    manifest = {"model": "mdx_extra", "cases": [{
        "song_id": "a", "source_audio_sha256": source_sha,
        "stem_sha256": "0" * 64, "duration_s": 1.0,
    }]}
    with tarfile.open(archive, "w:gz") as handle:
        for name, data in (
            ("results/manifest.json", json.dumps(manifest).encode()),
            ("results/a/vocals.wav", b"tampered"),
        ):
            info = tarfile.TarInfo(name); info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
    with pytest.raises(RuntimeError, match="stem SHA-256 mismatch"):
        import_results(golden, cache, archive)
