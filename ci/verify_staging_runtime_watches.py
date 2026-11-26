"""Read-only staging release gate for the SHA-bound API/worker watch contract."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "8b4db4c9-5955-40cf-aa4a-14e2a04996ca"
STAGING = "1e56ca0d-3b71-4c78-849b-a51f9ca00847"
CONFIGS = {"api": "api", "Worker": "worker", "ShortWorker": "short-worker",
           "BatchWorker": "batch-worker", "BatchShortWorker": "batch-short-worker",
           "quality-worker": "quality-worker"}
BATCH = {"BatchWorker", "BatchShortWorker"}


def verify(records, expected, phase="before", release_sha=None):
    """Pre-merge checks persisted triggers; post-rollout checks actual manifests too."""
    problems = []
    if set(records) != set(CONFIGS):
        return ["Expected exactly API and five worker service records"]
    for name, stem in CONFIGS.items():
        record = records[name]
        path = record.get("railwayConfigFile")
        if name in BATCH:
            if path is not None:
                problems.append(f"{name}: unexpected config-file binding")
            if record.get("watchPatterns") != expected:
                problems.append(f"{name}: persisted watch patterns drifted")
        elif path != f"/railway/{stem}.toml":
            problems.append(f"{name}: expected TOML binding missing")
        if phase == "after":
            deployment = record.get("latestDeployment") or {}
            meta = deployment.get("meta") or {}
            watch = ((meta.get("serviceManifest") or {}).get("build") or {}).get("watchPatterns")
            if not release_sha or meta.get("commitHash") != release_sha:
                problems.append(f"{name}: deployed release differs")
            if deployment.get("status") != "SUCCESS":
                problems.append(f"{name}: deployment is not successful")
            if watch != expected:
                problems.append(f"{name}: deployed watch patterns differ")
    return problems


def command(*args):
    return json.loads(subprocess.check_output(args, text=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["before", "after"], default="before")
    parser.add_argument("--release-sha")
    args = parser.parse_args()
    if args.phase == "after" and not args.release_sha:
        parser.error("--release-sha is required after rollout")
    expected = None
    for stem in CONFIGS.values():
        with (ROOT / "railway" / f"{stem}.toml").open("rb") as f:
            patterns = tomllib.load(f)["build"]["watchPatterns"]
        if expected is None:
            expected = patterns
        elif patterns != expected:
            raise SystemExit("Repository cohort watch lists differ")
    status = command("railway", "status", "--json")
    if status["id"] != PROJECT:
        raise SystemExit("Wrong Railway project")
    environment = next((e["node"] for e in status["environments"]["edges"]
                        if e["node"]["id"] == STAGING and e["node"]["name"] == "staging"), None)
    if environment is None:
        raise SystemExit("Expected staging environment missing")
    services = {s["node"]["name"]: s["node"]["id"] for s in status["services"]["edges"]}
    fields = "serviceName railwayConfigFile watchPatterns latestDeployment{id status meta}"
    query = "query{" + " ".join(
        f's{i}:serviceInstance(environmentId:"{STAGING}",serviceId:"{services[name]}")'
        f'{{{fields}}}' for i, name in enumerate(CONFIGS)) + "}"
    result = command("railway", "api", query, "--compact")
    if result.get("errors"):
        raise SystemExit("Railway read failed; do not assume configuration matches")
    records = {v["serviceName"]: v for v in result["data"].values()}
    problems = verify(records, expected, args.phase, args.release_sha)
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"staging {args.phase}: six-service watch contract verified (read only)")
    if args.phase == "before":
        print("Running manifests may still have old filters; post-rollout verification remains required.")


if __name__ == "__main__":
    main()
