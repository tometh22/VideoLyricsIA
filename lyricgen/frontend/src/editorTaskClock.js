// Clasificación de la TAREA en curso dentro del editor.
//
// Por qué existe: el baseline real de minutos de revisor (medido el 2026-09-03
// sobre 49 canciones de producción) dio mediana 8,8 min y p90 18,8 min, pero no
// dice EN QUÉ se van esos minutos. Sin el desglose no se puede decidir si la
// palanca es buscar el error, corregir el texto o arrastrar el timing.
//
// Diseño: NO se abre un canal de telemetría nuevo. El editor ya emite un latido
// cada 15 s (`editor_activity_heartbeat`); acá sólo se le agrega la etiqueta de
// la tarea en curso, y el servidor reparte los huecos entre latidos por tarea
// con la misma lógica ya validada para el total.
//
// La clasificación es una función PURA para poder testearla sin DOM.

export const TASKS = Object.freeze([
  "listen", "search", "text", "timing", "vocalization", "export", "unknown",
]);

/**
 * Devuelve la tarea en curso a partir de señales baratas y confiables.
 *
 * Prioridad, de más específica a más genérica:
 *  1. `taskAttr`: valor de `data-editor-task` del ancestro más cercano. Es la
 *     única forma de distinguir timing de "hacer clic en cualquier lado", así
 *     que los controles de timeline y el botón de aprobar lo llevan explícito.
 *  2. `editable`: el foco está en un input/textarea/contenteditable → texto.
 *  3. `isPlaying` sin interacción reciente → escuchar.
 *  4. Hubo interacción pero no sabemos de qué tipo → buscar (navegar, mirar).
 *
 * @param {{taskAttr?: string|null, editable?: boolean, isPlaying?: boolean,
 *          interactedRecently?: boolean}} signals
 * @returns {string} una de TASKS
 */
export function classifyTask({
  taskAttr = null,
  editable = false,
  isPlaying = false,
  interactedRecently = false,
} = {}) {
  const attr = typeof taskAttr === "string" ? taskAttr.trim().toLowerCase() : "";
  if (attr && TASKS.includes(attr)) return attr;
  if (editable) return "text";
  if (isPlaying && !interactedRecently) return "listen";
  if (interactedRecently) return "search";
  if (isPlaying) return "listen";
  return "unknown";
}

/**
 * ¿El nodo es un campo de edición de texto?
 * Se acepta cualquier input de texto, textarea o contenteditable.
 */
export function isEditableTarget(node) {
  if (!node || typeof node !== "object") return false;
  const tag = String(node.tagName || "").toLowerCase();
  if (tag === "textarea") return true;
  if (tag === "input") {
    const type = String(node.type || "text").toLowerCase();
    return !["button", "checkbox", "radio", "range", "submit", "file"].includes(type);
  }
  return node.isContentEditable === true;
}

/**
 * Lee `data-editor-task` del ancestro más cercano que lo tenga.
 * Tolera nodos sin `closest` (tests, nodos de texto).
 */
export function readTaskAttr(node) {
  if (!node || typeof node.closest !== "function") return null;
  try {
    const match = node.closest("[data-editor-task]");
    return match ? match.getAttribute("data-editor-task") : null;
  } catch {
    return null;
  }
}

/**
 * Reparte tiempo entre latidos por tarea. El servidor hace lo mismo sobre los
 * eventos persistidos; esta copia existe para poder testear la regla y para
 * mostrar un desglose en vivo si alguna vez se necesita.
 *
 * Cuenta sólo huecos `<= maxGapMs` (el latido es cada 15 s) y le atribuye el
 * hueco a la tarea del latido MÁS NUEVO: si el revisor pasó de escuchar a
 * escribir, ese tramo ya era de escritura.
 *
 * @param {Array<{atMs: number, task: string}>} beats
 * @returns {Record<string, number>} milisegundos por tarea
 */
export function bucketByTask(beats, { maxGapMs = 25000 } = {}) {
  const ordered = [...(beats || [])]
    .filter((b) => b && Number.isFinite(b.atMs))
    .sort((a, b) => a.atMs - b.atMs);
  const totals = {};
  for (let i = 1; i < ordered.length; i += 1) {
    const gap = ordered[i].atMs - ordered[i - 1].atMs;
    if (gap <= 0 || gap > maxGapMs) continue;
    const task = TASKS.includes(ordered[i].task) ? ordered[i].task : "unknown";
    totals[task] = (totals[task] || 0) + gap;
  }
  return totals;
}
