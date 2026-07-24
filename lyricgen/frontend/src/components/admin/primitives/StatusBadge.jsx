// Badge de estado unificado. ÚNICO lugar donde un status se convierte en
// color + label en el admin. Mapa por default = jobs; pasar `map` para
// otros dominios (invoices, change requests).
import { JOB_STATUS, STATUS_FALLBACK } from "../adminApi";

export default function StatusBadge({ status, map = JOB_STATUS, className = "" }) {
  const def = map[status] || STATUS_FALLBACK;
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-md text-label ring-1 whitespace-nowrap ${def.classes} ${className}`}
    >
      {def.label || status}
    </span>
  );
}
