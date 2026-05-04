import { useState, useEffect } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenParam() {
  const token = localStorage.getItem("genly_token");
  return token ? `token=${encodeURIComponent(token)}` : "";
}

const MEDIA_TABS = [
  { key: "video", label: "Lyric Video", desc: "1920x1080" },
  { key: "short", label: "Short", desc: "1080x1920" },
  { key: "thumbnail", label: "Thumbnail", desc: "1280x720" },
];

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
        <a
          href={`${API}/provenance/${jobId}/export?${tokenParam()}`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-brand hover:text-brand-light transition-colors flex items-center gap-1"
        >
          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          {t("prov.export") || "Export"}
        </a>
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
  const [activeTab, setActiveTab] = useState("video");
  const [uploading, setUploading] = useState(false);
  const [youtubeResult, setYoutubeResult] = useState(job.youtube || null);
  const [metadataPreview, setMetadataPreview] = useState(null);
  const [showYoutubePanel, setShowYoutubePanel] = useState(false);
  const [reviewNotes, setReviewNotes] = useState("");
  const [approving, setApproving] = useState(false);
  const name = (job.filename || "").replace(/\.mp3$/i, "");

  const canPreview = job.status === "done" || job.status === "pending_review";
  const canDownload = job.status === "done";
  const isPendingReview = job.status === "pending_review";
  const isValidationFailed = job.status === "validation_failed";

  if (!canPreview && !isValidationFailed) {
    return (
      <div className="w-full max-w-2xl animate-fade-in text-center py-20">
        <p className="text-gray-400">{t("detail.not_available")}</p>
        <button onClick={onBack} className="btn-secondary mt-4">{t("detail.back")}</button>
      </div>
    );
  }

  const downloadAll = () => {
    ["video", "short", "thumbnail"].forEach((type) => {
      const a = document.createElement("a");
      a.href = `${API}/download/${job.job_id}/${type}?${tokenParam()}`;
      a.download = "";
      a.click();
    });
  };

  const previewMetadata = async () => {
    setShowYoutubePanel(true);
    try {
      const res = await fetch(`${API}/youtube/metadata/${job.job_id}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      setMetadataPreview(data);
    } catch (err) {
      setMetadataPreview({ error: err.message });
    }
  };

  const uploadToYoutube = async (privacy = "unlisted") => {
    setUploading(true);
    try {
      const res = await fetch(`${API}/youtube/upload/${job.job_id}?privacy=${privacy}`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      setYoutubeResult(data);
    } catch (err) {
      setYoutubeResult({ error: err.message });
    }
    setUploading(false);
  };

  const handleApprove = async () => {
    setApproving(true);
    try {
      const res = await fetch(`${API}/approve/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ notes: reviewNotes }),
      });
      if (res.ok) {
        const updated = await (await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() })).json();
        onJobUpdate?.(updated);
      }
    } catch {}
    setApproving(false);
  };

  const handleReject = async () => {
    setApproving(true);
    try {
      const res = await fetch(`${API}/reject/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ notes: reviewNotes }),
      });
      if (res.ok) {
        const updated = await (await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() })).json();
        onJobUpdate?.(updated);
      }
    } catch {}
    setApproving(false);
  };

  const ALL_TABS = [
    ...MEDIA_TABS,
    { key: "provenance", label: t("prov.title") || "Provenance" },
  ];

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-4">
          <button onClick={onBack}
            className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-xl font-bold">{name}</h2>
              {isPendingReview && (
                <span className="px-2 py-0.5 rounded-md bg-amber-500/10 text-amber-400 text-[10px] font-bold uppercase">
                  {t("batch.pending_review") || "Pending Review"}
                </span>
              )}
              {isValidationFailed && (
                <span className="px-2 py-0.5 rounded-md bg-red-500/10 text-red-400 text-[10px] font-bold uppercase">
                  {t("batch.validation_failed") || "Validation Failed"}
                </span>
              )}
              {job.status === "done" && job.approved_by && (
                <span className="px-2 py-0.5 rounded-md bg-accent/10 text-accent text-[10px] font-bold uppercase">
                  {t("detail.approved") || "Approved"}
                </span>
              )}
            </div>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="flex gap-2">
          {canDownload && (
            <button onClick={downloadAll} className="btn-secondary text-sm py-2.5 px-4">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t("detail.download")}
            </button>
          )}
          {canDownload && !youtubeResult && (
            <button onClick={previewMetadata} className="btn-primary text-sm py-2.5 px-4">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.publish_youtube")}
            </button>
          )}
          {canDownload && youtubeResult && !youtubeResult.error && (
            <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
              className="btn-primary text-sm py-2.5 px-4 bg-red-600 hover:bg-red-700">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.view_youtube")}
            </a>
          )}
        </div>
      </div>

      {/* Validation failed detail */}
      {isValidationFailed && job.error && (
        <div className="mb-6 rounded-2xl bg-red-500/5 border border-red-500/10 px-5 py-4">
          <p className="text-sm font-medium text-red-400 mb-1">{t("detail.validation_issues") || "Content policy issues detected"}</p>
          <p className="text-xs text-red-400/70 mb-3">{job.error}</p>
          <div className="px-3 py-2 rounded-lg bg-accent/5 border border-accent/15 mb-3">
            <p className="text-[11px] text-accent/80">
              {t("detail.validation_no_quota") || "Este video NO consume tu cuota mensual — solo videos aprobados cuentan."}
            </p>
          </div>
          <button
            onClick={() => onBack && onBack()}
            className="btn-primary text-xs py-2 px-4"
          >
            {t("detail.upload_again") || "Subir el MP3 de nuevo"}
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 mb-6 p-1 glass rounded-2xl w-fit">
        {ALL_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-5 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 ${
              activeTab === tab.key
                ? "bg-brand text-white shadow-glow"
                : "text-gray-400 hover:text-white"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Provenance tab */}
      {activeTab === "provenance" && (
        <div className="glass rounded-3xl p-6 mb-6">
          <ProvenanceTab jobId={job.job_id} t={t} />
        </div>
      )}

      {/* Media preview */}
      {activeTab !== "provenance" && canPreview && (
        <>
          <div className="glass rounded-3xl overflow-hidden mb-6">
            {activeTab === "thumbnail" ? (
              <img
                src={`${API}/preview/${job.job_id}/thumbnail?${tokenParam()}`}
                alt="Thumbnail"
                className="w-full max-h-[500px] object-contain bg-black/30"
              />
            ) : (
              <video
                key={activeTab}
                src={`${API}/preview/${job.job_id}/${activeTab}?${tokenParam()}`}
                controls
                className={`w-full bg-black/30 ${
                  activeTab === "short" ? "max-h-[600px] mx-auto" : "max-h-[500px]"
                }`}
                style={activeTab === "short" ? { maxWidth: "340px", margin: "0 auto", display: "block" } : {}}
              />
            )}
          </div>

          {/* File info */}
          <div className="flex items-center justify-between mb-6">
            <p className="text-xs text-gray-500">
              {MEDIA_TABS.find((t) => t.key === activeTab)?.desc}
              {activeTab !== "thumbnail" ? " MP4" : " JPG"}
            </p>
            {canDownload && (
              <a href={`${API}/download/${job.job_id}/${activeTab}?${tokenParam()}`} download
                className="text-xs font-medium text-brand hover:text-brand-light transition-colors flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                {t("detail.download")} {MEDIA_TABS.find((tb) => tb.key === activeTab)?.label}
              </a>
            )}
          </div>
        </>
      )}

      {/* Approval panel for pending_review */}
      {isPendingReview && (
        <div className="glass rounded-3xl p-6 mb-6 animate-fade-in border border-amber-500/20">
          <h3 className="font-semibold mb-4 flex items-center gap-2">
            <svg className="w-5 h-5 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {t("review.title") || "Review & Approve"}
          </h3>
          <p className="text-sm text-gray-400 mb-3">
            {t("review.description") || "Review the generated content before making it available for download and YouTube upload."}
          </p>
          <div className="px-3 py-2 rounded-lg bg-accent/5 border border-accent/15 mb-4">
            <p className="text-[11px] text-accent/80">
              {t("review.reject_free") || "Rechazar es gratis — no consume tu cuota mensual. Solo los videos aprobados cuentan."}
            </p>
          </div>
          <textarea
            value={reviewNotes}
            onChange={(e) => setReviewNotes(e.target.value)}
            placeholder={t("review.notes_placeholder") || "Notes (optional)..."}
            className="w-full px-4 py-3 rounded-xl bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500 transition-all mb-4 resize-none"
            rows="2"
          />
          <div className="flex gap-3">
            <button
              onClick={handleApprove}
              disabled={approving}
              className="btn-primary py-2.5 px-6 disabled:opacity-50 bg-accent hover:bg-accent/90"
            >
              {approving ? (
                <div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />
              ) : (
                <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              )}
              {t("review.approve") || "Approve"}
            </button>
            <button
              onClick={handleReject}
              disabled={approving}
              className="btn-secondary py-2.5 px-6 disabled:opacity-50 text-red-400 hover:text-red-300"
            >
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
              {t("review.reject") || "Reject"}
            </button>
          </div>
        </div>
      )}

      {/* YouTube Panel (only for approved/done jobs) */}
      {canDownload && showYoutubePanel && (
        <div className="glass rounded-3xl p-6 animate-fade-in">
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
                <label className="text-xs text-gray-500 uppercase tracking-wider">{t("settings.title_format").split(" ")[0]}</label>
                <p className="text-sm text-white mt-1 glass rounded-xl px-4 py-2.5">{metadataPreview.title}</p>
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">{t("settings.desc_header").split(" ")[0]}</label>
                <p className="text-sm text-gray-300 mt-1 glass rounded-xl px-4 py-2.5 whitespace-pre-line">{metadataPreview.description}</p>
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Tags</label>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {(metadataPreview.tags || []).map((tag, i) => (
                    <span key={i} className="px-2 py-1 rounded-lg bg-surface-3/50 text-xs text-gray-400">{tag}</span>
                  ))}
                </div>
              </div>

              <div className="flex gap-3 pt-2">
                <button onClick={() => uploadToYoutube("unlisted")} disabled={uploading}
                  className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
                  {uploading ? (
                    <><div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />{t("detail.uploading")}</>
                  ) : (
                    t("detail.upload_unlisted")
                  )}
                </button>
                <button onClick={() => uploadToYoutube("public")} disabled={uploading}
                  className="btn-secondary text-sm py-2.5 px-5 disabled:opacity-50">
                  {t("detail.upload_public")}
                </button>
                <button onClick={() => setShowYoutubePanel(false)}
                  className="text-xs text-gray-500 hover:text-white transition-colors ml-auto">
                  {t("detail.cancel")}
                </button>
              </div>
            </div>
          )}

          {youtubeResult && !youtubeResult.error && (
            <div className="text-center py-6">
              <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-accent/10 flex items-center justify-center">
                <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <p className="text-sm font-medium text-white mb-1">{t("detail.published")}</p>
              <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
                className="text-sm text-brand hover:text-brand-light transition-colors underline">
                {youtubeResult.url}
              </a>
              <p className="text-xs text-gray-500 mt-2">Estado: {youtubeResult.privacy}</p>
            </div>
          )}

          {(metadataPreview?.error || youtubeResult?.error) && (
            <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-center">
              <p className="text-sm text-red-400">{metadataPreview?.error || youtubeResult?.error}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
