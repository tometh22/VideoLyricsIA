#!/usr/bin/env python3
"""Fail CI for Python advisories outside the reviewed temporary baseline."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "security_exceptions.json"


def main() -> int:
    exceptions = json.loads(BASELINE.read_text(encoding="utf-8"))
    today = dt.date.today()
    allowed: dict[tuple[str, str], dict] = {}
    for item in exceptions["exceptions"]:
        expiry = dt.date.fromisoformat(item["expires"])
        if expiry < today:
            print(f"Expired security exception: {item['package']} {item['id']}")
            return 2
        allowed[(item["package"].lower(), item["id"])] = item

    with tempfile.NamedTemporaryFile(suffix=".json") as output:
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "-r", str(ROOT / "requirements.txt"),
             "--format", "json", "--output", output.name],
            check=False,
        )
        output.seek(0)
        report = json.load(output)

    unexpected = []
    observed = set()
    for dependency in report.get("dependencies", []):
        package = dependency["name"].lower()
        for vuln in dependency.get("vulns", []):
            key = (package, vuln["id"])
            observed.add(key)
            if key not in allowed:
                unexpected.append(f"{package}=={dependency['version']} {vuln['id']}")

    if unexpected:
        print("Unreviewed Python dependency advisories:")
        print("\n".join(f"- {item}" for item in unexpected))
        return 1
    stale = sorted(set(allowed) - observed)
    if stale:
        print("Remove fixed/stale security exceptions:")
        print("\n".join(f"- {pkg} {vuln}" for pkg, vuln in stale))
        return 1
    print(f"Dependency audit passed with {len(observed)} reviewed temporary exceptions.")
    return 0 if result.returncode in (0, 1) else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
