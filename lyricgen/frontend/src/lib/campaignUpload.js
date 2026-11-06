// Browser port of scripts/campaign_uploader.py. The account token only asks
// for a short pairing code; the bytes travel with the campaign-scoped upload
// token issued by /upload-sessions/exchange, exactly like the terminal
// uploader. SHA-256 deduplicates items, so re-selecting the same folder after
// an interruption resumes instead of duplicating songs.
import { campaignApiBase, campaignRequest } from "./campaignApi";
import { putToR2WithProgress, withRetry } from "../r2Upload";

export const AUDIO_EXTENSIONS = [".wav", ".mp3"];
export const MAX_AUDIO_BYTES = 500 * 1024 * 1024;
export const MAX_AUDIO_SECONDS = 3600;
const MANIFEST_CHUNK = 100;
const TECHNICAL_CODE_RE = /(?:_)?(AR(?:F|UM)\d+)$/i;

/** Same contract as batch_manifest.parse_audio_filename (Title_ArtistARF123.wav). */
export function parseAudioFilename(filename) {
  const name = String(filename || "").normalize("NFC").split(/[\\/]/).pop();
  const stem = name.replace(/\.[^.]+$/, "");
  const code = stem.match(TECHNICAL_CODE_RE);
  if (!code) throw new Error(`technical ARF/ARUM code missing: ${filename}`);
  const remainder = stem.slice(0, code.index).replace(/[ _]+$/, "");
  const separator = remainder.lastIndexOf("_");
  if (separator < 0) throw new Error(`title/artist separator missing: ${filename}`);
  const title = remainder.slice(0, separator).trim();
  const artist = remainder.slice(separator + 1).trim();
  if (!title || !artist) throw new Error(`empty title or artist: ${filename}`);
  return { filename: name, title, artist, technical_code: code[1].toUpperCase() };
}

/** Best-effort split of "Artista - Título.wav" when there is no ARF code. */
export function guessFromFilename(filename) {
  const stem = String(filename || "").normalize("NFC").replace(/\.[^.]+$/, "").replace(/_/g, " ").trim();
  const parts = stem.split(" - ");
  return parts.length > 1
    ? { artist: parts[0].trim(), title: parts.slice(1).join(" - ").trim() }
    : { artist: "", title: stem };
}

function splitCsvLine(line, delimiter) {
  const cells = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (quoted) {
      if (char === '"' && line[index + 1] === '"') { cell += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === delimiter) { cells.push(cell); cell = ""; }
    else cell += char;
  }
  cells.push(cell);
  return cells.map((value) => value.trim());
}

const HEADER_ALIASES = {
  filename: ["filename", "archivo", "file", "nombre de archivo"],
  title: ["title", "titulo", "título", "tema", "cancion", "canción", "track"],
  artist: ["artist", "artista", "interprete", "intérprete"],
  technical_code: ["technical_code", "code", "codigo", "código", "arf", "arum", "isrc interno"],
};

/** Optional metadata sheet: filename,title,artist,code (comma or semicolon). */
export function parseMetadataCsv(text) {
  const lines = String(text || "").replace(/^﻿/, "").split(/\r?\n/).filter((line) => line.trim());
  if (lines.length < 2) return new Map();
  const delimiter = (lines[0].match(/;/g) || []).length > (lines[0].match(/,/g) || []).length ? ";" : ",";
  const header = splitCsvLine(lines[0], delimiter).map((value) => value.toLowerCase());
  const column = Object.fromEntries(Object.entries(HEADER_ALIASES).map(([key, aliases]) => [key, header.findIndex((name) => aliases.includes(name))]));
  if (column.filename < 0) return new Map();
  const rows = new Map();
  for (const line of lines.slice(1)) {
    const cells = splitCsvLine(line, delimiter);
    const filename = cells[column.filename]?.normalize("NFC");
    if (!filename) continue;
    rows.set(filename.toLowerCase(), {
      title: column.title >= 0 ? cells[column.title] || "" : "",
      artist: column.artist >= 0 ? cells[column.artist] || "" : "",
      technical_code: column.technical_code >= 0 ? (cells[column.technical_code] || "").toUpperCase() : "",
    });
  }
  return rows;
}

export function isAudioFile(file) {
  const name = String(file?.name || "").toLowerCase();
  return AUDIO_EXTENSIONS.some((extension) => name.endsWith(extension));
}

