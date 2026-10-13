#!/usr/bin/env python3
"""Turn the pipeline-config variables of a Railway environment into SHARED variables.

Every service computes a runtime token from the same pipeline/calibration/timing
environment; if one service drifts, quality jobs are silently discarded and /health
reports `fleet_runtime_token_mismatch`. Keeping each value in ONE shared variable that
the services reference (`${{shared.KEY}}`) makes drift structurally impossible.

Safe by construction:
  * dry run by default; `--apply` is required to write anything;
  * a key is only shared when every service that sets it holds the SAME value;
  * writes use skipDeploys: nothing restarts, and since each reference resolves to the
    value the service already has, a later deploy changes nothing;
  * after writing, the rendered value of every key is compared with the previous one
    and any difference is reported (exit 1) so it can be reverted before a deploy.

    python lyricgen/backend/scripts/railway_share_pipeline_config.py staging api Worker ShortWorker
    python lyricgen/backend/scripts/railway_share_pipeline_config.py production --apply

Revert one key: set the literal value on the service again (railway variables --set K=V).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://backboard.railway.com/graphql/v2"
DEFAULT_SERVICES = {
    "production": ("api", "Worker", "ShortWorker", "quality-worker"),
    "staging": ("api", "Worker", "ShortWorker", "quality-worker", "BatchWorker", "BatchShortWorker"),
}
TIMING_KEYS = ("LYRIC_LEAD_IN_S", "LYRIC_HOLD_S", "LYRIC_LEAD_IN_MS", "STABLE_PITCH_TAIL_ENABLED",
               "LYRIC_MIN_GAP_MS", "LYRIC_MIN_GAP_AB_ENABLED", "LYRIC_MIN_GAP_AB_ARM_B_MS")
_REF = "${{shared.%s}}"


def config_keys() -> tuple[str, ...]:
    from transcription_quality import _PIPELINE_CONFIG_KEYS
    calibration = (
        "TRANSCRIPTION_QUALITY_CALIBRATION_ID", "TRANSCRIPTION_QUALITY_CALIBRATION_POLICY",
        "TRANSCRIPTION_QUALITY_CALIBRATION_CONFIG_FINGERPRINT", "TRANSCRIPTION_QUALITY_CALIBRATED",
        "TRANSCRIPTION_QUALITY_RELEASE_REPORT_PATH", "TRANSCRIPTION_QUALITY_RELEASE_REPORT_SHA256",
        "TRANSCRIPTION_QUALITY_BENCHMARK_MANIFEST_PATH",
    )
    return tuple(dict.fromkeys((*_PIPELINE_CONFIG_KEYS, *calibration, *TIMING_KEYS)))


def plan(raw: dict[str, dict[str, str]], keys) -> tuple[dict[str, str], dict[str, list[str]], list[str]]:
    """(shared values, {service: keys to reference}, problems). `raw` holds UNRENDERED values."""
    shared: dict[str, str] = {}
    refs: dict[str, list[str]] = {service: [] for service in raw}
    problems: list[str] = []
    for key in keys:
        holders = {s: v[key] for s, v in raw.items() if v.get(key, "") != ""}
        if not holders:
            continue
        literals = {s: v for s, v in holders.items() if not v.startswith("${{")}
        if len(set(literals.values())) > 1:
            problems.append(f"{key}: valores distintos {literals}")
            continue
        if not literals:           # already all references
            continue
        if len(holders) != len(raw):
            problems.append(f"{key}: lo tienen {sorted(holders)} pero no todos los servicios")
            continue
        shared[key] = next(iter(literals.values()))
        for service, value in literals.items():
            refs[service].append(key)
    return shared, refs, problems


def diff_rendered(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]], keys) -> list[str]:
    return [f"{service}.{key}: {before[service].get(key, '')!r} -> {after[service].get(key, '')!r}"
            for service in before for key in keys
            if before[service].get(key, "") != after[service].get(key, "")]


class Railway:
    def __init__(self, environment: str):
        cfg = json.load(open(os.path.expanduser("~/.railway/config.json")))
        self.token = (cfg.get("user") or {}).get("accessToken") or cfg.get("accessToken")
        status = json.loads(subprocess.check_output(["railway", "status", "--json"]))
        self.project = status["id"]
        self.env = {e["node"]["name"]: e["node"]["id"] for e in status["environments"]["edges"]}[environment]
        self.services = {s["node"]["name"]: s["node"]["id"] for s in status["services"]["edges"]}

    def gql(self, query: str, variables: dict):
        request = urllib.request.Request(
            API, data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "User-Agent": "railway-cli"})
        body = None
        for attempt in range(4):      # a network blip must never leave a half-applied change
            try:
                body = json.load(urllib.request.urlopen(request, timeout=60))
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
        if body.get("errors"):
            raise RuntimeError(body["errors"][0]["message"])
        return body["data"]

    def read(self, service: str, *, rendered: bool) -> dict[str, str]:
        if rendered:
            data = self.gql("query($p:String!,$e:String!,$s:String!){variablesForServiceDeployment(projectId:$p,environmentId:$e,serviceId:$s)}",
                            {"p": self.project, "e": self.env, "s": self.services[service]})
            return data["variablesForServiceDeployment"]
        data = self.gql("query($p:String!,$e:String!,$s:String!){variables(projectId:$p,environmentId:$e,serviceId:$s,unrendered:true)}",
                        {"p": self.project, "e": self.env, "s": self.services[service]})
        return data["variables"]

    def upsert(self, variables: dict[str, str], service: str | None = None) -> None:
        payload = {"projectId": self.project, "environmentId": self.env, "variables": variables, "skipDeploys": True}
        if service:
            payload["serviceId"] = self.services[service]
        self.gql("mutation($i:VariableCollectionUpsertInput!){variableCollectionUpsert(input:$i)}", {"i": payload})


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    apply = "--apply" in argv
    environment = args[0] if args else "production"
    services = tuple(args[1:]) or DEFAULT_SERVICES.get(environment, ())
    rw = Railway(environment)
    keys = config_keys()
    raw = {s: rw.read(s, rendered=False) for s in services}
    before = {s: rw.read(s, rendered=True) for s in services}
    shared, refs, problems = plan(raw, keys)
    print(f"{environment}: {len(shared)} claves a compartir entre {', '.join(services)}")
    for problem in problems:
        print("  PROBLEMA:", problem)
    for service in services:
        print(f"  {service}: {len(refs[service])} referencias")
    if problems:
        print("No se aplica nada: resolver los problemas primero.")
        return 1
    if not apply:
        print("Simulación. Agregá --apply para escribir.")
        return 0
    rw.upsert(shared)
    for service in services:
        if refs[service]:
            rw.upsert({k: _REF % k for k in refs[service]}, service)
    after = {s: rw.read(s, rendered=True) for s in services}
    changed = diff_rendered(before, after, keys)
    if changed:
        print("ATENCIÓN: cambió el valor resuelto de:")
        for line in changed:
            print("  ", line)
        return 1
    print(f"OK: {len(shared)} variables compartidas; el valor resuelto de cada servicio no cambió. Sin reinicios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
