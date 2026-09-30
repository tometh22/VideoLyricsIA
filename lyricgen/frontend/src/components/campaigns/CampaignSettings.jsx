import { useState } from "react";
import { campaignApiBase, campaignRequest } from "../../lib/campaignApi";
import { PORTALS, artTrackPresetLabel } from "../../lib/campaignPipeline";
import { CampaignReviewerSummary } from "../CampaignReviewerStatus";
import ArtTrackPanel from "./ArtTrackPanel";
import CampaignAudioUploader from "./CampaignAudioUploader";
import ContractReport from "./ContractReport";
import StyleAssignment from "./StyleAssignment";
import { Banner, Button, Field, Modal, Skeleton, inputClass } from "./ui";

const STATUS = [["active", "Activa"], ["paused", "Pausada"], ["completed", "Completada"]];

export function exportMinutes(rows) {
  const cells = [["orden", "artista", "titulo", "estado", "minutos_activos"]];
  rows.forEach((row, index) => cells.push([row.priority || index + 1, row.artist, row.title, row.state, Number(row.active_minutes || 0).toFixed(2)]));
  const csv = cells.map((line) => line.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `minutos-revision-${new Date().toISOString().slice(0, 10)}.csv`; anchor.click();
  URL.revokeObjectURL(url);
}

function Section({ title, description, children, action }) {
  return <section className="rounded-card bg-surface-2/40 p-5 ring-1 ring-white/[0.06] sm:p-6">
    <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-base font-semibold">{title}</h2>{description && <p className="mt-1 max-w-2xl text-sm text-ink-secondary">{description}</p>}</div>
      {action}
    </div>
    {children}
  </section>;
}

function General({ campaign, canManage, isAdmin, onChanged }) {
  const [name, setName] = useState(campaign.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [json, setJson] = useState(() => JSON.stringify(campaign.default_render_params || {}, null, 2));
  const patch = async (value, done) => {
    setBusy(true); setError(""); setMessage("");
    try {
      await campaignRequest(`/batch/campaigns/${encodeURIComponent(campaign.id)}`, { method: "PATCH", json: value });
      setMessage(done); await onChanged?.();
    } catch (patchError) {
      setError(patchError.message);
    } finally { setBusy(false); }
  };
  const cancelled = campaign.status === "cancelled";
  return <div className="space-y-5">
    <Section title="Campaña" description="Nombre, estado y destino. Pausar detiene la promoción de nuevas canciones al pipeline; no borra nada.">
      <div className="grid gap-5 lg:grid-cols-2">
        <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void patch({ name: name.trim() }, "Nombre guardado."); }}>
          <Field label="Nombre"><input className={inputClass} value={name} maxLength={160} disabled={!canManage || busy} onChange={(event) => setName(event.target.value)} /></Field>
          {canManage && <Button type="submit" size="sm" disabled={busy || !name.trim() || name.trim() === campaign.name}>Guardar nombre</Button>}
        </form>
        <div className="space-y-3">
          <p className="text-xs font-medium text-ink-secondary">Estado</p>
          <div role="radiogroup" aria-label="Estado de la campaña" className="inline-flex rounded-button bg-black/30 p-1 ring-1 ring-white/10">
            {STATUS.map(([key, label]) => <button key={key} type="button" role="radio" aria-checked={campaign.status === key} disabled={!canManage || busy || cancelled}
              onClick={() => campaign.status !== key && patch({ status: key }, `Campaña ${label.toLowerCase()}.`)}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:opacity-50 ${campaign.status === key ? "bg-white/10 text-white" : "text-ink-secondary hover:text-white"}`}>{label}</button>)}
          </div>
          {cancelled && <p className="text-sm text-red-200">Campaña cancelada. No se puede reactivar.</p>}
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-ink-secondary">Tipo</dt><dd>{campaign.kind === "art_track" ? "Art tracks" : "Lyric videos"}</dd>
            <dt className="text-ink-secondary">Portal</dt><dd>{campaign.destination_portal === "files" ? "Archivos (sin portal)" : PORTALS[campaign.destination_portal]?.label || (campaign.destination_portal ? campaign.destination_portal : "Se elige al enviar")}</dd>
            {campaign.kind === "art_track" && <><dt className="text-ink-secondary">Estilo</dt><dd>{artTrackPresetLabel(campaign.default_render_params?.art_track_preset)}</dd></>}
            <dt className="text-ink-secondary">Creada</dt><dd>{campaign.created_at ? new Date(campaign.created_at).toLocaleDateString("es-AR", { day: "numeric", month: "long", year: "numeric" }) : "—"}</dd>
            <dt className="text-ink-secondary">Esperadas</dt><dd className="tabular-nums">{campaign.expected_count || "—"}</dd>
          </dl>
        </div>
      </div>
      {error && <div className="mt-4"><Banner tone="danger">{error}</Banner></div>}
      {message && <div className="mt-4"><Banner tone="success">{message}</Banner></div>}
      {canManage && !cancelled && <div className="mt-6 flex items-center justify-between gap-3 border-t border-white/[0.06] pt-4">
        <p className="text-sm text-ink-secondary">Cancelar es definitivo: la campaña deja de aceptar audios y no se puede reanudar.</p>
        <Button size="sm" variant="danger" disabled={busy} onClick={() => setConfirmCancel(true)}>Cancelar campaña</Button>
      </div>}
    </Section>
    {isAdmin && <Section title="Parámetros técnicos" description="Límites de la cola y calibración. Los ajustes visuales se cambian desde Estilo y reparto.">
      <details className="space-y-3">
        <summary className="cursor-pointer text-sm text-ink-secondary hover:text-white">Ver y editar JSON</summary>
        <textarea aria-label="Parámetros técnicos" value={json} onChange={(event) => setJson(event.target.value)} rows={10} className={`${inputClass} mt-3 font-mono text-xs`} />
        <Button size="sm" disabled={busy} onClick={() => {
          let parsed;
          try { parsed = JSON.parse(json); } catch { setError("El JSON no es válido."); return; }
          void patch({ default_render_params: parsed }, "Parámetros guardados.");
        }}>Guardar parámetros</Button>
      </details>
    </Section>}
    {confirmCancel && <Modal label="Cancelar campaña" busy={busy} onClose={() => setConfirmCancel(false)}>
      <div className="space-y-4">
        <h2 className="text-xl font-semibold">¿Cancelar «{campaign.name}»?</h2>
        <p className="text-sm text-ink-secondary">No se borran audios, letras ni videos, pero la campaña no se puede volver a activar.</p>
        <div className="flex justify-end gap-2"><Button variant="ghost" onClick={() => setConfirmCancel(false)}>Volver</Button>
          <Button variant="danger" disabled={busy} onClick={async () => { await patch({ status: "cancelled" }, "Campaña cancelada."); setConfirmCancel(false); }}>Sí, cancelar campaña</Button></div>
      </div>
    </Modal>}
  </div>;
}

function TerminalUploader({ campaignId }) {
  const [pair, setPair] = useState(null);
  const [error, setError] = useState("");
  return <details className="rounded-xl bg-black/20 p-4 ring-1 ring-white/[0.06]">
    <summary className="cursor-pointer text-sm font-medium">Cargador por terminal (carpetas muy grandes)</summary>
    <p className="mt-2 text-sm text-ink-secondary">Genera un código temporal de 10 minutos. El cargador nunca recibe el token de tu cuenta.</p>
    <Button size="sm" className="mt-3" onClick={async () => { setError(""); try { setPair(await campaignRequest(`/batch/campaigns/${encodeURIComponent(campaignId)}/upload-session`, { method: "POST" })); } catch (e) { setError(e.message); } }}>Generar código</Button>
    {error && <div className="mt-3"><Banner tone="danger">{error}</Banner></div>}
    {pair && <div className="mt-3 rounded-xl bg-black/30 p-4">
      <div className="font-mono text-2xl font-bold tracking-[.2em] text-brand-light">{pair.pairing_code}</div>
      <code className="mt-3 block whitespace-pre-wrap break-all text-xs text-ink-secondary">python3 scripts/campaign_uploader.py --api "{campaignApiBase() || window.location.origin}" --campaign {campaignId} --code {pair.pairing_code} --folder "/ruta/a/audios"</code>
    </div>}
  </details>;
}

export const SETTINGS_SECTIONS = [
  { key: "general", label: "General" },
  { key: "upload", label: "Carga de audios" },
  { key: "style", label: "Estilo y reparto", lyricOnly: true },
  { key: "contract", label: "Contrato", lyricOnly: true },
  { key: "activity", label: "Actividad", lyricOnly: true },
];

export default function CampaignSettings({ campaign, section, onSection, canManage, isAdmin, creative, report, lyrics, onChanged, onUploaded, remaining }) {
  const lyric = campaign.kind !== "art_track";
  const sections = SETTINGS_SECTIONS.filter((item) => lyric || !item.lyricOnly);
  const active = sections.some((item) => item.key === section) ? section : "general";
  const activeItems = (creative?.items || []).filter((item) => !item.discarded).map((item) => item.id);
  return <div className="grid gap-6 lg:grid-cols-[200px_minmax(0,1fr)]">
    <nav aria-label="Secciones de configuración" className="flex gap-1 overflow-x-auto lg:flex-col">
      {sections.map((item) => <button key={item.key} type="button" aria-current={active === item.key ? "page" : undefined} onClick={() => onSection(item.key)}
        className={`whitespace-nowrap rounded-xl px-3 py-2 text-left text-sm font-medium transition ${active === item.key ? "bg-white/[0.08] text-white ring-1 ring-white/10" : "text-ink-secondary hover:bg-white/[0.04] hover:text-white"}`}>{item.label}</button>)}
    </nav>
    <div className="min-w-0 space-y-5">
      {active === "general" && <General campaign={campaign} canManage={canManage} isAdmin={isAdmin} onChanged={onChanged} />}
      {active === "upload" && (lyric
        ? <Section title="Carga de audios" description="Subí la carpeta desde el navegador. Si se corta, volvé a elegir la misma carpeta: lo que ya subió se saltea.">
          {canManage ? <div className="space-y-4"><CampaignAudioUploader campaignId={campaign.id} onUploaded={onUploaded} remaining={remaining} /><TerminalUploader campaignId={campaign.id} /></div>
            : <p className="text-sm text-ink-secondary">Sólo quien creó la campaña o un administrador puede cargar audios.</p>}
        </Section>
        : <Section title="Audios y portadas"><ArtTrackPanel campaign={campaign} onChanged={onChanged} /></Section>)}
      {active === "style" && <Section title="Estilo y reparto" description="Define cómo se ve cada video. Con un solo estilo al 100 % todas las canciones activas usan el mismo; con varios, se reparten. Para cambiar sólo algunas, seleccionalas en la lista y usá «Asignar estilo».">
        {!creative ? <Skeleton className="h-40" /> : !creative.can_manage
          ? <p className="text-sm text-ink-secondary">Sólo quien creó la campaña o un administrador puede cambiar estilos.</p>
          : <StyleAssignment campaignId={campaign.id} creative={creative} itemIds={activeItems} onSaved={onChanged} />}
      </Section>}
      {active === "contract" && (report ? <ContractReport campaignId={campaign.id} report={report} fields={creative?.fields} items={creative?.items} onRegister={() => onSection("style")} /> : <Skeleton className="h-60" />)}
      {active === "activity" && <div className="space-y-5">
        <Section title="Minutos de revisión" description="Tiempo activo en el editor por canción. Pausas de más de 25 s no cuentan; canciones sin telemetría quedan fuera del promedio."
          action={<Button size="sm" disabled={!lyrics?.items?.length} onClick={() => exportMinutes(lyrics.items)}>Exportar minutos</Button>}>
          {lyrics ? <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <div className="rounded-xl bg-black/20 p-4"><p className="text-xs text-ink-secondary">Promedio hoy</p><p className="mt-1 text-2xl font-semibold tabular-nums">{lyrics.review_minutes_today?.average ?? "—"}<span className="text-sm font-normal text-ink-secondary"> min</span></p></div>
            <div className="rounded-xl bg-black/20 p-4"><p className="text-xs text-ink-secondary">Canciones con telemetría hoy</p><p className="mt-1 text-2xl font-semibold tabular-nums">{lyrics.review_minutes_today?.songs ?? 0}</p></div>
            <div className="rounded-xl bg-black/20 p-4"><p className="text-xs text-ink-secondary">Letras aprobadas hoy</p><p className="mt-1 text-2xl font-semibold tabular-nums">{lyrics.campaign_totals?.approved_today ?? 0}</p></div>
          </div> : <Skeleton className="h-24" />}
        </Section>
        <CampaignReviewerSummary status={campaign.reviewer_campaign_status} />
      </div>}
    </div>
  </div>;
}
