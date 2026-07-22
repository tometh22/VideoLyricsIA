/**
 * Store de segments por job — PR E del plan del editor (2026-07).
 *
 * PROBLEMA ESTRUCTURAL que reemplaza: el estado `edited` del LyricsEditor
 * vivía en un useState local, y App.jsx lo espejaba de vuelta como prop
 * (`onEditedChange` → mergeEditedSegments → currentReview.segments →
 * prop `segments`). Ese loop bidireccional generó DOS familias de P0:
 *
 *   1. reseed-storm / freeze (UMG): cada eco del espejo podía disparar el
 *      reseed destructivo del editor → _id reasignados → TODAS las filas
 *      re-montaban en loop (6-7×/s en canciones de 59+ líneas). Se fue
 *      parchando con guards (#724, eco-live, #detector), pero el loop
 *      seguía existiendo estructuralmente.
 *   2. "se borran los tiempos/locks al navegar" (Seba+Gaby): el wizard
 *      des-monta el editor al ir del paso 6 al 4; al volver, el editor
 *      re-seedeaba desde un prop `segments` STALE y pisaba todo lo
 *      editado que el autosave aún no había persistido.
 *
 * SOLUCIÓN: el estado vive acá, en un Map a nivel módulo keyed por jobId,
 * y SOBREVIVE al unmount del componente. El editor se re-engancha a la
 * entrada viva al re-montar (useSyncExternalStore); el espejo por
 * keystroke hacia App desaparece (los lectores se suscriben acá). El
 * reemplazo externo post-mount ya no viaja por props: hay un
 * `segmentsStore.replace(jobId, segs)` DISPONIBLE para ese caso —
 * preserva identidad de filas vía reseedPreservingIds (invariante de PR D:
 * un eco puro conserva todos los _id → cero remounts)— pero hoy NO tiene
 * caller de producción (API reservada; sólo la ejercitan los tests).
 *
 * Cero dependencias nuevas — React 18 trae useSyncExternalStore.
 */
import { useCallback, useRef, useState, useSyncExternalStore } from "react";
import { reseedPreservingIds } from "../lib/segmentIds";

// jobId → { segments, original, version }. Module-level a propósito:
// sobrevive al unmount de cualquier componente. `original` = baseline
// inmutable del seed (para "Resetear timings"). El ciclo de vida lo cierran
// los callers de evict()/evictAll() (App: approve, discard, logout).
const entries = new Map();
// jobId → Set<listener>. Separado de entries: un evict no debe romper la
// suscripción de un hook aún montado (su snapshot pasa a EMPTY y listo).
const listeners = new Map();
// Canary anti-loop: ventana deslizante de seeds/replaces por jobId.
const canaryWindows = new Map();

// Snapshot estable para "no hay entrada" — useSyncExternalStore exige
// que getSnapshot devuelva la MISMA referencia si nada cambió.
const EMPTY = Object.freeze([]);

function notify(jobId) {
  const set = listeners.get(jobId);
  if (!set) return;
  for (const fn of Array.from(set)) fn();
}

// [reseed-storm] canary (heredero del detector que vivía en el effect de
// prop-sync del LyricsEditor, hoy borrado). Si el mismo jobId se
// re-seedea o replace()a >2 veces en 1 s, algo está loopeando (un caller
// que evicta+seedea en ciclo, o un replace en cada render). Mismo tag
// que el detector viejo: observability.js reenvía a Sentry los
// console.warn que arrancan con "[reseed-storm]".
function bumpCanary(jobId, source) {
  const now = Date.now();
  let w = canaryWindows.get(jobId);
  if (!w || now - w.windowStart > 1000) {
    w = { windowStart: now, count: 0, warned: false };
    canaryWindows.set(jobId, w);
  }
  w.count += 1;
  if (w.count > 2 && !w.warned) {
    w.warned = true;
    // eslint-disable-next-line no-console
    console.warn("[reseed-storm] store reseed loop", {
      jobId,
      perSec: w.count,
      source,
      segments: entries.get(jobId)?.segments?.length ?? null,
    });
  }
}

// setEntry NUNCA pisa `original`: la baseline se fija UNA vez, al seed. Así
// "Resetear timings" del editor apunta al timing verdaderamente original aun
// después de un remount (F2): sin esto, originalSegmentsRef capturaba los
// segments YA editados de la entrada viva y Reset restauraba filas a sí mismas.
function setEntry(jobId, segments) {
  const prev = entries.get(jobId);
  entries.set(jobId, {
    segments,
    original: prev?.original,
    version: (prev?.version ?? 0) + 1,
  });
  notify(jobId);
}

