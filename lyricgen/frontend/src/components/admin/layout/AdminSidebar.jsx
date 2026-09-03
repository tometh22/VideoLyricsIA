// Navegación del Admin: 4 secciones agrupadas por intención (¿qué vengo a
// hacer?), no por orden histórico de aparición.
//
// POR QUÉ LA COLUMNA ES PLANA
// Antes cada ítem ocupaba dos líneas —label + una descripción que se lee
// una vez y pesa para siempre— y las sub-vistas se anidaban indentadas
// debajo del ítem activo. Con Gestión abierta la columna tenía 10 destinos
// y la sección activa se perdía entre sus propios hijos.
//
// Ahora la columna lista SÓLO las 4 secciones, en una línea cada una. Las
// sub-vistas pasaron a `SubTabs`, una fila horizontal arriba del contenido:
// se leen de un golpe, no empujan el resto hacia abajo, y el nivel
// "¿dónde estoy?" queda separado del "¿qué parte miro?".
//
// La descripción de cada sección sobrevive como `title` del botón: sigue
// disponible para quien la necesite, sin ocupar la pantalla del que ya sabe.
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
/** Los ids de sección válidos. Se derivan del NAV para que no exista una
 * segunda lista que se olvide de actualizar. */
export const SECCIONES = new Set(NAV.map((n) => n.id));

/** ¿`subTab` existe dentro de `sectionId`?
 *
 * Lo usa la restauración de navegación: un destino guardado puede apuntar a
 * una sub-vista que ya no existe. Pasó de verdad cuando se eliminó
 * `gestion/infra` al consolidar Costos.
 */
export function subTabValida(sectionId, subTab) {
  const s = NAV.find((n) => n.id === sectionId);
  return Boolean(s?.subTabs?.some((t) => t.id === subTab));
}

export function defaultSubTab(sectionId) {
  const section = NAV.find((s) => s.id === sectionId);
  return section?.subTabs?.[0]?.id ?? null;
}

/** Las sub-vistas de una sección, como fila horizontal arriba del contenido.
 *
 * Vive acá y no en cada sección porque el NAV es la única fuente de verdad
 * de qué sub-vistas existen: tenerlas en dos lados fue justamente lo que
 * dejó a `Insights` diciendo en su encabezado "una sola vista, sin
 * sub-tabs" mientras el nav definía cinco.
 *
 * Devuelve `null` cuando la sección no tiene sub-vistas, así el contenido
 * sube y no queda una barra vacía ocupando lugar.
 */
export function SubTabs({ section, subTab, onNavigate }) {
  const item = NAV.find((n) => n.id === section);
  if (!item?.subTabs?.length) return null;
  return (
    <div
      role="tablist"
      aria-label={`Vistas de ${item.label}`}
      className="flex items-center gap-1 flex-wrap border-b border-white/[0.06] mb-5 -mt-1"
    >
      {item.subTabs.map((st) => {
        const activo = subTab === st.id;
        return (
          <button
            key={st.id}
            type="button"
            role="tab"
            aria-selected={activo}
            onClick={() => onNavigate(section, st.id)}
            // El subrayado marca el activo en vez de un fondo: es más
            // liviano que una píldora y no compite con los KPIs de abajo.
            className={`px-3 py-2 text-caption -mb-px border-b-2 transition-colors duration-brand ${
              activo
                ? "border-brand text-white"
                : "border-transparent text-gray-500 hover:text-gray-200"
            }`}
          >
            {st.label}
          </button>
        );
      })}
    </div>
  );
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
              title={item.description}
              className={`w-full flex items-center gap-3 px-3 py-2 rounded-button text-left transition-colors duration-brand ${
                isActive
                  ? "bg-brand/15 ring-1 ring-brand/30 text-white"
                  : "text-gray-400 hover:text-white hover:bg-white/[0.03]"
              }`}
            >
              <span className={isActive ? "text-brand-light" : "text-gray-500"}>{item.icon}</span>
              <span className="flex-1 min-w-0 text-ui font-medium leading-tight">
                {item.label}
              </span>
              {badges[item.id] > 0 && (
                <span className="shrink-0 min-w-[1.25rem] text-center text-section font-bold rounded-full px-1.5 py-0.5 bg-amber-500/20 text-amber-300">
                  {badges[item.id]}
                </span>
              )}
            </button>
          </div>
        );
      })}
    </nav>
  );
}
