import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { campaignRequest } from "../lib/campaignApi";

const ZONE = "America/Argentina/Buenos_Aires";
export function productionDay(offset = 0, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const get = (type) => parts.find((part) => part.type === type).value;
  const date = new Date(`${get("year")}-${get("month")}-${get("day")}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

export default function OperatorActivity({ optional = false }) {
  const [day, setDay] = useState(() => productionDay());
  const [revision, refresh] = useState(0);
  const [state, setState] = useState({ loading: true });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true });
    campaignRequest(`/batch/activity/me?day=${encodeURIComponent(day)}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setState({ data });
      }).catch((error) => {
        if (!controller.signal.aborted) setState({ error, hidden: optional && error.status === 404 });
      });
    return () => controller.abort();
  }, [day, revision, optional]);
  if (state.hidden) return null;
  const data = state.data;
  return <section aria-label="Mi producción en campañas" className="mb-5 rounded-card bg-surface-2/50 p-5 ring-1 ring-white/[0.09]">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 className="text-lg font-semibold text-white">Mi producción en campañas</h2>
        <p className="text-xs text-ink-secondary">Tu actividad como operador · horario de Argentina (UTC−3)</p>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {[[0, "Hoy"], [-1, "Ayer"]].map(([offset, label]) => <button type="button" key={label} aria-pressed={day === productionDay(offset)}
          className={`rounded-lg px-3 py-2 ${day === productionDay(offset) ? "bg-brand text-white" : "bg-white/5 text-ink-secondary"}`}
          onClick={() => setDay(productionDay(offset))}>{label}</button>)}
        <input aria-label="Fecha de producción" type="date" value={day} max={productionDay()} className="rounded-lg bg-surface-2 px-2 py-2 text-white [color-scheme:dark]"
          onChange={(e) => { if (e.target.value) setDay(e.target.value); }} />
        <button type="button" className="px-2 py-2 text-brand-light underline" onClick={() => refresh((value) => value + 1)}>Actualizar</button>
      </div>
    </div>
    {state.loading && <p role="status" className="mt-4 text-sm text-ink-secondary">Cargando tu producción…</p>}
    {state.error && <p role="alert" className="mt-4 text-sm text-red-300">No pudimos cargar tu producción. Usá Actualizar para reintentar.</p>}
    {data && <>
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
        {[[data.generated, "Canciones convertidas a video", "Primer render terminado ese día"],
          [data.requested, "Canciones nuevas iniciadas", "Primera solicitud aceptada ese día"],
          [data.edited_songs, "Canciones con reedición solicitada", `${data.edit_requests} solicitudes · no suman canciones nuevas`]].map(([count, label, hint]) =>
          <div key={label}><p className="text-3xl font-semibold tabular-nums text-white">{count}</p><p className="text-sm text-white">{label}</p><p className="mt-1 text-xs text-ink-secondary">{hint}</p></div>)}
      </div>
      <p className="mt-4 text-xs text-ink-secondary">De esos videos, hoy: {data.approved_now} aprobados · {data.pending_review_now} por revisar. Se cuenta una vez cada canción, sin sumar formatos ni reintentos.</p>
      {data.campaigns?.length > 0 && <ul className="mt-3 space-y-1 text-sm">{data.campaigns.map((campaign) => <li key={campaign.id}>
        <Link className="text-brand-light hover:underline" to={`/campaigns/${encodeURIComponent(campaign.id)}`}>{campaign.name}</Link><span className="ml-2 font-semibold text-white">{campaign.count}</span>
      </li>)}</ul>}
      {data.songs?.length > 0 && <details className="mt-3 text-sm"><summary className="cursor-pointer text-brand-light">Ver las {data.generated} canciones</summary>
        <ul className="mt-2 max-h-64 space-y-2 overflow-y-auto">{data.songs.map((song) => <li key={song.job_id} className="text-ink-secondary">
          <Link className="hover:text-white hover:underline" to={`/campaigns/${encodeURIComponent(song.campaign_id)}`}>{song.artist} · {song.title}</Link>
        </li>)}</ul>
      </details>}
      <p className="mt-3 text-xs text-ink-secondary">Incluye campañas con registro de generación y render. Las generaciones anteriores a ese registro no están incluidas.</p>
    </>}
  </section>;
}
