/**
 * Direct-to-R2 upload client.
 *
 * Workflow:
 *   1. Ask the API for a presigned upload "ticket" via /upload-url. The
 *      response decides whether we go single-PUT or multipart based on
 *      file size (threshold lives on the backend, not duplicated here).
 *   2. Single-PUT: one XHR.PUT against the presigned URL. We use XHR
 *      (not fetch) so we get `progress` events for the UI.
 *   3. Multipart: ask /upload-multipart-init for an upload_id, then
 *      slice the File, sign each part via /upload-multipart-part-url,
 *      PUT it, capture the ETag, and finalize via
 *      /upload-multipart-complete. Parts upload in parallel (capped) and
 *      a failed part retries with exponential backoff before failing
 *      the whole upload.
 *
 * The API container never sees the audio body — that's the point. This
 * file historically routed through /upload-part-proxy (the API
 * container relayed bytes to R2) to dodge a CORS 403 from R2. Root
 * cause was the wrong AllowedOrigins in the bucket CORS policy. With
 * scripts/r2_cors.json updated to include app.genly.pro and
 * staging.app.genly.pro and applied via configure_r2_cors.sh, direct
 * PUT works again and we no longer take Cloudflare's ~100 s proxy
 * timeout on slow upstreams.
 *
 * Returns the job_id once the upload finishes; the caller follows up
 * with /transcribe-uploaded (editor flow) or /generate (direct).
 */

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiPost(path, body) {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try {
      const j = await res.clone().json();
      detail = j.detail ? `: ${j.detail}` : "";
    } catch {}
    const err = new Error(`POST ${path} failed (${res.status})${detail}`);
    err.status = res.status;
    err.response = res;
    throw err;
  }
  return res.json();
}

/**
 * PUT a blob to R2 via a presigned URL with progress + abort support.
 *
 * Why XHR and not fetch: the fetch API only emits `Response` body
 * progress (download), not request body progress (upload). XHR's
 * `upload.onprogress` is the only browser-portable way to get a real
 * 0-100% bar during the PUT.
 */
function putToR2WithProgress(url, blob, contentType, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);
    if (contentType) xhr.setRequestHeader("Content-Type", contentType);
    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        // ETag header is required for multipart_complete. R2's CORS
        // policy must expose it via ExposeHeaders: ["ETag"] (see
        // scripts/r2_cors.json). If the policy is missing, ETag reads
        // as null and the multipart upload finalizes broken.
        resolve({ etag: xhr.getResponseHeader("ETag") || null });
      } else {
        reject(new Error(`R2 PUT failed: ${xhr.status} ${xhr.statusText}`));
      }
    };
    xhr.onerror = () => reject(new Error("R2 PUT network error"));
    xhr.onabort = () => reject(Object.assign(new Error("aborted"), { aborted: true }));
    if (signal) {
      if (signal.aborted) {
        xhr.abort();
        return;
      }
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(blob);
  });
}

/** Backoff helper for retrying a single multipart part. */
async function withRetry(fn, { maxAttempts = 6, baseMs = 1000 } = {}) {
  let lastErr;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return await fn(attempt);
    } catch (err) {
      if (err.aborted) throw err;
      lastErr = err;
      if (attempt === maxAttempts - 1) break;
      const wait = baseMs * Math.pow(2, attempt);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
  throw lastErr;
}

/** Multipart upload. Slices the File, presigns each part, PUTs directly
 * to R2 in parallel (capped concurrency), tracks per-part progress,
 * finalizes via the backend. */
