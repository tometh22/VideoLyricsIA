import { useCallback, useEffect, useRef, useState } from "react";

/** Serialize visual autosave independently of the versioned lyrics document. */
export default function useCampaignCreativeSave({ identity, revision, settings, file, onSave }) {
  const session = useRef(null);
  const latest = useRef(null);
  const [state, setState] = useState({ status: "saved", error: "" });
  const signature = JSON.stringify(settings) + (file ? `:${file.name}:${file.size}:${file.lastModified}` : "");
  latest.current = { identity, revision, settings, file, onSave, signature };
  if (session.current?.identity !== identity) {
    session.current = { identity, revision, saved: signature, flight: null };
  }
  const save = useCallback(async () => {
    const current = session.current;
    if (!current?.identity) return;
    while (current.flight) await current.flight;
    if (current !== session.current) return;
    const next = latest.current;
    if (current.saved === next.signature) return;
    setState({ status: "saving", error: "" });
    const operation = (async () => {
      try {
        const result = await next.onSave(next.settings, current.revision, next.file);
        current.revision = result.revision;
        current.saved = next.signature;
        if (current === session.current) setState({ status: "saved", error: "" });
      } catch (error) {
        if (current === session.current) setState({ status: "error", error: error.message });
        throw error;
      }
    })();
    current.flight = operation;
    try { await operation; } finally { if (current.flight === operation) current.flight = null; }
    // A flush must include changes typed while a prior save was in flight.
    if (current === session.current && current.saved !== latest.current.signature) await save();
  }, []);
  useEffect(() => { setState({ status: "saved", error: "" }); }, [identity]);
  useEffect(() => {
    if (!identity || session.current.saved === signature) return;
    setState({ status: "pending", error: "" });
    const timer = setTimeout(() => { save().catch(() => {}); }, 1200);
    return () => clearTimeout(timer);
  }, [identity, signature, save]);
  return { ...state, save };
}
