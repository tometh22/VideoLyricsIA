import { useI18n } from "../../i18n";
import { useHelp } from "./HelpProvider";

// Round "?" button for the topbar. Carries data-tour="help-button" so
// the DashboardTour can highlight it as a final discovery step.
export default function HelpButton({ className = "" }) {
  const { t } = useI18n();
  const { openHelp, isOpen } = useHelp();
  const label = t("help.button.tooltip") || "Ayuda (?)";

  return (
    <button
      type="button"
      onClick={() => openHelp()}
      aria-label={label}
      title={label}
      data-tour="help-button"
      aria-expanded={isOpen}
      className={
        "relative inline-flex items-center justify-center w-8 h-8 rounded-full " +
        "bg-surface-2/60 border border-white/[0.08] text-ink-secondary " +
        "hover:bg-surface-2 hover:text-ink-primary hover:border-white/[0.14] " +
        "transition-all duration-240 active:scale-95 " +
        className
      }
    >
      <svg
        className="w-[18px] h-[18px]"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    </button>
  );
}
