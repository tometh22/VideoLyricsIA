import { useState, useEffect } from "react";

/**
 * Hook simple para persistir state del operador entre sesiones.
 *
 * Linear/Notion pattern — el operador setea su preferencia una vez y la
 * app la respeta. Default razonable para nuevos operadores.
 *
 * Valores: string-only — useLocalStorage("genly_history_view", "table").
 * Para booleanos usar string "1"/"0" o serializar JSON.parse manual.
 *
 * 2026-05-25 — extraído de HistoryView.jsx para reuso en LyricsEditor
 * (focus mode), Dashboard, y cualquier otro componente que persista
 * preferencias visuales.
 *
 * Errores de localStorage (Safari private mode, quota exceeded) son
 * swallowed silenciosamente: el state queda en memoria, el operador no
 * pierde la app por un browser config.
 */
export default function useLocalStorage(key, defaultValue) {
  const [value, setValue] = useState(() => {
    try {
      const v = localStorage.getItem(key);
      return v !== null ? v : defaultValue;
    } catch (_) {
      return defaultValue;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, value);
    } catch (_) {
      /* private mode / quota — ignore */
    }
  }, [key, value]);
  return [value, setValue];
}
