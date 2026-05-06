// Brand lockup v2 — single transparent PNG. The custom wordmark
// geometry (outlined letterforms, teal accent bar inside the "E") can't
// be reproduced with a system font, so we render the brand artwork as-is.
const SIZES = {
  sm: "h-6",   // 24 px — compact navbar
  md: "h-9",   // 36 px — sidebar, top nav
  lg: "h-12",  // 48 px — auth screens
};

export default function BrandLockup({ size = "md", className = "", title = "GenLy" }) {
  const heightClass = SIZES[size] ?? SIZES.md;
  return (
    <img
      src="/logo/genly-lockup-v2.png"
      alt={title}
      draggable={false}
      className={`${heightClass} w-auto select-none ${className}`}
    />
  );
}
