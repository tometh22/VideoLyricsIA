import { lazy } from "react";

// Resiliencia de stale-bundle para las rutas lazy. Cuando Vercel publica un
// build nuevo cambian los hashes de los chunks (LyricsEditor-X.js, etc.); una
// tab abierta con el index.html viejo sigue importando los hashes anteriores y
// puede fallar de DOS formas distintas:
//
//   1. import() RECHAZA — "Failed to fetch dynamically imported module" o
//      "Unable to preload CSS for ...". Ya lo cubre el hook `vite:preloadError`
//      de main.jsx (reload one-shot + preventDefault, PR #1062).
//
//   2. import() RESUELVE a un módulo undefined / sin `default` — entonces
//      React.lazy hace `moduleObject.default` sobre undefined → "Cannot read
//      properties of undefined (reading 'default')" (Sentry #32). Acá NO se
//      dispara ningún evento (la promesa resolvió, no rechazó), así que el hook
//      de vite:preloadError nunca se entera y el error burbujea al
//      GlobalErrorBoundary ("Algo salió mal"). Vectores típicos: el service
//      worker sirviendo un chunk viejo/parcial, o el propio preventDefault del
//      hook convirtiendo un fetch fallido en una resolución `undefined`.
//
// lazyWithReload cierra el caso 2 (y el 1 como defensa en profundidad): si el
// import rechaza O resuelve sin componente usable, fuerza un reload one-shot
// para que el browser baje el index.html + chunk frescos. Comparte EXACTAMENTE
// la misma llave y ventana de sessionStorage que el hook de main.jsx
// (`__vite_reload_at`, 10s) para que los dos mecanismos no entren en loop de
// recargas.

const RELOAD_KEY = "__vite_reload_at";
const RELOAD_WINDOW_MS = 10_000;

// Promesa que nunca resuelve: la devolvemos cuando ya disparamos el reload para
// que React.lazy quede suspendido (mostrando el fallback) en vez de intentar
// renderizar `undefined` mientras la página se recarga.
const NEVER = new Promise(() => {});

function reloadOnceForStaleBundle(reason) {
  const last = parseInt(sessionStorage.getItem(RELOAD_KEY) || "0", 10);
  if (Date.now() - last < RELOAD_WINDOW_MS) {
    // Ya recargamos hace poco por un stale-bundle: no insistir (puede ser un
    // problema de red/build real, no un deploy). Dejar que el error surja al
    // GlobalErrorBoundary para no loopear recargas infinitas.
    return false;
  }
  sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  console.warn("[stale-bundle] lazy import inválido, forzando reload", reason);
  window.location.reload();
  return true;
}

export default function lazyWithReload(factory) {
  return lazy(() =>
    factory().then(
      (mod) => {
        if (mod && mod.default) return mod;
        // Resolvió pero sin componente usable → tratar como stale-bundle.
        if (reloadOnceForStaleBundle("módulo sin export default")) return NEVER;
        // Circuit-breaker abierto: dejar que React tire el error real.
        return mod;
      },
      (err) => {
        if (reloadOnceForStaleBundle(err)) return NEVER;
        throw err;
      }
    )
  );
}
