import { useCallback, useEffect, useRef, useState } from "react";

async function responseBody(response) {
  try { return await response.clone().json(); } catch { return {}; }
}

function conflictFrom(body, localSegments, fallbackRevision = 0) {
  const detail = body?.detail && typeof body.detail === "object" ? body.detail : body;
  return {
    serverRevision: Number.isInteger(detail?.server_revision)
      ? detail.server_revision : fallbackRevision,
    serverSegments: Array.isArray(detail?.server_segments) ? detail.server_segments : [],
    updatedBy: detail?.updated_by || null,
    updatedAt: detail?.updated_at || null,
    localSegments,
  };
}

export function useEditorDocument({ jobId, enabled, request }) {
  const [document, setDocument] = useState(null);
  const [loading, setLoading] = useState(Boolean(enabled && jobId));
  const [error, setError] = useState(null);
  const [errorStatus, setErrorStatus] = useState(null);
  const [conflict, setConflict] = useState(null);
  const [lock, setLock] = useState(null);
  const revisionRef = useRef(0);
  const mountedRef = useRef(true);
  const conflictRef = useRef(null);
  const saveChainRef = useRef(Promise.resolve());
  const hasDocument = Boolean(document);
  conflictRef.current = conflict;

  const applyDocument = useCallback((value) => {
    if (!value || !mountedRef.current) return;
    setDocument(value);
    if (Number.isInteger(value.revision)) revisionRef.current = value.revision;
    if (value.lock) setLock(value.lock);
  }, []);

  const load = useCallback(async () => {
    if (!enabled || !jobId || !request) return null;
    setLoading(true);
    setError(null);
    setErrorStatus(null);
    try {
      const response = await request(`/editor/${jobId}`);
      const body = await responseBody(response);
      if (!response.ok) {
        const detail = typeof body?.detail === "string"
          ? body.detail : `editor_load_${response.status}`;
        const loadError = new Error(detail);
        loadError.status = response.status;
        throw loadError;
      }
      applyDocument(body);
      return body;
    } catch (err) {
      if (mountedRef.current) {
        setError(String(err));
        setErrorStatus(Number.isInteger(err?.status) ? err.status : null);
      }
      return null;
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [applyDocument, enabled, jobId, request]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => { mountedRef.current = false; };
  }, [load]);

  useEffect(() => {
    // A lock only exists after the durable document loaded.  Starting the
    // heartbeat before that point used to hammer a failing historical job
    // with a 404 every 15 seconds while the UI remained silently blocked.
    if (!enabled || !jobId || !request || !hasDocument) return undefined;
    let stopped = false;
    const heartbeat = async () => {
      try {
        const response = await request(`/editor/${jobId}/lock/heartbeat`, { method: "POST" });
        const body = await responseBody(response);
        if (!stopped && response.ok) setLock({
          active: true, user: body.user, expires_at: body.expires_at,
          acquired: body.acquired,
        });
      } catch { /* soft lock never blocks local editing */ }
    };
    heartbeat();
    const timer = window.setInterval(heartbeat, 15_000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      request(`/editor/${jobId}/lock`, { method: "DELETE", keepalive: true }).catch(() => {});
    };
  }, [enabled, hasDocument, jobId, request]);

  const performSave = useCallback(async (segments, checkpoint = "draft") => {
    if (!enabled || !jobId || !request) return { ok: false, reason: "disabled" };
    if (conflictRef.current) return { ok: false, reason: "conflict" };
    try {
      const response = await request(`/editor/${jobId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_revision: revisionRef.current,
          segments,
          checkpoint,
        }),
      });
      const body = await responseBody(response);
      if (response.status === 409) {
        const next = conflictFrom(body, segments, revisionRef.current);
        if (mountedRef.current) setConflict(next);
        return { ok: false, reason: "conflict", conflict: next };
      }
      if (!response.ok) return { ok: false, reason: `http-${response.status}`, status: response.status };
      if (Number.isInteger(body.revision)) revisionRef.current = body.revision;
      if (mountedRef.current) {
        setDocument((previous) => previous ? {
          ...previous,
          revision: body.revision,
          segments,
          updated_at: body.saved_at,
        } : previous);
      }
      return { ok: true, revision: body.revision, versionId: body.version_id, applied: body.applied !== false };
    } catch (err) {
      return { ok: false, reason: navigator.onLine === false ? "offline" : "network", error: String(err) };
    }
  }, [enabled, jobId, request]);

  // Draft, checkpoint and structural saves share one optimistic revision.
  // Serialize them so a slow request cannot make this tab conflict with its
  // own subsequent edit; each queued request reads revisionRef only when it
  // actually starts.
  const save = useCallback((segments, checkpoint = "draft") => {
    const operation = saveChainRef.current.then(
      () => performSave(segments, checkpoint),
      () => performSave(segments, checkpoint),
    );
    saveChainRef.current = operation.then(() => undefined, () => undefined);
    return operation;
  }, [performSave]);

  const reconcile = useCallback(async (localSegments) => {
    if (!enabled || !jobId || !request) return { ok: false, reason: "disabled" };
    try {
      const response = await request(`/editor/${jobId}`);
      const body = await responseBody(response);
      if (!response.ok) return { ok: false, reason: `http-${response.status}` };
      const remoteChanged = Number.isInteger(body.revision) && body.revision !== revisionRef.current;
      const sameContent = JSON.stringify(body.segments || []) === JSON.stringify(localSegments || []);
      if (remoteChanged && !sameContent) {
        const next = conflictFrom({
          server_revision: body.revision,
          server_segments: body.segments,
          updated_by: body.updated_by,
          updated_at: body.updated_at,
        }, localSegments, revisionRef.current);
        if (mountedRef.current) setConflict(next);
        return { ok: false, reason: "conflict", conflict: next };
      }
      applyDocument(body);
      return { ok: true, document: body, sameContent };
    } catch (err) {
      return { ok: false, reason: navigator.onLine === false ? "offline" : "network", error: String(err) };
    }
  }, [applyDocument, enabled, jobId, request]);

  const stageConflict = useCallback((localSegments, metadata = {}) => {
    setConflict({
      serverRevision: revisionRef.current,
      serverSegments: document?.segments || [],
      updatedBy: document?.updated_by || null,
      updatedAt: document?.updated_at || null,
      localSegments,
      ...metadata,
    });
  }, [document]);

  const resolve = useCallback(async (strategy) => {
    if (!conflict || !request || !jobId) return null;
    const response = await request(`/editor/${jobId}/conflicts/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        strategy,
        server_revision: conflict.serverRevision,
        ...(strategy === "save_local_as_new" ? { segments: conflict.localSegments } : {}),
      }),
    });
    const body = await responseBody(response);
    if (response.status === 409) {
      setConflict(conflictFrom(body, conflict.localSegments, conflict.serverRevision));
      return { ok: false, reason: "conflict" };
    }
    if (!response.ok) return { ok: false, reason: `http-${response.status}` };
    applyDocument(body);
    setConflict(null);
    return { ok: true, document: body };
  }, [applyDocument, conflict, jobId, request]);

  const listVersions = useCallback(async () => {
    const response = await request(`/editor/${jobId}/versions?limit=50`);
    const body = await responseBody(response);
    return response.ok ? body.versions || [] : [];
  }, [jobId, request]);

  const restoreVersion = useCallback(async (versionId) => {
    const response = await request(`/editor/${jobId}/restore`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version_id: versionId, base_revision: revisionRef.current }),
    });
    const body = await responseBody(response);
    if (response.status === 409) {
      setConflict(conflictFrom(body, document?.segments || [], revisionRef.current));
      return { ok: false, reason: "conflict" };
    }
    if (!response.ok) return { ok: false, reason: `http-${response.status}` };
    applyDocument({ ...document, ...body });
    return { ok: true, document: body };
  }, [applyDocument, document, jobId, request]);

  return {
    document, loading, error, errorStatus, conflict, lock,
    revisionRef, load, save, reconcile, stageConflict, resolve,
    listVersions, restoreVersion,
  };
}
