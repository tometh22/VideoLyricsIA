import { Component } from "react";

function ErrorFallback({ error, onReset }) {
  return (
    <div className="min-h-screen bg-[#09090f] flex items-center justify-center px-6">
      <div className="max-w-md w-full text-center">
        <div className="w-14 h-14 mx-auto mb-6 rounded-2xl bg-red-500/10 flex items-center justify-center ring-1 ring-red-500/20">
          <svg className="w-7 h-7 text-red-400" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"/>
          </svg>
        </div>
        <h1 className="text-xl font-semibold text-white mb-2">Algo salió mal</h1>
        <p className="text-sm text-gray-400 mb-6">
          Ocurrió un error inesperado. Podés intentar recargar la página.
        </p>
        {error?.message && (
          <pre className="text-left text-xs text-gray-500 bg-surface-2/40 rounded-xl px-4 py-3 mb-6 overflow-auto max-h-32 ring-1 ring-white/[0.04]">
            {error.message}
          </pre>
        )}
        <div className="flex gap-3 justify-center">
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
