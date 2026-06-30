import { useEffect, useState, useCallback } from "react";
import { useI18n } from "../i18n";
import { readCachedJson, writeCachedJson } from "../lib/cachedFetch";

const API = import.meta.env.VITE_API_URL || "";
const REFRESH_INTERVAL_MS = 60_000; // 60s — fast enough that the operator sees a fresh count
                                    // after approving a video, slow enough to avoid load on /usage.
// 2026-05-30 perf: cache the /usage response across sessions so the
// badge paints from localStorage instantly on mount (no waiting on a
// 600-700 ms backend round-trip from LATAM to Railway US-East). The
// background refresh fires right after, so within 1 s the operator
// sees the live value. TTL is 5 min — well above the 60 s polling
// interval, so the cache only matters between sessions.
const USAGE_CACHE_TTL_MS = 5 * 60_000;

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
  // 2026-05-30 perf: seed state from the per-user localStorage cache so
  // the badge paints during the FIRST render — no 700 ms wait on the
  // /usage round-trip. The background fetch below replaces this with
  // the live value within ~1 s; if /usage 401s the operator never sees
  // stale data because the parent route boots them out anyway.
  const cacheKey = user?.id ? `cache:usage:${user.id}` : null;
  const [usage, setUsage] = useState(() =>
    cacheKey ? readCachedJson(cacheKey, USAGE_CACHE_TTL_MS) : null,
  );
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
      // 2026-05-30 perf: persist for the next mount.
      if (cacheKey) writeCachedJson(cacheKey, data);
    } catch (e) {
      // Network error — same fallback as above. Don't crash the sidebar
      // because /usage is down; let the rest of the app work.
      setError(true);
    }
  }, [cacheKey]);

  useEffect(() => {
    if (!user) return undefined;
    fetchUsage();
    const id = setInterval(fetchUsage, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [user, fetchUsage]);

  if (!user) return null;
  if (error || !usage) return null;
  if (usage.plan === "unlimited") return null;

  const {
    used = 0, limit = 0, percent = 0, alert_80, alert_100,
    bonus_total = 0, bonus_remaining = 0, bonus_expires_at = null,
    total_available = 0, scenes_credit_cost = 3, projection = {},
  } = usage;
  const isAdmin = user?.role === "admin";

  // Días hasta el vencimiento del regalo (el backend ya excluye los vencidos).
  let giftDays = null;
  if (bonus_remaining > 0 && bonus_expires_at) {
    const ms = new Date(bonus_expires_at).getTime() - Date.now();
    giftDays = Number.isFinite(ms) ? Math.max(0, Math.ceil(ms / 86_400_000)) : null;
  }
  const projNormal = projection.normal ?? total_available;
  const projScenes = projection.escenas ?? Math.floor(total_available / (scenes_credit_cost || 1));

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

  const giftPct = bonus_total ? Math.round((bonus_remaining / bonus_total) * 100) : 0;

  return (
    <div className="px-5 pb-3">
      <div className="px-3 py-2.5 rounded-xl bg-surface-2/30 space-y-2">
        {/* Plan: uso del mes vs cupo (lo que se factura). */}
        <div>
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
        </div>

        {/* Créditos de regalo (promo). Solo si quedan. Cuenta regresiva al vto. */}
        {bonus_remaining > 0 ? (
          <div>
            <div className="text-[11px] font-medium text-emerald-300 flex items-center justify-between">
              <span>🎁 {bonus_remaining} {t("credits.gift_label") || "de regalo"}</span>
              {giftDays != null ? (
                <span className="text-[10px] opacity-80">
                  {giftDays === 0
                    ? (t("credits.gift_today") || "vence hoy")
                    : (t("credits.gift_expires") || "vencen en {n} días").replace("{n}", giftDays)}
                </span>
              ) : null}
            </div>
            <div className="mt-1.5 h-1 rounded-full bg-white/[0.06] overflow-hidden">
              <div
                className="h-full bg-emerald-400/80 transition-all duration-500"
                style={{ width: `${giftPct}%` }}
              />
            </div>
          </div>
        ) : null}

        {/* Total disponible + qué rinde (dinámico: usa el costo real del back). */}
        <div className="pt-1 border-t border-white/[0.06]">
          <div className="text-[11px] text-gray-300 font-medium mt-1.5">
            {t("credits.total_available") || "Total disponible"}: {total_available}
          </div>
          <div className="text-[10px] text-gray-500 leading-snug mt-0.5">
            {(t("credits.projection") || "{normal} normales o {escenas} con Escenas")
              .replace("{normal}", projNormal)
              .replace("{escenas}", projScenes)}
          </div>
        </div>

        {/* Leyenda de costo — siempre visible, así queda claro qué consume cada uno. */}
        <div className="text-[10px] text-gray-500 flex items-center gap-2 pt-0.5">
          <span>🎥 {t("credits.legend_normal") || "Normal"} 1</span>
          <span aria-hidden="true">·</span>
          <span>🎬 {t("credits.legend_scenes") || "Escenas"} {scenes_credit_cost}</span>
        </div>

        {/* Overage cost — admin-only. Operators don't see $$ amounts. */}
        {isAdmin && usage.overage_total > 0 ? (
          <div className="text-[10px] text-gray-500">
            +${usage.overage_total} {t("sidebar.usage.overage") || "excedente"}
          </div>
        ) : null}
      </div>
    </div>
  );
}
