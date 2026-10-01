// What the client's portal shows. Two small controls:
//  - ClientVisibilityControl: per delivery, "Automático / Siempre visible / Oculto".
//  - PublicationModeSwitch: global, publish copying files or without copies.
// The wording says what the CLIENT sees, never how the files are stored.

const OPTIONS = [
  { value: "auto", label: "Automático" },
  { value: "visible", label: "Siempre visible" },
  { value: "hidden", label: "Oculto" },
];

export function visibilityMessage(publication) {
  const mode = publication?.client_visibility || "auto";
  const hidden = publication?.hidden_from_client === true;
  if (mode === "hidden") return "Oculto: el cliente no ve este video hasta que lo muestres.";
  if (mode === "visible") return "Siempre visible: el cliente ve el video aunque haya cambios sin publicar.";
  if (hidden) return "Oculto por ahora: tiene cambios sin publicar. Aparece solo cuando lo publiques.";
  return "Visible. Si el video se edita, se oculta solo hasta que lo publiques.";
}

export function ClientVisibilityControl({ publication, deliveryId, busy = false, onChange }) {
  if (!publication || deliveryId == null) return null;
  const mode = publication.client_visibility || "auto";
  return (
    <section aria-label="Qué ve el cliente" className="rounded-xl bg-white/[0.03] p-3 ring-1 ring-white/[0.08]">
      <p className="text-label font-semibold uppercase tracking-[0.16em] text-gray-500">Qué ve el cliente</p>
      <p className="mt-2 text-caption leading-relaxed text-gray-200">{visibilityMessage(publication)}</p>
      <div role="group" aria-label="Visibilidad para el cliente" className="mt-2 flex flex-wrap gap-1.5">
        {OPTIONS.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={mode === option.value}
            disabled={busy}
            onClick={() => { if (mode !== option.value) onChange?.(deliveryId, option.value); }}
            className={`rounded-full px-3 py-1 text-label font-medium ring-1 transition disabled:opacity-50 ${
              mode === option.value
                ? "bg-brand/20 text-white ring-brand/50"
                : "bg-white/[0.03] text-gray-400 ring-white/10 hover:text-white"
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>
      {mode === "hidden" && (
        <p className="mt-2 text-label text-gray-500">
          Los enlaces que el cliente ya abrió siguen funcionando hasta que venzan (7 días).
        </p>
      )}
    </section>
  );
}

export function PublicationModeSwitch({ mode, busy = false, onChange }) {
  const pointer = mode === "pointer";
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl bg-white/[0.03] px-3 py-2 ring-1 ring-white/[0.08]" aria-label="Modo de publicación">
      <label className="flex items-center gap-2 text-caption text-gray-200">
        <input
          type="checkbox"
          role="switch"
          checked={pointer}
          disabled={busy}
          onChange={(event) => onChange?.(event.target.checked ? "pointer" : "snapshot")}
        />
        <span className="font-medium">Publicar sin copiar archivos</span>
      </label>
      <span className="text-label text-gray-500">
        {pointer
          ? "Publicar es instantáneo y no ocupa espacio extra. Cada video se oculta solo mientras tiene cambios sin publicar."
          : "Cada publicación guarda una copia del video (más lento y ocupa espacio). El cliente sigue viendo la versión anterior."}
      </span>
    </div>
  );
}
