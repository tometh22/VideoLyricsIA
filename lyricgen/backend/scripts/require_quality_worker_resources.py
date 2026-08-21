#!/usr/bin/env python3
"""Fail closed when the isolated quality worker lacks bounded cgroup limits."""
from __future__ import annotations

import os
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def detected_limits() -> tuple[float | None, int | None]:
    cpu_raw = _read("/sys/fs/cgroup/cpu.max")
    memory_raw = _read("/sys/fs/cgroup/memory.max")
    cpus = None
    memory = None
    if cpu_raw:
        quota, _, period = cpu_raw.partition(" ")
        if quota != "max" and period:
            cpus = int(quota) / int(period)
    else:  # cgroup v1
        quota = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota and period and int(quota) > 0:
            cpus = int(quota) / int(period)
    if memory_raw and memory_raw != "max":
        memory = int(memory_raw)
    else:
        legacy_memory = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if legacy_memory and int(legacy_memory) < 2**60:
            memory = int(legacy_memory)
    return cpus, memory


def validate_limits(cpus: float | None, memory_bytes: int | None,
                    *, max_cpus: float, max_memory_bytes: int) -> list[str]:
    errors = []
    if cpus is None:
        errors.append("cpu_limit_unavailable")
    elif cpus > max_cpus + 1e-9:
        errors.append(f"cpu_limit_{cpus:g}_exceeds_{max_cpus:g}")
    if memory_bytes is None:
        errors.append("memory_limit_unavailable")
    elif memory_bytes > max_memory_bytes:
        errors.append(f"memory_limit_{memory_bytes}_exceeds_{max_memory_bytes}")
    return errors


def main() -> int:
    max_cpus = float(os.environ.get("QUALITY_WORKER_MAX_CPUS", "2"))
    max_memory_bytes = int(os.environ.get("QUALITY_WORKER_MAX_MEMORY_BYTES", "4294967296"))
    errors = validate_limits(
        *detected_limits(), max_cpus=max_cpus, max_memory_bytes=max_memory_bytes,
    )
    if errors:
        print("[QUALITY-WORKER][RESOURCE-GATE] " + ",".join(errors))
        return 1
    print(
        "[QUALITY-WORKER][RESOURCE-GATE] bounded "
        f"max_cpus={max_cpus:g} max_memory_bytes={max_memory_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
