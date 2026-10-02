import { useEffect, useState } from "react";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
import { ART_TRACK_PRESETS, FILES_DESTINATION, PORTALS } from "../../lib/campaignPipeline";
import ArtTrackPanel from "./ArtTrackPanel";
import CampaignAudioUploader from "./CampaignAudioUploader";
import StyleControls, { StylePreview } from "./StyleControls";
import { Banner, Button, Field, Modal, Skeleton, inputClass } from "./ui";

const STEPS = ["Datos", "Estilo", "Audios"];
const DELIVERY = [
  ["youtube", "YouTube", "MP4 listo para publicar"],
  ["umg", "UMG (ProRes)", "Máster para el portal del sello"],
  ["both", "Ambos", "MP4 y ProRes"],
];

function Stepper({ step }) {
  return <ol className="flex items-center gap-2" aria-label="Pasos">
    {STEPS.map((label, index) => <li key={label} className="flex items-center gap-2" aria-current={index === step ? "step" : undefined}>
      <span className={`grid h-6 w-6 place-items-center rounded-full text-xs font-semibold ${index < step ? "bg-emerald-400/20 text-emerald-200" : index === step ? "bg-brand text-white" : "bg-white/[0.06] text-ink-secondary"}`}>{index < step ? "✓" : index + 1}</span>
      <span className={`text-sm ${index === step ? "font-semibold text-white" : "text-ink-secondary"}`}>{label}</span>
      {index < STEPS.length - 1 && <span aria-hidden="true" className="mx-1 h-px w-6 bg-white/15" />}
    </li>)}
  </ol>;
}

function KindCard({ value, current, title, description, onSelect }) {
  const active = value === current;
  return <button type="button" role="radio" aria-checked={active} onClick={() => onSelect(value)}
    className={`rounded-card p-4 text-left ring-1 transition duration-brand ${active ? "bg-brand/15 ring-brand/60" : "bg-black/20 ring-white/10 hover:ring-white/25"}`}>
    <span className="block font-semibold">{title}</span><span className="mt-1 block text-xs text-ink-secondary">{description}</span>
  </button>;
}

