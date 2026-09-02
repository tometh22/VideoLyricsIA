import { useCallback, useEffect, useRef, useState } from "react";
import { mergeThreeWay, segmentsEquivalent } from "../editorMerge";
import { editorSessionHeaders } from "../lib/editorSession";

async function responseBody(response) {
  try { return await response.clone().json(); } catch { return {}; }
}

const EDITOR_LOAD_MAX_ATTEMPTS = 3;

function isTransientEditorLoad(response) {
  const status = response?.status;
  return status === 408 || status === 429 || status === 503 || status >= 500;
}

export async function loadEditorDocumentWithRetry({
  request,
  path,
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  maxAttempts = EDITOR_LOAD_MAX_ATTEMPTS,
}) {
  let lastError = null;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const response = await request(path);
      const body = await responseBody(response);
      if (response.ok) return { ok: true, body };
      const detail = typeof body?.detail === "string"
        ? body.detail : `editor_load_${response.status}`;
      lastError = new Error(detail);
      lastError.status = response.status;
      if (!isTransientEditorLoad(response)) break;
      const retryAfter = Number.parseInt(response.headers?.get?.("Retry-After") || "", 10);
      if (attempt + 1 < maxAttempts) {
        await wait(Number.isFinite(retryAfter) && retryAfter > 0
          ? Math.min(retryAfter * 1_000, 10_000)
          : 500 * (2 ** attempt));
      }
    } catch (error) {
      lastError = error;
      if (attempt + 1 < maxAttempts) await wait(500 * (2 ** attempt));
    }
  }
  return { ok: false, error: lastError || new Error("editor_load_failed") };
}

function staleDocumentFrom(body, localSegments, fallbackRevision = 0) {
  const detail = body?.detail && typeof body.detail === "object" ? body.detail : body;
  return {
    serverRevision: Number.isInteger(detail?.server_revision)
      ? detail.server_revision : fallbackRevision,
    serverSegments: Array.isArray(detail?.server_segments) ? detail.server_segments : [],
    localSegments,
  };
}

