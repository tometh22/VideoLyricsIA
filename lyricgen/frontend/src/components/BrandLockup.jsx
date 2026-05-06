// Brand v1 lockup: SVG mark (geometry preserved verbatim) + Inter ExtraBold
// "GENLY" wordmark. The master SVG's geometric wordmark collapses below ~64px;
// rendering the wordmark as text keeps it sharp at every brand-kit size.

const SIZES = {
  sm: { height: "h-7",  fontSize: "20px", gap: "gap-1.5" }, // 28px mark, navbar-sm
  md: { height: "h-10", fontSize: "28px", gap: "gap-2"   }, // 40px mark, sidebar / nav
  lg: { height: "h-14", fontSize: "40px", gap: "gap-2.5" }, // 56px mark, auth screens
};

export default function BrandLockup({ size = "md", className = "", title = "GenLy" }) {
  const cfg = SIZES[size] ?? SIZES.md;
  return (
    <span
      className={`inline-flex items-center ${cfg.gap} ${cfg.height} ${className}`}
      role="img"
      aria-label={title}
    >
      <img
        src="/logo/genly-mark.svg"
        alt=""
        aria-hidden="true"
        className="h-full w-auto select-none"
        draggable={false}
      />
      <span
        className="text-ink-primary"
        style={{
          fontFamily: "'Inter', system-ui, sans-serif",
          fontWeight: 800,
          letterSpacing: "-0.02em",
          lineHeight: 1,
          fontSize: cfg.fontSize,
        }}
      >
        GENLY
      </span>
    </span>
  );
}
