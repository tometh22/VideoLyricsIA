import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { I18nProvider } from "./i18n";
import App from "./App";
import GlobalErrorBoundary from "./components/GlobalErrorBoundary";
import { AlertProvider } from "./components/AlertProvider";
import { ToastProvider } from "./components/ToastProvider";
import { HelpProvider } from "./components/HelpCenter/HelpProvider";
import { initSentry } from "./observability";
import { registerServiceWorker } from "./registerSW";
import { initAutoUpdate } from "./autoUpdate";
import "./index.css";

// Sentry init runs before React mounts so the SDK is ready to catch
// any error from the first render. No-op if VITE_SENTRY_DSN is unset
// (dev or pre-configured deploy) — see observability.js header for
// the DSN setup walkthrough.
initSentry();

// QA fix 2026-05-28: post-deploy stale-bundle reload. Cuando Vercel
// publica un build nuevo, los hashes de los chunks lazy-imported cambian
// (Settings-X.js, AdminPanel-Y.js, LyricsEditor-Z.js). Las tabs ya
// abiertas tienen el index.js viejo que sigue intentando importar las
// rutas con los hashes anteriores → falla con "Failed to fetch
// dynamically imported module" → boom GlobalErrorBoundary → operador
// confundido.
//
// Vite expone el evento `vite:preloadError` específicamente para esto.
// La recomendación oficial (https://vitejs.dev/guide/build.html#load-error-handling)
// es hacer reload al toque para forzar el browser a re-bajar el index.js
// nuevo + el chunk con el hash correcto. El operador ve un flash de la
// página + recarga limpia — mucho mejor que el error fullscreen.
//
// Cubre tanto el chunk JS ("Failed to fetch dynamically imported module")
// como el CSS asociado ("Unable to preload CSS for /assets/X.css",
// Sentry #31): ambos llegan por el mismo evento.
window.addEventListener("vite:preloadError", (event) => {
  // Evitar reloads en loop: usar sessionStorage como circuit breaker.
  // Si ya recargamos por este motivo en los últimos 10s, no insistir
  // (puede ser un problema de red real, no un stale bundle).
  const last = parseInt(sessionStorage.getItem("__vite_reload_at") || "0", 10);
  if (Date.now() - last < 10_000) {
    // ya intentamos hace poco, dejar que el error se propague al
    // GlobalErrorBoundary para que el operador vea algo más que un flash
    // infinito. NO llamamos preventDefault: queremos que Vite re-lance.
    return;
  }
  // NO llamar event.preventDefault() acá. Cancelar el evento hace que el
  // helper __vitePreload de Vite NO re-lance el error → la promesa del
  // import() lazy RESUELVE con `undefined` en vez de rejectar. Entonces
  // React.lazy hace `undefined.default` y tira "Cannot read properties of
  // undefined (reading 'default')" — un error que GlobalErrorBoundary NO
  // reconoce como stale-bundle (isStaleBundleError busca "failed to fetch
  // dynamically imported module", no "default"), así que muestra el cartel
  // rojo genérico "Algo salió mal" en vez del amber "Nueva versión
  // disponible", y encima le gana la carrera al reload → operador trabado.
  // (Regresión introducida por #1062, reportada en staging 2026-08-05.)
  //
  // Dejando que Vite re-lance: el import() rejecta con "Failed to fetch
  // dynamically imported module", React.lazy lo re-tira, GlobalErrorBoundary
  // lo detecta como stale-bundle (fallback amigable) mientras el reload de
  // abajo ya está en vuelo. El ruido en Sentry que motivó el preventDefault
  // ya lo cubre observability.js (ignoreErrors: /Failed to fetch dynamically
  // imported module/i + /Unable to preload CSS/i).
  sessionStorage.setItem("__vite_reload_at", String(Date.now()));
  console.warn("[stale-bundle] vite:preloadError detected, forcing reload", event);
  window.location.reload();
});

// Service Worker: caches hashed /assets/* for offline / flaky-network
// resilience. Prod-only, post-load registration — see registerSW.js for
// the safety contract. The SW never caches HTML or API responses so it
// can't serve stale builds or leak per-user data.
registerServiceWorker();

// Auto-update on new deploy: an open SPA tab never re-fetches index.html on its
// own and the static SW doesn't change between deploys, so a long-lived editor
// tab stays on the old build until a manual reload. This polls for a new build
// and reloads while the tab is backgrounded (never mid-edit; keepalive #727
// persists unsaved work on the reload). See autoUpdate.js.
initAutoUpdate();

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <GlobalErrorBoundary>
      <BrowserRouter>
        <I18nProvider>
          <AlertProvider>
            <ToastProvider>
              <HelpProvider>
                <App />
              </HelpProvider>
            </ToastProvider>
          </AlertProvider>
        </I18nProvider>
      </BrowserRouter>
    </GlobalErrorBoundary>
  </React.StrictMode>
);
