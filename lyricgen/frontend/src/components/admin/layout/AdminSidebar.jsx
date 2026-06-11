// Sub-navegación del Admin Panel v2: 4 secciones, cada una con sub-vistas.
// Reemplaza las 9 tabs horizontales del monolito — agrupadas por intención
// (¿qué vengo a hacer?) y no por orden de aparición histórico.
//
// badges: { [sectionId]: number } — contadores vivos (CRs pendientes, jobs
// colgados) que el shell calcula desde el contexto.

const NAV = [
  {
    id: "operacion",
    label: "Operación",
    description: "Salud, pipeline y pedidos",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
      </svg>
    ),
    subTabs: null,
  },
  {
    // Solo super-admin (showInsights) — el panel del CEO: comportamiento
    // detallado por app/tenant/usuario. Oculto por completo para el resto.
    id: "insights",
    label: "Insights",
    description: "Uso, features y retrabajo",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M3 3v18h18" />
        <path d="M7 14l4-4 4 4 5-6" />
      </svg>
    ),
    subTabs: null,
  },
  {
    id: "usuarios",
    label: "Usuarios",
    // La vieja sub-vista "Actividad" fue absorbida por Insights (2026-06-10).
    description: "Gestión de cuentas",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
        <circle cx="9" cy="7" r="4" />
        <path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" />
      </svg>
    ),
    subTabs: null,
  },
  {
    id: "contenido",
    label: "Contenido",
    description: "Fondos y compliance",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="8.5" cy="8.5" r="1.5" />
        <path d="M21 15l-5-5L5 21" />
      </svg>
    ),
    subTabs: [
      { id: "fondos", label: "Biblioteca de fondos" },
      { id: "compliance", label: "Compliance UMG" },
    ],
  },
  {
    id: "negocio",
    label: "Negocio",
    description: "Costos y facturación",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <line x1="12" y1="1" x2="12" y2="23" />
        <path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6" />
      </svg>
    ),
    subTabs: [
      { id: "costos", label: "Costos y márgenes" },
      { id: "invoices", label: "Facturación" },
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
    <nav className="w-52 shrink-0 space-y-1">
      {items.map((item) => {
        const isActive = section === item.id;
        return (
          <div key={item.id}>
            <button
              type="button"
              onClick={() => onNavigate(item.id, defaultSubTab(item.id))}
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
