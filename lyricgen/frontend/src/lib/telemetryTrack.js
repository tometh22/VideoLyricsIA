// Tracking de eventos de comportamiento del wizard → POST /telemetry/events.
//
// Best-effort por contrato, igual que el heartbeat: los errores se tragan
// (sin Sentry, sin toasts) y el gate real vive en el server
// (TELEMETRY_ENABLED). features.telemetry === false solo evita gastar
// requests cuando sabemos que está apagada.
//
// Los eventos se encolan en memoria y se flushean en batch (cap 25, el
// mismo del endpoint) cada FLUSH_MS o cuando la pestaña pasa a hidden —
// ahí usamos sendBeacon para que el batch sobreviva al unload. La whitelist
// de tipos vive en el backend (main._UI_EVENT_TYPES); un tipo desconocido
// se descarta allá en silencio, así que versiones desfasadas de cliente y
// server no generan errores.

const API = import.meta.env.VITE_API_URL || "";
const FLUSH_MS = 10_000;
const MAX_BATCH = 25;
const MAX_QUEUE = 200; // un cliente roto no acumula memoria sin límite

let _queue = [];
let _timer = null;

function _token() {
  try {
    return localStorage.getItem("genly_token");
  } catch {
    return null;
  }
}

function _telemetryOff() {
  try {
    const u = JSON.parse(localStorage.getItem("genly_user") || "null");
    return Boolean(u?.features && u.features.telemetry === false);
  } catch {
    return false;
  }
}

function _flush(useBeacon = false) {
  if (_timer) {
    clearTimeout(_timer);
    _timer = null;
  }
  if (_queue.length === 0) return;
  const token = _token();
  if (!token) {
    _queue = [];
    return;
  }
  const batch = _queue.slice(0, MAX_BATCH);
  _queue = _queue.slice(MAX_BATCH);
  const body = JSON.stringify({ events: batch });

  // sendBeacon no permite headers → solo sirve si el endpoint aceptara el
  // token en el body, y no queremos eso. keepalive en fetch cubre el caso
  // unload manteniendo el Authorization header.
  void useBeacon;
  fetch(`${API}/telemetry/events`, {
    method: "POST",
    keepalive: true,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body,
  }).catch(() => {});

  // Si quedó cola (batch > 25), reprogramar el resto.
  if (_queue.length > 0 && !_timer) {
    _timer = setTimeout(() => _flush(), FLUSH_MS);
  }
}

/**
 * Encola un evento de UI. No-op sin sesión o con telemetría apagada.
 * track("wizard.step", { step_from: 1, step_to: 2 })
 */
export function track(type, data = {}) {
  if (!_token() || _telemetryOff()) return;
  if (_queue.length >= MAX_QUEUE) return;
  _queue.push({ type, data });
  if (!_timer) {
    _timer = setTimeout(() => _flush(), FLUSH_MS);
  }
}

/** Flush inmediato (lo dispara el listener de visibilitychange). */
export function flushTelemetry() {
  _flush(true);
}

// Auto-flush cuando la pestaña pasa a hidden (navegación, cierre, cambio
// de app en mobile) — sin esto se pierde la cola de los últimos 10 s,
// que en un funnel de abandono es justo el dato que importa.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) _flush(true);
  });
}

// Solo para tests: estado interno inspeccionable/reseteable.
export function _resetForTests() {
  _queue = [];
  if (_timer) {
    clearTimeout(_timer);
    _timer = null;
  }
}
export function _queueForTests() {
  return _queue;
}
