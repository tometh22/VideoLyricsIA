import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { fetchWithTimeout } from "../../fetchWithTimeout";
import Listbox from "../Listbox";
import MetadataEditor from "./MetadataEditor";
import PublishProgress from "./PublishProgress";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const ACTIVE_STATUSES = ["queued", "scheduled", "uploading"];

// Background publish flow. Phase is derived from server state, not
// stored: any active/terminal PublishJob rows on mount → tracking view,
// so navigating away and back mid-upload just works.
export default function PublishPanel({ job, onJobUpdate, onClose }) {
  const { t } = useI18n();
  const navigate = useNavigate();

  const [rows, setRows] = useState(null);          // null = loading publish state
  const [channels, setChannels] = useState(null);  // null = loading channels
  const [channelId, setChannelId] = useState(null);
  const [privacy, setPrivacy] = useState("unlisted");
  const [includeShort, setIncludeShort] = useState(true);
  const [schedule, setSchedule] = useState(false);
  const [scheduledAt, setScheduledAt] = useState("");
  const [metadata, setMetadata] = useState(null);  // null = generating preview
  const [metaError, setMetaError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const pollRef = useRef(null);
  const mirroredRef = useRef(false);

  const activeChannels = (channels || []).filter((c) => c.status === "active");

  // ── Server state ───────────────────────────────────────────────────
  const fetchRows = useCallback(async () => {
    try {
      const res = await fetch(`${API}/youtube/publish/${job.job_id}`, { headers: authHeaders() });
      if (!res.ok) return null;
      const data = await res.json();
      setRows(data);
      return data;
    } catch {
      return null;
    }
  }, [job.job_id]);

  useEffect(() => {
    fetchRows().then((data) => { if (data === null) setRows([]); });
    fetch(`${API}/youtube/channels`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : []))
      .then(setChannels)
      .catch(() => setChannels([]));
  }, [fetchRows]);

  // Default channel pick once channels land.
  useEffect(() => {
    if (channelId === null && channels) {
      const def = channels.find((c) => c.is_default && c.status === "active")
        || channels.find((c) => c.status === "active");
      if (def) setChannelId(def.id);
    }
  }, [channels, channelId]);

  const latestByKind = {};
  (rows || []).forEach((r) => { if (!latestByKind[r.kind]) latestByKind[r.kind] = r; });
  const visibleRows = Object.values(latestByKind);
  const hasActivity = visibleRows.length > 0;
  const anyActive = visibleRows.some((r) => ACTIVE_STATUSES.includes(r.status));

  // ── Polling while anything is active ──────────────────────────────
  useEffect(() => {
    if (!anyActive) { clearInterval(pollRef.current); return; }
    pollRef.current = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      fetchRows();
    }, 3000);
    return () => clearInterval(pollRef.current);
  }, [anyActive, fetchRows]);

  // Refresh the parent job once the video publish lands (mirror badge).
  useEffect(() => {
    const published = visibleRows.find((r) => r.kind === "video" && r.status === "published");
    if (published && !mirroredRef.current) {
      mirroredRef.current = true;
      onJobUpdate?.({ ...job, youtube: { video_id: published.video_id, url: published.video_url, privacy: published.privacy } });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);

  // ── Metadata preview (only needed in editing phase) ───────────────
  const generateMetadata = useCallback(async () => {
    setMetaError(null);
    setMetadata(null);
    try {
      const res = await fetchWithTimeout(
        `${API}/youtube/metadata/${job.job_id}`,
        { method: "POST", headers: authHeaders() },
        60_000,
      );
      const data = await res.json();
      if (!res.ok) { setMetaError(data.detail || `Error ${res.status}`); return; }
      setMetadata(data);
    } catch (err) {
      setMetaError(err.message);
    }
  }, [job.job_id]);

  useEffect(() => {
    if (rows !== null && !hasActivity && metadata === null && !metaError) generateMetadata();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, hasActivity]);

  // ── Actions ────────────────────────────────────────────────────────
  const publish = async (retryKinds = null) => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const body = {
        channel_id: channelId,
        privacy,
        metadata,
        include_short: retryKinds ? retryKinds.includes("short") : includeShort,
        // datetime-local gives local wall time; toISOString() makes it
        // timezone-aware UTC, which the backend requires.
        scheduled_at: schedule && scheduledAt ? new Date(scheduledAt).toISOString() : null,
      };
      const res = await fetch(`${API}/youtube/publish/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        setSubmitError(data.detail || `Error ${res.status}`);
      } else {
        await fetchRows();
      }
    } catch (err) {
      setSubmitError(err.message);
    }
    setSubmitting(false);
  };

  const retryRow = (row) => publish([row.kind]);

  const cancelRow = async (row) => {
    try {
      await fetch(`${API}/youtube/publish-jobs/${row.id}/cancel`, { method: "POST", headers: authHeaders() });
    } catch {}
    fetchRows();
  };

  // ── Render ─────────────────────────────────────────────────────────
  if (rows === null || channels === null) {
    return (
      <div className="flex items-center justify-center py-8">
        <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  // Tracking phase: any publish activity exists.
  if (hasActivity) {
    return (
      <div className="space-y-4">
        <PublishProgress rows={visibleRows} onRetry={retryRow} onCancel={cancelRow} />
        {submitError && (
          <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
            <p className="text-sm text-red-400">{submitError}</p>
          </div>
        )}
        <button onClick={onClose} className="text-xs text-gray-500 hover:text-white transition-colors">
          {t("yt.publish.close")}
        </button>
      </div>
    );
  }

  // Editing phase — no connected channels: point at Settings.
  if (activeChannels.length === 0) {
    return (
      <div className="text-center py-6">
        <p className="text-sm text-gray-400 mb-4">{t("yt.publish.no_channels")}</p>
        <button onClick={() => navigate("/account")} className="btn-secondary text-sm py-2.5 px-5">
          {t("yt.publish.go_settings")}
        </button>
      </div>
    );
  }

  if (metaError) {
    return (
      <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-center">
        <p className="text-sm text-red-400">{metaError}</p>
        <button onClick={generateMetadata}
          className="mt-2 text-xs text-gray-400 hover:text-white transition-colors underline">
          {t("dash.retry")}
        </button>
      </div>
    );
  }

  if (metadata === null) {
    return (
      <div className="flex items-center justify-center py-8">
        <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
        <span className="ml-3 text-sm text-gray-400">{t("detail.generating_meta")}</span>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {activeChannels.length > 1 && (
        <div>
          <label className="text-xs text-gray-500 uppercase tracking-wider block mb-1">{t("yt.publish.channel")}</label>
          <Listbox
            value={String(channelId)}
            onChange={(code) => setChannelId(Number(code))}
            options={activeChannels.map((c) => ({
              code: String(c.id),
              label: c.channel_title || c.channel_id,
              hint: c.is_default ? t("yt.channels.default_badge") : "",
            }))}
          />
        </div>
      )}

      <MetadataEditor metadata={metadata} onChange={setMetadata} />
      <button onClick={generateMetadata}
        className="text-xs text-gray-500 hover:text-white transition-colors underline -mt-2">
        {t("yt.publish.regenerate")}
      </button>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label className="text-xs text-gray-500 uppercase tracking-wider block mb-1">{t("yt.publish.privacy")}</label>
          <Listbox
            value={privacy}
            onChange={setPrivacy}
            options={[
              { code: "unlisted", label: t("settings.privacy_unlisted") },
              { code: "public", label: t("settings.privacy_public") },
              { code: "private", label: t("settings.privacy_private") },
            ]}
          />
        </div>
        <div className="flex items-end pb-1">
          <label className="flex items-center gap-2.5 cursor-pointer select-none">
            <input type="checkbox" checked={includeShort}
              onChange={(e) => setIncludeShort(e.target.checked)}
              className="w-4 h-4 rounded accent-[#6D4AFF]" />
            <span className="text-sm text-gray-300">{t("yt.publish.include_short")}</span>
          </label>
        </div>
      </div>

      <div>
        <label className="flex items-center gap-2.5 cursor-pointer select-none mb-2">
          <input type="checkbox" checked={schedule}
            onChange={(e) => setSchedule(e.target.checked)}
            className="w-4 h-4 rounded accent-[#6D4AFF]" />
          <span className="text-sm text-gray-300">{t("yt.publish.schedule")}</span>
        </label>
        {schedule && (
          <div className="pl-6">
            <input
              type="datetime-local"
              value={scheduledAt}
              min={new Date(Date.now() + 15 * 60 * 1000).toISOString().slice(0, 16)}
              onChange={(e) => setScheduledAt(e.target.value)}
              className="input-field text-sm [color-scheme:dark]"
            />
            <p className="text-[10px] text-gray-600 mt-1.5 max-w-md">
              {privacy === "public"
                ? t("yt.publish.schedule_help_public")
                : t("yt.publish.schedule_help_private")}
            </p>
          </div>
        )}
      </div>

      {submitError && (
        <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
          <p className="text-sm text-red-400">{submitError}</p>
        </div>
      )}

      <div className="flex gap-3 pt-1">
        <button onClick={() => publish()}
          disabled={submitting || !metadata.title || (schedule && !scheduledAt)}
          className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
          {submitting ? (
            <><div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />{t("yt.publish.publishing")}</>
          ) : schedule && scheduledAt ? (
            t("yt.publish.publish_scheduled")
          ) : (
            t("yt.publish.publish")
          )}
        </button>
        <button onClick={onClose} className="text-xs text-gray-500 hover:text-white transition-colors ml-auto">
          {t("detail.cancel")}
        </button>
      </div>
    </div>
  );
}
