#!/usr/bin/env python3
"""Probe lrclib hit-rate baseline vs LRCLIB_COVERAGE_BOOST=1.

Why this exists: the bench harness (run_pipeline_local.py + score_benchmark.py)
mide calidad final pero asume que lrclib ya respondió. Esta probe mide la
pregunta upstream: ¿cuántos (artist, song) inputs disparan un hit synced
en lrclib hoy, y cuántos más bajo el flag de coverage boost?

Output:
    Tabla por caso: hit baseline / hit boost / synced baseline / synced boost
    Totales: hit-rate + synced-rate, baseline → boost

Usage:
    cd lyricgen/backend
    source venv/bin/activate
    python scripts/probe_lrclib_coverage.py
    python scripts/probe_lrclib_coverage.py --list custom_pairs.tsv
    # Cada línea de --list: artist <TAB> song
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# Casos canónicos: mezclan (a) baseline limpio del dataset bench, (b) typos
# comunes que un operador podría escribir, (c) titulos con noise (paren/Live/
# Remix/feat), y (d) UMG-relevant en castellano latino para validar cobertura
# del catálogo target.
DEFAULT_CASES: list[tuple[str, str, str]] = [
    # (label, artist, song)
    # ── (a) Dataset bench, control: deberían ya hitear ──
    ("bench/clean   ", "Viejas Locas",       "Aunque a nadie ya le importe"),
    ("bench/clean   ", "Bersuit Vergarabat", "Un Pacto Live In Buenos Aires  2001"),
    ("bench/clean   ", "Los Pericos",        "Runaway"),
    ("bench/clean   ", "Rata Blanca",        "Mujer amante"),
    ("bench/clean   ", "Abuelos de la nada", "Mil Horas"),
    ("bench/clean   ", "Babasonicos",        "Deshoras"),
    # ── (b) Typos / casing ──
    ("typo/case     ", "viejas locas",       "aunque a nadie ya le importe"),
    ("typo/accent   ", "Babasónicos",        "Deshoras"),                     # con acento
    ("typo/accent   ", "Abuelos de la Nada", "Mil horas"),                    # mixed case
    ("typo/abbrev   ", "Bersuit",            "Un Pacto"),                     # primer token + clean
    # ── (c) Títulos con noise ──
    ("noise/paren   ", "Viejas Locas",       "Aunque a nadie ya le importe (Remix)"),
    ("noise/live    ", "Bersuit Vergarabat", "Un Pacto (Live)"),
    ("noise/remstrd ", "Babasonicos",        "Deshoras - Remastered 2009"),
    ("noise/feat    ", "Karol G",            "Tusa (feat. Nicki Minaj)"),
    # ── (d) UMG-relevant latino ──
    ("umg/karolg    ", "Karol G",            "Provenza"),
    ("umg/badbunny  ", "Bad Bunny",          "Tití Me Preguntó"),
    ("umg/jbalvin   ", "J Balvin",           "Mi Gente"),
    ("umg/shakira   ", "Shakira",            "Hips Don't Lie"),
    # ── (e) Edge / no-match expected ──
    ("edge/garbage  ", "asdfqwer",           "no existe esta cancion"),
]


def _probe_one(artist: str, song: str, boost: bool) -> dict:
    """Reload pipeline module so env-flag changes take effect, then fetch."""
    if boost:
        os.environ["LRCLIB_COVERAGE_BOOST"] = "1"
    else:
        os.environ.pop("LRCLIB_COVERAGE_BOOST", None)
    # Importing inside the function keeps module-level state from caching the
    # env flag. _env_flag reads os.environ at call time, so re-import isn't
    # strictly needed — but doing it once at top is fine.
    from pipeline import _fetch_lrclib
    t0 = time.time()
    res = _fetch_lrclib(artist, song, db=None)
    elapsed = time.time() - t0
    hit = res is not None
    synced = bool(res and res.get("synced"))
    plain = bool(res and res.get("plain"))
    return {"hit": hit, "synced": synced, "plain": plain, "elapsed": elapsed}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", type=Path, default=None,
                   help="TSV file: artist\\tsong per line (default: DEFAULT_CASES)")
    args = p.parse_args()

    if args.list:
        cases = []
        for raw in args.list.read_text().splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("\t")
            if len(parts) < 2:
                continue
            cases.append(("custom        ", parts[0].strip(), parts[1].strip()))
    else:
        cases = DEFAULT_CASES

    print(f"Probing {len(cases)} case(s) against lrclib.net "
          f"(baseline vs LRCLIB_COVERAGE_BOOST=1)\n")
    print(f"{'label':16s} {'artist':22s} {'song':40s} | "
          f"{'base':6s} {'boost':6s} | {'base s':7s} {'boost s':7s} | result")
    print("-" * 120)

    n = len(cases)
    base_hits = base_synced = 0
    boost_hits = boost_synced = 0
    upgrade_count = 0
    rescue_count = 0

    for label, artist, song in cases:
        b = _probe_one(artist, song, boost=False)
        z = _probe_one(artist, song, boost=True)
        base_hits += int(b["hit"])
        base_synced += int(b["synced"])
        boost_hits += int(z["hit"])
        boost_synced += int(z["synced"])

        # detect upgrade (plain→synced) vs rescue (None→hit)
        marker = ""
        if not b["hit"] and z["hit"]:
            marker = "  ← RESCUED"
            rescue_count += 1
        elif b["hit"] and not b["synced"] and z["synced"]:
            marker = "  ← UPGRADED plain→synced"
            upgrade_count += 1
        elif b["synced"] and not z["synced"]:
            marker = "  ⚠ regressed (boost dropped synced)"

        def _emoji(d):
            if d["synced"]: return "🟢S"
            if d["plain"]:  return "🟡P"
            return "❌- "

        print(f"{label} {artist[:22]:22s} {song[:40]:40s} | "
              f"{_emoji(b):6s} {_emoji(z):6s} | {b['elapsed']:5.2f}  {z['elapsed']:5.2f}  |{marker}")

    print()
    print(f"=== Hit rate (any result):       baseline {base_hits}/{n} ({100*base_hits/n:.0f}%) "
          f"→ boost {boost_hits}/{n} ({100*boost_hits/n:.0f}%)  "
          f"Δ +{boost_hits - base_hits}")
    print(f"=== Synced rate (timing avail):  baseline {base_synced}/{n} ({100*base_synced/n:.0f}%) "
          f"→ boost {boost_synced}/{n} ({100*boost_synced/n:.0f}%)  "
          f"Δ +{boost_synced - base_synced}")
    print(f"=== Upgrades (plain→synced):     {upgrade_count}")
    print(f"=== Rescues  (no-hit→hit):       {rescue_count}")


if __name__ == "__main__":
    main()
