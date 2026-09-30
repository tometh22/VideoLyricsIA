import { useEffect, useRef, useState } from "react";
import { inspectAudioFiles, isAudioFile, metadataIssue, parseMetadataCsv, uploadCampaignAudios } from "../../lib/campaignUpload";
import { formatDuration } from "../../lib/campaignPipeline";
import { Banner, Button, Chip, ProgressBar, inputClass } from "./ui";

const ISSUE_LABEL = {
  missing_metadata: "Faltan datos",
  invalid_size: "Supera 500 MB",
  invalid_duration: "Duración inválida",
};

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}

async function filesFromDrop(dataTransfer) {
  const items = [...(dataTransfer.items || [])];
  const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return [...(dataTransfer.files || [])];
  const files = [];
  const walk = async (entry, prefix = "") => {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      Object.defineProperty(file, "webkitRelativePath", { value: `${prefix}${file.name}`, configurable: true });
      files.push(file);
      return;
    }
    if (!entry.isDirectory) return;
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
    }
  };
  for (const entry of entries) await walk(entry);
  return files;
}

/**
 * Upload a folder of WAV/MP3 straight from the browser: the file name
 * `Título_ArtistaARF123.wav` or an optional CSV (archivo, título, artista,
 * código) fills the song data; the operator fixes gaps before registering.
 */
