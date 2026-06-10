/**
 * useBackgroundPreview — Capa C del wizard refactor (2026-05-24).
 *
 * Observa los params del background (movement / effect / style / etc.) en la
 * entry actual del wizard. Cuando el set está estable 2s (debounce), dispara
 * POST /generate-preview que arranca Veo/Imagen en background. Pollea
 * /generate-preview-status hasta done|failed.
 *
 * El consumer (App.jsx) recibe `bgCacheKey` cuando el preview termina —
 * lo persiste en la entry del file y lo manda al POST /generate cuando
 * el operador clickea "Crear video".
 *
 * COSTO: cada cambio de params descarta el preview anterior (~$0.80-3.20
 * Veo wasted). El debounce 2s + el hash determinístico minimizan el waste.
 *
 * USO:
 *   const { bgCacheKey, status } = useBackgroundPreview(entry, {
 *     enabled: entry.artist && entry.songTitle && wizardStage === "review",
 *     api: API,
 *     authHeaders,
 *     onCacheKey: (key) => updateEntry(idx, { bgCacheKey: key }),
 *   });
 */
import { useEffect, useRef, useState, useCallback } from "react";

// Audit 2026-05-26 cost-leak fix: 2 s was too aggressive for an operator
// "exploring options" — every micro-adjustment past 2 s of inactivity
// fired a Veo/Imagen pre-gen at $0.80–$3.20/job. A typical 5-param
// review pass burned $5–$30 in wasted previews. 10 s captures the
// "I've settled on what I want" pause without significantly hurting
// the preview cache-hit rate at "Crear video" click time. Callers can
// override via the `debounceMs` option if a faster cycle is needed
// (e.g. automated tests).
const DEFAULT_DEBOUNCE_MS = 10000;
const POLL_INTERVAL_INITIAL_MS = 1500;
const POLL_INTERVAL_MAX_MS = 5000;
const POLL_TIMEOUT_MS = 5 * 60 * 1000;  // 5 min hard cap

function extractParams(entry) {
  // Sólo los campos del background. Si cambia alguno, el hash backend
  // difiere y se dispara un nuevo preview.
  return {
    artist: (entry.artist || "").trim(),
    song_title: (entry.songTitle || "").trim(),
    style: entry.style || "auto",
    movement_style: entry.movementStyle || "",
    effect: entry.effect || "",
    custom_colors: (entry.customColors || "").trim(),
    genre: entry.genre || "",
    concept: entry.concept || "",
    background_hint: (entry.backgroundHint || "").trim(),
    bg_verbatim: !!entry.bgVerbatim,
    background_mode: entry.backgroundMode || "veo",
    animate_image: !!entry.animateImage,
    match_lyrics: entry.matchLyrics !== false,
    target_duration_s: 30.0,
  };
}

function paramsKey(p) {
  // Cliente-side dedupe: el frontend NO computa el hash criptográfico —
  // ese vive en el backend (compute_bg_cache_key). Acá sólo serializamos
  // para detectar "cambió o no cambió" entre ticks del useEffect.
  return JSON.stringify(Object.keys(p).sort().map((k) => [k, p[k]]));
}

