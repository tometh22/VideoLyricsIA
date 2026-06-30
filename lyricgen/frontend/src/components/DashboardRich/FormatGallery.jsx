import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { CHANGELOG } from "../../changelog";

// ─── Device mockup SVGs ─────────────────────────────────────────────
// Inline SVGs so we keep the "no binary assets" pledge of the help center.
// Iteración 2 (2026-05-29): pasamos de gradients vacíos a escenas que
// parecen frames reales — lyrics en typography, glow atmospheric, UI chrome
// de cada plataforma. Cero KB extra (sigue siendo SVG inline).
// Cada mockup ocupa un frame 220×124 (16:9-ish) dentro de su card.

function YouTubeMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      <defs>
        {/* Cinematic background — capa profunda con un blob violeta y un
            blob teal, simulando el lighting de un lyric video. */}
        <radialGradient id="dr-yt-bg-a" cx="30%" cy="65%" r="55%">
          <stop offset="0%" stopColor="#6D4AFF" stopOpacity="0.85" />
          <stop offset="100%" stopColor="#6D4AFF" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="dr-yt-bg-b" cx="80%" cy="25%" r="50%">
          <stop offset="0%" stopColor="#14C8A8" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#14C8A8" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="dr-yt-base" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#1a1a2e" />
          <stop offset="100%" stopColor="#0a0a14" />
        </linearGradient>
        <clipPath id="dr-yt-clip"><rect x="6" y="20" width="208" height="86" rx="4" /></clipPath>
      </defs>
      {/* YT chrome (browser-style top bar) */}
      <rect x="0" y="0" width="220" height="124" rx="6" fill="#0f0f0f" />
      <rect x="0" y="0" width="220" height="14" fill="#1c1c1f" />
      <circle cx="8" cy="7" r="2.2" fill="#FF0000" />
      <path d="M5.5 6.5 L10.5 7 L5.5 8.2 Z" fill="white" />
      <rect x="14" y="5" width="36" height="4" rx="1" fill="#3a3a3a" />
      <rect x="170" y="3.5" width="40" height="7" rx="3.5" fill="#252525" />
      <circle cx="174" cy="7" r="1.4" fill="#666" />
      <rect x="178" y="5.5" width="20" height="3" rx="0.5" fill="#444" />
      {/* Video area: layered atmosphere */}
      <g clipPath="url(#dr-yt-clip)">
        <rect x="6" y="20" width="208" height="86" fill="url(#dr-yt-base)" />
        <rect x="6" y="20" width="208" height="86" fill="url(#dr-yt-bg-a)" />
        <rect x="6" y="20" width="208" height="86" fill="url(#dr-yt-bg-b)" />
        {/* Particles — small floating dots for depth */}
        <circle cx="32" cy="42" r="0.8" fill="white" opacity="0.5" />
        <circle cx="58" cy="32" r="0.6" fill="white" opacity="0.35" />
        <circle cx="186" cy="48" r="0.7" fill="white" opacity="0.55" />
        <circle cx="178" cy="86" r="0.5" fill="white" opacity="0.3" />
        <circle cx="44" cy="92" r="0.65" fill="white" opacity="0.4" />
        <circle cx="196" cy="74" r="0.5" fill="white" opacity="0.35" />
        {/* Lyrics — system font, fits the brand voice */}
        <text x="110" y="62" textAnchor="middle"
              fontFamily="Inter, system-ui, sans-serif"
              fontSize="9" fontWeight="700" fill="white"
              style={{ filter: "drop-shadow(0 1px 2px rgba(0,0,0,0.6))" }}>
          I see the lights tonight
        </text>
        <text x="110" y="76" textAnchor="middle"
              fontFamily="Inter, system-ui, sans-serif"
              fontSize="7" fontWeight="500" fill="rgba(255,255,255,0.7)">
          and I&apos;m not turning back
        </text>
      </g>
      {/* Time + duration overlay */}
      <text x="200" y="100" textAnchor="end"
            fontFamily="Inter, system-ui, sans-serif"
            fontSize="4" fontWeight="600" fill="white" opacity="0.9">
        1:14 / 3:24
      </text>
      {/* Progress bar */}
      <rect x="6" y="108" width="208" height="2.5" rx="1" fill="rgba(255,255,255,0.18)" />
      <rect x="6" y="108" width="78" height="2.5" rx="1" fill="#FF0000" />
      <circle cx="84" cy="109.25" r="2" fill="#FF0000" />
      {/* Bottom controls */}
      <path d="M10 116 L10 122 L14 119 Z" fill="white" opacity="0.9" />
      <rect x="18" y="116.5" width="2" height="5" fill="white" opacity="0.7" />
      <rect x="21" y="116.5" width="2" height="5" fill="white" opacity="0.7" />
      <path d="M28 119 L34 116 L34 122 Z" fill="white" opacity="0.9" />
      {/* Volume + title shred */}
      <path d="M40 118 L43 116 L43 121 L40 119 Z" fill="white" opacity="0.6" />
      <rect x="55" y="117" width="56" height="2.5" rx="0.5" fill="rgba(255,255,255,0.25)" />
      <rect x="55" y="117" width="22" height="2.5" rx="0.5" fill="white" opacity="0.65" />
      {/* Settings + fullscreen */}
      <circle cx="195" cy="119" r="2" fill="none" stroke="white" strokeOpacity="0.7" strokeWidth="1" />
      <path d="M201 116 L206 116 L206 121 M214 116 L214 121 L209 121"
            stroke="white" strokeOpacity="0.7" strokeWidth="1" fill="none" />
    </svg>
  );
}

function PhoneShortMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      <defs>
        <radialGradient id="dr-ph-bg-a" cx="50%" cy="70%" r="60%">
          <stop offset="0%" stopColor="#FF006E" stopOpacity="0.95" />
          <stop offset="100%" stopColor="#FF006E" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="dr-ph-bg-b" cx="50%" cy="20%" r="55%">
          <stop offset="0%" stopColor="#8338EC" stopOpacity="0.9" />
          <stop offset="100%" stopColor="#8338EC" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="dr-ph-base" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2a0a3e" />
          <stop offset="100%" stopColor="#0a0014" />
        </linearGradient>
        <clipPath id="dr-ph-clip"><rect x="89" y="12" width="42" height="100" rx="5" /></clipPath>
      </defs>
      {/* Phone outline + bezel */}
      <rect x="84" y="4" width="52" height="116" rx="10" fill="#0a0a0f" />
      <rect x="86" y="6" width="48" height="112" rx="8.5" fill="#1a1a1a" stroke="#3a3a3a" strokeWidth="0.8" />
      {/* Dynamic island notch */}
      <rect x="100" y="8" width="20" height="3.5" rx="1.75" fill="#0a0a0a" />
      {/* Screen — layered atmosphere */}
      <g clipPath="url(#dr-ph-clip)">
        <rect x="89" y="12" width="42" height="100" fill="url(#dr-ph-base)" />
        <rect x="89" y="12" width="42" height="100" fill="url(#dr-ph-bg-a)" />
        <rect x="89" y="12" width="42" height="100" fill="url(#dr-ph-bg-b)" />
        {/* Top: profile + handle */}
        <circle cx="95" cy="20" r="2.5" fill="rgba(255,255,255,0.95)" />
        <circle cx="95" cy="20" r="2" fill="#FF006E" />
        <text x="99.5" y="21.5" fontFamily="Inter, sans-serif" fontSize="3" fontWeight="700" fill="white">
          @artist
        </text>
        <rect x="119" y="17.5" width="9" height="4.5" rx="1" fill="white" opacity="0.95" />
        <text x="123.5" y="21" textAnchor="middle" fontFamily="Inter, sans-serif" fontSize="2.5" fontWeight="700" fill="#FF006E">
          Seguir
        </text>
        {/* Centered lyric */}
        <text x="110" y="58" textAnchor="middle"
              fontFamily="Inter, sans-serif" fontSize="5.5" fontWeight="800" fill="white"
              style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,0.6))" }}>
          Vuelvo
        </text>
        <text x="110" y="66" textAnchor="middle"
              fontFamily="Inter, sans-serif" fontSize="5.5" fontWeight="800" fill="white"
              style={{ filter: "drop-shadow(0 1px 1.5px rgba(0,0,0,0.6))" }}>
          a empezar
        </text>
        {/* Caption + music note */}
        <text x="95" y="96" fontFamily="Inter, sans-serif" fontSize="2.5" fill="rgba(255,255,255,0.85)">
          #lyricvideo #musica
        </text>
        <circle cx="97" cy="104" r="1.5" fill="white" opacity="0.85" />
        <path d="M97 102 L97 99 L101 98 L101 102" stroke="white" strokeOpacity="0.85" strokeWidth="0.6" fill="none" />
        <text x="103.5" y="105" fontFamily="Inter, sans-serif" fontSize="2.5" fill="rgba(255,255,255,0.85)">
          Sonido original
        </text>
      </g>
      {/* Right rail of social icons (IG/TikTok style) */}
      {/* Heart */}
      <path d="M138 38 C138 35, 142 35, 142 38 C142 35, 146 35, 146 38 C146 41, 142 43, 142 43 C142 43, 138 41, 138 38 Z"
            stroke="white" strokeWidth="1" fill="none" opacity="0.92" />
      <text x="142" y="48" textAnchor="middle" fontFamily="Inter, sans-serif" fontSize="2.5" fontWeight="600" fill="white" opacity="0.85">
        24k
      </text>
      {/* Comment */}
      <path d="M138 53 L138 58 L140 58 L140 60 L142 58 L146 58 L146 53 Z"
            stroke="white" strokeWidth="1" fill="none" opacity="0.92" />
      <text x="142" y="65" textAnchor="middle" fontFamily="Inter, sans-serif" fontSize="2.5" fontWeight="600" fill="white" opacity="0.85">
        312
      </text>
      {/* Share */}
      <path d="M138 70 L146 73 L138 76 L140 73 Z"
            stroke="white" strokeWidth="1" fill="none" opacity="0.92" />
      <text x="142" y="82" textAnchor="middle" fontFamily="Inter, sans-serif" fontSize="2.5" fontWeight="600" fill="white" opacity="0.85">
        88
      </text>
      {/* Save (bookmark) */}
      <path d="M139 87 L139 94 L142 92 L145 94 L145 87 Z"
            stroke="white" strokeWidth="1" fill="none" opacity="0.92" />
    </svg>
  );
}

function BroadcastMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      <defs>
        <radialGradient id="dr-tv-bg-a" cx="50%" cy="50%" r="70%">
          <stop offset="0%" stopColor="#6D4AFF" stopOpacity="0.45" />
          <stop offset="60%" stopColor="#6D4AFF" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="dr-tv-base" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#0d0d1a" />
          <stop offset="60%" stopColor="#1a1a2e" />
          <stop offset="100%" stopColor="#0a0a14" />
        </linearGradient>
        <linearGradient id="dr-tv-lowerthird" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#6D4AFF" stopOpacity="0" />
          <stop offset="100%" stopColor="#6D4AFF" stopOpacity="0.85" />
        </linearGradient>
        <clipPath id="dr-tv-clip"><rect x="18" y="20" width="184" height="84" rx="2" /></clipPath>
      </defs>
      {/* TV outer bezel — slightly thicker for "premium" feel */}
      <rect x="10" y="12" width="200" height="100" rx="4" fill="#050505" />
      <rect x="12" y="14" width="196" height="96" rx="3" fill="#0a0a0a" stroke="#3a3a3a" strokeWidth="0.8" />
      {/* Screen */}
      <g clipPath="url(#dr-tv-clip)">
        <rect x="18" y="20" width="184" height="84" fill="url(#dr-tv-base)" />
        <rect x="18" y="20" width="184" height="84" fill="url(#dr-tv-bg-a)" />
        {/* Subtle stage-light streaks */}
        <line x1="40" y1="20" x2="80" y2="104" stroke="white" strokeOpacity="0.05" strokeWidth="20" />
        <line x1="180" y1="20" x2="140" y2="104" stroke="white" strokeOpacity="0.04" strokeWidth="18" />
        {/* Lyric — bigger, cinematic */}
        <text x="110" y="58" textAnchor="middle"
              fontFamily="Inter, sans-serif" fontSize="11" fontWeight="800" fill="white"
              letterSpacing="-0.5"
              style={{ filter: "drop-shadow(0 2px 4px rgba(0,0,0,0.8))" }}>
          SIN MIRAR ATRÁS
        </text>
        <text x="110" y="72" textAnchor="middle"
              fontFamily="Inter, sans-serif" fontSize="6" fontWeight="500"
              fill="rgba(255,255,255,0.7)" letterSpacing="2">
          GENLY · LIVE SESSION
        </text>
        {/* Lower-third bar */}
        <rect x="18" y="92" width="184" height="12" fill="url(#dr-tv-lowerthird)" />
        <rect x="22" y="95" width="2" height="6" fill="#14C8A8" />
        <text x="27" y="100" fontFamily="Inter, sans-serif" fontSize="4" fontWeight="700" fill="white">
          BABASÓNICOS
        </text>
        <text x="27" y="103.5" fontFamily="Inter, sans-serif" fontSize="2.5" fill="rgba(255,255,255,0.7)">
          El loco · Album 2024
        </text>
        {/* LIVE indicator top-right */}
        <circle cx="190" cy="26" r="1.6" fill="#FF1A1A">
          <animate attributeName="opacity" values="1;0.4;1" dur="1.5s" repeatCount="indefinite" />
        </circle>
        <text x="194" y="27.5" fontFamily="Inter, sans-serif" fontSize="3" fontWeight="800" fill="white" letterSpacing="0.5">
          LIVE
        </text>
      </g>
      {/* Broadcast safe-area corners (small, top-only since lower-third covers bottom) */}
      <path d="M24 26 L24 30 M24 26 L28 26" stroke="rgba(255,255,255,0.25)" strokeWidth="0.7" fill="none" />
      <path d="M196 26 L196 30 M196 26 L192 26" stroke="rgba(255,255,255,0.25)" strokeWidth="0.7" fill="none" />
      {/* Stand */}
      <rect x="100" y="112" width="20" height="2.5" fill="#2a2a2a" />
      <rect x="88" y="114" width="44" height="1.5" rx="0.5" fill="#2a2a2a" />
    </svg>
  );
}