function StyleStep({ campaign, onDone, onSkip }) {
  const [creative, setCreative] = useState(null);
  const [settings, setSettings] = useState({});
  const [delivery, setDelivery] = useState(campaign.default_render_params?.delivery_profile || "youtube");
  const [umgFrameSize, setUmgFrameSize] = useState(campaign.default_render_params?.umg_frame_size || "HD");
  const [umgFps, setUmgFps] = useState(String(campaign.default_render_params?.umg_fps || "24"));
  const [umgProresProfile, setUmgProresProfile] = useState(String(campaign.default_render_params?.umg_prores_profile || "3"));
  const [umgConfigTouched, setUmgConfigTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    campaignRequest(`/batch/campaigns/${encodeURIComponent(campaign.id)}/creative`, { signal: controller.signal }).then(setCreative).catch((e) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [campaign.id]);
  const fields = creative ? Object.fromEntries(Object.entries(creative.fields || {}).filter(([key, field]) => field.group !== "Salida" && key !== "background_id")) : {};
  const save = async () => {
    setBusy(true); setError("");
    try {
      const params = {
        ...(campaign.default_render_params || {}), ...settings,
        delivery_profile: delivery,
        ...(umgConfigTouched ? {
          umg_frame_size: umgFrameSize,
          umg_fps: umgFps,
          umg_prores_profile: umgProresProfile,
        } : {}),
      };
      await campaignRequest(`/batch/campaigns/${encodeURIComponent(campaign.id)}`, { method: "PATCH", json: { default_render_params: params } });
      onDone();
    } catch (saveError) { setError(saveError.message); } finally { setBusy(false); }
  };
  return <div className="space-y-5">
    <div>
      <p className="mb-2 text-xs font-medium text-ink-secondary">Entrega</p>
      <div role="radiogroup" aria-label="Entrega" className="grid gap-2 sm:grid-cols-3">
        {DELIVERY.map(([value, title, description]) => <KindCard key={value} value={value} current={delivery} title={title} description={description} onSelect={setDelivery} />)}
      </div>
    </div>
    {delivery !== "youtube" && <div className="grid gap-3 rounded-card bg-black/20 p-4 sm:grid-cols-3">
      <Field label="Resolución del master"><select aria-label="Resolución del master UMG" className={inputClass} value={umgFrameSize} onChange={(event) => { setUmgConfigTouched(true); setUmgFrameSize(event.target.value); }}><option value="HD">HD · 1080p</option><option value="UHD-4K">UHD · 4K</option><option value="DCI-2K">DCI · 2K</option><option value="DCI-4K">DCI · 4K</option></select></Field>
      <Field label="Cuadros por segundo"><select aria-label="FPS del master UMG" className={inputClass} value={umgFps} onChange={(event) => { setUmgConfigTouched(true); setUmgFps(event.target.value); }}>{["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"].map((value) => <option key={value} value={value}>{value} fps</option>)}</select></Field>
      <Field label="Perfil ProRes"><select aria-label="Perfil ProRes del master UMG" className={inputClass} value={umgProresProfile} onChange={(event) => { setUmgConfigTouched(true); setUmgProresProfile(event.target.value); }}><option value="3">ProRes 422 HQ</option><option value="4">ProRes 4444</option><option value="5">ProRes 4444 XQ</option></select></Field>
      <p className="text-xs text-ink-secondary sm:col-span-3">El master se genera cuando el cliente lo pide desde el portal; así se evita almacenar ProRes que nadie descarga.</p>
    </div>}
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,320px)]">
      <div className="max-h-[42vh] overflow-y-auto pr-1">
        {creative ? <StyleControls fields={fields} values={settings} label="Estilo base" update={(key, value) => setSettings((old) => { const copy = { ...old }; if (value === undefined) delete copy[key]; else copy[key] = value; return copy; })} />
          : error ? null : <Skeleton className="h-48" />}
      </div>
      <div className="hidden lg:block"><StylePreview settings={settings} assets={[]} /></div>
    </div>
    <p className="text-xs text-ink-secondary">Es el estilo base de toda la campaña. Después podés repartir varios estilos (por ejemplo, foto con efecto y Veo) desde Configuración.</p>
    {error && <Banner tone="danger">{error}</Banner>}
    <div className="flex justify-between gap-2">
      <Button variant="ghost" disabled={busy} onClick={onSkip}>Usar el estilo por defecto</Button>
      <Button variant="primary" disabled={busy || !creative} onClick={save}>{busy ? "Guardando…" : "Guardar y seguir"}</Button>
    </div>
  </div>;
}

