import { useEffect, useState } from "react";

import { fetchWithTimeout } from "../fetchWithTimeout";

const API = import.meta.env.VITE_API_URL || "";
const POLL_MS = 60_000;

// Un ÚNICO poll de `/service-status/summary` compartido por todos los que
// lo muestran (la barra de incidente, el punto de estado del sidebar, y lo
// que venga después).
//
// Singleton a nivel de módulo y no un fetch por componente: son 2-3
// consumidores montados a la vez y el rate limit por IP es 120/min
// compartido entre usuarios detrás del mismo NAT (una oficina de UMG). Tres
// polls por pestaña × varias pestañas × varias personas empieza a comerse
// un presupuesto que existe para el trabajo real.
//
// El estado se guarda acá para que un consumidor que monta tarde tenga
// dato inmediato en vez de esperar el ciclo.
let cached = null;
let timer = null;
const subscribers = new Set();

function emit() {
  for (const fn of subscribers) fn(cached);
}

async function load() {
  try {
    const res = await fetchWithTimeout(`${API}/service-status/summary`, {}, 8000);
    if (!res.ok) {
      // Un error del propio endpoint de status NO se convierte en alarma:
      // un bundle viejo contra una API nueva daría 404 y anunciaría un
      // incidente inexistente. Ver ServiceStatusBanner.
      cached = null;
    } else {
      cached = await res.json();
    }
  } catch {
    // Timeout o red del usuario. Mismo criterio: silencio.
    cached = null;
  }
  emit();
}

function ensurePolling() {
  if (timer !== null) return;
  load();
  timer = setInterval(load, POLL_MS);
}

function stopPolling() {
  if (subscribers.size > 0 || timer === null) return;
  clearInterval(timer);
  timer = null;
}

/** Devuelve el último summary conocido, o `null` si no se pudo obtener. */
export function useServiceStatusSummary() {
  const [summary, setSummary] = useState(cached);

  useEffect(() => {
    subscribers.add(setSummary);
    ensurePolling();
    // Al volver a la pestaña se refresca sin esperar el minuto: quien
    // vuelve después de una hora vería un estado viejo.
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      subscribers.delete(setSummary);
      document.removeEventListener("visibilitychange", onVisible);
      stopPolling();
    };
  }, []);

  return summary;
}

/** Solo para tests: limpia el singleton entre casos. */
export function __resetServiceStatusPollForTests() {
  cached = null;
  if (timer !== null) clearInterval(timer);
  timer = null;
  subscribers.clear();
}
