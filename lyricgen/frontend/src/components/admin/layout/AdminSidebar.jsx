// Sub-navegación del Admin Panel v2: 4 secciones, cada una con sub-vistas.
// Reemplaza las 9 tabs horizontales del monolito — agrupadas por intención
// (¿qué vengo a hacer?) y no por orden de aparición histórico.
//
// badges: { [sectionId]: number } — contadores vivos (CRs pendientes, jobs
// colgados) que el shell calcula desde el contexto.

const NAV = [
  {
    // ¿Está sano AHORA? — triaje en vivo: zombies, salud del sistema,
    // pipeline. Lo analítico vive en Rendimiento (consolidación 2026-06-11).
    id: "ahora",
    label: "Ahora",
    description: "Salud y pipeline en vivo",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
      </svg>
    ),
    // "Estado público" cuelga de acá y no de Gestión a propósito: redactar
    // un incidente es parte del triaje en vivo, no de la administración.
    // Cuando algo se rompe, el operador ya está en esta sección.
    subTabs: [
      { id: "pipeline", label: "Pipeline en vivo" },
      { id: "estado-publico", label: "Estado público" },
    ],
  },
  {
    // ¿Mejor o peor que antes? — salud por cuenta, funnel, KPIs WoW.
    id: "rendimiento",
    label: "Rendimiento",
    description: "Tendencias y salud por cuenta",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M3 3v18h18" />
        <path d="M7 14l4-4 4 4 5-6" />
      </svg>
    ),
    subTabs: null,
  },
  {
    // ¿Qué hace cada usuario? — solo super-admin (showInsights).
    id: "insights",
    label: "Insights",
    description: "Uso, features y margen",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <circle cx="11" cy="11" r="8" />
        <path d="M21 21l-4.35-4.35" />
      </svg>
    ),
    // Feedback Tomi 2026-06-11: "todo como scroll, sin sub-pestañas, no
    // podés profundizar". Cada tab es UNA pregunta; el alcance
    // (app→tenant→usuario) se elige con el breadcrumb dentro de la sección.
    subTabs: [
      { id: "resumen", label: "Resumen" },
      { id: "features", label: "Features" },
      { id: "wizard", label: "Wizard" },
      { id: "margen", label: "Margen" },
      { id: "aprendizaje", label: "Aprendizaje" },
    ],
  },
  {
    // Administrar: cuentas, fondos, compliance, costos, facturación.
    id: "gestion",
    label: "Gestión",
    description: "Cuentas, contenido y negocio",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33h.01a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51h.01a1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v.01a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z" />
      </svg>
    ),
    subTabs: [
      { id: "usuarios", label: "Usuarios" },
      { id: "fondos", label: "Biblioteca de fondos" },
      { id: "compliance", label: "Compliance UMG" },
      // Una sola entrada: "Costos y márgenes" y "Costo de infra" eran dos
      // pantallas con dos controles de tiempo distintos que respondían la
      // misma pregunta con números que no coincidían.
      { id: "costos", label: "Costos" },
      { id: "facturacion", label: "Facturación" },
      { id: "creditos", label: "Créditos de regalo" },
    ],
  },
];

// Sub-tab default de cada sección (la primera).
export function defaultSubTab(sectionId) {
  const section = NAV.find((s) => s.id === sectionId);
  return section?.subTabs?.[0]?.id ?? null;
}

export default function AdminSidebar({ section, subTab, onNavigate, badges = {}, showInsights = false }) {
  const items = NAV.filter((item) => item.id !== "insights" || showInsights);
  return (
    <nav className="admin-subnav" aria-label="Secciones de administración">
      {items.map((item) => {
        const isActive = section === item.id;
        return (
          <div key={item.id}>
            <button
              type="button"
              onClick={() => onNavigate(item.id, defaultSubTab(item.id))}
              aria-current={isActive ? "page" : undefined}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-button text-left transition-colors duration-brand ${
                isActive
                  ? "bg-brand/15 ring-1 ring-brand/30 text-white"
                  : "text-gray-400 hover:text-white hover:bg-white/[0.03]"
              }`}
            >
              <span className={isActive ? "text-brand-light" : "text-gray-500"}>{item.icon}</span>
              <span className="flex-1 min-w-0">
                <span className="block text-ui font-medium leading-tight">{item.label}</span>
                <span className="block text-section text-gray-600 mt-0.5 normal-case tracking-normal">
                  {item.description}
                </span>
              </span>
              {badges[item.id] > 0 && (
                <span className="shrink-0 min-w-[1.25rem] text-center text-section font-bold rounded-full px-1.5 py-0.5 bg-amber-500/20 text-amber-300">
                  {badges[item.id]}
                </span>
              )}
            </button>
            {/* Sub-tabs visibles solo en la sección activa */}
            {isActive && item.subTabs && (
              <div className="mt-1 mb-2 ml-9 space-y-0.5">
                {item.subTabs.map((st) => (
                  <button
                    key={st.id}
                    type="button"
                    onClick={() => onNavigate(item.id, st.id)}
                    aria-pressed={subTab === st.id}
                    className={`block w-full text-left px-3 py-1.5 rounded-md text-caption transition-colors duration-brand ${
                      subTab === st.id
                        ? "text-white bg-white/[0.05]"
                        : "text-gray-500 hover:text-gray-300"
                    }`}
                  >
                    {st.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}