export const segmentsStore = {
  /** Segments vivos del job, o undefined si no hay entrada. */
  get(jobId) {
    return entries.get(jobId)?.segments;
  },

  /**
   * Baseline ORIGINAL del job (= output del seedFn en el primer mount),
   * inmutable a través de edits/replace/remount. La usa el editor para
   * "Resetear timings" — apuntar al timing original real, no al ya editado.
   */
  getOriginal(jobId) {
    return entries.get(jobId)?.original;
  },

  /**
   * Reemplazo EXTERNO post-mount (el sucesor del viejo "reseed por prop").
   * DISPONIBLE para reemplazo externo del contenido — hoy sin caller de
   * producción (API reservada; sólo la ejercitan los tests de survival).
   * Preserva el _id de las filas cuyo contenido ya está en la entrada
   * (invariante PR D): un eco puro no re-monta ninguna fila; solo el
   * contenido genuinamente nuevo recibe id fresco.
   */
  replace(jobId, segments) {
    if (!jobId) return;
    bumpCanary(jobId, "replace");
    const current = entries.get(jobId)?.segments || [];
    setEntry(jobId, reseedPreservingIds(current, Array.isArray(segments) ? segments : []));
  },

  /**
   * Cierra el ciclo de vida de un job (aprobado, descartado). Sin esto un
   * batch de 20 canciones deja 20 arrays vivos en memoria y un job
   * re-transcripto mostraría la versión vieja.
   */
  evict(jobId) {
    if (!jobId) return;
    if (!entries.has(jobId)) return;
    entries.delete(jobId);
    canaryWindows.delete(jobId);
    notify(jobId);
  },

  /** Descarte total (reset de batch, logout, resume fallido). */
  evictAll() {
    const ids = Array.from(entries.keys());
    entries.clear();
    canaryWindows.clear();
    for (const id of ids) notify(id);
  },

  /** Test-only: limpia entradas Y ventanas del canary entre tests. */
  _clearAll() {
    segmentsStore.evictAll();
  },
};

function subscribeJob(jobId, cb) {
  if (!jobId) return () => {};
  let set = listeners.get(jobId);
  if (!set) {
    set = new Set();
    listeners.set(jobId, set);
  }
  set.add(cb);
  return () => {
    set.delete(cb);
    if (set.size === 0) listeners.delete(jobId);
  };
}

/**
 * Drop-in del useState de `edited` en LyricsEditor.
 *
 *   const [edited, setEdited] = useJobSegments(jobId, seedFn)
 *
 * - Seedea UNA vez por jobId (seedFn corre solo si el store no tiene
 *   entrada). El unmount NO destruye la entrada; un remount se engancha a
 *   la entrada viva — así los tiempos/locks sobreviven la navegación
 *   entre pasos del wizard.
 * - setEdited acepta valor o updater fn, como setState.
 * - jobId falsy (editor sin job de backend, p.ej. unit tests) → estado
 *   local plano vía useState: no persiste ni leakea entre mounts.
 */
export function useJobSegments(jobId, seedFn) {
  // jobId falsy → useState plano. El jobId es estable durante la vida del
  // mount (App re-monta el editor vía `key` cuando cambia de job), así
  // que ambos caminos de hooks corren siempre en el mismo orden.
  const [local, setLocal] = useState(() => (jobId ? EMPTY : seedFn()));

  // Seed lazy durante el PRIMER render del mount (idempotente: solo si NO
  // hay entrada). Tiene que pasar antes del primer getSnapshot; StrictMode
  // double-render es inocuo porque la segunda pasada ve la entrada y no
  // re-seedea. `seededRef` limita el seed a la primera pasada del mount:
  // un evict() mientras el hook sigue montado NO debe resucitar la entrada
  // desde el prop stale en el siguiente render (el remount siguiente sí
  // re-seedea, que es el contrato del evict).
  const seededRef = useRef(false);
  if (jobId && !seededRef.current && !entries.has(jobId)) {
    bumpCanary(jobId, "seed");
    const seed = seedFn();
    // `original` = la baseline del seed, congelada acá para siempre (setEntry
    // la preserva). El editor la lee vía getOriginal() para "Resetear timings"
    // aun después de un remount que se re-engancha a la entrada ya editada.
    entries.set(jobId, { segments: seed, original: seed, version: 1 });
  }
  seededRef.current = true;

  const subscribe = useCallback((cb) => subscribeJob(jobId, cb), [jobId]);
  const getSnapshot = useCallback(
    () => (jobId ? (entries.get(jobId)?.segments ?? EMPTY) : EMPTY),
    [jobId],
  );
  const storeValue = useSyncExternalStore(subscribe, getSnapshot);

  const setSegments = useCallback(
    (next) => {
      if (!jobId) {
        setLocal((prev) => (typeof next === "function" ? next(prev) : next));
        return;
      }
      const prev = entries.get(jobId)?.segments ?? EMPTY;
      const value = typeof next === "function" ? next(prev) : next;
      if (value === prev) return;
      setEntry(jobId, value);
    },
    [jobId],
  );

  return [jobId ? storeValue : local, setSegments];
}

/**
 * Lectura reactiva (read-only) de los segments de un job — para App
 * (WizardLivePreview + snapshot de wizardPersistence), que antes leían
 * currentReview.segments alimentado por el espejo por keystroke.
 * Devuelve null si no hay jobId o no hay entrada todavía.
 */
export function useJobSegmentsValue(jobId) {
  const subscribe = useCallback((cb) => subscribeJob(jobId, cb), [jobId]);
  const getSnapshot = useCallback(
    () => (jobId ? (entries.get(jobId)?.segments ?? null) : null),
    [jobId],
  );
  return useSyncExternalStore(subscribe, getSnapshot);
}
