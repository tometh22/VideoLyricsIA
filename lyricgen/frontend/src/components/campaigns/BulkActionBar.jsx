import { Button, Kbd } from "./ui";
import { canDiscard, canGenerate, canSend } from "./songModel";

/** Floating bar for the current selection; only shows actions that apply. */
export default function BulkActionBar({ songs, kind, canManage, onClear, onAction, hiddenCount = 0 }) {
  if (!songs.length) return null;
  const generate = kind !== "art_track" ? songs.filter(canGenerate) : [];
  const send = canManage ? songs.filter(canSend) : [];
  const discard = songs.filter(canDiscard);
  const restore = songs.filter((song) => song.stage === "discarded");
  const retry = canManage ? songs.filter((song) => song.stage === "attention" || (song.stage === "audio" && song.upload_state === "error")) : [];
  const style = canManage && kind !== "art_track" ? songs.filter((song) => song.stage !== "discarded") : [];
  return <div role="region" aria-label="Acciones sobre la selección"
    className="fixed inset-x-3 bottom-4 z-[70] mx-auto flex max-w-4xl flex-wrap items-center gap-2 rounded-2xl bg-surface-3/95 px-4 py-3 shadow-depth-lg ring-1 ring-white/15 backdrop-blur md:inset-x-6">
    <span className="mr-1 text-sm" aria-live="polite"><strong className="tabular-nums">{songs.length}</strong> {songs.length === 1 ? "seleccionada" : "seleccionadas"}
      {hiddenCount > 0 && <span className="block text-xs text-amber-200">{hiddenCount} fuera del filtro actual</span>}</span>
    <Button size="sm" variant="ghost" onClick={onClear}>Limpiar <Kbd className="hidden sm:inline-flex">Esc</Kbd></Button>
    <div className="ml-auto flex flex-wrap items-center gap-2">
      {style.length > 0 && <Button size="sm" variant="secondary" onClick={() => onAction("style", style)}>Asignar estilo</Button>}
      {retry.length > 0 && <Button size="sm" variant="secondary" onClick={() => onAction("retry", retry)}>Reintentar {retry.length}</Button>}
      {restore.length > 0 && <Button size="sm" variant="secondary" onClick={() => onAction("restore", restore)}>Recuperar {restore.length}</Button>}
      {discard.length > 0 && <Button size="sm" variant="danger" onClick={() => onAction("discard", discard)}>Descartar {discard.length}</Button>}
      {send.length > 0 && <Button size="sm" variant={generate.length ? "secondary" : "primary"} onClick={() => onAction("send", send)}>Enviar {send.length} al portal</Button>}
      {generate.length > 0 && <Button size="sm" variant="primary" onClick={() => onAction("generate", generate)}>Generar {generate.length} {generate.length === 1 ? "video" : "videos"}</Button>}
    </div>
  </div>;
}
