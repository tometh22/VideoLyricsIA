import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { APP_ENV } from "../env";
import { isTrialSupportEnabled, supportUserKey, trialSupport } from "../lib/trialSupport";

// This Spain trial's support copy is deliberately Spanish and human-only.
// Mount once outside Routes, so navigation does not reset a conversation.
export default function TrialSupportChat({ userKey }) {
  const enabled = isTrialSupportEnabled(APP_ENV, window.location.hostname);
  const [expanded, setExpanded] = useState(false);
  const [status, setStatus] = useState("idle");
  const [blocked, setBlocked] = useState(false);
  const generation = useRef(0);

  useLayoutEffect(() => {
    if (!enabled) return;
    generation.current += 1;
    trialSupport.setOwner(userKey || null);
    setExpanded(false);
    setStatus("idle");
    setBlocked(false);
    // Identity belongs to the document/auth lifecycle, not component mounting.
    // StrictMode/remounts must not erase a same-account conversation.
  }, [enabled, userKey]);

  useEffect(() => {
    if (!enabled) return;
    const onStorage = (event) => {
      // Cross-tab account changes fail closed. Token refreshes alone do not
      // disturb support. Reload/re-login restores a safely bound conversation.
      let changedUser = false;
      if (event.key === "genly_user") {
        try { changedUser = supportUserKey(JSON.parse(event.newValue)) !== userKey; }
        catch { changedUser = true; }
      }
      if (event.key === null || changedUser ||
          (event.key === "genly_token" && !event.newValue)) {
        generation.current += 1;
        trialSupport.setOwner(null);
        setBlocked(true);
        setExpanded(false);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [enabled, userKey]);

  if (!enabled || !userKey || blocked) return null;

  const open = async () => {
    const requestedGeneration = generation.current;
    setStatus("loading");
    try {
      await trialSupport.open();
      if (requestedGeneration !== generation.current) return;
      // Keep Crisp's native minimized bubble/unread notifications available.
      setStatus("ready");
      setExpanded(false);
    } catch {
      if (requestedGeneration !== generation.current) return;
      setStatus("error");
    }
  };

  if (status === "ready") return null;

  return <aside className="fixed bottom-24 right-4 z-[70] max-w-[calc(100vw-2rem)]" aria-label="Soporte Genly">
    {expanded && <div id="trial-support-options" className="mb-3 w-80 max-w-full rounded-xl border border-white/15 bg-slate-950 p-4 text-sm text-white shadow-xl">
      <p className="font-semibold">Hablá con el equipo de Genly</p>
      <p className="my-2 text-slate-300">El chat funciona con Crisp. Compartí solo lo necesario para tu consulta; no envíes contraseñas ni material confidencial.</p>
      <p className="mb-3 text-slate-300">Si no estamos conectados, dejá tu consulta y tu email para que podamos responderte.</p>
      <button type="button" onClick={open} disabled={status === "loading" || status === "error"} className="rounded-lg bg-indigo-600 px-4 py-2 font-medium disabled:opacity-60">
        {status === "loading" ? "Abriendo chat…" : "Abrir chat"}
      </button>
      {status === "error" && <p role="alert" className="mt-3 text-amber-200">No pudimos abrir el chat. Podés escribirnos por email o recargar la página e intentarlo nuevamente.</p>}
      <a href="mailto:tomas@epical.digital" className="mt-3 block text-indigo-300 underline">Contactar por email</a>
    </div>}
    <button type="button" aria-expanded={expanded} aria-controls="trial-support-options" onClick={() => setExpanded(!expanded)} className="float-right rounded-full border border-white/15 bg-indigo-600 px-4 py-3 text-sm font-semibold text-white shadow-lg">
      {expanded ? "Cerrar ayuda" : "Soporte Genly"}
    </button>
  </aside>;
}
