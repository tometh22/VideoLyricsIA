// Cola de guardado por job (PR F). Centraliza el autosave de segmentos que
// hoy vive repartido en 5 efectos de LyricsEditor (debounce, flush-on-unmount,
// pagehide/keepalive, flush-on-drag, retry) + `_pendingFlushRef`. Objetivos:
//
//   1. Serialización estricta: como máximo UN POST en vuelo por job. Los
//      pedidos que llegan mientras hay uno en vuelo se COALESCEN en un único
//      "trailing save" (se dispara con los segmentos más frescos al terminar
//      el actual). Sin esto, dos saves solapados pueden llegar al backend
//      fuera de orden y un 200 viejo pisa uno nuevo (last-write-wins roto).
//   2. Debounce de 3s para el autosave normal; `flush()` dispara ya.
//   3. Secuencia monotónica: un status de un save viejo nunca pisa el de uno
//      más nuevo (defensa ante keepalive/otros races).
//   4. Status suscribible (idle|saving|saved|error + reason) que alimenta el
//      chip "Guardado ✓" y el banner honesto — una sola fuente de verdad.
//
// Es framework-agnóstico y puro (sin React) para poder unit-testearlo. El
// hook useSegmentsAutosave (App) lo instancia y lo conecta al segmentsStore.
//
// `persist(jobId, segments, opts)` debe retornar el contrato de
// lib/persistSegments: { ok, reason?, status? }.

export const SAVE_DEBOUNCE_MS = 3000;
export const SAVED_FADE_MS = 2000;

