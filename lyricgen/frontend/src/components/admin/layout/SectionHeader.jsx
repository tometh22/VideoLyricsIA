// Header de sección: título + subtítulo + slot de acciones a la derecha.
// Toda sección/sub-vista del admin arranca con uno de estos — es lo que
// hace que el panel se sienta "una sola herramienta" y no 9 pantallas
// pegadas.
export default function SectionHeader({ title, subtitle, right }) {
  return (
    <div className="flex items-start justify-between gap-4 mb-5">
      <div>
        <h2 className="text-lg font-semibold text-white">{title}</h2>
        {subtitle && <p className="text-caption text-gray-500 mt-0.5">{subtitle}</p>}
      </div>
      {right && <div className="flex items-center gap-2 shrink-0">{right}</div>}
    </div>
  );
}
