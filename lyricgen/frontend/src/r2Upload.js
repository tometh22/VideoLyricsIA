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
 * The API container never sees the audio body — that's the point.
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
 * Single-PUT upload with progress + abort support.
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
        // ETag header is what `multipart_complete` needs. Browsers
        // can sometimes block reading it (CORS exposure rules), but
        // for single-PUT we don't need it.
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

/** Upload a small file (<16 MB) via the backend proxy (POST raw bytes).
 * Avoids browser→R2 CORS preflight by routing through the same-origin API. */
function uploadFileProxy(jobId, blob, contentType, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/upload-file-proxy?job_id=${encodeURIComponent(jobId)}`, true);
    const token = localStorage.getItem("genly_token");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    if (contentType) xhr.setRequestHeader("Content-Type", contentType);
    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const { key } = JSON.parse(xhr.responseText);
          resolve({ key });
        } catch {
          reject(new Error("File proxy upload: invalid response"));
        }
      } else {
        let detail = "";
        try { detail = JSON.parse(xhr.responseText).detail || ""; } catch {}
        reject(new Error(`File proxy upload failed (${xhr.status})${detail ? ": " + detail : ""}`));
      }
    };
    xhr.onerror = () => reject(new Error("File proxy upload network error"));
    xhr.onabort = () => reject(Object.assign(new Error("aborted"), { aborted: true }));
    if (signal) {
      if (signal.aborted) { xhr.abort(); return; }
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(blob);
  });
}

/** Upload one multipart part via the backend proxy (POST raw bytes).
 * Avoids browser→R2 CORS preflight by routing through the same-origin API. */
function uploadPartProxy(jobId, partNumber, blob, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      `${API}/upload-part-proxy?job_id=${encodeURIComponent(jobId)}&part_number=${partNumber}`,
      true,
    );
    const token = localStorage.getItem("genly_token");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      };
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const { etag } = JSON.parse(xhr.responseText);
          resolve(etag);
        } catch {
          reject(new Error("Proxy upload: invalid response"));
        }
      } else {
        let detail = "";
        try { detail = JSON.parse(xhr.responseText).detail || ""; } catch {}
        reject(new Error(`Proxy upload failed (${xhr.status})${detail ? ": " + detail : ""}`));
      }
    };
    xhr.onerror = () => reject(new Error("Proxy upload network error"));
    xhr.onabort = () => reject(Object.assign(new Error("aborted"), { aborted: true }));
    if (signal) {
      if (signal.aborted) { xhr.abort(); return; }
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

/** Multipart upload. Slices the File, presigns each part, PUTs in
 * parallel (capped concurrency), tracks per-part progress, finalizes. */
async function multipartUpload({
  file,
  jobId,
  uploadId,
  key,
  partSize,
  contentType,
  concurrency = 4,
  onProgress,
  signal,
}) {
  const totalSize = file.size;
  const partCount = Math.ceil(totalSize / partSize);
  const parts = []; // {part_number, etag}
  // Per-part bytes uploaded so far. Aggregate sum drives the UI.
  const perPartLoaded = new Array(partCount).fill(0);

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
        const etag = await withRetry(async () => {
          // Reset the part's progress on retry so the UI doesn't
          // double-count (otherwise a retry from byte 0 would push the
          // global counter past 100%).
          perPartLoaded[i] = 0;
          reportProgress();
          const result = await uploadPartProxy(
            jobId, partNumber, blob,
            (loaded) => { perPartLoaded[i] = loaded; reportProgress(); },
            signal,
          );
          // ensure final byte count is reflected even if onprogress
          // missed the very last chunk.
          perPartLoaded[i] = blob.size;
          reportProgress();
          return result;
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
    // Best-effort abort so R2 doesn't keep the orphan parts around.
    try {
      await apiPost("/upload-multipart-abort", { job_id: jobId });
    } catch {}
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
 *     filename. Frontend already collects these per-row in UploadZone.
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
    // Route through the backend proxy so R2 bucket CORS is never on the
    // critical path — same approach as the multipart part proxy.
    await uploadFileProxy(ticket.job_id, file, contentType, onProgress, signal);
    return { jobId: ticket.job_id, key: ticket.key };
  }

  // Multipart path.
  const init = await apiPost("/upload-multipart-init", {
    job_id: ticket.job_id,
    filename: file.name,
    content_type: contentType,
  });

  return multipartUpload({
    file,
    jobId: ticket.job_id,
    uploadId: init.upload_id,
    key: init.key,
    partSize: init.part_size || ticket.part_size,
    contentType,
    onProgress,
    signal,
  });
}