export function createSaveQueue(persist, opts = {}) {
  const debounceMs = opts.debounceMs ?? SAVE_DEBOUNCE_MS;
  const savedFadeMs = opts.savedFadeMs ?? SAVED_FADE_MS;
  // El clasificador de error (result → categoría de copy) se inyecta para no
  // acoplar la cola al copy del editor; default identity sobre reason.
  const categorize = opts.categorize ?? ((r) => (r && r.reason) || "server");

  const jobs = new Map(); // jobId -> job state

  function ensure(jobId) {
    let j = jobs.get(jobId);
    if (!j) {
      j = {
        provider: null,      // () => segments (más frescos, evaluado al disparar)
        debounceTimer: null, // timer del debounce
        fadeTimer: null,     // timer del "saved" → "idle"
        inflight: false,     // hay un POST normal en vuelo
        pending: false,      // se pidió un save mientras había uno en vuelo
        waiters: [],         // flush callers waiting for the queue to drain
        status: "idle",      // idle|saving|saved|error
        reason: null,        // categoría de error o null
        listeners: new Set(),
      };
      jobs.set(jobId, j);
    }
    return j;
  }

  function emit(j) {
    for (const cb of j.listeners) cb({ status: j.status, reason: j.reason });
  }

  function setStatus(j, status, reason = null) {
    if (j.fadeTimer) { clearTimeout(j.fadeTimer); j.fadeTimer = null; }
    j.status = status;
    j.reason = reason;
    emit(j);
    // "saved" se desvanece a "idle" tras un rato (cosmético, igual que el
    // comportamiento previo del editor). No aplica a saving/error/idle.
    if (status === "saved" && savedFadeMs > 0) {
      j.fadeTimer = setTimeout(() => {
        // Solo si sigue en "saved" (no lo pisó un nuevo saving/error).
        if (j.status === "saved") { j.status = "idle"; j.reason = null; emit(j); }
      }, savedFadeMs);
    }
  }

  // Arranca un POST real AHORA (sin debounce). Respeta la serialización:
  // si ya hay uno en vuelo, marca `pending` y vuelve (trailing coalescido).
  function run(jobId, { keepalive = false } = {}) {
    const j = ensure(jobId);
    if (j.debounceTimer) { clearTimeout(j.debounceTimer); j.debounceTimer = null; }
    const segments = typeof j.provider === "function" ? j.provider() : null;
    if (!Array.isArray(segments) || segments.length === 0) {
      return Promise.resolve({ ok: false, reason: "no-data" });
    }

    // keepalive (pagehide/unload) es best-effort y NO sigue la serialización
    // ni toca el status: la página se está yendo, disparamos y listo.
    if (keepalive) {
      try { Promise.resolve(persist(jobId, segments, { keepalive: true })).catch(() => {}); }
      catch { /* best-effort */ }
      return Promise.resolve({ ok: true, keepalive: true });
    }

    const drained = new Promise((resolve) => j.waiters.push(resolve));
    if (j.inflight) { j.pending = true; return drained; } // coalesce

    j.inflight = true;
    setStatus(j, "saving");
    // Pasamos el OBJETO `j`, no solo el string jobId: si el job se evicta (y
    // quizás se recrea con el mismo id) mientras el POST está en vuelo, el
    // settle debe caer en la MISMA instancia o descartarse — nunca corromper
    // la instancia nueva (bug P1 de review adversarial: evict+recrear rompía
    // single-flight y trababa el chip en un status stale = modo de falla UMG).
    Promise.resolve(persist(jobId, segments, { keepalive: false }))
      .then((result) => settle(j, jobId, result))
      .catch((err) => settle(j, jobId, { ok: false, reason: "network", error: String(err) }));
    return drained;
  }

  function settle(j, jobId, result) {
    // Guard de identidad: si la instancia viva de este jobId ya no es `j`
    // (evictado/recreado), este settle es viejo → lo dropeamos en silencio.
    if (jobs.get(jobId) !== j) return;
    j.inflight = false;
    // Orden garantizado por la serialización estricta (un solo POST en vuelo;
    // el trailing arranca recién en este settle), así que no hace falta un
    // contador de secuencia: los settles llegan siempre en orden.
    if (result && result.ok === false) setStatus(j, "error", categorize(result));
    else setStatus(j, "saved", null);
    // Trailing coalescido: se pidieron más saves mientras este estaba en
    // vuelo → disparamos uno solo con los segmentos más frescos.
    if (j.pending) { j.pending = false; run(jobId, { keepalive: false }); return; }
    const waiters = j.waiters.splice(0);
    for (const resolve of waiters) resolve(result && result.ok === false
      ? result
      : { ok: true });
  }

  return {
    // Arma/reprograma el autosave debounced. `provider` se guarda y se evalúa
    // al disparar, así el POST lleva SIEMPRE los segmentos más frescos.
    schedule(jobId, provider) {
      if (!jobId) return;
      const j = ensure(jobId);
      j.provider = provider;
      if (j.debounceTimer) clearTimeout(j.debounceTimer);
      j.debounceTimer = setTimeout(() => { j.debounceTimer = null; run(jobId); }, debounceMs);
    },
    // Dispara ya (drag, retry manual, step-nav, pagehide con keepalive).
    // `provider` opcional actualiza la fuente antes de disparar.
    flush(jobId, { keepalive = false, provider } = {}) {
      if (!jobId) return;
      const j = ensure(jobId);
      if (typeof provider === "function") j.provider = provider;
      return run(jobId, { keepalive });
    },
    getStatus(jobId) {
      const j = jobs.get(jobId);
      return j ? { status: j.status, reason: j.reason } : { status: "idle", reason: null };
    },
    subscribe(jobId, cb) {
      const j = ensure(jobId);
      j.listeners.add(cb);
      return () => { j.listeners.delete(cb); };
    },
    // Cancela timers y descarta estado de un job (approve/reset/discard).
    evict(jobId) {
      const j = jobs.get(jobId);
      if (!j) return;
      if (j.debounceTimer) clearTimeout(j.debounceTimer);
      if (j.fadeTimer) clearTimeout(j.fadeTimer);
      for (const resolve of j.waiters.splice(0)) {
        resolve({ ok: false, reason: "evicted" });
      }
      jobs.delete(jobId);
    },
    // Solo para tests.
    _peek(jobId) { return jobs.get(jobId); },
  };
}