async function multipartUpload({
  file,
  jobId,
  uploadId,
  key,
  partSize,
  contentType,
  // 2026-05-25: Concurrencia 4→8. En conexiones argentinas estables
  // (Fibertel/Claro ~10-30 Mbps upload) los 4 anteriores saturaban ~25 %
  // del bandwidth porque cada socket TCP tarda ~RTT en crecer su window.
  // 8 workers paralelos saturan ~50 % sin pegar contra el TCP slow-start
  // de Cloudflare ni contra el cap del browser (HTTP/1.1 conexión
  // máxima ~6 per host; HTTP/2 conn. multiplex es ilimitado en práctica).
  concurrency = 8,
  // 2026-05-25 batch presign: si el server respondió init con
  // `presigned_parts: [{part_number, url}]`, los usamos en lugar de
  // hacer 1 round-trip por chunk. null/undefined = fallback per-part.
  presignedParts = null,
  onProgress,
  signal,
}) {
  const totalSize = file.size;
  const partCount = Math.ceil(totalSize / partSize);
  const parts = []; // {part_number, etag}
  // Per-part bytes uploaded so far. Aggregate sum drives the UI.
  const perPartLoaded = new Array(partCount).fill(0);
  // Lookup table {1: "url", 2: "url", ...} para acceso O(1) por part_number
  const presignedMap = {};
  if (Array.isArray(presignedParts)) {
    for (const p of presignedParts) {
      if (p && typeof p.part_number === "number" && typeof p.url === "string") {
        presignedMap[p.part_number] = p.url;
      }
    }
  }

  const reportProgress = () => {
    if (!onProgress) return;
    const loaded = perPartLoaded.reduce((a, b) => a + b, 0);
    onProgress(loaded, totalSize);
  };

  let nextPartIdx = 0;
  let firstError = null;

  const worker = async () => {
    while (nextPartIdx < partCount && !firstError && !(signal?.aborted)) {
      const i = nextPartIdx++;
      const partNumber = i + 1;
      const start = i * partSize;
      const end = Math.min(start + partSize, totalSize);
      const blob = file.slice(start, end);
      try {
        // Per-attempt scratch so retries don't push the global counter
        // past 100%, but the COMMITTED `perPartLoaded[i]` stays at its
        // highest seen value — the bar never visually regresses
        // (operator UX fix 2026-05-25; the previous code reset to 0 on
        // each retry and the bar would jump backward, suggesting
        // failure when the upload was actually mid-retry).
        let attemptLoaded = 0;
        const etag = await withRetry(async () => {
          attemptLoaded = 0;
          // Presigned URL: prefer batched init response on first attempt
          // (no round-trip). Retries always fetch fresh per-part —
          // batched URLs share the same TTL, so if one expired during
          // the retry-backoff sleeps the rest probably did too.
          let url = presignedMap[partNumber];
          if (url) {
            delete presignedMap[partNumber];  // consume — next retry per-part
          } else {
            const resp = await apiPost("/upload-multipart-part-url", {
              job_id: jobId, part_number: partNumber,
            });
            url = resp.url;
          }
          const res = await putToR2WithProgress(
            url, blob, contentType,
            (loaded /* total */) => {
              attemptLoaded = loaded;
              // Only advance the visible counter — never decrease it
              // (so retries from byte 0 don't make the bar regress).
              if (loaded > perPartLoaded[i]) {
                perPartLoaded[i] = loaded;
                reportProgress();
              }
            },
            signal,
          );
          if (!res.etag) {
            throw new Error(
              `Part ${partNumber}: R2 returned no ETag — ` +
              `likely a CORS ExposeHeaders: ["ETag"] config issue. ` +
              `Re-apply scripts/r2_cors.json via configure_r2_cors.sh.`
            );
          }
          // ensure final byte count is reflected even if onprogress
          // missed the very last chunk.
          perPartLoaded[i] = blob.size;
          reportProgress();
          return res.etag;
        });
        parts.push({ part_number: partNumber, etag });
      } catch (err) {
        if (!firstError) firstError = err;
        return;
      }
    }
  };

  const workerCount = Math.min(concurrency, partCount);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));

  if (firstError) {
    // QA fix 2026-05-28 (audit P0 #76): abort retry. Pre-fix el abort
    // era best-effort con try/catch{} silente — si la primera POST
    // fallaba (red caída justo cuando la subida ya falló), las
    // partes quedaban huérfanas en R2 forever (costo storage +
    // posible truncation por cleanup policies). Ahora intentamos con
    // backoff exponencial 4 veces (1s, 2s, 4s, 8s) y si todo falla
    // logueamos un warning estructurado para que el server-side
    // sweeper (PR siguiente) los limpie. El throw del firstError sigue
    // ocurriendo así que el caller se entera del upload fail.
    try {
      await withRetry(
        () => apiPost("/upload-multipart-abort", { job_id: jobId }),
        { maxAttempts: 4, baseMs: 1000 }
      );
    } catch (abortErr) {
      console.warn(
        "[r2Upload] multipart abort failed after retries — orphan parts in R2",
        { jobId, abortErr: String(abortErr) }
      );
    }
    throw firstError;
  }

  await apiPost("/upload-multipart-complete", {
    job_id: jobId,
    parts,
  });
  return { jobId, key };
}

/**
 * Public entrypoint. Uploads `file` directly to R2 and returns the
 * `job_id` once the bytes are durably stored.
 *
 * `meta` is forwarded to /upload-url:
 *   - artist, title: optional pre-fill so the backend can short-circuit
 *     the lrclib lookup with a clean string instead of parsing the
 *     filename.
 *
 * `onProgress(loaded, total)` is called with cumulative byte counts as
 * upload progresses (single-PUT and multipart both report).
 */
export async function uploadFileToR2(
  file,
  { meta = {}, onProgress = null, signal = null } = {},
) {
  const ticket = await apiPost("/upload-url", {
    filename: file.name,
    content_type: file.type || "",
    size_bytes: file.size,
    artist: meta.artist || "",
    title: meta.title || "",
  });

  const contentType = file.type || "application/octet-stream";

  if (!ticket.use_multipart) {
    // Single-PUT path: one XHR.PUT direct to the presigned URL, with the
    // same retry budget the multipart parts get — a single network blip
    // used to kill the whole flow (and the manual retry minted a NEW
    // job). Progress is clamped monotonic so a retry from byte 0
    // doesn't make the bar regress. The presigned URL stays valid
    // across attempts (15-min TTL vs ≤16 MB body: the retry window is
    // seconds, not minutes).
    let maxLoaded = 0;
    await withRetry(async () => {
      await putToR2WithProgress(
        ticket.upload_url, file, contentType,
        onProgress
          ? (loaded, total) => {
              if (loaded > maxLoaded) {
                maxLoaded = loaded;
                onProgress(loaded, total);
              }
            }
          : null,
        signal,
      );
    }, { maxAttempts: 3 });
    return { jobId: ticket.job_id, key: ticket.key };
  }

  // Multipart path. Compute expected chunk count up front and send it
  // to /upload-multipart-init so the backend can presign all parts in
  // a single round-trip — saves N×RTT (typical AR ~100 ms × 7 chunks
  // = 700 ms wallclock overhead removed).
  const effectivePartSize = ticket.part_size || (8 * 1024 * 1024);
  const expectedParts = Math.ceil(file.size / effectivePartSize);
  const init = await apiPost("/upload-multipart-init", {
    job_id: ticket.job_id,
    filename: file.name,
    content_type: contentType,
    expected_parts: expectedParts,
  });

  return multipartUpload({
    file,
    jobId: ticket.job_id,
    uploadId: init.upload_id,
    key: init.key,
    partSize: init.part_size || effectivePartSize,
    contentType,
    presignedParts: init.presigned_parts || null,
    onProgress,
    signal,
  });
}
