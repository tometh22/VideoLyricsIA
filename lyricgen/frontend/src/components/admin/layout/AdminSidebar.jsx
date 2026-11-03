// Sub-navegación del Admin Panel v2: 5 secciones, algunas con sub-vistas.
// Reemplaza las 9 tabs horizontales del monolito — agrupadas por intención
// (¿qué vengo a hacer?) y no por orden de aparición histórico.
//
// badges: { [sectionId]: number } — contadores vivos (CRs pendientes, jobs
// colgados) que el shell calcula desde el contexto.

import GenlyLogo from "../../GenlyLogo";

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
    // Trabajo editorial solicitado por los portales. Es sección principal y
    // no una columna dentro de "Ahora": el operador necesita verla, abrir un
    // pedido y comparar la letra completa sin competir con el pipeline.
    id: "cambios",
    label: "Cambios UMG",
    description: "Pedidos de corrección del portal",
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M4 4h16v16H4z" />
        <path d="M8 9h8M8 13h5" />
        <path d="M15 17l4-4" />
      </svg>
    ),
    subTabs: null,
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
    // Resultado, contrato, reparto con el socio y métricas de negocio.
    // Solo super-admin (como Insights): expone márgenes y mano de obra.
    id: "finanzas",
    label: "Finanzas",
    description: "Resultado, contrato y reparto",
    superAdminOnly: true,
    icon: (
      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
        <path d="M12 1v22" />
        <path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6" />
      </svg>
    ),
    subTabs: [
      { id: "liquidacion", label: "Liquidación por cuota" },
      { id: "resultado", label: "Resultado por mes" },
      { id: "contrato", label: "Contrato y reparto" },
      { id: "metricas", label: "Métricas" },
      { id: "mix", label: "Mix de fondos" },
      { id: "movimientos", label: "Movimientos" },
      { id: "criterio", label: "Criterio" },
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

export default function AdminSidebar({ section, subTab, onNavigate, badges = {}, showInsights = false, onBack }) {
  const items = NAV.filter((item) => (item.id !== "insights" && !item.superAdminOnly) || showInsights);
  return (
    <aside className="admin-side">
      <div className="admin-side__brand">
        <GenlyLogo variant="white" size="sm" priority />
        <span className="admin-side__pill">Admin</span>
      </div>
      {onBack && (
        <button type="button" onClick={onBack} className="admin-side__back">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
          Volver a Genly
        </button>
      )}
      <nav className="admin-side__nav" aria-label="Secciones de administración">
        {items.map((item) => {
          const isActive = section === item.id;
          return (
            <div key={item.id}>
              <button
                type="button"
                onClick={() => onNavigate(item.id, defaultSubTab(item.id))}
                aria-current={isActive ? "page" : undefined}
                className={`admin-side__item ${isActive ? "is-active" : ""}`}
              >
                <span className="admin-side__icon">{item.icon}</span>
                <span className="flex-1 min-w-0">
                  <span className="admin-side__label">{item.label}</span>
                  <span className="admin-side__desc">{item.description}</span>
                </span>
                {badges[item.id] > 0 && <span className="admin-side__badge">{badges[item.id]}</span>}
              </button>
              {/* Sub-tabs visibles solo en la sección activa */}
              {isActive && item.subTabs && (
                <div className="admin-side__subnav">
                  {item.subTabs.map((st) => (
                    <button
                      key={st.id}
                      type="button"
                      onClick={() => onNavigate(item.id, st.id)}
                      aria-pressed={subTab === st.id}
                      className={`admin-side__subitem ${subTab === st.id ? "is-active" : ""}`}
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
    </aside>
  );
}
