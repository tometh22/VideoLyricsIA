/**
 * useLookVariant — variante resuelta de un look para el preview y el panel
 * "Variante" (contrato: .context/variants_contract.md).
 *
 *   - catálogo: GET /lyric-looks/catalog UNA vez por sesión (cache de
 *     módulo); mientras tanto, o si falla, el respaldo estático.
 *   - detección: POST /lyric-looks/detect-genre una vez por canción
 *     (artista|título), sólo con look activo y género "automático".
 *   - resolución: POST /lyric-looks/resolve con debounce (~250 ms) cada vez
 *     que cambia look / género / variante / overrides. Se conserva el último
 *     resultado bueno y se descartan las respuestas viejas (una respuesta
 *     lenta de un pedido anterior no pisa a la más nueva).
 *
 * Nada de esto bloquea la UI: cualquier falla deja el último resultado bueno
 * (o null) y el preview cae a los colores/fuentes fijos del look.
 */
import { useEffect, useRef, useState } from "react";
import {
  STATIC_LOOK_CATALOG, buildResolveBody, cachedDetectedGenre, cachedLookCatalog,
  LOOK_API, ensureFontLoaded, fetchDetectGenre, fetchLookCatalog, fetchResolveLook,
  lookAuthHeaders, normalizeLyricGenre,
} from "../lib/lyricLookVariants";
import { getLyricLook, normalizeLyricLook } from "../lib/lyricLooks";

export const RESOLVE_DEBOUNCE_MS = 250;

/** Catálogo (cacheado por sesión) con respaldo estático. */
export function useLookCatalog({ api = LOOK_API, authHeaders = lookAuthHeaders, enabled = true } = {}) {
  const [catalog, setCatalog] = useState(() => cachedLookCatalog() || STATIC_LOOK_CATALOG);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    fetchLookCatalog(api, authHeaders).then((c) => {
      if (alive && c) setCatalog(c);
    });
    return () => { alive = false; };
    // authHeaders es estable a nivel módulo en los callers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, enabled]);
  return catalog;
}

export function useLookVariant({
  look = "",
  genre = "",
  variant = "",
  fontMain = "",
  fontKey = "",
  color = "",
  altColor = "",
  accentColor = "",
  jobId = null,
  artist = "",
  songTitle = "",
  lyricsText = "",
  api = LOOK_API,
  authHeaders = lookAuthHeaders,
  enabled = true,
  debounceMs = RESOLVE_DEBOUNCE_MS,
} = {}) {
  const lookCode = normalizeLyricLook(look);
  const active = enabled && !!getLyricLook(lookCode);
  const catalog = useLookCatalog({ api, authHeaders, enabled });
  const operatorGenre = normalizeLyricGenre(genre);

  // ── Detección de género (una vez por canción) ────────────────────────
  const hasSong = !!(String(artist || "").trim() || String(songTitle || "").trim());
  const wantDetect = active && !operatorGenre && hasSong;
  const [detected, setDetected] = useState(() => (wantDetect ? cachedDetectedGenre(artist, songTitle) : null));
  // La letra puede llegar después (review): no re-dispara la detección,
  // que se cachea por artista|título.
  const lyricsRef = useRef(lyricsText);
  lyricsRef.current = lyricsText;
  useEffect(() => {
    if (!wantDetect) return undefined;
    let alive = true;
    const hit = cachedDetectedGenre(artist, songTitle);
    if (hit) {
      setDetected(hit);
      return undefined;
    }
    setDetected(null);
    fetchDetectGenre({ artist, songTitle, lyricsText: lyricsRef.current, jobId }, api, authHeaders)
      .then((res) => { if (alive) setDetected(res); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantDetect, artist, songTitle, api]);

  // ── Resolución (debounce + descarte de respuestas viejas) ────────────
  const [resolved, setResolved] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const seqRef = useRef(0);
  const detectedGenre = detected?.genre || "";
  useEffect(() => {
    if (!active) {
      seqRef.current += 1; // invalida cualquier respuesta en vuelo
      setResolved(null);
      setLoading(false);
      return undefined;
    }
    const seq = ++seqRef.current;
    const body = buildResolveBody({
      look: lookCode, genre: operatorGenre, variant, fontMain, fontKey,
      color, altColor, accentColor, jobId, artist, songTitle,
    });
    setLoading(true);
    const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = setTimeout(() => {
      fetchResolveLook(body, api, authHeaders, ctrl?.signal)
        .then((data) => {
          if (seq !== seqRef.current) return; // llegó tarde: hay uno más nuevo
          if (data && typeof data === "object") {
            // `_request`: para qué canción se resolvió (el pin de envío no
            // puede aplicarse a otra si la review cambió mientras volaba).
            setResolved({ ...data, _request: { artist: body.artist, songTitle: body.song_title, jobId: body.job_id } });
            setError(false);
          }
          setLoading(false);
        })
        .catch(() => {
          if (seq !== seqRef.current) return;
          // Conservamos el último resultado bueno.
          setError(true);
          setLoading(false);
        });
    }, debounceMs);
    return () => {
      clearTimeout(timer);
      // Cancela la red del pedido viejo; si igual llega, el seq lo descarta.
      ctrl?.abort();
    };
    // `detectedGenre` re-resuelve cuando termina la detección: el backend
    // la toma de su cache por artista+título (genre_source "detected").
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, lookCode, operatorGenre, variant, fontMain, fontKey, color, altColor, accentColor,
    jobId, artist, songTitle, api, debounceMs, detectedGenre]);

  // Fuentes que el catálogo/la variante usan y no están precargadas.
  useEffect(() => {
    const fonts = resolved?.fonts;
    if (!fonts) return;
    for (const f of [fonts.main, fonts.key]) {
      if (f?.css_family) ensureFontLoaded(f.css_family, f.weight);
    }
  }, [resolved]);

  // Un resultado de otro look (p. ej. justo después de cambiarlo) no sirve.
  const current = resolved && normalizeLyricLook(resolved.look) === lookCode ? resolved : null;
  return {
    catalog,
    resolved: active ? current : null,
    detected: wantDetect ? detected : null,
    loading: active && loading,
    error: active && error,
  };
}

export default useLookVariant;
