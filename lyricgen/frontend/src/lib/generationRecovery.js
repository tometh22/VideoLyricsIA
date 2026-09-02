// Recovery for the narrow window between lyric approval and /generate.
// A transcribed job is normally reused by id.  If that temporary row was
// reaped or is otherwise unavailable, the browser may still have the original
// audio File and the exact approved segments.  In that case we can safely
// create a fresh job rather than asking the operator to redo the lyrics.

export function isMissingGenerationJob(response, payload) {
  return payload?.code === "job_not_found"
    || (response?.status === 404 && /job not found/i.test(String(payload?.detail || "")));
}

export function canRebuildMissingGenerationJob(job) {
  return !!job?._file && typeof job._file.slice === "function";
}

// Mutates only the request being retried.  The approved job object remains
// immutable, so its original selector stays available for recovery/Undo UI.
export function rebuildGenerationRequestFromLocalAudio(formData, job) {
  formData.delete("job_id");
  formData.delete("base_revision");
  formData.delete("editor_revision");
  formData.delete("editor_version_id");
  formData.set("file", job._file, job.filename || "audio.mp3");
  return formData;
}
