/**
 * Stable cache key for the background-transcription cache (App.jsx
 * `prefetchCache`).
 *
 * THE BUG THIS PREVENTS (incident 2026-06-01): the cache used to be keyed
 * by the ARRAY INDEX into `files`. `removeFile` re-packs the array (filter),
 * so when an operator removed the previous song's audio and uploaded a new
 * one into the freed index, the new audio inherited the previous song's
 * cached transcription — the editor showed the PREVIOUS song's lyrics over
 * the NEW audio (and `transcribeJobId` pointed at the wrong job).
 *
 * Keying by FILE IDENTITY instead of index makes a removed/replaced file
 * impossible to alias onto another file's results: a different audio has a
 * different (name, size, lastModified), and the same audio re-picked gets
 * the same key (so the cache hit is correct).
 *
 * `name + size + lastModified` uniquely identifies a picked File in
 * practice. Restored wizard stubs (wizardPersistence.rehydrate*) carry the
 * same three fields, so the key is stable across a resume too. A missing
 * file yields "" — degenerate, but there is nothing to transcribe then.
 */
export function prefetchKey(file) {
  if (!file) return "";
  return `${file.name || ""}:${file.size || 0}:${file.lastModified || 0}`;
}