export function useEditorDocument({ jobId, enabled, request }) {
  const [document, setDocument] = useState(null);
  const [loading, setLoading] = useState(Boolean(enabled && jobId));
  const [error, setError] = useState(null);
  const [errorStatus, setErrorStatus] = useState(null);
  const [lock, setLock] = useState(null);
  const revisionRef = useRef(0);
  const documentRef = useRef(null);
  const mountedRef = useRef(true);
  const saveChainRef = useRef(Promise.resolve());
  const hasDocument = Boolean(document);

  const applyDocument = useCallback((value) => {
    if (!value || !mountedRef.current) return false;
    const current = documentRef.current;
    const sameJob = !current?.job_id || !value?.job_id || current.job_id === value.job_id;
    const isOlderRevision = sameJob
      && Number.isInteger(current?.revision)
      && Number.isInteger(value.revision)
      && value.revision < current.revision;
    // A GET used for reconciliation can be served from a lagging replica or
    // an intermediary cache. Never let that response roll the local CAS base
    // backwards or replace the screen with an older document.
    if (isOlderRevision) return false;
    documentRef.current = value;
    setDocument(value);
    if (Number.isInteger(value.revision)) revisionRef.current = value.revision;
    if (value.lock) setLock(value.lock);
    return true;
  }, []);

  const load = useCallback(async () => {
    if (!enabled || !jobId || !request) return null;
    setLoading(true);
    setError(null);
    setErrorStatus(null);
    try {
      const result = await loadEditorDocumentWithRetry({
        request, path: `/editor/${jobId}`,
      });
      if (!result.ok) throw result.error;
      const body = result.body;
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
        const response = await request(`/editor/${jobId}/lock/heartbeat`, {
          method: "POST",
          headers: editorSessionHeaders(),
        });
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
      request(`/editor/${jobId}/lock`, {
        method: "DELETE",
        keepalive: true,
        headers: editorSessionHeaders(),
      }).catch(() => {});
    };
  }, [enabled, hasDocument, jobId, request]);

  const performSave = useCallback(async (segments, checkpoint = "draft") => {
    if (!enabled || !jobId || !request) return { ok: false, reason: "disabled" };
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
        const next = staleDocumentFrom(body, segments, revisionRef.current);
        const base = documentRef.current?.segments || document?.segments || [];
        const merged = mergeThreeWay(base, segments, next.serverSegments);
        // A revision can move because of this same session (a background
        // choice, metadata write or a delayed autosave), not just another
        // browser. Rebase every 409 silently. `mergeThreeWay` keeps the local
        // field when both snapshots changed it, while retaining remote-only
        // changes. The retry still uses the newly-read CAS revision; it never
        // performs an unchecked overwrite.
        const rebasedServerDocument = {
          ...(documentRef.current || document || {}),
          ...body,
          revision: next.serverRevision,
          segments: next.serverSegments,
        };
        // Do not publish the raw server snapshot to React here. The caller
        // immediately applies `merged.merged` and retries it, but publishing
        // first creates a render window where controlled lyric inputs can be
        // overwritten by the remote value (the real two-editor E2E caught
        // exactly that same-line regression). Keep it as the CAS base only;
        // the successful retry below publishes the merged local-wins value.
        documentRef.current = rebasedServerDocument;
        revisionRef.current = next.serverRevision;
        if (rebasedServerDocument.lock) setLock(rebasedServerDocument.lock);
        return {
          ok: false,
          reason: "merged",
          mergedSegments: merged.merged,
          serverRevision: next.serverRevision,
          hadLineConflicts: merged.conflicts.length > 0,
        };
      }
      if (!response.ok) return { ok: false, reason: `http-${response.status}`, status: response.status };
      if (Number.isInteger(body.revision)) revisionRef.current = body.revision;
      if (mountedRef.current) {
        setDocument((previous) => {
          if (!previous) return previous;
          const next = {
            ...previous,
            revision: body.revision,
            segments,
            updated_at: body.saved_at,
          };
          documentRef.current = next;
          return next;
        });
      }
      // `segments` (lo que se persistió) viaja en la respuesta para que el
      // caller pueda decidir si el borrador local ya quedó cubierto. Sin esto
      // el editor borraba el draft a ciegas y perdía lo tipeado entre la
      // captura del snapshot y el OK del servidor.
      return {
        ok: true,
        revision: body.revision,
        versionId: body.version_id,
        applied: body.applied !== false,
        segments,
      };
    } catch (err) {
      return { ok: false, reason: navigator.onLine === false ? "offline" : "network", error: String(err) };
    }
  }, [applyDocument, document, enabled, jobId, request]);

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
      const currentDocument = documentRef.current;
      const sameJob = !currentDocument?.job_id || !body?.job_id || currentDocument.job_id === body.job_id;
      if (sameJob && Number.isInteger(body.revision)
        && Number.isInteger(revisionRef.current)
        && body.revision < revisionRef.current) {
        return {
          ok: true,
          document: currentDocument || document,
          sameContent: segmentsEquivalent(
            (currentDocument || document)?.segments || [],
            localSegments || [],
          ),
          staleRead: true,
        };
      }
      const remoteChanged = Number.isInteger(body.revision) && body.revision !== revisionRef.current;
      const sameContent = JSON.stringify(body.segments || []) === JSON.stringify(localSegments || []);
      if (remoteChanged && !sameContent) {
        const merged = mergeThreeWay(
          documentRef.current?.segments || document?.segments || [],
          localSegments,
          body.segments || [],
        );
        applyDocument(body);
        return {
          ok: true,
          document: body,
          sameContent: false,
          mergedSegments: merged.merged,
          hadLineConflicts: merged.conflicts.length > 0,
        };
      }
      applyDocument(body);
      return { ok: true, document: body, sameContent };
    } catch (err) {
      return { ok: false, reason: navigator.onLine === false ? "offline" : "network", error: String(err) };
    }
  }, [applyDocument, document, enabled, jobId, request]);

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
      return { ok: false, reason: "stale-revision", currentRevision: body?.current_revision };
    }
    if (!response.ok) return { ok: false, reason: `http-${response.status}` };
    applyDocument({ ...document, ...body });
    return { ok: true, document: body };
  }, [applyDocument, document, jobId, request]);

  return {
    document, loading, error, errorStatus, lock,
    revisionRef, load, save, reconcile,
    listVersions, restoreVersion,
  };
}
