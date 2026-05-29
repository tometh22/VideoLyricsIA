import { useEffect, useState, useCallback } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";
const REFRESH_INTERVAL_MS = 60_000; // 60s — fast enough that the operator sees a fresh count
                                    // after approving a video, slow enough to avoid load on /usage.

// Renders the operator's current monthly usage against their plan
// limit (e.g. "12 / 250 este mes") with a slim progress bar underneath.
// Designed to live in the Sidebar between the Plan badge and the user
// avatar so it's visible on every page without occupying chrome real
// estate elsewhere.
//
// Hidden when:
//   - The user has no plan / hasn't loaded yet (`user` falsy).
//   - The plan is "unlimited" (no cap to visualise; showing X/999999
//     would be noise).
//
// Color thresholds match the backend's `alert_80` / `alert_100` flags:
// gray-violet under 80%, amber 80-100%, red over 100%.
//
// `overage_total` (a $$ amount the operator owes if they ship past the
// plan limit) is admin-only — operators see only the unit count + the
// percent. Showing dollar amounts to UMG operators isn't part of their
// contract; that's a billing decision tomas mediates with UMG comms,
// not an in-app surface for the operator.
export default function UsageBadge({ user }) {
  const { t } = useI18n();
  const [usage, setUsage] = useState(null);
  const [error, setError] = useState(false);

  const fetchUsage = useCallback(async () => {
    const token = localStorage.getItem("genly_token");
    if (!token) return;
    try {
      const res = await fetch(`${API}/usage`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        // 401 → token expired, parent will redirect. 5xx → backend blip.
        // Either way, just stop showing stale data without spamming
        // errors — the badge being absent is a fine fallback.
        setError(true);
        return;
      }
      const data = await res.json();
      setUsage(data);
      setError(false);
    } catch (e) {
      // Network error — same fallback as above. Don't crash the sidebar
      // because /usage is down; let the rest of the app work.
      setError(true);
    }
  }, []);

  useEffect(() => {
    if (!user) return undefined;
    fetchUsage();
    const id = setInterval(fetchUsage, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [user, fetchUsage]);

  if (!user) return null;
  if (error || !usage) return null;
  if (usage.plan === "unlimited") return null;

  const { used = 0, limit = 0, percent = 0, alert_80, alert_100 } = usage;
  const isAdmin = user?.role === "admin";

  // Tailwind color classes per threshold. Kept inline (instead of
  // computed clsx) because the variants are simple and explicit reads
  // better than a ternary nest. Bar background is always neutral; bar
  // fill changes color to draw the eye when the operator approaches
  // or crosses the cap.
  let barFill = "bg-brand";
  let textTone = "text-gray-400";
  if (alert_100) {
    barFill = "bg-red-500";
    textTone = "text-red-400";
  } else if (alert_80) {
    barFill = "bg-amber-400";
    textTone = "text-amber-300";
  }

  // Cap the bar width at 100% so 102% overage doesn't visually overflow
  // the container — but keep the percent text honest (showing "105%"
  // even if the bar maxes out).
  const barWidth = Math.min(100, percent);

  return (
    <div className="px-5 pb-3">
      <div className="px-3 py-2 rounded-xl bg-surface-2/30">
        <div className={`text-[11px] font-medium ${textTone} flex items-center justify-between`}>
          <span>
            {used} / {limit} {t("sidebar.usage.this_month") || "este mes"}
          </span>
          <span className="text-[10px] opacity-70">{percent}%</span>
        </div>
        <div className="mt-1.5 h-1 rounded-full bg-white/[0.06] overflow-hidden">
          <div
            className={`h-full ${barFill} transition-all duration-500`}
            style={{ width: `${barWidth}%` }}
          />
        </div>
        {/* Overage cost — admin-only. Operators don't see $$ amounts.
            Negative case (`!isAdmin`) is most users, so we render
            nothing in that branch instead of an explicit null. */}
        {isAdmin && usage.overage_total > 0 ? (
          <div className="mt-1 text-[10px] text-gray-500">
            +${usage.overage_total} {t("sidebar.usage.overage") || "excedente"}
          </div>
        ) : null}
      </div>
    </div>
  );
}
