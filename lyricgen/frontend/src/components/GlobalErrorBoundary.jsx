import { Component } from "react";

// QA fix 2026-05-28: detectar el caso específico del bundle viejo
// post-deploy. Vite/Vercel renombran los hashes de los chunks lazy
// (Settings-XXX.js, etc) en cada build; las tabs con el index.js viejo
// fallan al import('./route') con este mensaje. El listener de
// vite:preloadError en main.jsx ya hace reload automático para la
// mayoría de los casos, pero si llega acá igual (porque pasó por React
// promise.then o similar y no por el event), distinguimos para mostrar
// un copy más útil + un botón que hace hard reload de verdad (no solo
// reset del state de React, que vuelve a tirar el error en el próximo
// render).
function isStaleBundleError(error) {
  if (!error) return false;
  const msg = String(error.message || error).toLowerCase();
  return (
    msg.includes("failed to fetch dynamically imported module") ||
    msg.includes("error loading dynamically imported module") ||
    msg.includes("importing a module script failed") ||
    msg.includes("dynamically imported module") ||
    msg.includes("chunkloaderror")
  );
}

function ErrorFallback({ error, onReset }) {
  const stale = isStaleBundleError(error);
  return (
    <div className="min-h-screen bg-[#09090f] flex items-center justify-center px-6">
      <div className="max-w-md w-full text-center">
        <div className={`w-14 h-14 mx-auto mb-6 rounded-2xl flex items-center justify-center ring-1 ${
          stale ? "bg-amber-500/10 ring-amber-500/20" : "bg-red-500/10 ring-red-500/20"
        }`}>
          {stale ? (
            <svg className="w-7 h-7 text-amber-400" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99" />
            </svg>
          ) : (
            <svg className="w-7 h-7 text-red-400" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"/>
            </svg>
          )}
        </div>
        <h1 className="text-xl font-semibold text-white mb-2">
          {stale ? "Nueva versión disponible" : "Algo salió mal"}
        </h1>
        <p className="text-sm text-gray-400 mb-6">
          {stale
            ? "Subimos una versión nueva mientras tenías la app abierta. Recargá la página y volvés a entrar a lo que estabas haciendo."
            : "Ocurrió un error inesperado. Podés intentar recargar la página."}
        </p>
        {!stale && error?.message && (
          <pre className="text-left text-xs text-gray-500 bg-surface-2/40 rounded-xl px-4 py-3 mb-6 overflow-auto max-h-32 ring-1 ring-white/[0.04]">
            {error.message}
          </pre>
        )}
        <div className="flex gap-3 justify-center">
          {stale ? (
            <button
              onClick={() => window.location.reload()}
              className="px-5 py-2.5 rounded-xl bg-brand hover:bg-brand-light text-white text-sm font-medium transition-colors"
            >
              Recargar página
            </button>
          ) : (
            <>
              <button
                onClick={onReset}
                className="px-5 py-2.5 rounded-xl bg-brand/20 hover:bg-brand/30 text-brand-light text-sm font-medium transition-colors ring-1 ring-brand/30"
              >
                Reintentar
              </button>
              <button
                onClick={() => window.location.href = "/"}
                className="px-5 py-2.5 rounded-xl bg-surface-2/60 hover:bg-surface-2 text-gray-300 text-sm font-medium transition-colors ring-1 ring-white/[0.06]"
              >
                Ir al inicio
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default class GlobalErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    console.error("[GlobalErrorBoundary] Uncaught error:", error, info);
    // Surface to Sentry (no-op when VITE_SENTRY_DSN is unset). React's
    // componentStack is the most useful breadcrumb for debugging an
    // uncaught render error — Sentry shows it as the "React" frame in
    // the issue UI.
    try {
      // Lazy require to avoid hard-coupling the boundary to Sentry;
      // observability.js handles the missing-DSN no-op cleanly.
      import("../observability.js").then((obs) => {
        obs.captureHandledError?.(error, {
          source: "GlobalErrorBoundary",
          componentStack: info?.componentStack,
        });
      });
    } catch {
      /* never let the boundary itself throw */
    }
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <ErrorFallback
          error={this.state.error}
          onReset={this.handleReset}
        />
      );
    }
    return this.props.children;
  }
}
