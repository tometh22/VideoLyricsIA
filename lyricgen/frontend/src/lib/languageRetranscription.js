// Re-transcribe a song from its audio with a reviewer-chosen language.
// Mirrors SUPPORTED_LANGUAGES in lyricgen/backend/transcription_language.py.
export const RETRANSCRIBE_LANGUAGES = [
  { code: "es", label: "Español" },
  { code: "en", label: "Inglés" },
  { code: "pt", label: "Portugués" },
  { code: "fr", label: "Francés" },
  { code: "it", label: "Italiano" },
  { code: "de", label: "Alemán" },
];

export function languageLabel(code) {
  return RETRANSCRIBE_LANGUAGES.find((item) => item.code === code)?.label || code;
}

// Suggest the language the parallel language probe heard, else Spanish.
export function suggestedRetranscribeLanguage(...probes) {
  for (const probe of probes) {
    const code = String(probe?.main_language || "").trim().toLowerCase();
    if (RETRANSCRIBE_LANGUAGES.some((item) => item.code === code)) return code;
  }
  return "es";
}

const FAILURE_MESSAGES = {
  stale_revision: "La versión guardada cambió. Revisá la letra actual y volvé a intentar.",
  transcription_in_progress: "Esta canción ya se está transcribiendo. Esperá a que termine.",
  not_retranscribable: "La letra de esta canción ya no está en revisión; no se puede volver a transcribir.",
  audio_unavailable: "No encontramos el audio original de esta canción.",
  unsupported_language: "Elegí un idioma de la lista.",
  fleet_updating: "Genly se está actualizando, probá en unos minutos.",
  attempt_ended_without_result: "Genly se estaba actualizando y no pudo guardar la letra nueva. La versión anterior sigue en el editor; probá de nuevo en unos minutos.",
  edited_during_retranscription: "La letra se editó mientras Genly escuchaba la canción. Quedó tu edición; si querés, volvé a intentarlo.",
};

export function knownRetranscribeFailure(code) {
  return FAILURE_MESSAGES[code] || null;
}

export function retranscribeFailureMessage(code, fallback) {
  return FAILURE_MESSAGES[code] || fallback
    || "No pudimos volver a transcribir la canción. La versión anterior sigue disponible.";
}

export async function startLanguageRetranscription(request, jobId, language, baseRevision) {
  if (!jobId || !Number.isInteger(baseRevision) || baseRevision < 0) {
    return { ok: false, reason: "save_required" };
  }
  try {
    const response = await request(`/jobs/${jobId}/retranscribe-language`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language, base_revision: baseRevision }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      return { ok: false, reason: body?.code || (response.status === 409 ? "conflict" : "server"),
        message: body?.message || null };
    }
    return { ok: true, ...body };
  } catch {
    return { ok: false, reason: "network" };
  }
}

export async function fetchLanguageRetranscription(request, jobId) {
  try {
    const response = await request(`/jobs/${jobId}/retranscribe-language`);
    if (!response.ok) return { ok: false, reason: "server" };
    const body = await response.json().catch(() => ({}));
    return { ok: true, ...body };
  } catch {
    return { ok: false, reason: "network" };
  }
}

// Terminal states of the archive row.
export function isRetranscriptionDone(status) {
  return status === "applied" || status === "failed" || status === "discarded";
}
