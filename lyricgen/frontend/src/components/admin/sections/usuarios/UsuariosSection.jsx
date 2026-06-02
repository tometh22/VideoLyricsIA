// Sección "Usuarios" del Admin Panel v2. Dos sub-vistas — Actividad y Gestión —
// que se eligen desde el sidebar del admin (no hay tabs internos acá): el
// padre pasa subTab y esta sección renderiza la vista correspondiente.
import ActividadView from "./ActividadView";
import GestionView from "./GestionView";

export default function UsuariosSection({ subTab }) {
  if (subTab === "gestion") return <GestionView />;
  return <ActividadView />;
}