export function useBackgroundPreview(entry, {
  enabled = true,
  api = "",
  authHeaders = () => ({}),
  onCacheKey = null,
  debounceMs = DEFAULT_DEBOUNCE_MS,
} = {}) {
  const [bgCacheKey, setBgCacheKey] = useState(null);
  const [status, setStatus] = useState("idle");  // idle | queued | generating | done | error
  const [error, setError] = useState(null);

  const debounceRef = useRef(null);
  const pollAbortRef = useRef(null);
  const lastKeyRef = useRef(null);

  const startPreview = useCallback(async (params) => {
    // Staleness guard (audit adversarial 2026-06-09): el fetch no se
    // cancela cuando el operador cambia un param — la respuesta vieja
    // llegaba DESPUÉS del onCacheKey(null) y re-envenenaba el key. Cada
    // respuesta se compara contra los params vigentes (lastKeyRef): si
    // ya no coinciden, el key se entrega con stale=true para que el
    // consumer NO lo escriba en la review actual (sí sirve para el
    // backfill R-FRONT-2 de jobs ya aprobados con esos params).
    const myParamsKey = paramsKey(params);
    const isCurrent = () => lastKeyRef.current === myParamsKey;
    setStatus("queued");
    setError(null);
    try {
      const res = await fetch(`${api}/generate-preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(params),
      });
      if (!res.ok) {
        if (!isCurrent()) return;
        const txt = await res.text();
        setStatus("error");
        setError(`HTTP ${res.status}: ${txt.slice(0, 200)}`);
        return;
      }
      const data = await res.json();
      // Plan-tier guard del backend: free-tier no dispara pre-gen. Status
      // queda "disabled" + bgCacheKey null. El POST /generate igual corre
      // Veo/Imagen como siempre — solo no hay pre-warming.
      if (data.skipped) {
        if (isCurrent()) {
          setStatus("disabled");
          setError(null);
        }
        return;
      }
      const key = data.bg_cache_key;
      if (!isCurrent()) {
        if (onCacheKey) onCacheKey(key, { stale: true });
        return;
      }
      setBgCacheKey(key);
      if (onCacheKey) onCacheKey(key, { stale: false });

      if (data.cached) {
        setStatus("done");
        return;
      }
      // Async path — pollear hasta done/failed.
      pollUntilDone(data.job_id);
    } catch (e) {
      if (!isCurrent()) return;
      setStatus("error");
      setError(e.message || String(e));
    }
  }, [api, authHeaders, onCacheKey]);

  const pollUntilDone = useCallback(async (jobId) => {
    const ctrl = new AbortController();
    pollAbortRef.current = ctrl;
    const start = Date.now();
    let delay = POLL_INTERVAL_INITIAL_MS;
    setStatus("generating");
    while (Date.now() - start < POLL_TIMEOUT_MS) {
      if (ctrl.signal.aborted) return;
      try {
        const r = await fetch(`${api}/generate-preview-status/${jobId}`, {
          headers: authHeaders(),
          signal: ctrl.signal,
        });
        if (r.ok) {
          const d = await r.json();
          const s = d.status;
          if (s === "bg_preview_done") {
            setStatus("done");
            return;
          }
          if (s === "bg_preview_failed") {
            setStatus("error");
            setError(d.error || "Background pre-gen failed");
            return;
          }
        }
      } catch (e) {
        if (e.name === "AbortError") return;
        // Transient — keep polling.
      }
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 1.2, POLL_INTERVAL_MAX_MS);
    }
    setStatus("error");
    setError("Timeout esperando el preview");
  }, [api, authHeaders]);

  useEffect(() => {
    if (!enabled || !entry) return;
    const params = extractParams(entry);
    // Skip si faltan los campos esenciales (artist + songTitle deben estar).
    if (!params.artist || !params.song_title) return;

    const key = paramsKey(params);
    if (key === lastKeyRef.current) return;  // sin cambios reales, no dispara
    lastKeyRef.current = key;

    // Cancel el debounce + cualquier polling previo. El operador cambió
    // un param → descarta el preview viejo (ya queda como wasted en R2).
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (pollAbortRef.current) pollAbortRef.current.abort();
    setStatus("idle");
    setBgCacheKey(null);
    // Audit 2026-06-09 (familia del bug Ana M.): avisar al consumer que el
    // key quedó obsoleto. Sin esto, currentReview.bgCacheKey retiene el
    // hash de los params VIEJOS durante toda la ventana del debounce
    // (10s) + el roundtrip; si el operador aprueba ahí adentro, /generate
    // manda el key viejo y el backend cache-hitea el fondo de los params
    // anteriores — video con fondo que no refleja lo que se pidió.
    if (onCacheKey) onCacheKey(null);

    debounceRef.current = setTimeout(() => {
      startPreview(params);
    }, debounceMs);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // Re-run cuando cualquier param relevante cambie. JSON.stringify del
    // entry sería caro; mejor depender de los fields explícitos.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    enabled,
    entry?.artist,
    entry?.songTitle,
    entry?.style,
    entry?.movementStyle,
    entry?.effect,
    entry?.customColors,
    entry?.genre,
    entry?.concept,
    entry?.backgroundHint,
    entry?.bgVerbatim,
    entry?.backgroundMode,
    entry?.animateImage,
    entry?.matchLyrics,
  ]);

  // Cleanup al unmount.
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (pollAbortRef.current) pollAbortRef.current.abort();
    };
  }, []);

  return { bgCacheKey, status, error };
}
