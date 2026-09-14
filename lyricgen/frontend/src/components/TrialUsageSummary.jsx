import { useI18n } from "../i18n";

export default function TrialUsageSummary({ trial }) {
  const { t, lang } = useI18n();
  if (!trial) return null;
  const labelKey = {
    pending: "trial.pending", active: "trial.active",
    exhausted: "trial.exhausted", expired: "trial.expired",
  }[trial.state];
  const expiry = trial.expires_at ? new Date(trial.expires_at) : null;
  return <div role="status" className="space-y-1 text-xs text-ink-secondary">
    <p className="font-semibold text-white">{labelKey ? t(labelKey) : t("trial.title")}</p>
    <p>{t("trial.balance").replace("{reserved}", trial.reserved ?? 0).replace("{credits}", trial.credits ?? 9).replace("{available}", trial.available ?? 0)}</p>
    {trial.state === "pending" && <p>{t("trial.pending_help")}</p>}
    {expiry && Number.isFinite(expiry.getTime()) && <p>{t("trial.expires").replace("{date}", expiry.toLocaleString(lang || "es"))}</p>}
    <p>{t("trial.reservation_help")}</p>
    {trial.state === "exhausted" && <p>{t("trial.exhausted_help")}</p>}
  </div>;
}