async function sha256(file) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function audioDuration(file, timeoutMs = 8000) {
  if (typeof document === "undefined" || typeof URL?.createObjectURL !== "function") return Promise.resolve(null);
  return new Promise((resolve) => {
    const audio = document.createElement("audio");
    const url = URL.createObjectURL(file);
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(url);
      resolve(Number.isFinite(value) && value > 0 ? value : null);
    };
    const timer = setTimeout(() => done(null), timeoutMs);
    audio.preload = "metadata";
    audio.onloadedmetadata = () => { clearTimeout(timer); done(audio.duration); };
    audio.onerror = () => { clearTimeout(timer); done(null); };
    audio.src = url;
  });
}

export function metadataIssue(entry) {
  if (entry.size_bytes <= 0 || entry.size_bytes > MAX_AUDIO_BYTES) return "invalid_size";
  if (entry.duration_seconds != null && (entry.duration_seconds <= 0 || entry.duration_seconds > MAX_AUDIO_SECONDS)) return "invalid_duration";
  if (!entry.title?.trim() || !entry.artist?.trim() || !entry.technical_code?.trim()) return "missing_metadata";
  return null;
}

/** Describe the selected files: metadata from name or sheet, hash, duration. */
export async function inspectAudioFiles(files, { csvRows = new Map(), onProgress, signal, hash = sha256, duration = audioDuration } = {}) {
  const audios = [...files].filter(isAudioFile);
  const entries = [];
  for (const [index, file] of audios.entries()) {
    if (signal?.aborted) throw Object.assign(new Error("aborted"), { aborted: true });
    onProgress?.({ index, total: audios.length, name: file.name });
    let metadata;
    try { metadata = parseAudioFilename(file.name); }
    catch { metadata = { filename: file.name.normalize("NFC"), ...guessFromFilename(file.name), technical_code: "" }; }
    const sheet = csvRows.get(file.name.normalize("NFC").toLowerCase());
    if (sheet) {
      metadata = {
        ...metadata,
        title: sheet.title || metadata.title,
        artist: sheet.artist || metadata.artist,
        technical_code: sheet.technical_code || metadata.technical_code,
      };
    }
    const entry = {
      file,
      relative_path: file.webkitRelativePath || file.name,
      filename: metadata.filename,
      title: metadata.title || "",
      artist: metadata.artist || "",
      technical_code: metadata.technical_code || "",
      size_bytes: file.size,
      duration_seconds: file.size > 0 && file.size <= MAX_AUDIO_BYTES ? await duration(file) : null,
      sha256: file.size > 0 && file.size <= MAX_AUDIO_BYTES ? await hash(file) : "0".repeat(64),
    };
    entry.client_id = entry.sha256;
    entries.push(entry);
  }
  onProgress?.({ index: audios.length, total: audios.length, name: "" });
  // Two identical files in one selection would register once; keep the
  // first so the preview count equals what the backend will create.
  const seen = new Set();
  return entries.filter((entry) => (seen.has(entry.sha256) ? false : seen.add(entry.sha256)));
}

