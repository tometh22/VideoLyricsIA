// Mix de fondos del contrato UMG: Veo vs foto + efecto de lo que el cliente
// tiene publicado en los portales. Se tildan las campañas que entran en la
// cuenta del contrato y se calcula el total y lo que falta para el 50/50.
import { useEffect, useMemo, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import KpiCard from "../../primitives/KpiCard";
import { loadSaved, save, summarize } from "./mixFondosModel";

const PORTAL = { argentina: "Argentina", chile: "Chile" };
const MODEL = { lite: "Lite", fast: "Fast", otro: "otro modelo", reuso: "clip reusado" };
const TYPE = { veo: "Video Veo", foto: "Foto + efecto", sin_dato: "Sin dato" };
const pct = (v) => (v === null ? "—" : `${(v * 100).toLocaleString("es-AR", { maximumFractionDigits: 1 })}%`);
const day = (iso) => (iso ? new Date(iso).toLocaleDateString("es-AR", { day: "numeric", month: "short" }) : "—");

function FotoBar({ veo, foto }) {
  const total = veo + foto;
  const share = total ? (foto / total) * 100 : 0;
  return (
    <div className="relative h-1.5 w-24 rounded-full bg-brand/40" title={`${Math.round(share)}% foto`}>
      <div className="absolute inset-y-0 left-0 rounded-full bg-accent" style={{ width: `${share}%` }} />
      <div className="absolute inset-y-[-3px] left-1/2 w-px bg-white/50" />
    </div>
  );
}

export default function MixFondosView() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [meta, setMeta] = useState(() => loadSaved()?.meta || "");
  const [showItems, setShowItems] = useState(false);
  const [typeFilter, setTypeFilter] = useState("");

  useEffect(() => {
    let active = true;
    fetchJson(`${API}/admin/finance/background-mix`)
      .then((res) => {
        if (!active) return;
        setData(res);
        const saved = loadSaved()?.selected;
        const keys = new Set((res.groups || []).map((g) => g.key));
        // Primera vez: todo tildado. Después, lo que se dejó tildado.
        setSelected(new Set(saved ? saved.filter((k) => keys.has(k)) : keys));
      })
      .catch((err) => active && setError(err.message));
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (selected) save(selected, meta);
  }, [selected, meta]);

  const groups = useMemo(() => data?.groups || [], [data]);
  const s = useMemo(() => summarize(groups, selected || new Set(), meta), [groups, selected, meta]);
  const items = useMemo(
    () => (data?.items || []).filter((i) => selected?.has(i.group) && (!typeFilter || i.type === typeFilter)),
    [data, selected, typeFilter],
  );

  if (error) return <p className="text-caption text-red-300">No se pudo cargar el mix: {error}</p>;
  if (!data || !selected) return <p className="text-caption text-gray-500">Cargando…</p>;

  const toggle = (key) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const pick = (filter) => setSelected(new Set(groups.filter(filter).map((g) => g.key)));
  const quick = "px-2 py-1 rounded text-label ring-1 ring-white/[0.08] text-gray-300 hover:text-white hover:bg-white/[0.05]";

  return (
    <div className="space-y-5">
      <div className="grid gap-3 md:grid-cols-4">
        <KpiCard label="Video Veo" value={pct(s.pctVeo)} hint={`${s.veo} de ${s.veo + s.foto} videos · meta 50%`} tone={s.pctVeo !== null && s.pctVeo > 0.5 ? "warn" : "brand"} />
        <KpiCard label="Foto + efecto" value={pct(s.pctFoto)} hint={`${s.foto} de ${s.veo + s.foto} videos · meta 50%`} tone={s.pctFoto !== null && s.pctFoto > 0.5 ? "warn" : "accent"} />
        <KpiCard
          label="Para el 50/50"
          value={s.faltanFoto ? `${s.faltanFoto} fotos` : s.faltanVeo ? `${s.faltanVeo} Veo` : "Parejo"}
          hint={s.faltanFoto || s.faltanVeo ? "sobre lo ya entregado, sin sumar del otro tipo" : "mismo número de cada tipo"}
          tone={s.faltanFoto || s.faltanVeo ? "warn" : "accent"}
        />
        <KpiCard
          label="Veo que no es Lite"
          value={String(s.veoNoLite)}
          hint={`Lite ${s.modelos.lite || 0} · reusados ${s.modelos.reuso || 0}`}
          tone={s.veoNoLite ? "warn" : "default"}
        />
      </div>

      <section className="glass rounded-card p-4 flex flex-wrap items-center gap-3 text-caption text-gray-300">
        <label className="flex items-center gap-2">
          <span>Meta del contrato</span>
          <input
            type="number" min="0" inputMode="numeric" aria-label="Meta del contrato"
            className="w-24 bg-surface-3/60 ring-1 ring-white/[0.08] rounded px-2 py-1 text-white tabular-nums focus:outline-none focus:ring-brand/40"
            value={meta} placeholder="videos" onChange={(e) => setMeta(e.target.value)}
          />
          <span className="text-gray-500">video lyrics</span>
        </label>
        {s.meta && (
          <span>
            Faltan {s.meta.restantes}: <strong className="text-white">{s.meta.foto} con foto</strong> y{" "}
            <strong className="text-white">{s.meta.veo} con Veo</strong>
            {!s.meta.alcanzable && <span className="text-amber-300"> · ya hay más de la mitad de un tipo: el 50/50 no se alcanza con esta meta</span>}
          </span>
        )}
        {s.sinDato > 0 && <span className="text-amber-300">{s.sinDato} sin dato de fondo (no cuentan en el %)</span>}
      </section>

      <section className="glass rounded-card p-4 overflow-x-auto">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
          <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">
            Campañas · {s.grupos} de {groups.length} tildadas · {s.videos} videos
          </p>
          <div className="flex gap-1.5">
            <button type="button" className={quick} onClick={() => pick(() => true)}>Todas</button>
            <button type="button" className={quick} onClick={() => pick((g) => g.portal === "argentina")}>Argentina</button>
            <button type="button" className={quick} onClick={() => pick((g) => g.portal === "chile")}>Chile</button>
            <button type="button" className={quick} onClick={() => pick(() => false)}>Ninguna</button>
          </div>
        </div>
        <table className="w-full text-caption">
          <thead>
            <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
              <th className="py-1 w-6" />
              <th className="py-1 text-left font-medium">Campaña</th>
              <th className="py-1 text-left font-medium">Portal</th>
              <th className="py-1 text-left font-medium">Publicado</th>
              <th className="py-1 text-right font-medium">Videos</th>
              <th className="py-1 text-right font-medium">Veo</th>
              <th className="py-1 text-right font-medium">Foto</th>
              <th className="py-1 text-right font-medium">% foto</th>
              <th className="py-1 pl-3 font-medium" />
            </tr>
          </thead>
          <tbody>
            {groups.length === 0 && <tr><td colSpan={9} className="py-2 text-gray-600">No hay video lyrics publicados</td></tr>}
            {groups.map((g) => {
              const on = selected.has(g.key);
              const total = g.veo + g.foto;
              return (
                <tr key={g.key} className={`border-t border-white/[0.05] ${on ? "text-gray-200" : "text-gray-600"}`}>
                  <td className="py-1.5">
                    <input type="checkbox" checked={on} onChange={() => toggle(g.key)} aria-label={`Incluir ${g.name} (${PORTAL[g.portal] || g.portal})`} />
                  </td>
                  <td className="py-1.5">
                    <button type="button" onClick={() => toggle(g.key)} className="text-left hover:text-white">{g.name}</button>
                    {g.veo_models?.fast ? <span className="ml-2 text-label text-amber-300">{g.veo_models.fast} Veo Fast</span> : null}
                  </td>
                  <td className="py-1.5">{PORTAL[g.portal] || g.portal}</td>
                  <td className="py-1.5 whitespace-nowrap">{day(g.first_delivery_at)} – {day(g.last_delivery_at)}</td>
                  <td className="py-1.5 text-right tabular-nums">{g.videos}</td>
                  <td className="py-1.5 text-right tabular-nums">{g.veo}</td>
                  <td className="py-1.5 text-right tabular-nums">{g.foto}</td>
                  <td className="py-1.5 text-right tabular-nums">{total ? `${Math.round((g.foto / total) * 100)}%` : "—"}</td>
                  <td className="py-1.5 pl-3"><FotoBar veo={g.veo} foto={g.foto} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="mt-2 text-label text-gray-600">
          Cuenta la primera publicación vigente de cada video en los portales, sin art tracks. El tipo de fondo sale de lo que
          generó el pipeline (Veo o imagen), no del estilo elegido en el wizard.
          {data.excluded_art_tracks ? ` ${data.excluded_art_tracks} art track publicado con otra etiqueta quedó afuera.` : ""}
          {data.single_environment ? " Sólo se miró la base de este entorno: puede faltar lo que vive en el otro." : ""}
          {data.jobs_not_found ? ` ${data.jobs_not_found} entregas sin job encontrado (cuentan como sin dato).` : ""}
        </p>
      </section>

      <section className="glass rounded-card p-4 overflow-x-auto">
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={quick} onClick={() => setShowItems((v) => !v)}>
            {showItems ? "Ocultar videos" : `Ver videos (${s.videos})`}
          </button>
          {showItems && (
            <select
              aria-label="Filtrar por tipo de fondo"
              className="bg-surface-3/60 ring-1 ring-white/[0.08] rounded px-2 py-1 text-caption text-white"
              value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}
            >
              <option value="">Todos</option>
              <option value="veo">Video Veo</option>
              <option value="foto">Foto + efecto</option>
              <option value="sin_dato">Sin dato</option>
            </select>
          )}
        </div>
        {showItems && (
          <table className="w-full text-caption mt-3">
            <thead>
              <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
                <th className="py-1 text-left font-medium">Artista</th>
                <th className="py-1 text-left font-medium">Canción</th>
                <th className="py-1 text-left font-medium">Portal</th>
                <th className="py-1 text-left font-medium">Fondo</th>
                <th className="py-1 text-left font-medium">Detalle</th>
                <th className="py-1 text-right font-medium">Publicado</th>
              </tr>
            </thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.job_id} className="border-t border-white/[0.05] text-gray-300">
                  <td className="py-1">{i.artist}</td>
                  <td className="py-1">{i.song}</td>
                  <td className="py-1">{PORTAL[i.portal] || i.portal}</td>
                  <td className="py-1">{TYPE[i.type] || i.type}</td>
                  <td className="py-1 text-gray-500">{i.type === "veo" ? MODEL[i.veo_model] || i.veo_model : i.effect || ""}</td>
                  <td className="py-1 text-right tabular-nums">{day(i.added_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
