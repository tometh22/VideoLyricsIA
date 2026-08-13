// Adopción de features: qué eligen los usuarios al crear videos —
// agregación de Job.render_params que el backend expone en
// /admin/insights/adoption. Mismo patrón visual de barras horizontales
// que FunnelCard (Operación).
//
// markUnused (nivel usuario): features sin uso muestran "nunca usó" — la
// señal de oportunidad (¿no la necesita o no la descubrió?).

const FEATURE_LABELS = {
  lyrics_animation: "Animación de letra",
  line_transition: "Transición de línea",
  effect: "Efecto FX",
  movement_style: "Movimiento de cámara",
  text_case: "Mayúsculas/minúsculas",
  text_contrast: "Contraste de texto",
  title_template: "Plantilla de título",
  style: "Estilo de color",
  delivery_profile: "Perfil de entrega",
};

const VALUE_LABELS = {
  "(default)": "(default)",
  none: "Ninguna",
  karaoke: "Karaoke",
  word_reveal: "Word reveal",
  pop: "Pop",
  glow: "Glow",
  slide_up: "Slide up",
  slide_side: "Slide lateral",
  wipe: "Wipe",
  dissolve_blur: "Dissolve blur",
  upper: "MAYÚSCULAS",
  title: "Tipo Título",
  lower: "minúsculas",
  original: "Original",
  subtle: "Sutil",
  medium: "Medio",
  strong: "Fuerte",
  auto: "Auto",
  centered: "Centrado",
  lower_third: "Lower third",
  badge: "Badge",
};

const FLAG_LABELS = {
  custom_colors: "Colores custom",
  lyric_color: "Color de letra",
  lyric_sung_color: "Color karaoke",
  background_hint: "Prompt de fondo",
  bg_verbatim: "Prompt literal",
  concept: "Concepto",
  title_song_break: "Corte manual de título",
};

const BG_SOURCE_LABELS = {
  library_as_is: "Biblioteca (tal cual)",
  library_variation: "Biblioteca (variación)",
  ai_video: "IA video (Veo)",
  ai_image: "IA imagen",
  other: "Otro / sin rastro",
};

function Bars({ dist, total, onDrill }) {
  const entries = Object.entries(dist || {}).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return <p className="text-label text-gray-600">nunca usó</p>;
  }
  const max = Math.max(total, 1);
  return (
    <div className="space-y-1.5">
      {entries.slice(0, 6).map(([value, count]) => {
        const pct = Math.round((count / max) * 100);
        const row = (
          <>
            <span className="text-label text-gray-400 w-28 shrink-0 truncate" title={value}>
              {VALUE_LABELS[value] || value}
            </span>
            <div className="flex-1 h-5 bg-white/[0.03] rounded-md overflow-hidden ring-1 ring-white/[0.04]">
              <div
                className="h-full rounded-md flex items-center px-1.5"
                style={{
                  width: `${Math.max(5, pct)}%`,
                  background: "linear-gradient(90deg, rgba(139,92,246,0.8), rgba(139,92,246,0.4))",
                }}
              >
                <span className="text-label font-semibold text-white tabular-nums">{count}</span>
              </div>
            </div>
            <span className="text-label text-gray-600 w-9 text-right tabular-nums shrink-0">{pct}%</span>
          </>
        );
        // Con onDrill la barra es un botón: click → quién/qué la usa.
        return onDrill ? (
          <button
            key={value}
            type="button"
            onClick={() => onDrill(value, VALUE_LABELS[value] || value)}
            className="w-full flex items-center gap-2 rounded hover:bg-white/[0.03] transition-colors duration-brand text-left"
            title="Click: quién la usa"
          >
            {row}
          </button>
        ) : (
          <div key={value} className="flex items-center gap-2">{row}</div>
        );
      })}
    </div>
  );
}

export default function AdoptionPanel({ adoption, title = "Qué features se usan", onDrill }) {
  if (!adoption || adoption.total_jobs === 0) return null;
  const denom = adoption.jobs_with_params || adoption.total_jobs;
  const flags = Object.entries(adoption.flags || {});
  const fonts = adoption.font || [];

  return (
    <div className="glass rounded-card p-5">
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <p className="text-section uppercase text-gray-500">{title}</p>
        <span className="text-label text-gray-500">
          basado en {adoption.jobs_with_params}/{adoption.total_jobs} jobs con parámetros
        </span>
      </div>

      <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-5">
        {Object.entries(FEATURE_LABELS).map(([key, label]) => (
          <div key={key}>
            <p className="text-label uppercase tracking-wider text-gray-500 mb-1.5">{label}</p>
            <Bars
              dist={adoption.features?.[key]}
              total={key === "style" || key === "delivery_profile" ? adoption.total_jobs : denom}
              onDrill={onDrill ? (value, lbl) => onDrill(key, value, lbl) : undefined}
            />
          </div>
        ))}

        {/* Tipografías top */}
        <div>
          <p className="text-label uppercase tracking-wider text-gray-500 mb-1.5">Tipografías (top)</p>
          {fonts.length === 0 ? (
            <p className="text-label text-gray-600">nunca eligió una fuente</p>
          ) : (
            <Bars
              dist={Object.fromEntries(fonts.map((f) => [f.value, f.count]))}
              total={denom}
              onDrill={onDrill ? (value, lbl) => onDrill("font", value, lbl) : undefined}
            />
          )}
        </div>

        {/* Fuente del fondo */}
        <div>
          <p className="text-label uppercase tracking-wider text-gray-500 mb-1.5">Fuente del fondo</p>
          <Bars
            dist={Object.fromEntries(
              Object.entries(adoption.background_source || {}).map(([k, v]) => [
                BG_SOURCE_LABELS[k] || k,
                v,
              ])
            )}
            total={adoption.total_jobs}
          />
        </div>

        {/* Personalizaciones (flags por presencia) */}
        <div>
          <p className="text-label uppercase tracking-wider text-gray-500 mb-1.5">Personalizaciones</p>
          <div className="flex flex-wrap gap-1.5">
            {flags.map(([key, count]) => (
              <span
                key={key}
                className={`px-2 py-0.5 rounded-full text-label ring-1 ${
                  count > 0
                    ? "bg-brand/10 ring-brand/30 text-brand-light"
                    : "bg-surface-3/40 ring-white/[0.04] text-gray-600"
                }`}
              >
                {FLAG_LABELS[key] || key}: {count > 0 ? count : "nunca usó"}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