async function uploadRequest(path, token, body, signal) {
  const response = await fetch(`${campaignApiBase()}${path}`, {
    method: "POST",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json", "X-Batch-Upload-Token": token },
    body: JSON.stringify(body ?? {}),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    throw Object.assign(new Error(typeof detail === "string" ? detail : detail?.code || `Error ${response.status}`), { status: response.status });
  }
  return payload;
}

/** Pair this tab with the campaign and exchange the code for an upload token. */
export async function openUploadSession(campaignId, { signal } = {}) {
  const pair = await campaignRequest(`/batch/campaigns/${encodeURIComponent(campaignId)}/upload-session`, { method: "POST", signal });
  const response = await fetch(`${campaignApiBase()}/batch/upload-sessions/exchange`, {
    method: "POST",
    cache: "no-store",
    signal,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ campaign_id: campaignId, code: pair.pairing_code }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error("No se pudo abrir la sesión de carga."), { status: response.status });
  return body.upload_token;
}

async function uploadOne({ entry, itemId, token, signal, onBytes }) {
  const ticket = await withRetry(() => uploadRequest(`/batch/uploads/${encodeURIComponent(itemId)}/ticket`, token, {}, signal), { maxAttempts: 4 });
  if (ticket.complete) { onBytes(entry.size_bytes); return "already"; }
  const contentType = ticket.content_type || (entry.filename.toLowerCase().endsWith(".wav") ? "audio/wav" : "audio/mpeg");
  const parts = [];
  if (ticket.use_multipart) {
    const done = new Map((ticket.uploaded_parts || []).map((part) => [
      Number(part.part_number ?? part.PartNumber), String(part.etag ?? part.ETag ?? "").replaceAll('"', ""),
    ]));
    const partSize = Number(ticket.part_size);
    for (const part of ticket.parts) {
      const number = Number(part.part_number);
      const start = (number - 1) * partSize;
      const blob = entry.file.slice(start, Math.min(start + partSize, entry.size_bytes));
      if (done.get(number)) {
        parts.push({ part_number: number, etag: done.get(number) });
        onBytes(blob.size);
        continue;
      }
      let sent = 0;
      const { etag } = await withRetry(() => {
        onBytes(-sent); sent = 0;
        return putToR2WithProgress(part.url, blob, undefined, (loaded) => { onBytes(loaded - sent); sent = loaded; }, signal);
      }, { maxAttempts: 5 });
      if (!etag) throw new Error("El almacenamiento no devolvió la confirmación de la parte (ETag). Usá el cargador por terminal.");
      parts.push({ part_number: number, etag: etag.replaceAll('"', "") });
    }
  } else {
    let sent = 0;
    await withRetry(() => {
      onBytes(-sent); sent = 0;
      return putToR2WithProgress(ticket.upload_url, entry.file, contentType, (loaded) => { onBytes(loaded - sent); sent = loaded; }, signal);
    }, { maxAttempts: 5 });
  }
  await withRetry(() => uploadRequest(`/batch/uploads/${encodeURIComponent(itemId)}/complete`, token, { parts }, signal), { maxAttempts: 4 });
  return "uploaded";
}

/**
 * Register and upload. `onProgress` receives
 * { phase, registered, uploaded, failed, bytesDone, bytesTotal, current, errors }.
 */
export async function uploadCampaignAudios(campaignId, entries, { onProgress, signal, concurrency = 3, openSession = openUploadSession } = {}) {
  const valid = entries.filter((entry) => !["invalid_size", "invalid_duration"].includes(metadataIssue(entry)));
  const state = {
    phase: "registering", registered: 0, duplicates: 0, uploaded: 0, failed: 0,
    bytesDone: 0, bytesTotal: valid.reduce((sum, entry) => sum + entry.size_bytes, 0),
    current: "", errors: [], skipped: entries.length - valid.length,
  };
  const emit = () => onProgress?.({ ...state, errors: [...state.errors] });
  emit();
  const token = await openSession(campaignId, { signal });
  const itemIds = new Map();
  for (let start = 0; start < valid.length; start += MANIFEST_CHUNK) {
    const chunk = valid.slice(start, start + MANIFEST_CHUNK);
    const manifest = await uploadRequest(`/batch/campaigns/${encodeURIComponent(campaignId)}/manifest`, token, {
      items: chunk.map((entry) => ({
        client_id: entry.client_id,
        filename: entry.filename,
        title: entry.title.trim() || null,
        artist: entry.artist.trim() || null,
        technical_code: entry.technical_code.trim() || null,
        size_bytes: entry.size_bytes,
        duration_seconds: entry.duration_seconds,
        sha256: entry.sha256,
        metadata_error: metadataIssue(entry),
      })),
    }, signal);
    for (const result of manifest.items || []) {
      itemIds.set(result.client_id, result.item_id);
      if (result.duplicate) state.duplicates += 1;
    }
    state.registered = itemIds.size;
    emit();
  }
  state.phase = "uploading";
  emit();
  let cursor = 0;
  const worker = async () => {
    while (cursor < valid.length) {
      if (signal?.aborted) return;
      const entry = valid[cursor];
      cursor += 1;
      const itemId = itemIds.get(entry.client_id);
      if (!itemId) { state.failed += 1; state.errors.push({ filename: entry.filename, message: "No quedó registrada." }); emit(); continue; }
      state.current = entry.filename;
      emit();
      let counted = 0;
      try {
        await uploadOne({ entry, itemId, token, signal, onBytes: (delta) => { counted += delta; state.bytesDone += delta; emit(); } });
        state.uploaded += 1;
      } catch (error) {
        if (error?.aborted || signal?.aborted) return;
        state.bytesDone -= counted;
        state.failed += 1;
        state.errors.push({ filename: entry.filename, message: error.message || "No se pudo subir." });
      }
      emit();
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, valid.length) }, worker));
  if (signal?.aborted) throw Object.assign(new Error("aborted"), { aborted: true });
  state.phase = "done";
  state.current = "";
  emit();
  return { ...state };
}
