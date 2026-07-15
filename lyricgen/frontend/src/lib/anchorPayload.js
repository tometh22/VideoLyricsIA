// Versión B (letra anclada) — cuánto texto de ancla mandar en el POST
// /transcribe-uploaded para una entry del wizard.
//
// El selector "IA de Genly vs letra oficial" manda: solo se ancla cuando el
// operador eligió "official" Y pegó texto no vacío. Volver a "IA de Genly"
// desactiva el anclado aunque el textarea conserve texto. Extraído a lib
// para tener una única fuente de verdad (lo usan el prefetch y el slow path
// de App.jsx) y poder testear la decisión real.
export function anchorLyricsForEntry(entry) {
  if (!entry || entry.lyricsSource !== "official") return "";
  return (entry.anchorLyrics || "").trim();
}
