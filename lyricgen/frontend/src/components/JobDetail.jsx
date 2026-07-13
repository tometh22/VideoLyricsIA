import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import { getDownloadUrl, useMediaUrl } from "../mediaUrl";
import { JobDetailTour } from "./OnboardingTour";
import ProResBadge from "./ProResBadge";
import EditRequestPanel from "./EditRequestPanel";
import ContentValidationToggle, { isUniversalAccount } from "./ContentValidationToggle";
import { useAlert } from "./AlertProvider";
import HelpTip from "./HelpCenter/HelpTip";
import EnableProResModal from "./EnableProResModal";
import DriveTransferModal from "./DriveTransferModal";
import VariantCreateModal from "./VariantCreateModal";
import ScenesFilmstrip from "./ScenesFilmstrip";
import SceneEditModal from "./SceneEditModal";
import MediaPreview from "./MediaPreview";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Read the cached user out of localStorage. App.jsx is the source of truth
// (it sets/clears genly_user on login/logout) so we don't keep a separate
// copy of the parsing logic here — duplicated 6 lines, but reaching for a
// shared hook just for this would force prop-drilling through JobDetailRoute.
function readCurrentUser() {
  try {
    return JSON.parse(localStorage.getItem("genly_user") || "null");
  } catch {
    return null;
  }
}

const MEDIA_TABS = [
  { key: "video", label: "Lyric Video", desc: "1920x1080" },
  { key: "short", label: "Short", desc: "1080x1920" },
  { key: "thumbnail", label: "Thumbnail", desc: "1280x720" },
];

/**
 * @deprecated PR feat/edit-wizard-mode 2026-05-27. Metadata editing
 * moved into the edit-wizard at /videos/:id/edit-lyrics (top banner with
 * artist + song_title inputs). The pencil affordance was removed because
 * operators kept asking "where do I correct the title?" — fragmenting
 * editing across N icons defeated the wizard's purpose. The function
 * stays here unreferenced so a future rollback can re-mount it without
 * a git revert. handleEditTriggered still serves EditRequestPanel's
 * background-regen flow.
 */