export default function CampaignAudioUploader({ campaignId, onUploaded, remaining = null }) {
  const [phase, setPhase] = useState("idle");
  const [entries, setEntries] = useState([]);
  const [inspect, setInspect] = useState(null);
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [csvName, setCsvName] = useState("");
  const controller = useRef(null);
  const folderInput = useRef(null);
  const filesInput = useRef(null);

  useEffect(() => {
    if (phase !== "uploading" && phase !== "inspecting") return undefined;
    const warn = (event) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [phase]);
  useEffect(() => () => controller.current?.abort(), []);

  const accept = async (fileList) => {
    const files = [...fileList];
    const csv = files.find((file) => file.name.toLowerCase().endsWith(".csv"));
    const audios = files.filter(isAudioFile);
    setError("");
    if (!audios.length) { setError("No encontramos archivos .wav o .mp3 en la selección."); return; }
    controller.current = new AbortController();
    setPhase("inspecting");
    try {
      const csvRows = csv ? parseMetadataCsv(await csv.text()) : new Map();
      setCsvName(csv ? csv.name : "");
      const inspected = await inspectAudioFiles(audios, { csvRows, signal: controller.current.signal, onProgress: setInspect });
      setEntries(inspected);
      setPhase("review");
    } catch (inspectError) {
      if (!inspectError.aborted) setError(inspectError.message || "No se pudieron leer los archivos.");
      setPhase("idle");
    } finally {
      setInspect(null);
    }
  };

  const edit = (index, key, value) => setEntries((old) => old.map((entry, position) => (position === index ? { ...entry, [key]: key === "technical_code" ? value.toUpperCase() : value } : entry)));

  const upload = async () => {
    controller.current = new AbortController();
    setPhase("uploading"); setError("");
    try {
      const summary = await uploadCampaignAudios(campaignId, entries, { signal: controller.current.signal, onProgress: setProgress });
      setResult(summary);
      setPhase("done");
      onUploaded?.(summary);
    } catch (uploadError) {
      if (uploadError.aborted) { setPhase("review"); setError("Subida detenida. Volvé a subir: lo que ya llegó se saltea."); }
      else { setError(uploadError.message || "No se pudo completar la subida."); setPhase("review"); }
      onUploaded?.(null);
    }
  };

  const issues = entries.map(metadataIssue);
  const blocking = issues.filter((issue) => issue === "invalid_size" || issue === "invalid_duration").length;
  const missing = issues.filter((issue) => issue === "missing_metadata").length;
  const totalBytes = entries.reduce((sum, entry) => sum + entry.size_bytes, 0);

  if (phase === "idle" || phase === "inspecting") {
    return <div className="space-y-3">
      <div onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)}
        onDrop={async (event) => { event.preventDefault(); setDragging(false); if (phase === "idle") await accept(await filesFromDrop(event.dataTransfer)); }}
        className={`flex flex-col items-center justify-center gap-3 rounded-card border-2 border-dashed px-6 py-10 text-center transition duration-brand ${dragging ? "border-brand bg-brand/10" : "border-white/15 bg-black/10"}`}>
        {phase === "inspecting" ? <div role="status" className="w-full max-w-sm space-y-2">
          <p className="text-sm font-medium">Leyendo {inspect ? `${Math.min(inspect.index + 1, inspect.total)} de ${inspect.total}` : "archivos"}…</p>
          <ProgressBar value={inspect?.index || 0} max={inspect?.total || 1} label="Lectura de archivos" />
          <p className="truncate text-xs text-ink-secondary">{inspect?.name}</p>
          <Button size="sm" variant="ghost" onClick={() => controller.current?.abort()}>Cancelar</Button>
        </div> : <>
          <div aria-hidden="true" className="grid h-12 w-12 place-items-center rounded-2xl bg-brand/15 text-2xl text-brand-light ring-1 ring-brand/30">↥</div>
          <div>
            <p className="font-semibold">Arrastrá la carpeta con los audios</p>
            <p className="mt-1 text-sm text-ink-secondary">WAV o MP3 · nombre <span className="font-mono text-xs">Título_ArtistaARF123.wav</span> o un CSV con archivo, título, artista y código{remaining != null ? ` · quedan ${remaining} lugares` : ""}.</p>
          </div>
          <div className="flex flex-wrap justify-center gap-2">
            <Button variant="primary" onClick={() => folderInput.current?.click()}>Elegir carpeta</Button>
            <Button onClick={() => filesInput.current?.click()}>Elegir archivos</Button>
          </div>
        </>}
        <input ref={folderInput} type="file" multiple webkitdirectory="" directory="" className="sr-only" aria-label="Elegir carpeta de audios" onChange={(event) => { const files = [...(event.target.files || [])]; event.target.value = ""; if (files.length) void accept(files); }} />
        <input ref={filesInput} type="file" multiple accept=".wav,.mp3,.csv,audio/wav,audio/mpeg,text/csv" className="sr-only" aria-label="Elegir archivos de audio" onChange={(event) => { const files = [...(event.target.files || [])]; event.target.value = ""; if (files.length) void accept(files); }} />
      </div>
      {error && <Banner tone="danger">{error}</Banner>}
    </div>;
  }

  if (phase === "done" && result) {
    return <div className="space-y-4 rounded-card bg-emerald-500/[0.06] p-5 ring-1 ring-emerald-400/20">
      <p className="text-lg font-semibold">{result.uploaded} {result.uploaded === 1 ? "audio subido" : "audios subidos"}</p>
      <p className="text-sm text-ink-secondary">{result.registered} registrados{result.duplicates ? ` · ${result.duplicates} ya estaban en la campaña` : ""}{result.skipped ? ` · ${result.skipped} omitidos por tamaño o duración` : ""}. La transcripción arranca sola; seguí el avance en la etapa Audio.</p>
      {result.errors.length > 0 && <Banner tone="warning">{result.errors.length} con error: {result.errors.slice(0, 5).map((item) => `${item.filename} (${item.message})`).join("; ")}{result.errors.length > 5 ? "…" : ""}. Volvé a subir la misma carpeta para reintentar sólo esos.</Banner>}
      <Button onClick={() => { setPhase("idle"); setEntries([]); setResult(null); setProgress(null); }}>Subir más audios</Button>
    </div>;
  }

  return <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-2">
      <Chip tone="brand">{entries.length} audios · {formatBytes(totalBytes)}</Chip>
      {missing > 0 && <Chip tone="warning">{missing} con datos incompletos</Chip>}
      {blocking > 0 && <Chip tone="danger">{blocking} no se pueden subir</Chip>}
      {csvName && <Chip tone="info">Datos desde {csvName}</Chip>}
    </div>
    {missing > 0 && phase === "review" && <Banner tone="warning">Completá título, artista y código acá o después, desde el detalle de cada canción. Los audios con datos incompletos se transcriben igual, pero quedan marcados para revisión completa.</Banner>}
    <div className="max-h-[26rem] overflow-auto rounded-card ring-1 ring-white/[0.06]">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead className="sticky top-0 bg-surface-2 text-[11px] uppercase tracking-wider text-ink-secondary"><tr><th className="p-2.5 font-medium">Archivo</th><th className="p-2.5 font-medium">Título</th><th className="p-2.5 font-medium">Artista</th><th className="p-2.5 font-medium">Código</th><th className="p-2.5 font-medium">Duración</th></tr></thead>
        <tbody>{entries.map((entry, index) => {
          const issue = issues[index];
          return <tr key={entry.sha256} className="border-t border-white/[0.05] align-top">
            <td className="max-w-[12rem] p-2.5"><p className="truncate text-xs" title={entry.relative_path}>{entry.filename}</p>{issue && <Chip tone={issue === "missing_metadata" ? "warning" : "danger"} className="mt-1">{ISSUE_LABEL[issue]}</Chip>}</td>
            {["title", "artist", "technical_code"].map((key) => <td key={key} className="p-1.5">
              <input aria-label={`${key === "title" ? "Título" : key === "artist" ? "Artista" : "Código"} de ${entry.filename}`} disabled={phase === "uploading"} value={entry[key]}
                onChange={(event) => edit(index, key, event.target.value)} className={`${inputClass} !py-1.5 text-xs ${key === "technical_code" ? "font-mono" : ""} ${!entry[key]?.trim() ? "ring-amber-400/40" : ""}`} />
            </td>)}
            <td className="p-2.5 tabular-nums text-xs text-ink-secondary">{formatDuration(entry.duration_seconds)}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>
    {phase === "uploading" && progress && <div role="status" aria-live="polite" className="space-y-2 rounded-card bg-brand/10 p-4 ring-1 ring-brand/30">
      <div className="flex flex-wrap justify-between gap-2 text-sm"><strong>{progress.phase === "registering" ? `Registrando ${progress.registered} de ${entries.length - blocking}…` : `Subiendo · ${progress.uploaded} de ${entries.length - blocking} listos`}</strong>
        <span className="tabular-nums text-ink-secondary">{formatBytes(progress.bytesDone)} de {formatBytes(progress.bytesTotal)}</span></div>
      <ProgressBar value={progress.bytesDone} max={progress.bytesTotal || 1} label="Progreso de la subida" />
      <p className="truncate text-xs text-ink-secondary">{progress.current}{progress.failed ? ` · ${progress.failed} con error` : ""} · No cierres esta pestaña.</p>
    </div>}
    {error && <Banner tone="danger">{error}</Banner>}
    <div className="flex flex-wrap justify-end gap-2">
      {phase === "uploading"
        ? <Button variant="ghost" onClick={() => controller.current?.abort()}>Detener</Button>
        : <><Button variant="ghost" onClick={() => { setPhase("idle"); setEntries([]); setCsvName(""); }}>Cambiar selección</Button>
          <Button variant="primary" disabled={entries.length === blocking} onClick={upload}>Subir {entries.length - blocking} {entries.length - blocking === 1 ? "audio" : "audios"}</Button></>}
    </div>
  </div>;
}
