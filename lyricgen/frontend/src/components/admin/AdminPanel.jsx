// Admin Panel v2 — shell.
//
// Layout: [sub-sidebar de 4 secciones] | [contenido de la sección activa]
// La navegación es estado local (una sola ruta /admin, sin query params —
// herramienta interna de 2 operadores, no hace falta deep-linking).
//
// El estado transversal (banner de error, stats globales) vive en
// AdminContext; todo lo demás es local de cada sección.
import { useState } from "react";

import { AdminProvider, useAdmin } from "./AdminContext";
import AdminSidebar, {
  SECCIONES,
  SubTabs,
  defaultSubTab,
  subTabValida,
} from "./layout/AdminSidebar";
import OperacionSection from "./sections/operacion/OperacionSection";
import RendimientoSection from "./sections/rendimiento/RendimientoSection";
import InsightsSection from "./sections/insights/InsightsSection";
import GestionSection from "./sections/gestion/GestionSection";

const DESTINO_KEY = "genly_admin_destino";

/** Sección + sub-vista a mostrar al abrir, en orden de prioridad.
 *
 * 1. El hash de la URL (`#/costos/infra`) — un link compartido manda.
 * 2. Lo último visitado, guardado en localStorage.
 * 3. "Ahora", el default de siempre.
 *
 * Tolera basura: una sección que ya no existe (por ejemplo la vieja
 * `insights/margen`) cae al default en vez de dejar la pantalla vacía.
 * Eso ya pasó cuando se eliminó la sub-tab `infra` de Costos.
 */
export function leerDestino(hash = typeof window !== "undefined" ? window.location.hash : "",
                            guardado = null) {
  const valido = (sec, sub) => {
    if (!sec || !SECCIONES.has(sec)) return null;
    const sinDefault = defaultSubTab(sec);
    // Una sub-vista inválida no invalida la sección: se cae al default de
    // ella, que es lo que el usuario esperaría.
    return { section: sec, subTab: sub && subTabValida(sec, sub) ? sub : sinDefault };
  };

  const desdeHash = String(hash || "").replace(/^#\/?/, "").split("/").filter(Boolean);
  const porHash = valido(desdeHash[0], desdeHash[1]);
  if (porHash) return porHash;

  try {
    const raw = guardado ?? window.localStorage.getItem(DESTINO_KEY);
    if (raw) {
      const g = JSON.parse(raw);
      const porGuardado = valido(g.section, g.subTab);
      if (porGuardado) return porGuardado;
    }
  } catch {
    // localStorage bloqueado o JSON roto: no es motivo para no abrir el panel.
  }
  return { section: "ahora", subTab: defaultSubTab("ahora") };
}

function guardarDestino(section, subTab) {
  try {
    window.localStorage.setItem(DESTINO_KEY, JSON.stringify({ section, subTab }));
    // El hash mantiene el link copiable sin agregar una entrada al historial
    // por cada click: `replaceState` no ensucia el botón Atrás del browser.
    const ruta = subTab ? `#/${section}/${subTab}` : `#/${section}`;
    window.history.replaceState(null, "", ruta);
  } catch {
    // Idem: persistir es una comodidad, no un requisito para navegar.
  }
}


function AdminShell({ onBack, isSuperAdmin }) {
  const { adminError, setAdminError, stats } = useAdmin();
  // La navegación PERSISTE y se refleja en la URL.
  //
  // Antes vivía sólo en memoria, así que cada recarga devolvía a "Ahora":
  // quien vive en Costos pagaba dos clicks en cada visita, y no había forma
  // de mandarle a nadie el link de una pantalla. El comentario original
  // decía "somos 2 operadores, no hace falta deep-linking"; hoy hay varias
  // personas y agentes mirando el mismo panel.
  //
  // El hash gana sobre lo guardado: un link compartido tiene que llevarte
  // adonde dice, no adonde estabas vos.
  const inicial = leerDestino();
  const [section, setSection] = useState(inicial.section);
  const [subTab, setSubTab] = useState(inicial.subTab);

  const navigate = (nextSection, nextSubTab) => {
    setSection(nextSection);
    setSubTab(nextSubTab);
    guardarDestino(nextSection, nextSubTab);
  };

  // Badges vivos del sidebar: cosas que necesitan atención del operador.
  const badges = {
    ahora: stats?.jobs?.pending_review || 0,
  };

  // Ancho: antes estaba capado a `max-w-7xl` (1280px). Las tablas del admin
  // llegan a 9 columnas y en un monitor ancho quedaban apretadas mientras
  // sobraba media pantalla. Se suelta con un tope alto para que en pantallas
  // muy anchas el texto no quede en líneas ilegibles.
  return (
    <div className="w-full max-w-[1800px] animate-fade-in">
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
          <SubTabs section={section} subTab={subTab} onNavigate={navigate} />
          {section === "ahora" && <OperacionSection />}
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
