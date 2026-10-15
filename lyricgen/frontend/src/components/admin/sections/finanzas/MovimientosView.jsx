// Todos los renglones cargados, editables uno por uno, con su evidencia.
// También importar/exportar CSV: los montos no viven en el repo, así que la
// planilla se carga desde acá.
import { useMemo, useRef, useState } from "react";

import { BASIS_LABELS, SECTION_LABELS, csvToEntries, entriesToCsv, monthLabel } from "./finanzasModel";
import { BasisBadge } from "./ResultadoView";

const cell = "bg-transparent hover:bg-white/[0.04] focus:bg-surface-3/60 focus:ring-1 focus:ring-brand/40 rounded px-1.5 py-0.5 text-caption text-gray-200 focus:outline-none w-full";

function Row({ e, onSave, onDelete }) {
  const [draft, setDraft] = useState(e);
  const commit = (key, cast = (v) => v) => {
    const value = cast(draft[key]);
    if (value === e[key]) return;
    onSave(e.id, { [key]: value });
  };
  const text = (key, w) => (
    <input className={`${cell} ${w}`} value={draft[key] ?? ""} onChange={(ev) => setDraft({ ...draft, [key]: ev.target.value })} onBlur={() => commit(key, (v) => (v === "" ? null : v))} />
  );
  return (
    <tr className="border-t border-white/[0.05] align-top">
      <td className="py-1">{text("period", "w-20")}</td>
      <td className="py-1">
        <select className={cell} value={draft.section} onChange={(ev) => { setDraft({ ...draft, section: ev.target.value }); onSave(e.id, { section: ev.target.value }); }}>
          {Object.entries(SECTION_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </td>
      <td className="py-1">{text("category", "w-28")}</td>
      <td className="py-1">{text("provider", "w-32")}</td>
      <td className="py-1">{text("concept", "w-44")}</td>
      <td className="py-1">
        <input className={`${cell} w-24 text-right tabular-nums`} value={draft.amount_usd ?? ""} onChange={(ev) => setDraft({ ...draft, amount_usd: ev.target.value })} onBlur={() => commit("amount_usd", (v) => (v === "" || v === null ? null : Number(String(v).replace(",", "."))))} />
      </td>
      <td className="py-1">
        <select className={cell} value={draft.basis || ""} onChange={(ev) => { setDraft({ ...draft, basis: ev.target.value || null }); onSave(e.id, { basis: ev.target.value || null }); }}>
          <option value="">—</option>
          {Object.entries(BASIS_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </td>
      <td className="py-1 text-center">
        <input
          type="checkbox"
          title="Se repite todos los meses siguientes"
          checked={!!draft.payload?.recurrente}
          onChange={(ev) => {
            const payload = { ...(draft.payload || {}), recurrente: ev.target.checked };
            setDraft({ ...draft, payload });
            onSave(e.id, { payload });
          }}
        />
      </td>
      <td className="py-1">{text("evidence", "w-56")}</td>
      <td className="py-1">{text("note", "w-48")}</td>
      <td className="py-1 text-right">
        <button type="button" onClick={() => onDelete(e.id)} className="text-label text-gray-600 hover:text-red-300" title="Borrar renglón">✕</button>
      </td>
    </tr>
  );
}

export default function MovimientosView({ f }) {
  const [filter, setFilter] = useState("todos");
  const [replace, setReplace] = useState(true);
  const [importMsg, setImportMsg] = useState(null);
  const fileRef = useRef(null);
  const rows = useMemo(
    () => f.entries.filter((e) => e.section !== "parametros" && (filter === "todos" || e.section === filter)),
    [f.entries, filter],
  );

  const exportCsv = () => {
    const blob = new Blob([entriesToCsv(f.entries.filter((e) => e.section !== "parametros"))], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `genly-finanzas-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };
  const importCsv = async (file) => {
    setImportMsg(null);
    try {
      const parsed = csvToEntries(await file.text());
      const res = await f.importEntries(parsed, replace);
      setImportMsg(`Importados ${res.imported} renglones${res.replaced ? ` (reemplazó ${res.replaced})` : ""}.`);
    } catch (err) {
      setImportMsg(`No se pudo importar: ${err.message}`);
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="space-y-4">
      <div className="glass rounded-card p-3 flex flex-wrap items-center gap-3">
        <select className="bg-surface-3/60 ring-1 ring-white/[0.08] rounded px-2 py-1 text-caption text-white" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="todos">Todas las secciones</option>
          {Object.entries(SECTION_LABELS).filter(([k]) => k !== "parametros").map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button type="button" onClick={() => f.createEntry({ period: f.months[f.months.length - 1], section: filter === "todos" ? "costo_directo" : filter, provider: "Nuevo renglón", basis: "estimado" })} className="text-caption px-3 py-1 rounded bg-brand/30 hover:bg-brand/50 text-white">+ Renglón</button>
        <span className="ml-auto flex items-center gap-3">
          <label className="text-caption text-gray-400 flex items-center gap-1.5">
            <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
            Reemplazar mes y sección al importar
          </label>
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden" onChange={(e) => e.target.files?.[0] && importCsv(e.target.files[0])} />
          <button type="button" onClick={() => fileRef.current?.click()} className="text-caption px-3 py-1 rounded glass hover:bg-white/[0.06]">Importar CSV</button>
          <button type="button" onClick={exportCsv} className="text-caption px-3 py-1 rounded glass hover:bg-white/[0.06]">Exportar CSV</button>
        </span>
      </div>
      {importMsg && <p className="text-caption text-gray-400">{importMsg}</p>}
      <div className="glass rounded-card p-3 overflow-x-auto">
        <table className="w-full min-w-[1100px]">
          <thead>
            <tr className="text-label uppercase tracking-[0.08em] text-gray-500 text-left">
              <th className="py-1 font-medium">Mes</th><th className="font-medium">Sección</th><th className="font-medium">Categoría</th>
              <th className="font-medium">Proveedor</th><th className="font-medium">Concepto</th><th className="font-medium text-right">USD</th>
              <th className="font-medium">Base</th><th className="font-medium" title="Se repite todos los meses">Mensual</th><th className="font-medium">Evidencia</th><th className="font-medium">Nota</th><th />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={11} className="py-3 text-caption text-gray-600">Sin renglones. Importá el CSV de la planilla o agregá uno.</td></tr>}
            {rows.map((e) => <Row key={`${e.id}-${e.updated_at}`} e={e} onSave={f.updateEntry} onDelete={f.deleteEntry} />)}
          </tbody>
        </table>
      </div>
      <p className="text-label text-gray-600">
        Bases: {Object.keys(BASIS_LABELS).map((b) => <span key={b} className="mr-1.5"><BasisBadge basis={b} /></span>)} — Factura/API = hecho; Captura = dashboard del proveedor; Estimado/Imputado = cálculo nuestro.
        {f.months.length > 0 && ` Período visible: ${monthLabel(f.months[0])} → ${monthLabel(f.months[f.months.length - 1])}.`}
      </p>
    </div>
  );
}
