import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { I18nProvider } from "./i18n";
import App from "./App";
import GlobalErrorBoundary from "./components/GlobalErrorBoundary";
import { AlertProvider } from "./components/AlertProvider";
import { ToastProvider } from "./components/ToastProvider";
import "./index.css";

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
window.addEventListener("vite:preloadError", (event) => {
  // Evitar reloads en loop: usar sessionStorage como circuit breaker.
  // Si ya recargamos por este motivo en los últimos 10s, no insistir
  // (puede ser un problema de red real, no un stale bundle).
  const last = parseInt(sessionStorage.getItem("__vite_reload_at") || "0", 10);
  if (Date.now() - last < 10_000) {
    // ya intentamos hace poco, dejar que el GlobalErrorBoundary muestre
    // el error para que el operador vea algo más que un flash infinito.
    return;
  }
  sessionStorage.setItem("__vite_reload_at", String(Date.now()));
  console.warn("[stale-bundle] vite:preloadError detected, forcing reload", event);
  window.location.reload();
});

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <GlobalErrorBoundary>
      <BrowserRouter>
        <I18nProvider>
          <AlertProvider>
            <ToastProvider>
              <App />
            </ToastProvider>
          </AlertProvider>
        </I18nProvider>
      </BrowserRouter>
    </GlobalErrorBoundary>
  </React.StrictMode>
);
