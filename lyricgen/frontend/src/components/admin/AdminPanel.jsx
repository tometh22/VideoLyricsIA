// Admin Panel v2 — shell.
//
// Layout: [sub-sidebar de 5 secciones] | [contenido de la sección activa]
// La navegación vive en una sola ruta /admin. El estado visual sigue siendo
// local, pero la sección activa se refleja en el query string para permitir
// retornos directos desde el editor y enlaces a un pedido puntual.
//
// El estado transversal (banner de error, stats globales) vive en
// AdminContext; todo lo demás es local de cada sección.
import { useEffect, useState } from "react";

import { AdminProvider, useAdmin } from "./AdminContext";
import { API, fetchJson } from "./adminApi";
import AdminSidebar, { defaultSubTab } from "./layout/AdminSidebar";
import OperacionSection from "./sections/operacion/OperacionSection";
import StatusIncidentsPanel from "./sections/operacion/StatusIncidentsPanel";
import ChangeRequestsSection from "./sections/operacion/ChangeRequestsSection";
import RendimientoSection from "./sections/rendimiento/RendimientoSection";
import InsightsSection from "./sections/insights/InsightsSection";
import GestionSection from "./sections/gestion/GestionSection";

function AdminShell({ onBack, isSuperAdmin }) {
  const { adminError, setAdminError, stats } = useAdmin();
  const initialSection = (() => {
    if (typeof window === "undefined") return "ahora";
    const requested = new URLSearchParams(window.location.search).get("section");
    if (requested === "insights" && !isSuperAdmin) return "ahora";
    return ["ahora", "cambios", "rendimiento", "insights", "gestion"].includes(requested)
      ? requested
      : "ahora";
  })();
  const [section, setSection] = useState(initialSection);
  const [subTab, setSubTab] = useState(defaultSubTab(initialSection));
  const [pendingChangeRequests, setPendingChangeRequests] = useState(0);

  // El badge debe ser visible antes de entrar a la pantalla de Cambios.
  // Es una consulta mínima y best-effort; la sección carga el detalle recién
  // cuando el operador la abre.
  useEffect(() => {
    let active = true;
    const loadPendingCount = () => {
      fetchJson(`${API}/admin/change-requests?status=pending&limit=1`)
        .then((data) => {
          if (active) setPendingChangeRequests(data.pending_count || 0);
        })
        .catch(() => {});
    };
    loadPendingCount();
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadPendingCount();
    }, 30000);
    return () => {
      active = false;
      clearInterval(iv);
    };
  }, []);

  const navigate = (nextSection, nextSubTab) => {
    setSection(nextSection);
    setSubTab(nextSubTab);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("section", nextSection);
      if (nextSection !== "cambios") {
        url.searchParams.delete("change_request_id");
        url.searchParams.delete("render_submitted");
      }
      window.history.replaceState(window.history.state, "", url);
    }
  };

  // Badges vivos del sidebar: cosas que necesitan atención del operador.
  const badges = {
    ahora: stats?.jobs?.pending_review || 0,
    cambios: pendingChangeRequests,
  };

  return (
    <div
      data-testid="admin-shell"
      className={`w-full animate-fade-in ${section === "cambios" ? "max-w-none" : "max-w-7xl"}`}
    >
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={onBack}
          className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors duration-brand"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-2xl font-bold">Admin</h1>
          <p className="text-ui text-gray-500">Operación de la plataforma</p>
        </div>
      </div>

      {/* Banner de error de mutaciones — visible desde cualquier sección.
          Una acción rechazada por el backend NUNCA puede parecer exitosa. */}
      {adminError && (
        <div className="mb-4 rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-4 py-3 flex items-start gap-3">
          <svg className="w-5 h-5 text-red-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <div className="flex-1 text-ui text-red-200">{adminError}</div>
          <button
            type="button"
            onClick={() => setAdminError(null)}
            className="text-caption text-red-300 hover:text-red-100 px-2 py-1"
          >
            ✕
          </button>
        </div>
      )}

      {/* Sub-sidebar + contenido */}
      <div className="admin-workspace-layout">
        <AdminSidebar
          section={section}
          subTab={subTab}
          onNavigate={navigate}
          badges={badges}
          showInsights={isSuperAdmin}
        />
        <div className="flex-1 min-w-0">
          {section === "ahora" && subTab === "estado-publico"
            ? <StatusIncidentsPanel />
            : section === "ahora" && <OperacionSection />}
          {section === "cambios" && (
            <ChangeRequestsSection
              initialPendingCount={pendingChangeRequests}
              onPendingCountChange={setPendingChangeRequests}
            />
          )}
          {section === "rendimiento" && <RendimientoSection />}
          {/* Doble guard: el sidebar ya oculta la entrada, pero si el flag
              quedó stale en localStorage el render también la niega. La
              seguridad real son los 403 del backend. */}
          {section === "insights" && isSuperAdmin && <InsightsSection subTab={subTab} />}
          {section === "gestion" && <GestionSection subTab={subTab} />}
        </div>
      </div>
    </div>
  );
}

export default function AdminPanel({ onBack, isSuperAdmin = false }) {
  return (
    <AdminProvider>
      <AdminShell onBack={onBack} isSuperAdmin={isSuperAdmin} />
    </AdminProvider>
  );
}
