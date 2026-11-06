import { stageMeta } from "../../lib/campaignPipeline";

// Human QC of Ricardo's 100 approved CancioneroRock lyrics, 30 Oct 2026.
// This is a campaign-specific review queue, not a new pipeline stage: songs
// can appear here while they are in Letra or Lista.
export const RICARDO_PRIORITY_CAMPAIGN = "5f1bd3aa1b31";
export const RICARDO_PRIORITY = [
  { jobId: "49c027206392", title: "Sr. Cobranza", artist: "Bersuit Vergarabat", lines: "36, 38", finding: "Dos líneas muy largas; cotejar «son capaz» y la división con el audio." },
  { jobId: "9341692b6c93", title: "Seguir Viviendo Sin Tu Amor", artist: "Luis Alberto Spinetta", lines: "6, 14, 22", finding: "«Y quedara yo atrapado aquí» se repite también en la planilla. Verificar la frase cantada antes de corregir." },
  { jobId: "4cf10efa11d8", title: "Chalamán", artist: "Los Abuelos De La Nada", lines: "27", finding: "«ya o me haces» difiere de «ya no me haces» en la primera repetición y en la planilla. Cotejar con el audio." },
  { jobId: "3865d13e26e3", title: "El Arriero", artist: "Divididos", lines: "19", finding: "«camios» difiere de «caminos» en otra repetición. Confirmar la palabra cantada." },
  { jobId: "6b2406ef8a82", title: "Celdas", artist: "Los Enanitos Verdes", lines: "27", finding: "«inimal» difiere de «animal» en otras repeticiones; la planilla también contiene esa variante. Confirmar con el audio." },
  { jobId: "dfbf892f4d5b", title: "Era Un Ángel", artist: "Los Enanitos Verdes", lines: "39, 40", finding: "Dos líneas largas. Revisar legibilidad y cortes sin cambiar el sentido." },
  { jobId: "6153c240f62b", title: "No Hieras Mi Corazón", artist: "GIT", lines: "1, 5, 10, 13, 23", finding: "Tildes de «corazón» corregidas; cotejar la frase de la línea 13 con el audio y reaprobar." , corrected: true },
  { jobId: "06b732b51d02", title: "Se Viene", artist: "Bersuit Vergarabat", lines: "12, 24, 54", finding: "Tildes de «qué es» corregidas. Verificar y reaprobar.", corrected: true },
  { jobId: "c72601f10898", title: "Ciudad De Pobres Corazones", artist: "Fito Páez", lines: "19, 20", finding: "Tildes de «verás» corregidas. Verificar y reaprobar.", corrected: true },
  { jobId: "338ac71148bd", title: "Viento Loco", artist: "GIT", lines: "16", finding: "Tildes de «sé» y «habrá» corregidas. Verificar y reaprobar.", corrected: true },
  { jobId: "15c5a575f577", title: "A La Carga Mi Rock'n Roll", artist: "La Renga", lines: "6", finding: "Punto final retirado según el estilo de Lyrics. Verificar y reaprobar.", corrected: true },
  { jobId: "a5e8bdab4cf0", title: "Desnudo Para Siempre (O Despedazado)", artist: "La Renga", lines: "9", finding: "Punto final retirado según el estilo de Lyrics. Verificar y reaprobar.", corrected: true },
  { jobId: "db00052dc842", title: "El Hombre De La Estrella", artist: "La Renga", lines: "4", finding: "Punto final retirado según el estilo de Lyrics. Verificar y reaprobar.", corrected: true },
  { jobId: "d88f417f1889", title: "Hacé Lo Que Quieras", artist: "Los Pericos", lines: "3, 8, 21", finding: "Tildes de «sé», «cómo», «ahí» y «corazón» corregidas. Verificar y reaprobar.", corrected: true },
  { jobId: "f19744454482", title: "Días Duros", artist: "Rata Blanca", lines: "32", finding: "Punto final retirado según el estilo de Lyrics. Verificar y reaprobar.", corrected: true },
  { jobId: "00e6dc40f2d3", title: "Guerrero Del Arco Iris", artist: "Rata Blanca", lines: "4", finding: "Tilde de «qué harás» corregida. Verificar y reaprobar.", corrected: true },
  { jobId: "27ac22e4816c", title: "Homero", artist: "Viejas Locas", lines: "8", finding: "Tilde de «cuándo» corregida. Verificar y reaprobar.", corrected: true },
];

export default function PriorityReview({ songs = [], onOpen }) {
  const byJob = new Map(songs.map((song) => [song.job_id, song]));
  return <section className="overflow-hidden rounded-card bg-surface-2/40 ring-1 ring-amber-400/20">
    <div className="border-b border-white/[0.06] p-5">
      <h2 className="text-lg font-semibold text-white">Canciones prioritarias para revisar</h2>
      <p className="mt-1 text-sm text-ink-secondary">17 canciones señaladas en la auditoría de las 100 letras aprobadas por Ricardo. Las correcciones de texto vuelven a Letra; las dudas de interpretación y los cortes largos requieren escucha antes de generar.</p>
      <p className="mt-2 text-xs text-amber-200">La auditoría no confirmó la sincronía con la voz. No se modificaron tiempos automáticamente.</p>
    </div>
    <ul className="divide-y divide-white/[0.06]">
      {RICARDO_PRIORITY.map((item) => {
        const song = byJob.get(item.jobId);
        return <li key={item.jobId} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><strong className="text-sm text-white">{item.title}</strong><span className="text-xs text-ink-tertiary">{item.artist}</span></div>
            <p className="mt-1 text-sm text-ink-secondary">Línea{item.lines.includes(",") ? "s" : ""} {item.lines} · {item.finding}</p>
            <p className="mt-1 text-xs text-amber-200">{item.corrected ? (song?.stage === "ready" ? "Reaprobada tras la corrección" : "Corrección guardada · pendiente de revalidación") : "Señal para cotejo manual"}{song?.stage ? ` · ${stageMeta(song.stage).title}` : ""}</p>
          </div>
          <button type="button" onClick={() => onOpen(item.jobId)} className="shrink-0 rounded-lg bg-brand/15 px-3 py-2 text-xs font-semibold text-brand-light hover:bg-brand/25">Abrir letra</button>
        </li>;
      })}
    </ul>
  </section>;
}
