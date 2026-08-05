import { useCallback, useEffect, useRef, useState } from "react";

const API = import.meta.env.VITE_API_URL || "";
const LOCAL_DRAFT_PREFIX = "genly_editor_draft:";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function draftKey(jobId) {
  return `${LOCAL_DRAFT_PREFIX}${jobId}`;
}

function readLocalDraft(jobId) {
  if (!jobId) return null;
  try {
    const value = JSON.parse(localStorage.getItem(draftKey(jobId)) || "null");
    return Array.isArray(value?.segments) ? value : null;
  } catch {
    return null;
  }
}

function writeLocalDraft(jobId, segments) {
  if (!jobId) return;
  try {
    localStorage.setItem(draftKey(jobId), JSON.stringify({ segments, updatedAt: Date.now() }));
  } catch {}
}

export function useEditorDocument({ jobId, initialSegments, onRemoteSegments }) {
  const [revision, setRevision] = useState(0);
  const [saveState, setSaveState] = useState(jobId ? "loading" : "local");
  const [remoteMeta, setRemoteMeta] = useState(null);
  const [conflict, setConflict] = useState(null);
  const [versions, setVersions] = useState([]);
  const revisionRef = useRef(0);
  const pendingRef = useRef(null);
  const timerRef = useRef(null);
  const inflightRef = useRef(false);
  const inflightPromiseRef = useRef(null);
  const mountedRef = useRef(true);

  useEffect(() => () => {
    mountedRef.current = false;
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const load = useCallback(async () => {
    if (!jobId) {
      setSaveState("local");
      return;
    }
    setSaveState("loading");
    try {
      const response = await fetch(`${API}/editor/${encodeURIComponent(jobId)}`, {
        headers: authHeaders(),
      });
      if (!response.ok) throw new Error(`editor_load_${response.status}`);
      const data = await response.json();
      revisionRef.current = Number(data.revision) || 0;
      setRevision(revisionRef.current);
      setRemoteMeta(data);
      onRemoteSegments?.(data.segments || initialSegments || []);
      setSaveState("saved");
      return data;
    } catch {
      // A seeded transcription should normally make this impossible. Keep a
      // usable local editor if an older job has no document yet or the API is
      // temporarily unavailable.
      const local = readLocalDraft(jobId);
      if (local?.segments) onRemoteSegments?.(local.segments);
      setSaveState("offline");
      return null;
    }
  }, [initialSegments, jobId, onRemoteSegments]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    const lockUrl = `${API}/editor/${encodeURIComponent(jobId)}/lock`;
    const acquire = async (heartbeat = false) => {
      try {
        const response = await fetch(
          heartbeat ? `${lockUrl}/heartbeat` : lockUrl,
          { method: "POST", headers: authHeaders() },
        );
        if (!response.ok || cancelled) return;
        const lock = await response.json();
        setRemoteMeta((current) => ({ ...current, lock }));
      } catch {}
    };
    acquire();
    const timer = setInterval(() => acquire(true), 15_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
      fetch(lockUrl, { method: "DELETE", headers: authHeaders(), keepalive: true }).catch(() => {});
    };
  }, [jobId]);

  const saveRequest = useCallback(async (segments, checkpoint = "autosave") => {
    if (!jobId) {
      writeLocalDraft(jobId, segments);
      setSaveState("local");
      return { ok: true, local: true };
    }
    let response;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        response = await fetch(`${API}/editor/${encodeURIComponent(jobId)}`, {
          method: "PATCH",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({
            base_revision: revisionRef.current,
            segments,
            checkpoint,
          }),
        });
        if (response.status < 500 || attempt === 2) break;
      } catch (error) {
        if (attempt === 2) throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, 250 * (2 ** attempt)));
    }
    let data = null;
    try { data = await response.json(); } catch {}
    if (response.status === 409) {
      const detail = data?.detail || data || {};
      const nextConflict = {
        serverRevision: detail.server_revision,
        serverSegments: detail.server_segments || [],
        updatedBy: detail.updated_by,
        updatedAt: detail.updated_at,
      };
      setConflict(nextConflict);
      setSaveState("conflict");
      return { ok: false, conflict: nextConflict };
    }
    if (!response.ok) throw new Error(data?.detail || `editor_save_${response.status}`);
    revisionRef.current = Number(data.revision) || revisionRef.current + 1;
    if (mountedRef.current) {
      setRevision(revisionRef.current);
      setSaveState("saved");
    }
    writeLocalDraft(jobId, segments);
    return { ok: true, data };
  }, [jobId]);

  const flush = useCallback(async () => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
    if (inflightRef.current) return inflightPromiseRef.current;
    if (!pendingRef.current) return null;
    const pending = pendingRef.current;
    pendingRef.current = null;
    inflightRef.current = true;
    setSaveState("saving");
    const operation = (async () => {
      try {
        return await saveRequest(pending.segments, pending.checkpoint);
      } catch {
        setSaveState("offline");
        return { ok: false, offline: true };
      } finally {
        inflightRef.current = false;
        inflightPromiseRef.current = null;
        if (pendingRef.current) {
          timerRef.current = setTimeout(flush, 800);
        }
      }
    })();
    inflightPromiseRef.current = operation;
    return operation;
  }, [saveRequest]);

  const scheduleSave = useCallback((segments, checkpoint = "autosave") => {
    writeLocalDraft(jobId, segments);
    pendingRef.current = { segments, checkpoint };
    if (timerRef.current) clearTimeout(timerRef.current);
    setSaveState("saving");
    timerRef.current = setTimeout(flush, 800);
  }, [flush, jobId]);

  const fetchVersions = useCallback(async () => {
    if (!jobId) return [];
    try {
      const response = await fetch(`${API}/editor/${encodeURIComponent(jobId)}/versions`, {
        headers: authHeaders(),
      });
      if (!response.ok) return [];
      const data = await response.json();
      setVersions(data.versions || []);
      return data.versions || [];
    } catch { return []; }
  }, [jobId]);

  const restore = useCallback(async (versionId) => {
    if (!jobId) return { ok: false };
    const response = await fetch(`${API}/editor/${encodeURIComponent(jobId)}/restore`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ version_id: versionId, base_revision: revisionRef.current }),
    });
    let data = null;
    try { data = await response.json(); } catch {}
    if (response.status === 409) {
      setConflict(data?.detail || data);
      setSaveState("conflict");
      return { ok: false, conflict: data?.detail || data };
    }
    if (!response.ok) return { ok: false };
    revisionRef.current = Number(data.revision) || revisionRef.current + 1;
    setRevision(revisionRef.current);
    setRemoteMeta((current) => ({ ...current, revision: revisionRef.current }));
    onRemoteSegments?.(data.segments || []);
    setSaveState("saved");
    setConflict(null);
    writeLocalDraft(jobId, data.segments || []);
    await fetchVersions();
    return { ok: true, data };
  }, [fetchVersions, jobId, onRemoteSegments]);

  const resolveConflictWithServer = useCallback(() => {
    if (!conflict) return;
    revisionRef.current = Number(conflict.serverRevision) || revisionRef.current;
    setRevision(revisionRef.current);
    onRemoteSegments?.(conflict.serverSegments || []);
    setConflict(null);
    setSaveState("saved");
  }, [conflict, onRemoteSegments]);

  const rebaseLocalChanges = useCallback(async (segments) => {
    if (!conflict) return { ok: false };
    revisionRef.current = Number(conflict.serverRevision) || revisionRef.current;
    setRevision(revisionRef.current);
    setConflict(null);
    pendingRef.current = { segments, checkpoint: "manual" };
    return flush();
  }, [conflict, flush]);

  const dismissConflict = useCallback(() => {
    setConflict(null);
    setSaveState("local");
  }, []);

  return {
    revision,
    saveState,
    remoteMeta,
    conflict,
    versions,
    scheduleSave,
    flush,
    load,
    fetchVersions,
    restore,
    resolveConflictWithServer,
    rebaseLocalChanges,
    dismissConflict,
  };
}
