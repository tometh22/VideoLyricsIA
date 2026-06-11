// Sección "Usuarios" del Admin Panel v2 — gestión de cuentas.
//
// La sub-vista "Actividad" (solo super-admin) fue absorbida por la sección
// Insights (2026-06-10): mismo dato, mejor dispuesto — jerarquía
// app → tenant → usuario en vez de drill-down anidado en una tabla.
import GestionView from "./GestionView";

export default function UsuariosSection() {
  return <GestionView />;
}