// eslint-disable-next-line no-unused-vars
function EditableMetadataField({
  field,                // "artist" | "songTitle"
  value,                // current text shown when not editing
  jobId,
  enabled,              // false → render as plain text without pencil
  className = "",       // styling for the display text (inherits header)
  ariaLabel,            // for screen readers + tests
  maxLength,            // 255 for artist, 500 for songTitle
  onEditTriggered,      // callback after successful POST (flips UI to editing)
  t,
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  // Sync draft if the prop changes while we're NOT editing (e.g. polling
  // returns the new value after the worker finished).
  useEffect(() => {
    if (!editing) setDraft(value ?? "");
  }, [value, editing]);

  // Auto-focus when entering edit mode.
  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  if (!enabled) {
    return <span className={className}>{value}</span>;
  }

  const cancel = () => {
    setDraft(value ?? "");
    setError(null);
    setEditing(false);
  };

  const submit = async (allowYoutubeDrift = false) => {
    const trimmed = (draft || "").trim();
    if (!trimmed) {
      setError(t("metadata.empty_error") || "No puede estar vacío");
      return;
    }
    if (trimmed === (value || "").trim()) {
      // No change — just exit edit mode without round-tripping.
      cancel();
      return;
    }
    if (trimmed.length > maxLength) {
      setError(
        (t("metadata.too_long") || "Máximo {n} caracteres").replace("{n}", maxLength)
      );
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const backendKey = field === "artist" ? "artist" : "song_title";
      const body = {
        edit_type: "metadata",
        [backendKey]: trimmed,
      };
      if (allowYoutubeDrift) body.allow_youtube_drift = true;
      const res = await fetch(`${API}/edit/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      if (res.status === 409) {
        const detail = (await res.json()).detail || {};
        if (detail.code === "youtube_already_published") {
          // Same UX as the lyrics flow's YouTube drift confirm.
          if (window.confirm(
            t("edit.youtube_drift_confirm") ||
            "Este video ya fue subido a YouTube. El cambio se guardará en la plataforma pero NO va a reemplazar el archivo en YouTube. ¿Continuar?"
          )) {
            await submit(true);
          }
          setSaving(false);
          return;
        }
      }
      if (!res.ok) {
        let msg = `Error ${res.status}`;
        try {
          const j = await res.json();
          if (j.detail) msg = typeof j.detail === "string" ? j.detail : msg;
        } catch { /* leave msg */ }
        setError(msg);
        setSaving(false);
        return;
      }
      const resp = await res.json();
      setSaving(false);
      setEditing(false);
      if (onEditTriggered) onEditTriggered(resp);
    } catch (err) {
      setError(t("metadata.network_error") || "Error de red");
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <span className={`inline-flex items-center gap-1.5 group ${className}`}>
        <span>{value}</span>
        <button
          type="button"
          aria-label={ariaLabel || t("metadata.edit") || "Editar"}
          onClick={() => setEditing(true)}
          className="opacity-0 group-hover:opacity-100 transition-opacity text-ink-secondary hover:text-white"
          title={t("metadata.edit") || "Editar"}
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7M18.5 2.5a2.121 2.121 0 113 3L12 15l-4 1 1-4 9.5-9.5z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-2 flex-wrap">
      <input
        ref={inputRef}
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); submit(false); }
          if (e.key === "Escape") { e.preventDefault(); cancel(); }
        }}
        maxLength={maxLength}
        disabled={saving}
        aria-label={ariaLabel || t("metadata.edit") || "Editar"}
        className="px-2 py-1 rounded-md bg-surface-3 ring-1 ring-white/[0.10] focus:ring-brand text-white text-sm font-normal min-w-[200px]"
      />
      <button
        type="button"
        onClick={() => submit(false)}
        disabled={saving}
        className="px-2.5 py-1 rounded-md bg-brand hover:bg-brand-light text-white text-xs font-medium disabled:opacity-50"
      >
        {saving ? (t("metadata.saving") || "Guardando…") : (t("metadata.save") || "Guardar")}
      </button>
      <button
        type="button"
        onClick={cancel}
        disabled={saving}
        className="px-2.5 py-1 rounded-md bg-surface-3/40 hover:bg-surface-3/60 text-ink-secondary hover:text-white text-xs font-medium"
      >
        {t("metadata.cancel") || "Cancelar"}
      </button>
      {error && (
        <span className="text-xs text-red-400 ml-1" role="alert">{error}</span>
      )}
    </span>
  );
}

// Broadcast master tab — added conditionally only when the job's
// delivery_profile is "umg" or "both". ProRes 422 HQ in a .mov, not
// previewable in browser, so the tab shows a download-only panel.
// (Internal `umg_master` key is preserved end-to-end on the wire so
// existing jobs keep working; only the visible label is generic.)
const PRORES_MASTER_TAB = {
  key: "umg_master",
  label: "Máster ProRes",
  desc: "ProRes 422 HQ · MOV",
};

function ProvenanceTab({ jobId, t }) {
  const [records, setRecords] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);

  useEffect(() => {
    fetch(`${API}/provenance/${jobId}`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { setRecords(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, [jobId]);

  const STEP_ICONS = {
    lyrics_analysis: { icon: "M9 19V6l12-2v13", color: "text-purple-400" },
    video_bg: { icon: "M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z", color: "text-blue-400" },
    image_bg: { icon: "M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z", color: "text-green-400" },
    yt_metadata: { icon: "M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z", color: "text-red-400" },
    output_validation: { icon: "M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z", color: "text-amber-400" },
    background_human: { icon: "M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z", color: "text-cyan-400" },
  };

  const STEP_LABELS = {
    lyrics_analysis: t("prov.lyrics_analysis") || "Lyrics Analysis",
    video_bg: t("prov.video_bg") || "Video Background",
    image_bg: t("prov.image_bg") || "Image Background",
    yt_metadata: t("prov.yt_metadata") || "YouTube Metadata",
    output_validation: t("prov.output_validation") || "Content Validation",
    background_human: t("prov.background_human") || "Human Background",
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!records || records.length === 0) {
    return (
      <div className="text-center py-12">
        <p className="text-gray-500 text-sm">{t("prov.no_records") || "No AI provenance records found"}</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs text-gray-500 uppercase tracking-wider">{t("prov.title") || "AI Provenance"}</p>
        <button
          onClick={async () => {
            const res = await fetch(`${API}/provenance/${jobId}/export`, { headers: authHeaders() });
            if (!res.ok) return;
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${jobId}-provenance.json`;
            a.click();
            URL.revokeObjectURL(url);
          }}
          className="text-xs text-brand hover:text-brand-light transition-colors flex items-center gap-1"
        >
          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          {t("prov.export") || "Export"}
        </button>
      </div>

      {records.map((r) => {
        const stepInfo = STEP_ICONS[r.step] || { icon: "M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z", color: "text-gray-400" };
        const isExpanded = expandedId === r.id;

        return (
          <div key={r.id} className="glass rounded-xl overflow-hidden">
            <button
              onClick={() => setExpandedId(isExpanded ? null : r.id)}
              className="w-full flex items-center gap-3 px-4 py-3 hover:bg-white/[0.02] transition-colors"
            >
              <div className={`w-8 h-8 rounded-lg bg-surface-1 flex items-center justify-center shrink-0`}>
                <svg className={`w-4 h-4 ${stepInfo.color}`} fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                  <path d={stepInfo.icon} />
                </svg>
              </div>
              <div className="flex-1 min-w-0 text-left">
                <p className="text-sm font-medium text-white">{STEP_LABELS[r.step] || r.step}</p>
                <p className="text-[10px] text-gray-500">{r.tool_name}</p>
              </div>
              <div className="text-right shrink-0">
                <p className="text-[10px] text-gray-500">{r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : ""}</p>
                <p className="text-[10px] text-gray-600">{r.created_at ? new Date(r.created_at).toLocaleTimeString() : ""}</p>
              </div>
              <svg className={`w-4 h-4 text-gray-500 transition-transform ${isExpanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {isExpanded && (
              <div className="px-4 pb-3 pt-0 space-y-2 border-t border-white/[0.04]">
                {r.input_data_types && (
                  <div>
                    <p className="text-[10px] text-gray-600 uppercase mb-1">{t("prov.data_sent") || "Data Sent"}</p>
                    <div className="flex flex-wrap gap-1">
                      {r.input_data_types.map((dt, i) => (
                        <span key={i} className="px-2 py-0.5 rounded bg-surface-1 text-[10px] text-gray-400">{dt}</span>
                      ))}
                    </div>
                  </div>
                )}
                <div>
                  <p className="text-[10px] text-gray-600 uppercase mb-1">Prompt</p>
                  <pre className="text-[11px] text-gray-400 bg-surface-1 rounded-lg px-3 py-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words">
                    {r.prompt_sent}
                  </pre>
                </div>
                {r.response_summary && (
                  <div>
                    <p className="text-[10px] text-gray-600 uppercase mb-1">{t("prov.response") || "Response"}</p>
                    <p className="text-[11px] text-gray-400 bg-surface-1 rounded-lg px-3 py-2">{r.response_summary}</p>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function JobDetail({ job, onBack, onJobUpdate }) {
  const { t } = useI18n();
  const { alert } = useAlert();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState("video");
  const [uploading, setUploading] = useState(false);
  const [youtubeResult, setYoutubeResult] = useState(job.youtube || null);
  const [metadataPreview, setMetadataPreview] = useState(null);
  const [editingMeta, setEditingMeta] = useState(false);
  const [editedTitle, setEditedTitle] = useState("");
  const [editedDescription, setEditedDescription] = useState("");
  const [editedTags, setEditedTags] = useState([]);
  const [showYoutubePanel, setShowYoutubePanel] = useState(false);
  const [confirmPublicYoutube, setConfirmPublicYoutube] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(-1);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const [youtubeShortResult, setYoutubeShortResult] = useState(job.youtube_short || null);
  const [shortMetadataPreview, setShortMetadataPreview] = useState(null);
  const [editingShortMeta, setEditingShortMeta] = useState(false);
  const [editedShortTitle, setEditedShortTitle] = useState("");
  const [editedShortDescription, setEditedShortDescription] = useState("");
  const [showYoutubeShortPanel, setShowYoutubeShortPanel] = useState(false);
  const [confirmPublicYoutubeShort, setConfirmPublicYoutubeShort] = useState(false);
  const [uploadShortProgress, setUploadShortProgress] = useState(-1);
  const [copiedShortUrl, setCopiedShortUrl] = useState(false);
  const [uploadingShort, setUploadingShort] = useState(false);
  const [reviewNotes, setReviewNotes] = useState("");
  const [approving, setApproving] = useState(false);
  const [retrying, setRetrying] = useState(false);

  // "Enviar a UMG" button state. Gated by role=admin AND status=done — we
  // resolve the role at render time from localStorage so we don't need to
  // pass `user` through JobDetailRoute. isInUmgPortal mirrors the value
  // /status returns (server-side source of truth) but is optimistically
  // flipped to true the moment the POST succeeds, so the user sees the
  // "Ya en UMG" state immediately without a poll round-trip.
  const currentUser = readCurrentUser();
  const isUmgAdmin = currentUser?.role === "admin";
  const [sendingUmg, setSendingUmg] = useState(false);
  const [isInUmgPortal, setIsInUmgPortal] = useState(
    Boolean(job.is_in_umg_portal),
  );
  // Keep local mirror in sync if the parent re-fetches /status. Without this
  // the button would stay "✓ En UMG" even after the entry is deleted from
  // the portal (the server would have flipped the flag back, but our local
  // state wouldn't know).
  useEffect(() => {
    setIsInUmgPortal(Boolean(job.is_in_umg_portal));
  }, [job.is_in_umg_portal]);

  const handleSendToUMG = async () => {
    if (sendingUmg) return;
    setSendingUmg(true);
    try {
      const resp = await fetch(`${API}/admin/deliveries/from-job/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        // 409 = files not yet in R2 (renders still cooking). 400 = not
        // approved yet. 403 = caller isn't admin (shouldn't happen here
        // since the button is hidden, but defensive). Surface the message
        // straight from the backend so the operator knows what to fix.
        alert({
          title: "No se pudo enviar a UMG",
          description: body.detail || "Probá de nuevo en un momento.",
          tone: "error",
        });
        return;
      }
      const result = await resp.json();
      setIsInUmgPortal(true);
      const label = result.label || "";
      const verbed = result.replaced ? "actualizado" : "publicado";
      alert({
        title: `Video ${verbed} en umg.genly.pro`,
        description: label ? `Aparece como "${label}".` : undefined,
        tone: "success",
      });
    } catch (err) {
      console.error("Send to UMG failed:", err);
      alert({
        title: "No se pudo enviar a UMG",
        description: "Hubo un problema de red. Revisá tu conexión y probá de nuevo.",
        tone: "error",
      });
    } finally {
      setSendingUmg(false);
    }
  };
  // Dropdown for HD/2K/4K selection on retry. Only shown when the job
  // has a meaningful umg_spec to override (i.e. went through the UMG
  // pipeline). YouTube-only jobs hide this — they don't have a frame
  // size concept the user can pick.
  const [retryFrameSize, setRetryFrameSize] = useState(
    job.umg_spec?.frame_size || null
  );
  // Tenant-aware content-validation choice for retry. Boolean: true =
  // run validator on retry, false = skip. Default per tenant. Mapped to
  // bypass/force in the POST /retry body. See ContentValidationToggle.jsx
  // for full rationale.
  const _retryTenantId = currentUser?.tenant_id || null;
  const _retryBillingGroup = currentUser?.billing_group || null;
  const _retryIsUmg = isUniversalAccount(_retryTenantId, _retryBillingGroup);
  const [retryValidationEnabled, setRetryValidationEnabled] = useState(true);
  const showRetrySpecSelector =
    (job.delivery_profile === "umg" || job.delivery_profile === "both") &&
    job.umg_spec != null;

  // handleRetry MUST estar definida antes del early-return que la usa
  // (línea ~311 para jobs con status=error). Si se la pone más abajo
  // junto a los otros handlers, el JSX del early-return accede a la
  // const en su temporal dead zone → ReferenceError "Cannot access
  // 'handleRetry' before initialization" → GlobalErrorBoundary catch
  // → app entera crashea. Lo aprendimos cuando un job en error rompió
  // toda la dashboard de un cliente.
  const handleRetry = async (overrideFrameSize) => {
    if (retrying) return;
    setRetrying(true);
    try {
      // Guard: callers like `onClick={handleRetry}` pass a React
      // SyntheticEvent as the first arg. Treating that as a frame_size
      // poisons bodyPayload, JSON.stringify hits circular refs, the
      // fetch never fires, and the catch surfaces "Error de red al
      // reintentar" with NO request visible in DevTools Network (the
      // bug that hid this for weeks). Only accept strings.
      const fs = (typeof overrideFrameSize === "string" ? overrideFrameSize : null)
        ?? retryFrameSize;
      // If the caller (or the dropdown) gave us a frame_size that
      // differs from what's currently on the job, pass it in the body.
      // Otherwise call /retry plain — backend keeps the existing spec.
      // Tenant-aware validation flag: translate the toggle's boolean to
      // bypass (UMG departing default) or force (non-UMG departing default).
      const wantFrameOverride = fs && fs !== job.umg_spec?.frame_size;
      const bodyPayload = {};
      if (wantFrameOverride) bodyPayload.frame_size = fs;
      // Always send one of the two flags based on operator intent.
      // The tenant-conditional version silently dropped BOTH flags when
      // frontend tenant detection failed (stale localStorage, old login)
      // and the backend defaulted to UMG-validate. See VariantCreateModal
      // comment for the full incident write-up (2026-05-19).
      if (!retryValidationEnabled) {
        bodyPayload.bypass_content_validation = true;
      } else {
        bodyPayload.force_content_validation = true;
      }
      const fetchOpts = {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
      };
      if (Object.keys(bodyPayload).length > 0) {
        fetchOpts.body = JSON.stringify(bodyPayload);
      }
      const res = await fetch(`${API}/retry/${job.job_id}`, fetchOpts);
      if (res.ok) {
        const statusRes = await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() });
        if (!statusRes.ok) throw new Error(`Error ${statusRes.status}`);
        const updated = await statusRes.json();
        onJobUpdate?.(updated);
        // Navigate back so the user sees the batch/history with the job now processing.
        onBack?.();
      } else {
        const body = await res.json().catch(() => ({}));
        alert({
          title: "No se pudo reintentar el video",
          description: body.detail || "Probá de nuevo en un momento.",
          tone: "error",
        });
      }
    } catch {
      alert({
        title: "No se pudo reintentar el video",
        description: "Hubo un problema de red. Revisá tu conexión y probá de nuevo.",
        tone: "error",
      });
    }
    setRetrying(false);
  };

  // Synchronous guard against double-click — `approving` (state) is updated
  // asynchronously by React, so a rapid second click can fire its handler
  // before the re-render flips the disabled flag. The ref is set BEFORE
  // any await, so the second handler sees `current=true` immediately and
  // bails out.
  const approveLockRef = useRef(false);
  const name = (job.filename || "").replace(/\.mp3$/i, "");

  // Short-lived media URLs (re-fetch when the active tab changes).
  const previewMediaType = activeTab === "thumbnail" ? "thumbnail" : activeTab;
  // Render-version key so the player reloads when an edit finishes. Edits
  // overwrite the SAME R2 key, so without this the <video src> never changes
  // and a mounted player keeps showing the pre-edit render forever (UMG
  // "no me lo está actualizando", job eaff5c7baf50 — 4 edits OK server-side,
  // operator kept seeing v0 and burned her 3 edits re-requesting them).
  // Why edit_count AND status: edit_count bumps when the edit is REQUESTED
  // (pending_review→editing), not when it completes — status flipping back
  // to pending_review is the completion signal that must swap the URL.
  const mediaVersion = `${job.edit_count || 0}-${job.status || ""}`;
  const previewSrc = useMediaUrl(job.job_id, previewMediaType, "preview", mediaVersion);
  const downloadHref = useMediaUrl(job.job_id, previewMediaType, "download", mediaVersion);

  // Auto-retry the <video> load. A just-finished job flips to
  // pending_review the instant the DB row updates, but the MP4 can lag a
  // beat landing in R2. The <video> loads once, fails, and (without this)
  // shows a crossed-out play button until the operator manually switches
  // tabs and back — which remounts the element and reloads. We reproduce
  // that remount automatically on error, with backoff to let R2 settle.
  const [videoReloadKey, setVideoReloadKey] = useState(0);
  const videoRetriesRef = useRef(0);
  // Fresh retry budget whenever the source or tab changes (new media).
  useEffect(() => { videoRetriesRef.current = 0; }, [previewSrc, activeTab]);
  const handleVideoError = useCallback(() => {
    if (videoRetriesRef.current < 4) {
      videoRetriesRef.current += 1;
      const delay = 1500 * videoRetriesRef.current; // 1.5s, 3s, 4.5s, 6s
      setTimeout(() => setVideoReloadKey((k) => k + 1), delay);
    }
  }, []);

  const canPreview = job.status === "done" || job.status === "pending_review";
  const canDownload = job.status === "done";
  const isPendingReview = job.status === "pending_review";
  const isDone = job.status === "done";
  const isRejected = job.status === "rejected";
  const isEditing = job.status === "editing";
  const isValidationFailed = job.status === "validation_failed";
  const isError = job.status === "error";
  // An edit that failed AFTER a prior successful render: the deliverable in R2 is
  // untouched (the pipeline only flips status=error, it never clears s3_keys), so
  // the previous video is still intact. Surfaced to reassure operators who see a
  // cryptic "Edit falló" and assume they lost the video / must re-upload.
  const hasPriorDeliverable = !!(
    (job.s3_keys && job.s3_keys.video) || job.video_url || job.thumbnail_url
  );
  const isEditFailureWithPriorVideo =
    isError && (job.edit_count || 0) > 0 && hasPriorDeliverable;
  // Lyrics edit is allowed on done/pending_review/rejected per backend
  // validation (main.py:/edit). The panel renders for ANY of those
  // statuses with allowedModes scoped per state:
  //   - pending_review → all three (operator is reviewing, full toolkit)
  //   - done           → lyrics only (video already accepted, only typo
  //                                    corrections warrant re-render)
  //   - rejected       → lyrics only (recovery path instead of re-upload)
  const canEditLyrics = isPendingReview || isDone || isRejected;
  // Jobs multi-escena: el modo "Fondo" queda afuera del panel — ese edit
  // pertenece al mundo fondo-único (regenera UN clip y pisaba el timeline
  // de escenas; incidente 2026-07-01). Para escenas, la regeneración va
  // por el filmstrip (por escena, sin consumir cupo). El backend además
  // lo rechaza con 400 por si llega igual.
  const _hasScenes = !!(job.scene_plan && job.scene_plan.scenes && job.scene_plan.scenes.length);
  const editPanelAllowedModes = isPendingReview
    ? (_hasScenes ? ["lyrics"] : ["lyrics", "background"])
    : ["lyrics"];

  // While the worker is re-rendering an edit request, poll /status every
  // 5s and propagate updates up so the rest of the screen (status badge,
  // approve panel visibility, preview URLs) stays in sync. The interval
  // cleans itself up the moment status leaves "editing".
  useEffect(() => {
    if (!isEditing) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() });
        if (!res.ok || cancelled) return;
        const updated = await res.json();
        if (cancelled) return;
        // Merge into existing job so we don't drop fields /status doesn't return
        // (youtube_data, etc.). onJobUpdate flows it back through App state.
        if (onJobUpdate) onJobUpdate({ ...job, ...updated });
      } catch {}
    };
    const iv = setInterval(tick, 5000);
    tick(); // first tick immediately, no need to wait 5s
    return () => { cancelled = true; clearInterval(iv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isEditing, job.job_id]);

  // Hooks moved before early returns (React rules of hooks: no hooks after
  // conditional returns). These were previously declared after the
  // isEditing / isError / canPreview guards and caused React error #300.
  const [localProresReady, setLocalProresReady] = useState(
    Boolean(
      (job.s3_keys && job.s3_keys.umg_master && job.s3_keys.umg_short)
      || job.prores_ready
    )
  );
  const [proResHint, setProResHint] = useState(null);
  const [showProResModal, setShowProResModal] = useState(false);
  const [proResToast, setProResToast] = useState(null);
  const [driveConnected, setDriveConnected] = useState(false);
  const [showDriveModal, setShowDriveModal] = useState(false);
  const [showVariantModal, setShowVariantModal] = useState(false);
  const user = (() => {
    try { return JSON.parse(localStorage.getItem("genly_user") || "null"); } catch { return null; }
  })();
  const driveFeatureEnabled = user?.features?.drive_export === true;
  useEffect(() => {
    if (!isDone) return;
    if (!driveFeatureEnabled) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API}/drive/status`, { headers: authHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setDriveConnected(!!data.connected);
      } catch {
        // Silent fail — si /drive/status no responde, no mostramos el
        // botón, lo cual es la conducta segura. El user puede ir a
        // Settings a conectar.
      }
    })();
    return () => { cancelled = true; };
  }, [isDone, job.job_id, driveFeatureEnabled]);

  const handleEditTriggered = (resp) => {
    // Server already flipped status to "editing" + bumped edit_count.
    // Reflect that immediately in the UI so the approve panel hides and
    // the editing overlay appears, then let polling take over.
    if (onJobUpdate) {
      onJobUpdate({
        ...job,
        status: "editing",
        edit_count: resp?.edit_count ?? (job.edit_count || 0) + 1,
        edits_remaining: resp?.edits_remaining ?? Math.max(0, (job.edits_remaining ?? 3) - 1),
        edit_limit_exempt: resp?.edit_limit_exempt ?? job.edit_limit_exempt ?? false,
        current_step: resp?.edit_type === "background" ? "background" : "video",
        progress: 0,
      });
    }
  };

  // ── Multi-escena: filmstrip + regenerar escena ──────────────────────────
  const videoRef = useRef(null);
  const scenePlan = job.scene_plan && job.scene_plan.scenes ? job.scene_plan : null;
  const [sceneThumbs, setSceneThumbs] = useState({});
  const [sceneBusyKey, setSceneBusyKey] = useState(null);
  const [editingScene, setEditingScene] = useState(null);
  // Alineado con canPreview (done/pending_review): la tira de escenas solo se
  // renderiza dentro del bloque de preview, y un job "rejected" muestra la
  // pantalla "no disponible" (sin reproductor), así que incluir isRejected acá
  // prometía una edición inalcanzable. Se saca para que ambas capas coincidan.
  const scenesEditable = (isPendingReview || isDone) && !isEditing;

  // Pósters firmados (1 llamada). Recarga cuando cambia el plan (cache_token
  // distinto tras regenerar) para traer el thumb nuevo.
  const _sceneSig = scenePlan
    ? scenePlan.scenes.map((s) => `${s.recurrence_key}:${s.cache_token || ""}`).join(",")
    : "";
  useEffect(() => {
    if (!scenePlan) { setSceneThumbs({}); return; }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API}/jobs/${job.job_id}/scenes/thumbs`, { headers: authHeaders() });
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setSceneThumbs(data.thumbs || {});
      } catch { /* sin thumbs → placeholder */ }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.job_id, _sceneSig]);

  // El spinner de la escena se apaga cuando el job deja de re-renderizar.
  useEffect(() => { if (!isEditing) setSceneBusyKey(null); }, [isEditing]);
  // Audit M7: watchdog — si el job queda trabado en "editing" (worker muerto,
  // poll que no avanza), liberá el spinner de la escena igual a los 10 min para
  // no dejar la card girando para siempre.
  useEffect(() => {
    if (!sceneBusyKey) return undefined;
    const id = setTimeout(() => setSceneBusyKey(null), 600000);
    return () => clearTimeout(id);
  }, [sceneBusyKey]);

  const regenerateScene = useCallback(async (scene, opts = {}, allowYoutubeDrift = false) => {
    if (!scene) return;
    const key = scene.recurrence_key;
    const apps = (scenePlan?.sections || []).filter((s) => s.recurrence_key === key).length;
    const isOtraToma = !opts.prompt && !opts.hint && !opts.movement_style;
    if (isOtraToma && apps > 1) {
      const ok = window.confirm(
        (t("scenes.recurrence_confirm") ||
          "Esta escena aparece {n} veces (es recurrente). Regenerarla cambia todas sus apariciones. ¿Continuar?").replace("{n}", apps)
      );
      if (!ok) return;
    }
    setSceneBusyKey(key);
    try {
      const body = {};
      if (opts.prompt) body.prompt = opts.prompt;
      if (opts.hint) body.hint = opts.hint;
      if (opts.movement_style) body.movement_style = opts.movement_style;
      if (allowYoutubeDrift) body.allow_youtube_drift = true;
      const res = await fetch(`${API}/jobs/${job.job_id}/scenes/${encodeURIComponent(key)}/regenerate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      if (res.status === 409) {
        const detail = (await res.json()).detail || {};
        if (detail.code === "youtube_already_published") {
          setSceneBusyKey(null);
          if (window.confirm(t("edit.youtube_drift_confirm") ||
            "Este video ya fue subido a YouTube. El cambio se guardará en la plataforma pero NO va a reemplazar el archivo en YouTube. ¿Continuar?")) {
            return regenerateScene(scene, opts, true);
          }
          return;
        }
      }
      if (!res.ok) {
        let msg = `Error ${res.status}`;
        try { const j = await res.json(); if (j.detail && typeof j.detail === "string") msg = j.detail; } catch { /* keep */ }
        setSceneBusyKey(null);
        window.alert(msg);
        return;
      }
      const resp = await res.json();
      setEditingScene(null);
      handleEditTriggered({ ...resp, edit_type: "scene" });
    } catch {
      setSceneBusyKey(null);
      window.alert(t("scenes.regen_error") || "No se pudo regenerar la escena.");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenePlan, job.job_id, t]);

  const seekVideo = useCallback((seconds) => {
    const v = videoRef.current;
    if (v && Number.isFinite(seconds)) {
      try { v.currentTime = seconds; v.play?.(); } catch { /* ignore */ }
    }
  }, []);

  // Editing in progress: render a focused panel instead of falling through
  // to the "not available" early-return below. canPreview is false during
  // editing (the video bytes are being rewritten on R2) but we DO want to
  // show progress + clear messaging — not the generic dead-end message.
  if (isEditing) {
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card p-5 bg-brand/[0.08] ring-1 ring-brand/25">
          <div className="flex items-start gap-3">
            <div className="w-9 h-9 rounded-lg bg-brand/15 ring-1 ring-brand/30 flex items-center justify-center shrink-0">
              <span className="w-4 h-4 border-2 border-brand-light border-t-transparent rounded-full animate-spin" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-white">
                {t("edit.in_progress_title") || "Aplicando tus cambios..."}
              </p>
              <p className="text-xs text-ink-secondary mt-0.5">
                {job.current_step === "background"
                  ? (t("edit.in_progress_bg") || "Generando nuevo video cinemático · mantiene lyrics y tiempos · ~10-15 min")
                  : (t("edit.in_progress_typo") || "Re-renderizando con la tipografía nueva · usa el fondo cacheado · ~5-10 min")}
              </p>
              <div className="mt-3 h-1.5 rounded-full bg-surface-3/60 overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-700 ease-out"
                  style={{ width: `${Math.min(100, Math.max(3, job.progress || 0))}%` }}
                />
              </div>
              <p className="text-[10px] text-gray-500 mt-1 font-mono">
                {job.current_step || "?"} · {job.progress || 0}%
              </p>
              <p className="text-[11px] text-gray-500 mt-3 leading-relaxed">
                {t("edit.no_video_during_editing") || "El video viejo se está reemplazando con tus cambios. Cuando termine vas a poder verlo acá."}
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // INCIDENT (2026-05-24): the original "not available" fallback was a
  // dead-end for every status that wasn't done/pending_review/editing —
  // including the 19 jobs currently in `transcribed` / `transcribed_pending`
  // / `transcribing*` / `awaiting_upload`. The operator couldn't act on
  // them at all from the history view. Now each state renders a focused
  // panel with the actionable next step instead of "Volver".
  const isTranscribed = job.status === "transcribed";
  const isTranscribedPending = job.status === "transcribed_pending";
  const isTranscribing = job.status === "transcribing" || job.status === "transcribing_queued";
  const isAwaitingUpload = job.status === "awaiting_upload";
  const isTranscriptionFailed = job.status === "transcription_failed";

  // `transcribed` y `transcribed_pending` = audio + segments están en la DB
  // y el user nunca hizo /generate. La acción correcta es abrir el wizard
  // pre-cargado (/new?resume=...) para que edite lyrics y dispare la
  // generación. Vía `/transcribe-uploaded` queda `transcribed_pending` (path
  // sync legacy) o el async worker setea `transcribed_pending` como estado
  // FINAL — ver transcription_worker.py:147 y jobs.py:49-51. NO es "subida
  // en curso"; el verdadero "subida en curso" es `awaiting_upload`, que
  // está manejado en el bloque siguiente.
  //
  // Bug pre-fix (operator report 2026-05-26): `transcribed_pending` caía en
  // este branch pero mostraba el subtitle "La subida está en curso" y NO
  // renderizaba el CTA. El usuario veía 27 jobs "Sin generar" en el
  // historial y al entrar no había forma de seguir — panel sin acción.
  if (isTranscribed || isTranscribedPending) {
    const navHref = `/new?resume=${encodeURIComponent(job.job_id)}`;
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card p-5 bg-surface-2/40 ring-1 ring-white/[0.06]">
          <p className="text-sm font-semibold text-white">
            {t("detail.transcribed_title") || "Este video todavía no se generó"}
          </p>
          <p className="text-xs text-ink-secondary mt-1.5 leading-relaxed">
            {t("detail.transcribed_subtitle") || "La transcripción está lista pero nunca se disparó la generación. Continuá el wizard para revisar lyrics, elegir estilo y generar el video."}
          </p>
          <a
            href={navHref}
            className="inline-flex items-center gap-2 mt-4 px-4 py-2 rounded-lg bg-brand hover:bg-brand-light text-white font-medium text-sm transition-colors"
          >
            {t("detail.transcribed_cta") || "Continuar wizard y generar video"}
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
          </a>
        </div>
      </div>
    );
  }

  // `transcribing*` / `awaiting_upload` = active work, no user action
  // possible yet. Show a focused progress panel; the dashboard poller
  // will refresh `job` and the user will see the badge flip when ready.
  if (isTranscribing || isAwaitingUpload) {
    const label = isAwaitingUpload
      ? (t("detail.uploading_title") || "Subiendo audio…")
      : (t("detail.transcribing_title") || "Transcribiendo…");
    const subtitle = isAwaitingUpload
      ? (t("detail.uploading_subtitle") || "El archivo está subiendo a nuestro storage. Si se queda mucho tiempo acá puede ser una pérdida de conexión: refrescá y reintentá.")
      : (t("detail.transcribing_subtitle") || "Whisper + alineación de letras a la canción. Suele tardar 2-5 minutos. Volvé en un rato.");
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card p-5 bg-brand/[0.08] ring-1 ring-brand/25">
          <div className="flex items-start gap-3">
            <div className="w-9 h-9 rounded-lg bg-brand/15 ring-1 ring-brand/30 flex items-center justify-center shrink-0">
              <span className="w-4 h-4 border-2 border-brand-light border-t-transparent rounded-full animate-spin" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-white">{label}</p>
              <p className="text-xs text-ink-secondary mt-0.5">{subtitle}</p>
              {typeof job.progress === "number" && job.progress > 0 && (
                <div className="mt-3 h-1.5 rounded-full bg-surface-3/60 overflow-hidden">
                  <div
                    className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-700 ease-out"
                    style={{ width: `${Math.min(100, Math.max(3, job.progress))}%` }}
                  />
                </div>
              )}
              {job.current_step && (
                <p className="text-[10px] text-gray-500 mt-1 font-mono">{job.current_step} · {job.progress || 0}%</p>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // `transcription_failed` = the transcription pipeline crashed before
  // segments could be persisted. Surface the error + offer retry via the
  // wizard (which re-encodes + re-uploads from the user's local file
  // if needed).
  if (isTranscriptionFailed) {
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card bg-red-500/[0.06] ring-1 ring-red-500/20 px-5 py-5">
          <p className="text-sm font-semibold text-red-300 mb-1">
            {t("detail.transcription_failed_title") || "La transcripción falló"}
          </p>
          <p className="text-xs text-red-400/70 mb-4">
            {job.error || t("detail.transcription_failed_unknown") || "Error desconocido durante la transcripción."}
          </p>
          <p className="text-xs text-ink-secondary mb-4 leading-relaxed">
            {t("detail.transcription_failed_help") || "Tu archivo sigue guardado. Apretá «Reintentar» para volver a transcribirlo sin re-subir."}
          </p>
          <div className="flex flex-wrap items-center gap-3">
            {/* Reintentar sin re-subir: el flujo /new?resume=<jobId> reusa el
                audio que sigue en R2 (App.jsx resume → source-audio-url →
                /transcribe-uploaded, que ahora acepta transcription_failed).
                Honra el mensaje del reaper (P0 2026-06-08 follow-up). */}
            <a
              href={`/new?resume=${encodeURIComponent(job.job_id)}`}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-brand hover:bg-brand-light text-white font-medium text-sm transition-colors"
            >
              {t("detail.transcription_failed_retry") || "Reintentar sin re-subir"}
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 4v6h6M20 20v-6h-6M20 9a8 8 0 0 0-14.9-3M4 15a8 8 0 0 0 14.9 3" /></svg>
            </a>
            {/* Fallback: si el audio ya no está en R2 (limpieza/borrado), el
                resume falla con 422 y el operador re-sube desde cero. */}
            <a
              href="/new"
              className="inline-flex items-center gap-2 px-3 py-2 rounded-lg text-ink-secondary hover:text-white text-xs transition-colors"
            >
              {t("detail.transcription_failed_cta") || "Subir de nuevo"}
            </a>
          </div>
        </div>
      </div>
    );
  }

  if (!canPreview && !isValidationFailed && !isError) {
    return (
      <div className="w-full max-w-2xl animate-fade-in text-center py-20">
        <p className="text-gray-400">{t("detail.not_available")}</p>
        <p className="text-[11px] text-gray-600 mt-2">status: {job.status || "(unknown)"}</p>
        <button onClick={onBack} className="btn-secondary mt-4">{t("detail.back")}</button>
      </div>
    );
  }

  // Error state: show a compact error panel with retry option.
  if (isError && !isValidationFailed) {
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card bg-red-500/[0.06] ring-1 ring-red-500/20 px-5 py-5">
          <p className="text-sm font-semibold text-red-300 mb-1">{t("detail.error_title") || "El video falló durante la generación"}</p>
          <p className="text-xs text-red-400/70 mb-4">{job.error || t("detail.error_unknown") || "Error desconocido"}</p>

          {isEditFailureWithPriorVideo && (
            <div className="rounded-lg bg-emerald-500/[0.07] ring-1 ring-emerald-500/20 px-3 py-2.5 mb-4">
              <p className="text-xs text-emerald-300/90">
                {t("detail.edit_failed_prior_safe") ||
                  "Tu video anterior sigue intacto: el error fue solo al aplicar la edición. Podés reintentar sin volver a subir el audio."}
              </p>
            </div>
          )}

          {showRetrySpecSelector && (
            <div className="mb-3">
              <label className="text-[11px] text-gray-400 uppercase tracking-wider block mb-1.5">
                {t("detail.retry_spec_label") || "Resolución al reintentar"}
              </label>
              <select
                value={retryFrameSize || "HD"}
                onChange={(e) => setRetryFrameSize(e.target.value)}
                className="text-xs bg-surface-2/60 ring-1 ring-white/[0.08] rounded-lg px-3 py-2 text-white focus:ring-brand outline-none"
              >
                <option value="HD">HD · 1920×1080 (más rápido)</option>
                <option value="DCI-2K">2K · 2048×1080</option>
                <option value="UHD-4K">4K UHD · 3840×2160 (más lento)</option>
                <option value="DCI-4K">4K DCI · 4096×2160 (más lento)</option>
              </select>
              {retryFrameSize !== (job.umg_spec?.frame_size || "HD") && (
                <p className="text-[10px] text-amber-300/70 mt-1.5">
                  {t("detail.retry_spec_changed") ||
                    "Esta resolución se aplicará en el reintento — sobrescribe la original del job."}
                </p>
              )}
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={() => handleRetry()}
              disabled={retrying}
              className="btn-primary text-xs h-9 px-4 disabled:opacity-50"
            >
              {retrying ? (
                <><div className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin mr-1.5" />Reintentando…</>
              ) : (
                t("detail.retry") || "Reintentar sin re-subir"
              )}
            </button>
            <button onClick={() => onBack && onBack()} className="btn-secondary text-xs h-9 px-4">
              {t("detail.back") || "Volver"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Single navigation to a server-streamed zip. The previous "loop three
  // <a>.click() calls" approach got blocked as popup spam by Chrome —
  // the browser would only honour the last click (thumbnail) and open it
  // in a tab instead of downloading. /download/{id}/all bundles the
  // small deliverables server-side so we get one click → one file. We
  // mint a short-lived media token first so the URL doesn't carry the
  // long-lived JWT (C3 fix).
  const downloadAllZip = async () => {
    try {
      const url = await getDownloadUrl(job.job_id, "all");
      window.location.href = url;
    } catch {}
  };
  // ProRes is generated lazily server-side. Fast path: prewarm has
  // already produced the .mov → 200 with bytes (or 302 to R2). Slow
  // path: backend returns 202 + Retry-After when the transcode is
  // queued or in progress; we keep the toast up and re-fetch until
  // 200/302 lands. The whole point is to NEVER block a uvicorn worker
  // for the 60-300 s of ffmpeg — under multi-tenant load, blocking
  // would tie up workers and hang every other request.
  //
  // Hard ceiling at 8 minutes total wait (16 polls × 30 s). 4K@60 cold
  // transcode + R2 upload is ~3-4 min; 8 min covers a queue depth of
  // 2-3 jobs ahead before we give up and tell the user to retry.
  // localProresReady / proResHint are declared before the early returns above.
  const PRORES_MAX_WAIT_MS = 8 * 60 * 1000;
  const PRORES_POLL_FALLBACK_MS = 30 * 1000;

  const fetchProResAndSave = async (fileType, suggestedName) => {
    setProResHint(fileType);
    const deadline = Date.now() + PRORES_MAX_WAIT_MS;
    try {
      while (Date.now() < deadline) {
        const url = await getDownloadUrl(job.job_id, fileType);
        // `redirect: 'manual'` is critical: /download responds with
        // 302 → R2 signed URL when the file is cached. Default
        // `redirect: 'follow'` would make fetch hit R2 cross-origin
        // and fail CORS (R2 doesn't allow XHR from our origin).
        // With 'manual' we get opaqueredirect → we then navigate the
        // main window to the same-origin /download URL which follows
        // the 302 natively. The R2 signed URL includes
        // ResponseContentDisposition: attachment so the browser
        // downloads without navigating away.
        const res = await fetch(url, { redirect: "manual" });
        if (res.type === "opaqueredirect") {
          // Navigate same-tab: the 302 → R2 URL carries
          // Content-Disposition: attachment so the browser triggers
          // a download, not a page navigation. _blank would open
          // a new tab AND lose the Content-Disposition hint for
          // cross-origin URLs.
          window.location.href = url;
          setLocalProresReady(true);
          return;
        }
        if (res.status === 200) {
          // Bytes arrived — turn into a blob download and exit.
          const blob = await res.blob();
          const blobUrl = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = blobUrl;
          a.download = suggestedName;
          a.click();
          setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
          setLocalProresReady(true);
          return;
        }
        if (res.status === 202) {
          // Backend queued/in-progress. Honour Retry-After header.
          const retryHdr = parseInt(res.headers.get("Retry-After") || "", 10);
          const retryMs = (Number.isFinite(retryHdr) && retryHdr > 0)
            ? retryHdr * 1000
            : PRORES_POLL_FALLBACK_MS;
          await new Promise((r) => setTimeout(r, retryMs));
          continue;
        }
        // Hard error (400/404/500). Surface the backend's own `detail`
        // string when present so the operator sees a real reason instead
        // of a generic HTTP code. 404 specifically means the source MP4
        // is no longer on disk/R2 — irrecoverable, needs a full re-render.
        let backendDetail = "";
        try {
          const body = await res.json();
          backendDetail = (body && body.detail) || "";
        } catch {
          /* non-JSON body — fall through with empty detail */
        }
        const err = new Error(`HTTP ${res.status}`);
        err.status = res.status;
        err.detail = backendDetail;
        throw err;
      }
      const err = new Error("timeout");
      err.kind = "timeout";
      throw err;
    } catch (err) {
      console.error("ProRes download failed:", err);
      let message;
      if (err.kind === "timeout") {
        message = t("detail.prores_timeout");
      } else if (err.status === 404) {
        // Source MP4 missing on the server — only a full re-render fixes it.
        message = t("detail.prores_source_missing");
      } else {
        const reason = err.detail || err.message || "error";
        message = t("detail.prores_failed", { reason });
      }
      alert({
        title: "No se pudo descargar el archivo ProRes",
        description: message,
        tone: "error",
      });
    } finally {
      setProResHint(null);
    }
  };
  const songSlug = (job.filename || "video").replace(/\.[^.]+$/, "");
  const downloadProResMaster = () =>
    fetchProResAndSave("umg_master", `${songSlug}_master.mov`);
  const downloadProResShort = () =>
    fetchProResAndSave("umg_short", `${songSlug}_short.mov`);

  const previewMetadata = async () => {
    setShowYoutubePanel(true);
    try {
      const res = await fetch(`${API}/youtube/metadata/${job.job_id}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(_resolveYtError(data.detail));
      }
      const data = await res.json();
      setMetadataPreview(data);
      setEditedTitle(data.title || "");
      setEditedDescription(data.description || "");
      setEditedTags(data.tags || []);
    } catch (err) {
      setMetadataPreview({ error: err.message });
    }
  };

  const _resolveYtError = (detail) => {
    if (!detail) return "Error desconocido";
    const code = typeof detail === "object" ? detail.code : detail;
    const i18nKey = `detail.yt_error.${code}`;
    const mapped = t(i18nKey);
    if (mapped && mapped !== i18nKey) return mapped;
    return typeof detail === "object" ? (detail.message || JSON.stringify(detail)) : detail;
  };

  const _pollProgress = (progressKey, setter) => {
    const interval = setInterval(async () => {
      try {
        const r = await fetch(`${API}/youtube/upload-progress/${job.job_id}`, { headers: authHeaders() });
        if (!r.ok) { clearInterval(interval); return; }
        const d = await r.json();
        const val = progressKey === "short" ? d.short_progress : d.progress;
        setter(val);
        if (val === 100 || val === -1) clearInterval(interval);
      } catch { clearInterval(interval); }
    }, 2000);
    return interval;
  };

  const uploadToYoutube = async (privacy = "unlisted") => {
    setConfirmPublicYoutube(false);
    setUploading(true);
    setUploadProgress(0);
    const pollId = _pollProgress("video", setUploadProgress);
    try {
      const res = await fetch(`${API}/youtube/upload/${job.job_id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        // Always send the previewed/edited values so what you approve is
        // exactly what gets published (the backend would otherwise
        // regenerate the metadata with a fresh AI call).
        body: JSON.stringify({
          privacy,
          title: editedTitle || undefined,
          description: editedDescription || undefined,
          tags: editedTags,
        }),
      });
      clearInterval(pollId);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(_resolveYtError(data.detail));
      }
      const data = await res.json();
      setYoutubeResult(data);
    } catch (err) {
      clearInterval(pollId);
      setYoutubeResult({ error: err.message });
    }
    setUploadProgress(-1);
    setUploading(false);
  };

  const previewShortMetadata = async () => {
    setShowYoutubeShortPanel(true);
    try {
      const res = await fetch(`${API}/youtube/metadata-short/${job.job_id}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(_resolveYtError(data.detail));
      }
      const data = await res.json();
      setShortMetadataPreview(data);
      setEditedShortTitle(data.title || "");
      setEditedShortDescription(data.description || "");
    } catch (err) {
      setShortMetadataPreview({ error: err.message });
    }
  };

  const uploadShortToYoutube = async (privacy = "unlisted") => {
    setConfirmPublicYoutubeShort(false);
    setUploadingShort(true);
    setUploadShortProgress(0);
    const pollId = _pollProgress("short", setUploadShortProgress);
    try {
      const res = await fetch(`${API}/youtube/upload-short/${job.job_id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          privacy,
          title: editingShortMeta ? editedShortTitle : undefined,
          description: editingShortMeta ? editedShortDescription : undefined,
        }),
      });
      clearInterval(pollId);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(_resolveYtError(data.detail));
      }
      const data = await res.json();
      setYoutubeShortResult(data);
    } catch (err) {
      clearInterval(pollId);
      setYoutubeShortResult({ error: err.message });
    }
    setUploadShortProgress(-1);
    setUploadingShort(false);
  };

  const handleApprove = async () => {
    if (approveLockRef.current) return;
    approveLockRef.current = true;
    setApproving(true);
    try {
      const res = await fetch(`${API}/approve/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ notes: reviewNotes }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `${t("detail.approve_error_description")} (${res.status})`);
      }
      try {
        const statusRes = await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() });
        if (!statusRes.ok) throw new Error(`${t("detail.refresh_error_description")} (${statusRes.status})`);
        const updated = await statusRes.json();
        onJobUpdate?.(updated);
      } catch (refreshError) {
        onJobUpdate?.({ ...job, status: "done" });
        alert({
          title: t("detail.refresh_warning_title"),
          description: refreshError?.message || t("detail.refresh_error_description"),
          tone: "warning",
        });
      }
    } catch (err) {
      alert({
        title: t("detail.approve_error_title"),
        description: err?.message || t("detail.approve_error_description"),
        tone: "error",
      });
    } finally {
      setApproving(false);
      approveLockRef.current = false;
    }
  };

  // handleRetry está definida más arriba (~línea 175) para que esté
  // disponible antes del early-return de status=error. No duplicar acá.

  const handleReject = async () => {
    if (approveLockRef.current) return;
    approveLockRef.current = true;
    setApproving(true);
    try {
      const res = await fetch(`${API}/reject/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ notes: reviewNotes }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `${t("detail.reject_error_description")} (${res.status})`);
      }
      // Refresh the job state for any listing in the parent so the row
      // shows "rejected", then go back. Staying on the detail screen
      // would show "this job is not previewable" because rejected jobs
      // intentionally can't be re-opened — better UX is to land the
      // user back on the dashboard / batch view.
      try {
        const statusRes = await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() });
        if (!statusRes.ok) throw new Error(`${t("detail.refresh_error_description")} (${statusRes.status})`);
        const updated = await statusRes.json();
        onJobUpdate?.(updated);
      } catch (refreshError) {
        onJobUpdate?.({ ...job, status: "rejected" });
        alert({
          title: t("detail.refresh_warning_title"),
          description: refreshError?.message || t("detail.refresh_error_description"),
          tone: "warning",
        });
      }
      onBack?.();
    } catch (err) {
      alert({
        title: t("detail.reject_error_title"),
        description: err?.message || t("detail.reject_error_description"),
        tone: "error",
      });
    } finally {
      setApproving(false);
      approveLockRef.current = false;
    }
  };

  // ProRes button visibility — gated by delivery profile + done status,
  // NOT by the presence of `files.umg_master_url`. The download endpoint
  // (/download/{id}/umg_master) handles the missing-file case by
  // enqueueing a lazy prewarm and returning 202 + Retry-After; the
  // fetchProResAndSave polls until ready (up to 8 min).
  //
  // Why decouple from the URL: jobs created before the prewarm feature
  // existed (or whose prewarm died silently) sit forever with
  // umg_master_url=null and no way for the operator to recover the file.
  // Showing the button always lets clicking it trigger the recovery.
  // Un job es "UMG" si fue creado con delivery_profile=umg/both, O si
  // se le habilitó ProRes retroactivamente via POST /enable-prores
  // (que persiste umg_spec sin tocar delivery_profile, para no perder
  // el dato histórico de cómo se rindió originalmente).
  const isUmgJob =
    job.delivery_profile === "umg"
    || job.delivery_profile === "both"
    || !!job.umg_spec;
  const isJobDone = job.status === "done";
  const hasUmgMaster = isUmgJob && isJobDone;

  // El botón "Exportar a ProRes" aparece solo cuando el job está done,
  // NO tiene ProRes habilitado todavía, y el usuario tiene el feature
  // flag prores_export. Click → modal que persiste umg_spec en el job
  // y dispara el transcoding. Una vez hecho, isUmgJob flipea a true en
  // el próximo /status poll y aparece el tab de ProRes Master.
  // user / driveFeatureEnabled / showProResModal / proResToast /
  // driveConnected / showDriveModal / showVariantModal / Drive useEffect
  // are all declared before the early returns above (hooks rules).
  const canEnableProRes =
    isJobDone && !isUmgJob && user?.features?.prores_export === true;
  // file_type a transferir cuando el user abre el modal: por default el
  // umg_master si está disponible, sino el video MP4.
  const driveFileType = isUmgJob ? "umg_master" : "video";
  // Short ProRes follows the same opt-in: any UMG-flavoured job gets a
  // separate vertical-format master alongside the main one. Generated
  // lazily by /download/{id}/umg_short the first time it's clicked.
  const hasUmgShort = isUmgJob && isJobDone;

  const ALL_TABS = [
    ...MEDIA_TABS,
    ...(hasUmgMaster ? [PRORES_MASTER_TAB] : []),
    { key: "provenance", label: t("prov.title") || "Provenance" },
  ];

  return (
    <div className="job-detail-workspace w-full max-w-[1380px] mx-auto animate-fade-in">
      {/* JobDetail tour: auto-fires on the FIRST pending_review job a
          new operator opens. The tour walks through approval semantics
          + ProRes download. We read `user` from localStorage here so
          we don't have to thread it through the route — the age-gate
          just needs `created_at`. */}
      <JobDetailTour
        user={(() => { try { return JSON.parse(localStorage.getItem("genly_user") || "null"); } catch { return null; } })()}
        hasUmgMaster={hasUmgMaster}
        isPendingReview={isPendingReview}
      />
      {/* Header */}
      <div className="job-detail-command flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 mb-6">
        <div className="flex items-center gap-3 min-w-0">
          <button onClick={onBack}
            className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              {/* PR feat/edit-wizard-mode 2026-05-27: pencil icons removed.
                  Metadata editing now lives in the edit-wizard at
                  /videos/:id/edit-lyrics — one surface for every editable
                  wizard field instead of N fragmented pencils. The
                  fallback to `name` (filename minus .mp3) covers legacy
                  jobs without a persisted song_title. */}
              <h2 className="text-xl font-bold tracking-tight truncate">
                {job.song_title || name}
              </h2>
              {isPendingReview && (
                <span
                  data-tour="jobdetail-status-badge"
                  className="px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30 text-[10px] font-semibold uppercase tracking-wider"
                >
                  {t("batch.pending_review") || "Pendiente"}
                  <HelpTip articleId="approve-reject" />
                </span>
              )}
              {isValidationFailed && (
                <span className="px-2 py-0.5 rounded-full bg-red-500/15 text-red-300 ring-1 ring-red-500/30 text-[10px] font-semibold uppercase tracking-wider">
                  {t("batch.validation_failed") || "Falló validación"}
                </span>
              )}
              <ProResBadge
                deliveryProfile={job.delivery_profile}
                proresReady={localProresReady}
                jobStatus={job.status}
                size="md"
              />
              {job.status === "done" && job.approved_by && (
                <span className="px-2 py-0.5 rounded-full bg-accent/15 text-accent ring-1 ring-accent/30 text-[10px] font-semibold uppercase tracking-wider">
                  {t("detail.approved") || "Aprobado"}
                </span>
              )}
              {/* Lineage pill: visible si este job es variante de otro.
                  Click lleva al padre. Si no hay parent_job_id, no se
                  renderea nada — los jobs primarios no muestran badge. */}
              {job.parent_job_id && (
                <button
                  type="button"
                  onClick={() => { window.location.href = `/job/${job.parent_job_id}`; }}
                  className="px-2 py-0.5 rounded-full bg-purple-500/15 text-purple-300 ring-1 ring-purple-500/30 text-[10px] font-semibold uppercase tracking-wider hover:bg-purple-500/25 transition-colors"
                  title={t("detail.variant_of_tooltip") || "Ver job padre"}
                >
                  {t("detail.variant_of") || "Variante"}
                </button>
              )}
            </div>
            <p className="text-sm text-ink-secondary mt-0.5 truncate">
              {job.artist}
            </p>
          </div>
        </div>
        <div className="job-detail-actions flex flex-wrap gap-2">
          {canDownload && (() => {
            // All profiles (youtube, umg, both) now produce the MP4 +
            // short + thumbnail set in the pipeline, so "Descargar todo"
            // is always relevant. ProRes is generated on demand via the
            // dedicated button when the job opted into UMG.
            const profile = job.delivery_profile || "youtube";
            const downloadIcon = (
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            );
            return (
              <>
                <button
                  onClick={downloadAllZip}
                  className="btn-secondary text-xs h-10 px-4"
                  data-tour="jobdetail-download-all"
                >
                  {downloadIcon}
                  {t("detail.download_all") || "Descargar todo"}
                </button>
                {hasUmgMaster && (
                  <span className="inline-flex items-center">
                    <button
                      onClick={downloadProResMaster}
                      className="btn-secondary text-xs h-10 px-4"
                      data-tour="jobdetail-prores-master"
                    >
                      {downloadIcon}
                      {t("detail.download_master") || "Master ProRes"}
                    </button>
                    <HelpTip articleId="prores-master" />
                  </span>
                )}
                {hasUmgShort && (
                  <button onClick={downloadProResShort} className="btn-secondary text-xs h-10 px-4">
                    {downloadIcon}
                    {t("detail.download_short_prores") || "Short ProRes"}
                  </button>
                )}
                {/* "Enviar a UMG" — admin only, only for approved jobs. Publishes
                    the 5-file set (ProRes master + ProRes short + MP4 + MP4 short
                    + thumbnail) to umg.genly.pro. Re-sending the same job_id
                    replaces the existing entry rather than duplicating. */}
                {isUmgAdmin && isDone && job.approved_by && (
                  <button
                    onClick={handleSendToUMG}
                    disabled={sendingUmg || isInUmgPortal}
                    className="btn-secondary text-xs h-10 px-4 disabled:opacity-60"
                    title={isInUmgPortal
                      ? "Este video ya está publicado en umg.genly.pro"
                      : "Publicar este video en umg.genly.pro (visible para Universal Music)"}
                  >
                    {isInUmgPortal
                      ? (t("detail.in_umg_portal") || "✓ En UMG")
                      : sendingUmg
                        ? (t("detail.sending_umg") || "Enviando…")
                        : (t("detail.send_umg") || "Enviar a UMG")}
                  </button>
                )}
              </>
            );
          })()}
          {/* Variantes: visible cuando el job está done. Crea un job
              nuevo (mismo audio + lyrics) con otro background Veo. */}
          {canDownload && (
            <button
              onClick={() => setShowVariantModal(true)}
              className="btn-secondary text-xs h-10 px-4"
              title={t("detail.variant_tooltip") ||
                "Crear otro video con las mismas lyrics aprobadas (cuesta 1 video del plan)"}
            >
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M12 5v14M5 12h14" strokeLinecap="round" />
              </svg>
              {t("detail.create_variant") || "Crear variante"}
            </button>
          )}
          {canDownload && !youtubeResult && (
            <button onClick={previewMetadata} className="btn-primary text-xs h-10 px-5">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.publish_youtube")}
            </button>
          )}
          {canDownload && youtubeResult && !youtubeResult.error && (
            <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center h-10 px-5 rounded-button text-xs font-semibold text-white bg-red-600 hover:bg-red-700 transition-colors">
              <svg className="inline-block w-4 h-4 mr-1.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.view_youtube")}
            </a>
          )}
          {canDownload && !youtubeShortResult && (
            <button onClick={previewShortMetadata} className="btn-secondary text-xs h-10 px-5">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.publish_short_youtube")}
            </button>
          )}
          {canDownload && youtubeShortResult && !youtubeShortResult.error && (
            <a href={youtubeShortResult.url} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center h-10 px-5 rounded-button text-xs font-semibold text-white bg-red-600 hover:bg-red-700 transition-colors">
              <svg className="inline-block w-4 h-4 mr-1.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.view_short_youtube")}
            </a>
          )}
        </div>
      </div>

      {/* ProRes hint toast — only on first click. The transcode runs
          on the server (~60-120 s for a 3-min song) and the browser
          shows its native download UI during the wait, so the user
          knows something is happening; this banner explains why. */}
      {proResHint && (
        <div className="mb-4 rounded-card bg-brand/[0.08] ring-1 ring-brand/25 px-4 py-3 flex items-center gap-3">
          <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin shrink-0" />
          <div className="flex-1 text-sm text-brand-light">
            {proResHint === "umg_short"
              ? "Generando Short ProRes (vertical) desde el MP4… puede tomar 1-2 minutos. La descarga arranca cuando esté listo (no cierres la pestaña)."
              : "Generando Master ProRes desde el MP4… puede tomar 1-2 minutos. La descarga arranca cuando esté listo (no cierres la pestaña)."}
          </div>
        </div>
      )}

      {/* Validation failed detail */}
      {isValidationFailed && job.error && (
        <div className="mb-6 rounded-card bg-red-500/[0.06] ring-1 ring-red-500/20 px-5 py-4">
          <p className="text-sm font-semibold text-red-300 mb-1">{t("detail.validation_issues") || "Problemas de política de contenido detectados"}</p>
          <p className="text-xs text-red-400/70 mb-3">{job.error}</p>
          <div className="px-3 py-2 rounded-xl bg-accent/[0.06] ring-1 ring-accent/20 mb-3">
            <p className="text-[11px] text-accent">
              {t("detail.validation_no_quota") || "Este video NO consume tu cuota mensual — solo los aprobados cuentan."}
            </p>
          </div>
          {/* Operator override toggle: only relevant on validation_failed,
              so we mount it here (not at the top-level of JobDetail).
              `initialOpen` is forced true because the validator ALREADY
              rejected this job — collapsing the toggle here hides the
              one action that actually unblocks the operator. Caso real
              2026-05-19: operator hit Reintentar without expanding,
              flag never sent, same validation failed again. */}
          <div className="mb-3">
            <ContentValidationToggle
              value={retryValidationEnabled}
              onChange={setRetryValidationEnabled}
              tenantId={_retryTenantId}
              billingGroup={_retryBillingGroup}
              disabled={retrying}
              initialOpen={true}
            />
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleRetry}
              disabled={retrying}
              className="btn-primary text-xs h-9 px-4 disabled:opacity-50"
            >
              {retrying ? (
                <><div className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin mr-1.5" />Reintentando…</>
              ) : (
                _retryIsUmg && !retryValidationEnabled
                  ? (t("detail.retry_bypass") || "Reintentar (fondo libre)")
                  : !_retryIsUmg && retryValidationEnabled
                    ? (t("detail.retry_force") || "Reintentar (con verificación)")
                    : (t("detail.retry") || "Reintentar sin re-subir")
              )}
            </button>
            <button onClick={() => onBack && onBack()} className="btn-secondary text-xs h-9 px-4">
              {t("detail.upload_again") || "Subir nuevo archivo"}
            </button>
          </div>
        </div>
      )}

      {/* Tabs — pill style matching the rest of the app */}
      <div className="flex flex-wrap gap-2 mb-6">
        {ALL_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`h-9 px-4 rounded-full text-xs font-medium transition-all ${
              activeTab === tab.key
                ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
                : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Provenance tab */}
      {activeTab === "provenance" && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6 mb-6">
          <ProvenanceTab jobId={job.job_id} t={t} />
        </div>
      )}

      {/* UMG master tab — non-previewable, download-only panel */}
      {activeTab === "umg_master" && canPreview && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-8 mb-6 text-center">
          <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-brand/10 ring-1 ring-brand/25 flex items-center justify-center">
            <svg className="w-7 h-7 text-brand-light" fill="none" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 24 24">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h3 className="text-base font-semibold text-white mb-1.5">
            {t("detail.umg_master_title") || "Máster ProRes 422 HQ"}
          </h3>
          <p className="text-xs text-ink-secondary mb-1">
            1920×1080 · 24 fps · BT.709 · pcm_s24le · QuickTime .mov
          </p>
          <p className="text-[11px] text-gray-600 mb-5">
            {t("detail.umg_master_subtitle") || "ProRes no se reproduce en el navegador. Descargá el archivo para reproducirlo en QuickTime / DaVinci / Premiere."}
          </p>
          {canDownload ? (
            <button
              onClick={downloadProResMaster}
              className="inline-flex items-center gap-2 btn-primary text-sm h-11 px-5"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t("detail.download_master") || "Descargar máster"}
            </button>
          ) : (
            <p className="text-[11px] text-amber-300/90">
              {t("detail.master_pending_approval") || "Aprobá el video para habilitar la descarga."}
            </p>
          )}
        </div>
      )}

      {/* Media preview (video / short / thumbnail) */}
      {activeTab !== "provenance" && activeTab !== "umg_master" && canPreview && (
        <>
          <div
            data-tour="jobdetail-preview"
            className={`job-detail-media-frame rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] overflow-hidden mb-4 mx-auto ${
              activeTab === "short"
                ? "job-detail-media-frame--short"
                : "job-detail-media-frame--landscape"
            }`}
          >
            {activeTab === "thumbnail" ? (
              <MediaPreview src={previewSrc} status={job.status} alt="Thumbnail" className="w-full h-full" imageClassName="bg-black/40" imageFit="contain" />
            ) : (
              previewSrc ? (
                <video
                  key={`${activeTab}-${videoReloadKey}-${mediaVersion}`}
                  ref={activeTab === "video" ? videoRef : undefined}
                  src={previewSrc}
                  controls
                  onError={handleVideoError}
                  className="job-detail-media-video w-full h-full block object-contain bg-black/40"
                />
              ) : (
                <MediaPreview status={job.status} className="w-full h-full" label="Preparando reproducción" />
              )
            )}
          </div>

          {/* File info */}
          <div className="flex items-center justify-between mb-6">
            <p className="text-xs text-gray-500">
              {MEDIA_TABS.find((t) => t.key === activeTab)?.desc}
              {activeTab !== "thumbnail" ? " MP4" : " JPG"}
            </p>
            {canDownload && downloadHref && (
              <a href={downloadHref} download
                className="text-xs font-medium text-brand hover:text-brand-light transition-colors flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                {t("detail.download")} {MEDIA_TABS.find((tb) => tb.key === activeTab)?.label}
              </a>
            )}
          </div>

          {/* Filmstrip de escenas (add-on multi-escena). Sólo si el job tiene
              scene_plan. Permite regenerar una escena puntual sin rehacer todo. */}
          {scenePlan && (
            <div className="mb-6">
              <ScenesFilmstrip
                scenePlan={scenePlan}
                thumbUrlFor={(scene) => sceneThumbs[scene.recurrence_key] || null}
                onRegenerate={(scene) => regenerateScene(scene)}
                onEditPrompt={(scene) => setEditingScene(scene)}
                onSeek={seekVideo}
                busyKey={sceneBusyKey}
                disabled={!scenesEditable}
              />
            </div>
          )}
        </>
      )}

      {editingScene && (
        <SceneEditModal
          scene={editingScene}
          onClose={() => setEditingScene(null)}
          onSubmit={(opts) => regenerateScene(editingScene, opts)}
        />
      )}

      {/* The dedicated full-page editing UI lives in the early return at
          the top of the component — by the time we get down here, status
          is pending_review or done, so no editing overlay needed. */}

      {/* Edit request panel:
            - pending_review: full toolkit (typography / lyrics / background)
            - done / rejected: lyrics-only (typo recovery without re-upload) */}
      {canEditLyrics && (
        <EditRequestPanel
          job={job}
          onEditTriggered={handleEditTriggered}
          allowedModes={editPanelAllowedModes}
          // El click "Editar lyrics" abre el Studio Console en su ruta
          // dedicada (mismo layout 3-col que /new) en vez del modal
          // fullscreen interno. El background mode del panel queda intacto.
          onLyricsClick={() => navigate(`/videos/${job.job_id}/edit-lyrics`)}
        />
      )}

      {/* Approval panel for pending_review */}
      {isPendingReview && (
        <div
          data-tour="jobdetail-approve-panel"
          className="rounded-card p-6 mb-6 animate-fade-in bg-gradient-to-br from-brand/[0.08] via-brand/[0.04] to-transparent ring-1 ring-brand/25"
        >
          <div className="flex items-center gap-2 mb-1.5">
            <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <h3 className="text-sm font-semibold tracking-tight">
              {t("review.title") || "Revisar y aprobar"}
            </h3>
          </div>
          <p className="text-xs text-ink-secondary mb-4">
            {t("review.description") || "Revisá el video generado antes de habilitar la descarga y publicación."}
          </p>
          <div className="px-3 py-2 rounded-xl bg-accent/[0.06] ring-1 ring-accent/20 mb-4">
            <p className="text-[11px] text-accent">
              {t("review.reject_free") || "Rechazar es gratis — solo los videos aprobados cuentan en tu cuota mensual."}
            </p>
          </div>
          <textarea
            value={reviewNotes}
            onChange={(e) => setReviewNotes(e.target.value)}
            placeholder={t("review.notes_placeholder") || "Notas (opcional)…"}
            className="input-field text-sm mb-4 resize-none"
            rows="2"
          />
          <div className="flex flex-wrap gap-3">
            <button
              onClick={handleApprove}
              disabled={approving}
              className="inline-flex items-center justify-center h-12 px-6 rounded-button text-sm font-semibold text-white bg-accent hover:bg-accent/90 disabled:opacity-50 transition-colors"
            >
              {approving ? (
                <div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />
              ) : (
                <svg className="inline-block w-4 h-4 mr-1.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              )}
              {t("review.approve") || "Aprobar"}
            </button>
            <button
              onClick={handleReject}
              disabled={approving}
              className="btn-secondary h-12 px-6 disabled:opacity-50 !text-red-300 hover:!text-red-200"
            >
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
              {t("review.reject") || "Rechazar"}
            </button>
          </div>
        </div>
      )}

      {/* Exportar a ProRes — para jobs MP4-only cuyo tenant tiene
          prores_export habilitado. Persiste umg_spec retroactivo y
          dispara el transcoding lazy. */}
      {canEnableProRes && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5 mb-4 flex items-start gap-4">
          <div className="w-10 h-10 shrink-0 rounded-xl bg-brand/10 ring-1 ring-brand/30 flex items-center justify-center text-brand">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M12 4v12m0 0l-4-4m4 4l4-4M4 20h16" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-white">
              {t("prores.cta_title") || "Exportar a ProRes (.mov broadcast)"}
            </div>
            <div className="text-xs text-gray-400 mt-0.5">
              {t("prores.cta_desc") ||
                "Este video se rindió como MP4. Generá una versión ProRes para broadcast / cliente."}
            </div>
            {proResToast && (
              <div className="mt-2 text-xs text-accent">
                {proResToast}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowProResModal(true)}
            className="shrink-0 px-4 py-2 rounded-md text-sm font-medium text-white bg-brand hover:bg-brand-strong ring-1 ring-brand/30 transition-colors"
          >
            {t("prores.cta_button") || "Exportar"}
          </button>
        </div>
      )}

      {showProResModal && (
        <EnableProResModal
          jobId={job.job_id}
          onClose={() => setShowProResModal(false)}
          onSuccess={(data) => {
            setShowProResModal(false);
            setProResToast(
              t("prores.queued_toast") ||
                "ProRes encolado. En 1-5 min va a estar disponible para descargar.",
            );
            // Trigger un refresh del job en el próximo tick para que
            // isUmgJob flipee a true (gracias al umg_spec recién
            // persistido) y aparezca el tab de Máster ProRes.
            onJobUpdate?.({ ...job, umg_spec: data.umg_spec });
          }}
        />
      )}

      {/* Guardar en Drive — botón visible cuando el job está done y el
          user tiene Drive conectado. El flow R2 → Drive server-to-server
          es ~30x más rápido que descargar+subir desde casa para ProRes
          de 16 GB. Si el user no tiene Drive conectado, en Settings está
          el botón Conectar. */}
      {isJobDone && driveFeatureEnabled && driveConnected && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5 mb-4 flex items-start gap-4">
          <div className="w-10 h-10 shrink-0 rounded-xl bg-accent/10 ring-1 ring-accent/30 flex items-center justify-center text-accent">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M3 8l3-5h12l3 5M3 8v11a2 2 0 002 2h14a2 2 0 002-2V8M3 8h18M12 12v6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-white">
              {t("drive.cta_title")}
            </div>
            <div className="text-xs text-gray-400 mt-0.5">
              {t("drive.cta_desc")}
            </div>
          </div>
          <button
            type="button"
            onClick={() => setShowDriveModal(true)}
            className="shrink-0 px-4 py-2 rounded-md text-sm font-medium text-white bg-accent hover:bg-accent/90 ring-1 ring-accent/30 transition-colors"
          >
            {t("drive.cta_button")}
          </button>
        </div>
      )}

      {showDriveModal && (
        <DriveTransferModal
          jobId={job.job_id}
          fileType={driveFileType}
          onClose={() => setShowDriveModal(false)}
        />
      )}

      {showVariantModal && (
        <VariantCreateModal
          job={job}
          onClose={() => setShowVariantModal(false)}
          onCreated={(newJobId) => {
            setShowVariantModal(false);
            // El caller (Dashboard / parent) decide cómo navegar. Por
            // simplicidad, redirigimos al detalle del nuevo job.
            window.location.href = `/job/${newJobId}`;
          }}
        />
      )}

      {/* YouTube Panel (only for approved/done jobs) */}
      {canDownload && showYoutubePanel && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6 animate-fade-in">
          <h3 className="font-semibold mb-4 flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24">
              <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
            </svg>
            {t("detail.publish_youtube")}
          </h3>

          {!metadataPreview && !youtubeResult && (
            <div className="flex items-center justify-center py-8">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              <span className="ml-3 text-sm text-gray-400">{t("detail.generating_meta")}</span>
            </div>
          )}

          {metadataPreview && !metadataPreview.error && !youtubeResult && (
            <div className="space-y-4">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs text-gray-500 uppercase tracking-wider">Título</label>
                  <button onClick={() => setEditingMeta((v) => !v)}
                    className="text-[11px] text-brand-light hover:text-white transition-colors">
                    {t("detail.edit_metadata")}
                  </button>
                </div>
                {editingMeta ? (
                  <input value={editedTitle} onChange={(e) => setEditedTitle(e.target.value)}
                    className="input-field text-sm w-full" maxLength={100} />
                ) : (
                  <p className="text-sm text-white mt-1 glass rounded-xl px-4 py-2.5">{metadataPreview.title}</p>
                )}
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Descripción</label>
                {editingMeta ? (
                  <textarea value={editedDescription} onChange={(e) => setEditedDescription(e.target.value)}
                    rows={5} className="input-field text-sm w-full resize-none mt-1" />
                ) : (
                  <p className="text-sm text-gray-300 mt-1 glass rounded-xl px-4 py-2.5 whitespace-pre-line line-clamp-4">{metadataPreview.description}</p>
                )}
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Tags</label>
                {editingMeta ? (
                  <>
                    <textarea
                      value={editedTags.join(", ")}
                      onChange={(e) => setEditedTags(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
                      rows={2}
                      className="input-field text-sm w-full resize-none mt-1"
                      placeholder="tag1, tag2, tag3" />
                    <p className="text-[10px] text-gray-600 mt-1">{t("detail.tags_help") || "Separados por coma."}</p>
                  </>
                ) : (
                  <div className="flex flex-wrap gap-1.5 mt-1">
                    {editedTags.slice(0, 12).map((tag, i) => (
                      <span key={i} className="px-2 py-1 rounded-lg bg-surface-3/50 text-xs text-gray-400">{tag}</span>
                    ))}
                    {editedTags.length > 12 && (
                      <span className="px-2 py-1 text-xs text-gray-600">+{editedTags.length - 12}</span>
                    )}
                  </div>
                )}
              </div>

              {/* Upload progress bar */}
              {uploading && uploadProgress >= 0 && (
                <div className="space-y-1.5">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>{t("detail.upload_progress")}</span>
                    <span>{uploadProgress}%</span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-surface-3/40 overflow-hidden">
                    <div className="h-full rounded-full bg-red-500 transition-all duration-500"
                      style={{ width: `${uploadProgress}%` }} />
                  </div>
                </div>
              )}

              {/* Confirm public dialog */}
              {confirmPublicYoutube ? (
                <div className="rounded-xl bg-amber-500/10 ring-1 ring-amber-500/30 px-4 py-3 space-y-3">
                  <p className="text-sm font-medium text-amber-300">{t("detail.confirm_public_title")}</p>
                  <p className="text-xs text-amber-200/70">{t("detail.confirm_public_body")}</p>
                  <div className="flex gap-2">
                    <button onClick={() => uploadToYoutube("public")} disabled={uploading}
                      className="btn-primary text-sm py-2 px-4 bg-amber-500 hover:bg-amber-400 disabled:opacity-50">
                      {t("detail.confirm_public_cta")}
                    </button>
                    <button onClick={() => setConfirmPublicYoutube(false)}
                      className="text-xs text-gray-400 hover:text-white transition-colors px-3">
                      {t("detail.cancel")}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex gap-3 pt-2">
                  <button onClick={() => uploadToYoutube("unlisted")} disabled={uploading}
                    className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
                    {uploading ? (
                      <><div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />{t("detail.uploading")}</>
                    ) : t("detail.upload_unlisted")}
                  </button>
                  <button onClick={() => setConfirmPublicYoutube(true)} disabled={uploading}
                    className="btn-secondary text-sm py-2.5 px-5 disabled:opacity-50">
                    {t("detail.upload_public")}
                  </button>
                  <button onClick={() => setShowYoutubePanel(false)}
                    className="text-xs text-gray-500 hover:text-white transition-colors ml-auto">
                    {t("detail.cancel")}
                  </button>
                </div>
              )}
            </div>
          )}

          {youtubeResult && !youtubeResult.error && (
            <div className="text-center py-6">
              <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-accent/10 flex items-center justify-center">
                <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <p className="text-sm font-medium text-white mb-2">{t("detail.published")}</p>
              <p className="text-xs text-gray-500 mb-3">
                {youtubeResult.privacy === "public" ? "Público" : "No listado"}
                {youtubeResult.thumbnail_set === false && (
                  <span className="ml-2 text-amber-400">· {t("detail.thumbnail_warning")}</span>
                )}
              </p>
              <div className="flex items-center justify-center gap-2 flex-wrap">
                <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
                  className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
                  </svg>
                  {t("detail.open_youtube")}
                </a>
                <button onClick={() => { navigator.clipboard.writeText(youtubeResult.url); setCopiedUrl(true); setTimeout(() => setCopiedUrl(false), 2000); }}
                  className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
                  </svg>
                  {copiedUrl ? t("detail.copied") : t("detail.copy_url")}
                </button>
              </div>
            </div>
          )}

          {(metadataPreview?.error || youtubeResult?.error) && (
            <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
              <p className="text-sm text-red-400">{metadataPreview?.error || youtubeResult?.error}</p>
            </div>
          )}
        </div>
      )}

      {/* YouTube Shorts Panel */}
      {canDownload && showYoutubeShortPanel && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6 animate-fade-in">
          <h3 className="font-semibold mb-4 flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24">
              <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
            </svg>
            {t("detail.publish_short_youtube")}
          </h3>

          {!shortMetadataPreview && !youtubeShortResult && (
            <div className="flex items-center justify-center py-8">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              <span className="ml-3 text-sm text-gray-400">{t("detail.generating_meta")}</span>
            </div>
          )}

          {shortMetadataPreview && !shortMetadataPreview.error && !youtubeShortResult && (
            <div className="space-y-4">
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs text-gray-500 uppercase tracking-wider">Título</label>
                  <button onClick={() => setEditingShortMeta((v) => !v)}
                    className="text-[11px] text-brand-light hover:text-white transition-colors">
                    {t("detail.edit_metadata")}
                  </button>
                </div>
                {editingShortMeta ? (
                  <input value={editedShortTitle} onChange={(e) => setEditedShortTitle(e.target.value)}
                    className="input-field text-sm w-full" maxLength={100} />
                ) : (
                  <p className="text-sm text-white mt-1 glass rounded-xl px-4 py-2.5">{shortMetadataPreview.title}</p>
                )}
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Descripción</label>
                {editingShortMeta ? (
                  <textarea value={editedShortDescription} onChange={(e) => setEditedShortDescription(e.target.value)}
                    rows={5} className="input-field text-sm w-full resize-none mt-1" />
                ) : (
                  <p className="text-sm text-gray-300 mt-1 glass rounded-xl px-4 py-2.5 whitespace-pre-line line-clamp-4">{shortMetadataPreview.description}</p>
                )}
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Tags</label>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {(shortMetadataPreview.tags || []).slice(0, 12).map((tag, i) => (
                    <span key={i} className="px-2 py-1 rounded-lg bg-surface-3/50 text-xs text-gray-400">{tag}</span>
                  ))}
                  {(shortMetadataPreview.tags || []).length > 12 && (
                    <span className="px-2 py-1 text-xs text-gray-600">+{(shortMetadataPreview.tags || []).length - 12}</span>
                  )}
                </div>
              </div>

              {/* Upload progress bar */}
              {uploadingShort && uploadShortProgress >= 0 && (
                <div className="space-y-1.5">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>{t("detail.upload_progress")}</span>
                    <span>{uploadShortProgress}%</span>
                  </div>
                  <div className="h-1.5 w-full rounded-full bg-surface-3/40 overflow-hidden">
                    <div className="h-full rounded-full bg-red-500 transition-all duration-500"
                      style={{ width: `${uploadShortProgress}%` }} />
                  </div>
                </div>
              )}

              {/* Confirm public dialog */}
              {confirmPublicYoutubeShort ? (
                <div className="rounded-xl bg-amber-500/10 ring-1 ring-amber-500/30 px-4 py-3 space-y-3">
                  <p className="text-sm font-medium text-amber-300">{t("detail.confirm_public_title")}</p>
                  <p className="text-xs text-amber-200/70">{t("detail.confirm_public_body")}</p>
                  <div className="flex gap-2">
                    <button onClick={() => uploadShortToYoutube("public")} disabled={uploadingShort}
                      className="btn-primary text-sm py-2 px-4 bg-amber-500 hover:bg-amber-400 disabled:opacity-50">
                      {t("detail.confirm_public_cta")}
                    </button>
                    <button onClick={() => setConfirmPublicYoutubeShort(false)}
                      className="text-xs text-gray-400 hover:text-white transition-colors px-3">
                      {t("detail.cancel")}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex gap-3 pt-2">
                  <button onClick={() => uploadShortToYoutube("unlisted")} disabled={uploadingShort}
                    className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
                    {uploadingShort ? (
                      <><div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />{t("detail.uploading")}</>
                    ) : t("detail.upload_unlisted")}
                  </button>
                  <button onClick={() => setConfirmPublicYoutubeShort(true)} disabled={uploadingShort}
                    className="btn-secondary text-sm py-2.5 px-5 disabled:opacity-50">
                    {t("detail.upload_public")}
                  </button>
                  <button onClick={() => setShowYoutubeShortPanel(false)}
                    className="text-xs text-gray-500 hover:text-white transition-colors ml-auto">
                    {t("detail.cancel")}
                  </button>
                </div>
              )}
            </div>
          )}

          {youtubeShortResult && !youtubeShortResult.error && (
            <div className="text-center py-6">
              <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-accent/10 flex items-center justify-center">
                <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <p className="text-sm font-medium text-white mb-2">{t("detail.published_short")}</p>
              <p className="text-xs text-gray-500 mb-3">
                {youtubeShortResult.privacy === "public" ? "Público" : "No listado"}
                {youtubeShortResult.thumbnail_set === false && (
                  <span className="ml-2 text-amber-400">· {t("detail.thumbnail_warning")}</span>
                )}
              </p>
              <div className="flex items-center justify-center gap-2 flex-wrap">
                <a href={youtubeShortResult.url} target="_blank" rel="noopener noreferrer"
                  className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
                  </svg>
                  {t("detail.open_youtube")}
                </a>
                <button onClick={() => { navigator.clipboard.writeText(youtubeShortResult.url); setCopiedShortUrl(true); setTimeout(() => setCopiedShortUrl(false), 2000); }}
                  className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
                  </svg>
                  {copiedShortUrl ? t("detail.copied") : t("detail.copy_url")}
                </button>
              </div>
            </div>
          )}

          {(shortMetadataPreview?.error || youtubeShortResult?.error) && (
            <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
              <p className="text-sm text-red-400">{shortMetadataPreview?.error || youtubeShortResult?.error}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
