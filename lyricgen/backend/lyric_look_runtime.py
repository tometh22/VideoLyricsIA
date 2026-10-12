"""Render-time resolution of a job's lyric look (genre + variant + overrides).

Keeps pipeline.py thin: the pipeline builds `opts` once per job with
`prepare_opts`, persists what it decided (detected genre, concrete variant,
signature) and hands `opts` to every render (master, short, edit). Each render
calls `resolve_for_render`, which is pure given `opts` (+ the background
brightness, which only tunes the grade).

opts keys: genre, genre_detected, variant, seed_key, font_main, font_key, ink,
alt, accent.
"""
from __future__ import annotations

import logging
import subprocess

import lyric_looks as L
import lyric_recipes as R

logger = logging.getLogger("genly.lyric_look_runtime")

OPT_FIELDS = ("lyric_genre", "lyric_variant", "lyric_font_main", "lyric_font_key",
              "lyric_alt_color", "lyric_accent_color")


def normalize_variant(value) -> int | None:
    """int ≥ 0, or None for "automatic"."""
    if value is None or str(value).strip() == "":
        return None
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 9999 else None


def clean_fields(**kw) -> dict:
    """Validated lyric-variant fields; empty/unknown values dropped."""
    out = {}
    g = R.normalize_genre(kw.get("lyric_genre"))
    if g:
        out["lyric_genre"] = g
    v = normalize_variant(kw.get("lyric_variant"))
    if v is not None:
        out["lyric_variant"] = v
    for k in ("lyric_font_main", "lyric_font_key"):
        f = str(kw.get(k) or "").strip()
        if f in R.FONTS:
            out[k] = f
    for k in ("lyric_alt_color", "lyric_accent_color"):
        c = R._hex_or_empty(kw.get(k))
        if c:
            out[k] = c
    return out


def sibling_signatures(job_id: str, lyric_look: str) -> set:
    """Recipe signatures already used by sibling songs with the same look:
    same campaign, else same artist in the same tenant (recent 300)."""
    try:
        from database import SessionLocal, Job
        db = SessionLocal()
        try:
            me = db.query(Job).filter(Job.job_id == job_id).first()
            if me is None:
                return set()
            q = db.query(Job.render_params).filter(Job.job_id != job_id,
                                                   Job.tenant_id == me.tenant_id)
            if getattr(me, "campaign_id", None):
                q = q.filter(Job.campaign_id == me.campaign_id)
            else:
                q = q.filter(Job.artist == me.artist)
            sigs = set()
            for (rp,) in q.order_by(Job.created_at.desc()).limit(300):
                rp = rp or {}
                if rp.get("lyric_look") == lyric_look and rp.get("lyric_variant_sig"):
                    sigs.add(tuple(tuple(x) if isinstance(x, list) else x
                                   for x in rp["lyric_variant_sig"]))
            return sigs
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("[LOOK] sibling lookup failed for %s: %s", job_id, exc)
        return set()


def _sig_json(sig: tuple) -> list:
    return [list(x) if isinstance(x, tuple) else x for x in sig]


def prepare_opts(*, job_id: str, lyric_look: str, artist: str = "", song_title: str = "",
                 lyrics_text: str = "", render_params: dict | None = None,
                 detect_genre: bool = True) -> tuple[dict, dict]:
    """(opts, render_params_update) for a job about to render with a look.

    - operator genre wins; else a previously detected one; else detect now.
    - operator variant wins; else a previously chosen one; else choose the
      first variant not used by sibling songs.
    """
    rp = dict(render_params or {})
    if not L.get_look(lyric_look):
        return {}, {}
    genre = R.normalize_genre(rp.get("lyric_genre"))
    detected = R.normalize_genre(rp.get("lyric_genre_detected"))
    update = {}
    if not genre and not detected and detect_genre:
        try:
            import genre_detect
            detected, src = genre_detect.detect(artist, song_title, lyrics_text, job_id=job_id)
            if detected:
                update["lyric_genre_detected"] = detected
        except Exception as exc:  # pragma: no cover
            logger.warning("[LOOK] genre detection failed for %s: %s", job_id, exc)
    eff_genre = genre or detected
    variant = normalize_variant(rp.get("lyric_variant"))
    if variant is None:
        variant = R.choose_auto_variant(lyric_look, eff_genre, job_id,
                                        sibling_signatures(job_id, lyric_look))
        update["lyric_variant"] = variant
    rec = R.recipe_for(lyric_look, eff_genre, job_id, variant)
    update["lyric_variant_sig"] = _sig_json(rec.signature)
    opts = {
        "genre": eff_genre, "variant": variant, "seed_key": job_id,
        "font_main": rp.get("lyric_font_main") or "", "font_key": rp.get("lyric_font_key") or "",
        "ink": rp.get("lyric_color") or "", "alt": rp.get("lyric_alt_color") or "",
        "accent": rp.get("lyric_accent_color") or "",
    }
    return opts, update


def bg_luma(path: str | None) -> float | None:
    """Mean brightness (0..1) of one background frame; None on failure."""
    if not path:
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", "1", "-i", path, "-frames:v", "1",
             "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"],
            capture_output=True, timeout=30).stdout
        return (sum(out) / (len(out) * 255.0)) if out else None
    except Exception:
        return None


def resolve_for_render(lyric_look: str, opts: dict | None, *, bg_path: str | None = None):
    """The Look to render (None when there is no look)."""
    if not L.get_look(lyric_look):
        return None
    opts = opts or {}
    look, _info = R.resolve(
        lyric_look, genre=opts.get("genre", ""), variant=opts.get("variant") or 0,
        seed_key=opts.get("seed_key", ""),
        overrides={"font_main": opts.get("font_main", ""), "font_key": opts.get("font_key", ""),
                   "ink": opts.get("ink", ""), "alt": opts.get("alt", ""),
                   "accent": opts.get("accent", "")},
        bg_luma=bg_luma(bg_path))
    return look


def owns_background(lyric_look: str, opts: dict | None) -> bool:
    look = resolve_for_render(lyric_look, opts)
    return look is not None and look.background in ("flat", "gradient")
