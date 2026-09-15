#!/usr/bin/env python3
"""Comparación pareada offline: motor de fondo viejo vs anclado.

Corre SÓLO el paso de texto (extracción de anclas + composición del prompt)
sobre canciones reales, sin tocar Veo ni Imagen: el objetivo es medir si el
prompt sale de la canción, y eso se ve en el texto. Un fondo cuesta USD 0,20-0,80;
un prompt cuesta ~USD 0,0006. Por eso esta comparación se puede correr sobre
decenas de canciones antes de prender el flag en ningún entorno.

Métricas que reporta, con el baseline medido sobre 269 prompts entregados en
staging (modo letra, 60 días) para comparar:

    golden hour / atardecer ...... 59%   → objetivo < 20%
    niebla / bruma ............... 29%   → objetivo < 10%
    callejón ..................... 14,5% → informativo (puede ser correcto)
    cobertura de anclas .......... n/a   → objetivo ≥ 4 de las extraídas
    largo del prompt ............. 80-120 palabras → objetivo 260-360

Uso:
    # desde una base de datos (jobs con letra real, modo letra, sin hint)
    DATABASE_URL=... python3 scripts/compare_lyric_anchors_offline.py --limit 20

    # o desde un JSON [{"artist","song_title","lyrics","genre","concept"}, ...]
    python3 scripts/compare_lyric_anchors_offline.py --input canciones.json

Escribe un JSON con el detalle por canción y un resumen por consola. No escribe
en la base ni encola nada.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Motivos medidos como sangrado del bloque de ejemplos del prompt viejo.
MOTIVOS = {
    "atardecer": r"golden hour|\bdusk\b|\btwilight\b|sunset|atardecer|crep[uú]sculo",
    "niebla": r"\bmist|\bfog|\bhaze|niebla|bruma|neblina",
    "callejon": r"\balley\b|\bcallej[oó]n\b|rain-slicked|wet pavement|\bgraffiti\b|fire escape",
    "motas": r"dust motes|motas de polvo",
    "desierto": r"\bdesert\b|desierto",
}


def _cargar_desde_db(limit: int) -> list[dict]:
    from sqlalchemy import create_engine, text
    url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if not url:
        raise SystemExit("falta DATABASE_URL (o DATABASE_PUBLIC_URL)")
    engine = create_engine(url.replace("postgres://", "postgresql://", 1))
    sql = text("""
        SELECT job_id, artist, song_title, segments_json,
               COALESCE(render_params->>'genre','')   AS genre,
               COALESCE(render_params->>'concept','') AS concept,
               COALESCE(render_params->>'movement_style','') AS movement_style
        FROM jobs
        WHERE render_params->>'match_lyrics' = 'true'
          AND COALESCE(render_params->>'background_hint','') = ''
          AND segments_json IS NOT NULL
          AND length(segments_json::text) > 20000
        ORDER BY created_at DESC
        LIMIT :limit
    """)
    filas = []
    with engine.connect() as conn:
        for row in conn.execute(sql, {"limit": limit}).mappings():
            segs = row["segments_json"]
            if isinstance(segs, str):
                segs = json.loads(segs)
            letra = " ".join(
                str(s.get("text") or "") for s in (segs or []) if isinstance(s, dict)
            ).strip()
            if len(letra) < 200:
                continue  # smoke tests y jobs sin letra real
            filas.append({
                "job_id": row["job_id"], "artist": row["artist"] or "",
                "song_title": row["song_title"] or "", "lyrics": letra,
                "genre": row["genre"], "concept": row["concept"],
                "movement_style": row["movement_style"],
            })
    return filas


def _motivos(texto: str) -> list[str]:
    t = texto or ""
    return [k for k, patron in MOTIVOS.items() if re.search(patron, t, re.IGNORECASE)]


def _correr(cancion: dict, modo: str) -> dict:
    """Genera el prompt para una canción bajo un modo dado."""
    os.environ["BG_LYRIC_ANCHORS"] = modo
    import importlib
    import lyric_anchors
    importlib.reload(lyric_anchors)
    import pipeline
    pipeline.lyric_anchors = lyric_anchors

    anclas = None
    if modo == "on":
        anclas = pipeline._extract_lyric_anchors(
            cancion["lyrics"], cancion["artist"], cancion["song_title"], job_id=None,
        )
    res = pipeline._get_unique_prompt(
        lyrics_text=cancion["lyrics"], artist=cancion["artist"],
        song_title=cancion["song_title"], genre=cancion.get("genre", ""),
        concept=cancion.get("concept", ""),
        movement_style=cancion.get("movement_style", ""),
        match_lyrics=True, anchors=anclas,
    )
    prompt = res.get("prompt") or ""
    salida = {
        "modo": modo,
        "prompt": prompt,
        "palabras": len(prompt.split()),
        "motivos": _motivos(prompt),
        "negativos": len(res.get("negatives") or []),
        "anclas": lyric_anchors.anchor_terms(anclas) if anclas else [],
    }
    if anclas:
        salida["cobertura"] = lyric_anchors.anchor_coverage(prompt, anclas)
        # Métrica que mira el sello: ¿la escena transcurre en un lugar que sale
        # de la letra, y esa afirmación es rastreable hasta una línea del texto?
        salida["lugar"] = lyric_anchors.names_place_from_lyrics(prompt, anclas)
    return salida


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--input", help="JSON con las canciones (evita la DB)")
    ap.add_argument("--out", default="anchors-comparison.json")
    args = ap.parse_args()

    canciones = (json.load(open(args.input)) if args.input
                 else _cargar_desde_db(args.limit))
    if not canciones:
        print("no hay canciones con letra real para comparar")
        return 1
    print(f"comparando {len(canciones)} canciones (sin generar ningún video)\n")

    resultados = []
    for i, c in enumerate(canciones, 1):
        etiqueta = f"{c['artist']} — {c['song_title']}"
        print(f"[{i}/{len(canciones)}] {etiqueta}")
        fila = {"cancion": etiqueta, "job_id": c.get("job_id")}
        for modo in ("off", "on"):
            try:
                fila[modo] = _correr(c, modo)
            except Exception as e:  # noqa: BLE001
                fila[modo] = {"error": f"{type(e).__name__}: {e}"}
        cov = (fila.get("on") or {}).get("cobertura") or {}
        print(f"    viejo: {fila['off'].get('palabras', 0):>3} palabras "
              f"{fila['off'].get('motivos', [])}")
        print(f"    nuevo: {fila['on'].get('palabras', 0):>3} palabras "
              f"{fila['on'].get('motivos', [])} "
              f"anclas {cov.get('covered', 0)}/{cov.get('total', 0)}")
        resultados.append(fila)

    json.dump(resultados, open(args.out, "w"), indent=1, ensure_ascii=False)

    def _tasa(modo: str, motivo: str) -> str:
        vivos = [r for r in resultados if "motivos" in (r.get(modo) or {})]
        if not vivos:
            return "n/a"
        n = sum(1 for r in vivos if motivo in r[modo]["motivos"])
        return f"{100 * n / len(vivos):.0f}% ({n}/{len(vivos)})"

    print("\n" + "=" * 64)
    print(f"{'motivo':12} {'viejo':>16} {'nuevo':>16}   {'baseline staging'}")
    baseline = {"atardecer": "59%", "niebla": "29%", "callejon": "14,5%",
                "motas": "6,7%", "desierto": "10,5%"}
    for motivo in MOTIVOS:
        print(f"{motivo:12} {_tasa('off', motivo):>16} {_tasa('on', motivo):>16}"
              f"   {baseline.get(motivo, ''):>6}")

    def _prom(modo: str, campo: str) -> float:
        vals = [(r.get(modo) or {}).get(campo, 0) for r in resultados
                if campo in (r.get(modo) or {})]
        return sum(vals) / len(vals) if vals else 0.0

    print(f"\npalabras promedio  viejo {_prom('off', 'palabras'):.0f}  "
          f"nuevo {_prom('on', 'palabras'):.0f}   (objetivo 260-360)")
    covs = [(r.get("on") or {}).get("cobertura") for r in resultados]
    covs = [c for c in covs if c]
    if covs:
        prom = sum(c["covered"] for c in covs) / len(covs)
        suf = sum(1 for c in covs if c["covered"] >= min(4, c["total"]))
        print(f"cobertura de anclas  {prom:.1f} promedio   "
              f"{suf}/{len(covs)} canciones con cobertura suficiente")
    print(f"negativos por canción  {_prom('on', 'negativos'):.1f}")

    lugares = [(r.get("on") or {}).get("lugar") for r in resultados]
    lugares = [x for x in lugares if x]
    if lugares:
        con_lugar = [x for x in lugares if x["names_place"]]
        citados = [x for x in con_lugar if x.get("citado")]
        print(f"\nnombra un lugar concreto de la letra   "
              f"{100 * len(con_lugar) / len(lugares):.0f}% "
              f"({len(con_lugar)}/{len(lugares)})")
        print(f"  de esos, con la línea citada         "
              f"{len(citados)}/{len(con_lugar)}")
        sin = [x for x in lugares if not x["names_place"]]
        for x in sin[:4]:
            print(f"  · {x['reason']}"
                  + (f" ({x['lugar']})" if x.get("lugar") else ""))

    # Exposición a `enforce_text`: cuántas escenas legítimas piden una
    # superficie que podría llevar texto. Es el número que decide si poner el
    # validador de texto en bloqueo es barato o caro.
    import bg_frame_checks
    for modo, etiqueta in (("off", "motor viejo"), ("on", "anclado")):
        prompts = [(r.get(modo) or {}).get("prompt") for r in resultados]
        exp = bg_frame_checks.signage_exposure([p for p in prompts if p])
        if exp["total"]:
            print(f"\nescenas con cartelería ({etiqueta})   "
                  f"{100 * exp['ratio']:.0f}% ({exp['with_signage']}/{exp['total']})")
            if exp["terms"]:
                top = ", ".join(f"{k}×{v}" for k, v in list(exp["terms"].items())[:6])
                print(f"  términos: {top}")
    print(f"\ndetalle -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