/** Create → style → audios, without leaving the campaign section. */
export default function CampaignCreateWizard({ artTrackAllowed = true, onClose, onFinished }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("lyric_video");
  const [destination, setDestination] = useState("");
  const [expected, setExpected] = useState("");
  const [preset, setPreset] = useState("waveform");
  const [umgFrameSize, setUmgFrameSize] = useState("HD");
  const [umgFps, setUmgFps] = useState("24");
  const [umgProresProfile, setUmgProresProfile] = useState("3");
  const [campaign, setCampaign] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uploaded, setUploaded] = useState(false);
  const art = kind === "art_track";
  const create = async (event) => {
    event.preventDefault();
    if (!name.trim() || (art && !destination)) return;
    setBusy(true); setError("");
    try {
      const created = await campaignPost("/batch/campaigns", {
        name: name.trim(),
        expected_count: Number(expected) || 0,
        kind,
        destination_portal: destination || null,
        default_render_params: art
          ? { delivery_profile: "both", art_track: true, art_track_preset: preset, umg_frame_size: umgFrameSize, umg_fps: umgFps, umg_prores_profile: umgProresProfile }
          : { background_mode: "ai", delivery_profile: "youtube" },
      });
      setCampaign(created);
      setStep(art ? 2 : 1);
    } catch (createError) { setError(createError.message); } finally { setBusy(false); }
  };
  const finish = () => (campaign ? onFinished(campaign) : onClose());
  return <Modal label="Nueva campaña" busy={busy} onClose={finish} width="max-w-4xl">
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><p className="text-[11px] font-semibold uppercase tracking-[.18em] text-brand-light">Nueva campaña</p>
          <h2 className="mt-1 text-xl font-semibold">{campaign ? campaign.name : "Crear campaña"}</h2></div>
        <Stepper step={step} />
      </div>
      {step === 0 && <form onSubmit={create} className="space-y-5">
        <Field label="Nombre"><input autoFocus aria-label="Nombre de la campaña" className={inputClass} value={name} maxLength={160} placeholder="UMG Octubre 2026 — Etapa 1" onChange={(event) => setName(event.target.value)} /></Field>
        <div>
          <p className="mb-2 text-xs font-medium text-ink-secondary">Tipo</p>
          <div role="radiogroup" aria-label="Tipo de campaña" className="grid gap-2 sm:grid-cols-2">
            <KindCard value="lyric_video" current={kind} title="Lyric videos" description="Letra sincronizada sobre fondo IA, foto o video. Hasta 1000 canciones." onSelect={setKind} />
            {artTrackAllowed && <KindCard value="art_track" current={kind} title="Art tracks" description="Portada + audio, sin letra. Hasta 500 canciones." onSelect={setKind} />}
          </div>
        </div>
        {art && <div>
          <p className="mb-2 text-xs font-medium text-ink-secondary">Master ProRes para el portal</p>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Resolución"><select aria-label="Resolución del master UMG" className={inputClass} value={umgFrameSize} onChange={(event) => setUmgFrameSize(event.target.value)}><option value="HD">HD · 1080p</option><option value="UHD-4K">UHD · 4K</option><option value="DCI-2K">DCI · 2K</option><option value="DCI-4K">DCI · 4K</option></select></Field>
            <Field label="Cuadros por segundo"><select aria-label="FPS del master UMG" className={inputClass} value={umgFps} onChange={(event) => setUmgFps(event.target.value)}>{["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"].map((value) => <option key={value} value={value}>{value} fps</option>)}</select></Field>
            <Field label="Perfil ProRes"><select aria-label="Perfil ProRes del master UMG" className={inputClass} value={umgProresProfile} onChange={(event) => setUmgProresProfile(event.target.value)}><option value="3">ProRes 422 HQ</option><option value="4">ProRes 4444</option><option value="5">ProRes 4444 XQ</option></select></Field>
          </div>
          <p className="mt-2 text-xs text-ink-secondary">Se genera el master cuando el cliente lo descarga desde el portal.</p>
        </div>}
        {art && <div>
          <p className="mb-2 text-xs font-medium text-ink-secondary">Estilo de Art Track</p>
          <div role="radiogroup" aria-label="Estilo de Art Track" className="grid gap-2 sm:grid-cols-2">
            {ART_TRACK_PRESETS.map((item) => <KindCard key={item.key} value={item.key} current={preset} title={item.label} description={item.description} onSelect={setPreset} />)}
          </div>
        </div>}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={art ? "Portal de destino" : "Portal de destino (opcional)"} hint={art ? undefined : "Si lo dejás vacío, lo elegís al enviar."}>
            <select aria-label="Portal de destino" className={inputClass} value={destination} onChange={(event) => setDestination(event.target.value)}>
              <option value="">{art ? "Elegí un portal" : "Elegir al enviar"}</option>
              {Object.entries(PORTALS).map(([id, value]) => <option key={id} value={id}>UMG {value.label} · {value.host}</option>)}
              {art && <option value={FILES_DESTINATION}>Archivos (sin portal)</option>}
            </select>
          </Field>
          <Field label="Canciones esperadas (opcional)" hint="Sólo para seguir el avance; se ajusta sola al subir.">
            <input aria-label="Cantidad esperada" type="number" min="0" max={art ? 500 : 1000} className={inputClass} value={expected} onChange={(event) => setExpected(event.target.value)} />
          </Field>
        </div>
        {error && <Banner tone="danger">{error}</Banner>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancelar</Button>
          <Button type="submit" variant="primary" disabled={busy || !name.trim() || (art && !destination)}>{busy ? "Creando…" : "Crear y seguir"}</Button>
        </div>
      </form>}
      {step === 1 && campaign && <StyleStep campaign={campaign} onDone={() => setStep(2)} onSkip={() => setStep(2)} />}
      {step === 2 && campaign && <div className="space-y-4">
        {art ? <ArtTrackPanel campaign={campaign} onChanged={() => setUploaded(true)} />
          : <CampaignAudioUploader campaignId={campaign.id} onUploaded={(summary) => setUploaded(Boolean(summary?.uploaded))} />}
        <div className="flex justify-end gap-2">
          <Button variant={uploaded ? "primary" : "ghost"} onClick={finish}>{uploaded ? "Ir a la campaña" : "Subir después"}</Button>
        </div>
      </div>}
    </div>
  </Modal>;
}
