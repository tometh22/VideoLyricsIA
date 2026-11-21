"""Isolated Linux/cgroup-v2 benchmark; synthetic file I/O, not app load.

Run in a disposable container with >= 3 GiB RAM and >= 2 GiB free disk.
No application imports, credentials, database, R2 or network are used.
JSON lines compare retained memory, peak memory and reread latency using
the shipped helper. All files are temporary and SHA-256 verified.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_page_cache import release_file_page_cache, snapshot_file_identity

CGROUP = Path("/sys/fs/cgroup")
MIB = 1024 * 1024


def memory():
    stats = dict(line.split() for line in (CGROUP / "memory.stat").read_text().splitlines())
    return {
        "current": int((CGROUP / "memory.current").read_text()),
        "file": int(stats["file"]),
        "anon": int(stats["anon"]),
    }


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            result.update(chunk)
    return result.hexdigest()


def run_case(enabled, client_mib, batch_mib):
    with tempfile.TemporaryDirectory(prefix="prores-cache-bench-") as directory:
        before = memory()
        samples = [before]
        stop = threading.Event()
        barrier = threading.Barrier(7, timeout=30)

        def sample():
            while not stop.wait(0.05):
                samples.append(memory())

        def work(index):
            path = Path(directory) / f"synthetic-{index}.mov"
            block = bytes([index + 1]) * MIB
            expected = hashlib.sha256()
            barrier.wait()
            with path.open("wb") as handle:
                for _ in range(batch_mib if index == 6 else client_mib):
                    handle.write(block)
                    expected.update(block)
                handle.flush()
                os.fsync(handle.fileno())
            identity = snapshot_file_identity(str(path))
            # A bounded read simulates an uploader consuming the complete file.
            if digest(path) != expected.hexdigest():
                raise RuntimeError("Initial content verification failed")
            advised = release_file_page_cache(str(path), identity) if enabled else 0
            return path, expected.hexdigest(), advised

        sampler = threading.Thread(target=sample)
        sampler.start()
        started = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=7) as pool:
                results = list(pool.map(work, range(7)))
            elapsed = time.monotonic() - started
            time.sleep(0.4)
            retained = memory()
        finally:
            stop.set()
            sampler.join()
        reread_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=7) as pool:
            actual = list(pool.map(digest, [path for path, _, _ in results]))
        if actual != [expected for _, expected, _ in results]:
            raise RuntimeError("Post-advice content verification failed")
        return {
            "enabled": enabled,
            "operations": 7,
            "file_bytes": (6 * client_mib + batch_mib) * MIB,
            "before": before,
            "peak_bytes": max(item["current"] for item in samples + [retained]),
            "retained": retained,
            "seconds": round(elapsed, 3),
            "reread_seconds": round(time.monotonic() - reread_start, 3),
            "advised_bytes": sum(count for _, _, count in results),
            "verified_files": len(results),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-mib", type=int, default=128)
    parser.add_argument("--batch-mib", type=int, default=512)
    parser.add_argument("--rounds", type=int, default=2)
    args = parser.parse_args()
    if min(args.client_mib, args.batch_mib, args.rounds) < 1:
        parser.error("Sizes and rounds must be positive")
    if sys.platform != "linux" or not (CGROUP / "memory.current").exists():
        parser.error("Run inside an isolated Linux container with cgroup v2")
    if shutil.disk_usage(tempfile.gettempdir()).free < (6 * args.client_mib + args.batch_mib) * MIB * 1.2:
        parser.error("Insufficient temporary disk space")
    for round_index in range(args.rounds):
        # Reverse the order each round to expose warm-up/order effects.
        for enabled in ([False, True] if round_index % 2 == 0 else [True, False]):
            print(json.dumps({"round": round_index + 1, **run_case(
                enabled, args.client_mib, args.batch_mib,
            )}), flush=True)


if __name__ == "__main__":
    main()
