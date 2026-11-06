/**
 * useJobBeats — beats reales de la canción de un job para el preview en vivo.
 *
 * Los looks con beat_sync (Cinético, Neón) patean / titilan en el render
 * sobre los beats detectados del audio. GET /jobs/{id}/beats devuelve
 * {bpm, beats:[s...]}; se pide UNA vez por job (cache de módulo) y sólo si
 * `enabled`. Cualquier falla (404/422/red) devuelve null: el preview sigue
 * con su reloj fijo, sin ruido.
 */
import { useEffect, useState } from "react";

// jobId → beats[] (ok) | null (falló: no reintentar) | Promise (en vuelo).
const cache = new Map();

/** Sólo para tests. */
export function _clearJobBeatsCache() {
  cache.clear();
}

function fetchBeats(jobId, api, authHeaders) {
  const hit = cache.get(jobId);
  if (hit !== undefined) return Promise.resolve(hit);
  const p = fetch(`${api}/jobs/${encodeURIComponent(jobId)}/beats`, { headers: authHeaders() })
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      const beats = Array.isArray(data?.beats)
        ? data.beats.filter((b) => Number.isFinite(b)).sort((a, b) => a - b)
        : [];
      return beats.length ? beats : null;
    })
    .catch(() => null)
    .then((beats) => {
      cache.set(jobId, beats);
      return beats;
    });
  cache.set(jobId, p);
  return p;
}

export function useJobBeats(jobId, enabled, { api = "", authHeaders = () => ({}) } = {}) {
  const [state, setState] = useState({ jobId: null, beats: null });
  useEffect(() => {
    if (!enabled || !jobId) return undefined;
    let alive = true;
    fetchBeats(jobId, api, authHeaders).then((beats) => {
      if (alive) setState({ jobId, beats });
    });
    return () => { alive = false; };
    // authHeaders es una función estable a nivel módulo en los callers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, enabled, api]);
  return enabled && jobId && state.jobId === jobId ? state.beats : null;
}

/**
 * Estado del pulso en `t`: uno de cada `every` beats (como el backend
 * `_beats_in(..., every=2)`: el detector suele dar doble tiempo). `pulsing`
 * mientras t cae dentro de `window` s después del último beat de pulso.
 */
export function beatPulseAt(beats, t, { every = 2, window = 0.12 } = {}) {
  if (!Array.isArray(beats) || !beats.length || !Number.isFinite(t)) return null;
  let last = -1;
  for (let i = 0; i < beats.length && beats[i] <= t; i += 1) {
    if (i % every === 0) last = i;
  }
  if (last < 0) return { pulsing: false, since: null };
  const since = t - beats[last];
  return { pulsing: since <= window, since };
}

export default useJobBeats;
