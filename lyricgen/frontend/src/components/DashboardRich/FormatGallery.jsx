import { useI18n } from "../../i18n";

// ─── Device mockup SVGs ─────────────────────────────────────────────
// Inline SVGs so we keep the "no binary assets" pledge of the help center.
// Each mockup occupies a 16:9-ish frame inside its card.

function YouTubeMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      {/* YT chrome */}
      <rect x="0" y="0" width="220" height="124" rx="6" fill="#0f0f0f" />
      <rect x="0" y="0" width="220" height="14" fill="#1a1a1a" />
      <circle cx="8" cy="7" r="2" fill="#FF0000" />
      <rect x="14" y="5" width="36" height="4" rx="1" fill="#3a3a3a" />
      {/* Video area */}
      <defs>
        <linearGradient id="dr-yt-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#6D4AFF" />
          <stop offset="50%" stopColor="#3a1ab8" />
          <stop offset="100%" stopColor="#14C8A8" />
        </linearGradient>
      </defs>
      <rect x="6" y="20" width="208" height="86" rx="4" fill="url(#dr-yt-grad)" />
      {/* Lyric ghost text */}
      <rect x="60" y="58" width="100" height="6" rx="1" fill="rgba(255,255,255,0.7)" />
      <rect x="76" y="68" width="68" height="5" rx="1" fill="rgba(255,255,255,0.5)" />
      {/* Play button overlay */}
      <circle className="dr-fmt-play" cx="110" cy="63" r="14" fill="rgba(255,0,0,0.9)" />
      <path d="M105 58 L120 63 L105 68 Z" fill="white" />
      {/* Progress bar */}
      <rect x="6" y="108" width="208" height="3" rx="1" fill="#3a3a3a" />
      <rect x="6" y="108" width="78" height="3" rx="1" fill="#FF0000" />
      {/* Bottom controls */}
      <circle cx="14" cy="118" r="3" fill="#3a3a3a" />
      <rect x="24" y="115" width="40" height="5" rx="1" fill="#2a2a2a" />
    </svg>
  );
}

function PhoneShortMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      {/* Phone outline */}
      <rect x="86" y="6" width="48" height="112" rx="8" fill="#1a1a1a" stroke="#3a3a3a" strokeWidth="1.5" />
      {/* Notch */}
      <rect x="100" y="6" width="20" height="4" rx="2" fill="#0a0a0a" />
      <defs>
        <linearGradient id="dr-ph-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#FF006E" />
          <stop offset="100%" stopColor="#8338EC" />
        </linearGradient>
      </defs>
      {/* Screen */}
      <rect x="89" y="12" width="42" height="100" rx="5" fill="url(#dr-ph-grad)" />
      {/* Lyric centered */}
      <rect x="95" y="56" width="30" height="3" rx="1" fill="rgba(255,255,255,0.85)" />
      <rect x="98" y="62" width="24" height="3" rx="1" fill="rgba(255,255,255,0.65)" />
      {/* Right rail of icons (IG style) */}
      <circle cx="138" cy="40" r="2.5" fill="rgba(255,255,255,0.7)" />
      <circle cx="138" cy="50" r="2.5" fill="rgba(255,255,255,0.7)" />
      <circle cx="138" cy="60" r="2.5" fill="rgba(255,255,255,0.7)" />
      <circle cx="138" cy="70" r="2.5" fill="rgba(255,255,255,0.7)" />
    </svg>
  );
}

function BroadcastMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      {/* TV outer bezel */}
      <rect x="12" y="14" width="196" height="96" rx="3" fill="#0a0a0a" stroke="#2a2a2a" strokeWidth="1.5" />
      <defs>
        <linearGradient id="dr-tv-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#1a1a2e" />
          <stop offset="100%" stopColor="#2d1b4e" />
        </linearGradient>
      </defs>
      {/* Screen */}
      <rect x="18" y="20" width="184" height="84" rx="2" fill="url(#dr-tv-grad)" />
      {/* Broadcast safe-area corners */}
      <path d="M28 28 L28 36 M28 28 L36 28" stroke="rgba(255,255,255,0.3)" strokeWidth="1" fill="none" />
      <path d="M192 28 L192 36 M192 28 L184 28" stroke="rgba(255,255,255,0.3)" strokeWidth="1" fill="none" />
      <path d="M28 96 L28 88 M28 96 L36 96" stroke="rgba(255,255,255,0.3)" strokeWidth="1" fill="none" />
      <path d="M192 96 L192 88 M192 96 L184 96" stroke="rgba(255,255,255,0.3)" strokeWidth="1" fill="none" />
      {/* Lyric ghost */}
      <rect x="60" y="56" width="100" height="6" rx="1" fill="rgba(255,255,255,0.85)" />
      <rect x="76" y="68" width="68" height="5" rx="1" fill="rgba(255,255,255,0.6)" />
      {/* Stand */}
      <rect x="98" y="110" width="24" height="3" fill="#2a2a2a" />
      <rect x="86" y="113" width="48" height="2" rx="1" fill="#2a2a2a" />
    </svg>
  );
}

function ThumbnailMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      <defs>
        <linearGradient id="dr-th-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#14C8A8" />
          <stop offset="100%" stopColor="#6D4AFF" />
        </linearGradient>
      </defs>
      {/* Square cover centered */}
      <rect x="80" y="14" width="60" height="60" rx="4" fill="url(#dr-th-grad)" />
      {/* Title placeholder */}
      <rect x="80" y="82" width="60" height="5" rx="1" fill="#3a3a3a" />
      <rect x="80" y="91" width="42" height="4" rx="1" fill="#2a2a2a" />
      {/* Faded sibling thumbs (gallery) */}
      <rect x="18" y="34" width="40" height="40" rx="3" fill="#1a1a1a" opacity="0.6" />
      <rect x="162" y="34" width="40" height="40" rx="3" fill="#1a1a1a" opacity="0.6" />
    </svg>
  );
}

// ─── Format card ────────────────────────────────────────────────────
function FormatCard({ format, locked, onSelect, t }) {
  const labelKey = `dash.formats.${format.id}.title`;
  const subKey = `dash.formats.${format.id}.sub`;
  const ctaKey = locked ? "dash.formats.upgrade_cta" : `dash.formats.${format.id}.cta`;

  return (
    <button
      type="button"
      onClick={() => onSelect(format)}
      className={`dr-fmt-card ${locked ? "dr-fmt-card-locked" : ""}`}
      aria-label={t(labelKey) || format.id}
    >
      <div className="dr-fmt-mockup-wrap">
        {format.Mockup()}
        {locked && (
          <div className="dr-fmt-lock">
            <svg className="dr-fmt-lock-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="5" y="11" width="14" height="10" rx="2" />
              <path d="M8 11V7a4 4 0 0 1 8 0v4" />
            </svg>
            <span>{t("dash.formats.locked_chip") || "Pro / Enterprise"}</span>
          </div>
        )}
      </div>
      <div className="dr-fmt-body">
        <div className="dr-fmt-title">
          {t(labelKey) || format.id}
          {format.badge && <span className="dr-fmt-badge">{format.badge}</span>}
        </div>
        <div className="dr-fmt-sub">{t(subKey) || ""}</div>
        <div className="dr-fmt-cta">
          {t(ctaKey) || (locked ? "Subir de plan" : "Crear")}
          <svg className="dr-fmt-cta-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </div>
      </div>
    </button>
  );
}

// ─── Main gallery ───────────────────────────────────────────────────
export default function FormatGallery({ user, onSelectFormat, onUpgrade }) {
  const { t } = useI18n();
  // Pro/Enterprise plans see ProRes unlocked. Anything else (free, trial,
  // starter, etc.) sees the lock icon and an upgrade CTA on that card.
  const plan = (user && user.plan) || "free";
  const proResAvailable = plan === "pro" || plan === "enterprise" || plan === "umg";

  const formats = [
    { id: "youtube",   profile: "youtube", Mockup: YouTubeMockup },
    { id: "short",     profile: "youtube", subType: "short", Mockup: PhoneShortMockup },
    { id: "prores",    profile: "both", Mockup: BroadcastMockup, badge: "PRORES" },
    { id: "thumbnail", profile: "youtube", Mockup: ThumbnailMockup },
  ];

  const handleSelect = (fmt) => {
    if (fmt.id === "prores" && !proResAvailable) {
      onUpgrade?.("prores");
      return;
    }
    onSelectFormat?.(fmt);
  };

  return (
    <section className="dr-fmt-gallery mb-6" aria-labelledby="dr-fmt-title">
      <header className="flex items-baseline justify-between mb-3">
        <h3 id="dr-fmt-title" className="text-section text-gray-500 uppercase tracking-[0.18em]">
          {t("dash.formats.title") || "Para qué usás GenLy hoy"}
        </h3>
        <p className="text-[11px] text-gray-500 hidden md:block">
          {t("dash.formats.sub") || "Elegí el formato del entregable"}
        </p>
      </header>
      <div className="dr-fmt-grid">
        {formats.map((f) => (
          <FormatCard
            key={f.id}
            format={f}
            locked={f.id === "prores" && !proResAvailable}
            onSelect={handleSelect}
            t={t}
          />
        ))}
      </div>
    </section>
  );
}
