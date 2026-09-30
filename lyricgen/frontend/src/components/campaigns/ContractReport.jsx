import { useState } from "react";
import "./ContractReport.print.css";
import { authHeaders, campaignApiBase } from "../../lib/campaignApi";
import { Banner, Button, EmptyState, ProgressBar } from "./ui";

const METRICS = [
  ["assigned", "Asignadas"], ["generated", "Generadas"], ["approved", "Aprobadas"], ["verified", "Verificadas"], ["delivered", "Entregadas"],
];

async function download(campaignId, extension) {
  const response = await fetch(`${campaignApiBase()}/batch/campaigns/${encodeURIComponent(campaignId)}/creative/export.${extension}`, { headers: authHeaders() });
  if (!response.ok) throw new Error(`No se pudo exportar (${response.status})`);
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `campana-${campaignId}.${extension}`; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Contract and compliance: what was promised versus what exists as evidence. */
export default function ContractReport({ campaignId, report, fields = {}, items = [], onRegister }) {
  const [error, setError] = useState("");
  if (!report) return null;
  const contract = report.contract || {};
  const universe = contract.item_ids?.length || 0;
  const hasContract = Boolean(contract.revision || contract.agreement || report.groups?.length);
  const exports = <div className="campaign-no-print flex flex-wrap gap-2">
    <Button size="sm" onClick={() => download(campaignId, "csv").catch((e) => setError(e.message))}>Exportar CSV</Button>
    <Button size="sm" onClick={() => download(campaignId, "xlsx").catch((e) => setError(e.message))}>Exportar Excel</Button>
    <Button size="sm" variant="ghost" onClick={() => {
      const nodes = [...document.querySelectorAll("#campaign-contract-report details")];
      const closed = nodes.filter((node) => !node.open);
      closed.forEach((node) => { node.open = true; });
      window.addEventListener("afterprint", () => closed.forEach((node) => { node.open = false; }), { once: true });
      window.print();
    }}>Imprimir / guardar PDF</Button>
  </div>;

  if (!hasContract) {
    return <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.06]">
      <EmptyState icon="§" title="Sin acuerdo contractual registrado"
        description="Si el cliente pidió un reparto de estilos (por ejemplo, mitad foto con efecto y mitad Veo), registralo al asignar estilos y acá vas a ver su cumplimiento con evidencia de cada archivo."
        action={<div className="flex flex-wrap justify-center gap-2">{onRegister && <Button variant="primary" onClick={onRegister}>Registrar un reparto</Button>}{exports}</div>} />
      {error && <div className="p-4"><Banner tone="danger">{error}</Banner></div>}
    </div>;
  }

  const totals = Object.fromEntries(METRICS.map(([key]) => [key, (report.groups || []).reduce((sum, group) => sum + (Number(group[key]) || 0), 0)]));
  return <div id="campaign-contract-report" className="space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h2 className="text-xl font-semibold">Contrato y cumplimiento · {report.name}</h2>
        <p className="mt-1 text-sm text-ink-secondary">Informe del {new Date(report.at).toLocaleString("es-AR")} · Acuerdo versión {contract.revision || "sin registrar"} · {universe} entregas principales</p>
      </div>
      {exports}
    </div>
    {error && <Banner tone="danger">{error}</Banner>}
    {contract.agreement && <blockquote className="whitespace-pre-wrap rounded-card bg-black/20 p-4 text-sm ring-1 ring-white/[0.06]">{contract.agreement}{contract.rounding_note && <span className="mt-2 block text-ink-secondary">{contract.rounding_note}</span>}</blockquote>}
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
      {METRICS.map(([key, label]) => <div key={key} className="rounded-card bg-surface-2/50 p-4 ring-1 ring-white/[0.06]">
        <p className="text-xs text-ink-secondary">{label}</p>
        <p className="mt-1 text-2xl font-semibold tabular-nums">{totals[key]}<span className="text-sm font-normal text-ink-secondary"> / {universe}</span></p>
        <div className="mt-2"><ProgressBar value={totals[key]} max={universe} label={label} /></div>
      </div>)}
    </div>
    <div className="overflow-x-auto rounded-card ring-1 ring-white/[0.06]">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="bg-black/20 text-[11px] uppercase tracking-wider text-ink-secondary"><tr>{["Grupo", "Compromiso", "Objetivo", ...METRICS.map(([, label]) => label)].map((heading) => <th key={heading} className="p-3 font-medium">{heading}</th>)}</tr></thead>
        <tbody>{report.groups.map((group) => <tr key={group.id} className="border-t border-white/[0.06]">
          <td className="p-3 font-medium">{group.name}</td>
          <td className="p-3">{group.target_weight}{contract.mode === "percent" ? "%" : " videos"}</td>
          <td className="p-3 tabular-nums">{group.target}</td>
          {METRICS.map(([key]) => <td key={key} className="p-3 tabular-nums">{group[key] || 0} <span className="text-xs text-ink-secondary">({(100 * (group[key] || 0) / Math.max(1, universe)).toFixed(1)}%)</span></td>)}
        </tr>)}</tbody>
      </table>
    </div>
    <p className="text-xs text-ink-secondary">La asignación no prueba cumplimiento: «Verificadas» exige evidencia del archivo generado y la entrega se registra después de la aprobación final.</p>
    <section className="space-y-2">
      <h3 className="font-semibold">Registro por video</h3>
      <div className="divide-y divide-white/[0.06] rounded-card ring-1 ring-white/[0.06]">
        {report.videos.map((video) => <details key={video.job_id} className="px-4 py-2.5 text-sm">
          <summary className="cursor-pointer">{video.artist} — {video.title} · {video.assignment?.group_name || "Sin clasificación"} · <span className={video.compliance === "verified" ? "text-emerald-200" : video.compliance === "deviation" ? "text-red-200" : "text-ink-secondary"}>{video.compliance === "verified" ? "Verificado" : video.compliance === "deviation" ? "Desviación" : "Evidencia pendiente"}</span></summary>
          <p className="mt-1 break-all text-xs text-ink-secondary">Efecto aplicado: {video.evidence?.effect_applied || "No acreditado"} · Modelos: {video.evidence?.models?.join(", ") || "No acreditados"} · Huella: {video.evidence?.video_sha256 || "Pendiente"}</p>
        </details>)}
      </div>
    </section>
    {report.history?.length > 0 && <section className="space-y-2">
      <h3 className="font-semibold">Historial de cambios</h3>
      <div className="divide-y divide-white/[0.06] rounded-card ring-1 ring-white/[0.06]">
        {report.history.map((entry, index) => <details key={index} className="px-4 py-2.5 text-sm">
          <summary className="cursor-pointer">{new Date(entry.at).toLocaleString("es-AR")} · Usuario {entry.actor} · {entry.detail.reason || entry.detail.destination || "Cambio registrado"}</summary>
          <p className="mt-1 text-xs text-ink-secondary">Versión {entry.detail.revision || entry.detail.after?.revision || "—"}</p>
          {(entry.detail.changes || (entry.detail.item_id ? [entry.detail] : [])).map((changeRow) => <div key={changeRow.item_id} className="ml-4 mt-1 text-xs">
            <p>{changeRow.artist} · {changeRow.title || items.find((item) => item.id === changeRow.item_id)?.title || changeRow.item_id}</p>
            {Object.entries(changeRow.after || {}).filter(([key, value]) => fields[key] && JSON.stringify(changeRow.before?.[key]) !== JSON.stringify(value)).map(([key, value]) => <p key={key} className="text-ink-secondary">{fields[key].label}: {String(changeRow.before?.[key] ?? "Heredado")} → {String(value)}</p>)}
          </div>)}
        </details>)}
      </div>
    </section>}
  </div>;
}