function ThumbnailMockup() {
  return (
    <svg viewBox="0 0 220 124" className="dr-fmt-mockup" aria-hidden="true">
      <defs>
        <radialGradient id="dr-th-cover-a" cx="35%" cy="35%" r="70%">
          <stop offset="0%" stopColor="#14C8A8" stopOpacity="0.95" />
          <stop offset="100%" stopColor="#14C8A8" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="dr-th-cover-b" cx="75%" cy="70%" r="60%">
          <stop offset="0%" stopColor="#6D4AFF" stopOpacity="0.95" />
          <stop offset="100%" stopColor="#6D4AFF" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="dr-th-base" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#1a1a2e" />
          <stop offset="100%" stopColor="#0a0a14" />
        </linearGradient>
        <clipPath id="dr-th-clip"><rect x="76" y="10" width="68" height="68" rx="5" /></clipPath>
      </defs>
      {/* Faded sibling thumbs (gallery context) */}
      <rect x="14" y="22" width="44" height="44" rx="4" fill="#1a1a1a" opacity="0.5" />
      <rect x="14" y="74" width="44" height="14" rx="2" fill="#1a1a1a" opacity="0.4" />
      <rect x="14" y="92" width="32" height="3" rx="0.5" fill="#2a2a2a" opacity="0.6" />
      <rect x="162" y="22" width="44" height="44" rx="4" fill="#1a1a1a" opacity="0.5" />
      <rect x="162" y="74" width="44" height="14" rx="2" fill="#1a1a1a" opacity="0.4" />
      <rect x="162" y="92" width="32" height="3" rx="0.5" fill="#2a2a2a" opacity="0.6" />
      {/* Central cover (square, more prominent) */}
      <g clipPath="url(#dr-th-clip)">
        <rect x="76" y="10" width="68" height="68" fill="url(#dr-th-base)" />
        <rect x="76" y="10" width="68" height="68" fill="url(#dr-th-cover-a)" />
        <rect x="76" y="10" width="68" height="68" fill="url(#dr-th-cover-b)" />
        {/* Cover typography — artist + song */}
        <text x="80" y="26" fontFamily="Inter, sans-serif" fontSize="3.5" fontWeight="700"
              fill="white" letterSpacing="1.5" opacity="0.95">
          BABASÓNICOS
        </text>
        <text x="80" y="68" fontFamily="Inter, sans-serif" fontSize="8" fontWeight="900"
              fill="white" letterSpacing="-0.4"
              style={{ filter: "drop-shadow(0 1px 2px rgba(0,0,0,0.4))" }}>
          El loco
        </text>
        <text x="80" y="73.5" fontFamily="Inter, sans-serif" fontSize="3" fontWeight="500"
              fill="rgba(255,255,255,0.75)" letterSpacing="0.5">
          OFFICIAL LYRIC VIDEO
        </text>
      </g>
      {/* Title + sub below cover */}
      <text x="110" y="92" textAnchor="middle" fontFamily="Inter, sans-serif"
            fontSize="5" fontWeight="700" fill="white">
        Babasónicos — El loco
      </text>
      <text x="110" y="100" textAnchor="middle" fontFamily="Inter, sans-serif"
            fontSize="3.5" fill="rgba(255,255,255,0.6)">
        3:24 · 4K · Lyric Video
      </text>
      {/* Platform badges */}
      <rect x="84" y="106" width="14" height="6" rx="1" fill="#1DB954" opacity="0.85" />
      <text x="91" y="110.3" textAnchor="middle" fontFamily="Inter, sans-serif"
            fontSize="3" fontWeight="700" fill="white">Spotify</text>
      <rect x="100" y="106" width="14" height="6" rx="1" fill="#FA243C" opacity="0.85" />
      <text x="107" y="110.3" textAnchor="middle" fontFamily="Inter, sans-serif"
            fontSize="3" fontWeight="700" fill="white">Apple</text>
      <rect x="116" y="106" width="20" height="6" rx="1" fill="#FF0000" opacity="0.85" />
      <text x="126" y="110.3" textAnchor="middle" fontFamily="Inter, sans-serif"
            fontSize="3" fontWeight="700" fill="white">YouTube</text>
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
        {/* Image renders on top of the SVG fallback. Lazy-loaded so it
            doesn't block first paint; SVG underneath is instant chrome
            that survives if the image 404s or is still in flight.
            Pass `imageUrl` in the formats array to activate per-card. */}
        {format.Mockup()}
        {format.imageUrl && (
          <img
            src={format.imageUrl}
            alt=""
            loading="lazy"
            decoding="async"
            className="dr-fmt-mockup-img"
            onError={(e) => { e.currentTarget.style.display = "none"; }}
          />
        )}
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

// ─── Novedad card ───────────────────────────────────────────────────
// Surfacea la última novedad del changelog como una card más de la galería,
// con el mismo look (dr-fmt-card). Click → CTA de la entrada (ej. /new). Se
// actualiza sola: cuando agregás una entrada nueva en changelog.js, esta card
// muestra la más reciente.
function NovedadCard({ entry, t, onClick }) {
  const isVideo = (entry.media || "").endsWith(".mp4");
  const mediaStyle = {
    width: "100%", aspectRatio: "220 / 124", objectFit: "cover",
    display: "block", borderRadius: "0.375rem",
  };
  return (
    <button type="button" onClick={onClick} className="dr-fmt-card" aria-label={t(entry.titleKey)}>
      <div className="dr-fmt-mockup-wrap">
        {entry.media && (isVideo
          ? <video src={entry.media} autoPlay muted loop playsInline style={mediaStyle} />
          : <img src={entry.media} alt="" loading="lazy" style={mediaStyle} />
        )}
      </div>
      <div className="dr-fmt-body">
        <div className="dr-fmt-title">
          {t(entry.titleKey)}
          <span className="dr-fmt-badge">{t("announce.scenes_badge") || "NUEVO"}</span>
        </div>
        <div className="dr-fmt-sub">{t(entry.bodyKey)}</div>
        <div className="dr-fmt-cta">
          {t(entry.ctaKey) || "Probar"}
          <svg className="dr-fmt-cta-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </div>
      </div>
    </button>
  );
}

// ─── Main gallery ───────────────────────────────────────────────────
export default function FormatGallery({ user, onSelectFormat, onUpgrade }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const novedad = CHANGELOG.length ? CHANGELOG[0] : null;
  // Pro/Enterprise plans see ProRes unlocked. Anything else (free, trial,
  // starter, etc.) sees the lock icon and an upgrade CTA on that card.
  const plan = (user && user.plan) || "free";
  const proResAvailable = plan === "pro" || plan === "enterprise" || plan === "umg";

  // imageUrl es opcional. Cuando lo pongas (típicamente apuntando a un
  // asset de R2 con un frame curado de un lyric video real), la imagen
  // se renderiza encima del SVG mockup con lazy loading. Si el fetch
  // falla o todavía no asignaste URL, queda el SVG actual sin penalty.
  //
  // Recomendación de assets:
  //   • youtube:   frame de un lyric video tuyo en 16:9, 800x450px ~60 KB
  //   • short:     frame de un short vertical en 9:16, 360x640px ~50 KB
  //   • prores:    similar a youtube pero con paleta más cinemática
  //   • thumbnail: cover art finalizado, 600x600px ~40 KB
  // Total esperado tras optimización (mozjpeg / webp): 200-250 KB.
  const formats = [
    { id: "youtube",   profile: "youtube", Mockup: YouTubeMockup,
      imageUrl: null /* ej: "https://r2.genly.pro/mockups/fmt-yt-v1.jpg" */ },
    { id: "short",     profile: "youtube", subType: "short", Mockup: PhoneShortMockup,
      imageUrl: null /* ej: "https://r2.genly.pro/mockups/fmt-short-v1.jpg" */ },
    { id: "prores",    profile: "both", Mockup: BroadcastMockup, badge: "PRORES",
      imageUrl: null /* ej: "https://r2.genly.pro/mockups/fmt-prores-v1.jpg" */ },
    { id: "thumbnail", profile: "youtube", Mockup: ThumbnailMockup,
      imageUrl: null /* ej: "https://r2.genly.pro/mockups/fmt-thumb-v1.jpg" */ },
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
        {novedad && (
          <NovedadCard
            entry={novedad}
            t={t}
            onClick={() => navigate(novedad.ctaTo || "/new")}
          />
        )}
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
