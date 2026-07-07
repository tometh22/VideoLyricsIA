import { useState, useEffect } from "react";
import { useI18n } from "../../i18n";
import Sparkline from "./Sparkline";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function Stat({ value, label, lang }) {
  const fmt = new Intl.NumberFormat(lang, { notation: "compact", maximumFractionDigits: 1 });
  return (
    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
      <p className="text-2xl font-bold text-white">{value == null ? "—" : fmt.format(value)}</p>
      <p className="text-[11px] text-gray-500 uppercase tracking-wider mt-0.5">{label}</p>
    </div>
  );
}

function VideoBlock({ kind, data }) {
  const { t, lang } = useI18n();
  if (!data || !data.totals) {
    return (
      <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-5 py-6 text-center">
        <p className="text-sm text-gray-500">
          {kind === "short" ? "Short" : "Video"} — {t("yt.analytics.no_data_yet")}
        </p>
      </div>
    );
  }
  const series = data.series || [];
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold text-white">{kind === "short" ? "Short" : "Video"}</h4>
        {data.last_synced_at && (
          <span className="text-[10px] text-gray-600">
            {t("yt.analytics.synced")} {new Date(data.last_synced_at).toLocaleDateString(lang)}
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat value={data.totals.views} label={t("yt.analytics.views")} lang={lang} />
        <Stat value={data.totals.likes} label={t("yt.analytics.likes")} lang={lang} />
        <Stat value={data.totals.comments} label={t("yt.analytics.comments")} lang={lang} />
        <Stat value={data.totals.estimated_minutes_watched} label={t("yt.analytics.minutes")} lang={lang} />
      </div>
      {series.length >= 2 && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">{t("yt.analytics.views_28d")}</p>
          <Sparkline data={series.map((d) => d.views)} width={520} height={56} />
        </div>
      )}
    </div>
  );
}

export default function AnalyticsTab({ jobId }) {
  const { t } = useI18n();
  const [data, setData] = useState(null);   // null = loading
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    fetch(`${API}/youtube/analytics/${jobId}`, { headers: authHeaders() })
      .then(async (res) => {
        if (res.status === 404) { if (alive) setData({}); return; }
        if (!res.ok) throw new Error();
        const d = await res.json();
        if (alive) setData(d);
      })
      .catch(() => { if (alive) setError(true); });
    return () => { alive = false; };
  }, [jobId]);

  if (error) {
    return (
      <div className="rounded-card bg-red-500/10 border border-red-500/20 px-4 py-3 text-center">
        <p className="text-sm text-red-400">{t("yt.analytics.load_error")}</p>
      </div>
    );
  }
  if (data === null) {
    return (
      <div className="space-y-3 animate-pulse">
        <div className="h-20 rounded-card bg-surface-3/30" />
        <div className="h-14 rounded-card bg-surface-3/30" />
      </div>
    );
  }
  const kinds = Object.keys(data);
  if (kinds.length === 0) {
    return (
      <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] px-5 py-8 text-center">
        <p className="text-sm text-gray-400">{t("yt.analytics.no_data_yet")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {["video", "short"].filter((k) => data[k]).map((kind) => (
        <VideoBlock key={kind} kind={kind} data={data[kind]} />
      ))}
    </div>
  );
}
