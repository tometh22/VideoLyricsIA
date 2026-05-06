/** @type {import('tailwindcss').Config} */
// GenLy brand system v1.0 — implementation maps directly to
// GENLY_Web_Rebrand_Guidelines_v1.docx and brand-system.png.
// Colors / typography / radii / motion all match the spec verbatim.
// "Do not redesign. Implement exactly." — see commit message for any
// justified deviations.
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // §3 Color tokens — exact hex from the brand guidelines.
        // brand.* points at violet-primary so the existing `bg-brand`,
        // `text-brand`, `border-brand`, `ring-brand` utilities keep
        // working everywhere they are referenced (~80 sites).
        brand: {
          DEFAULT: "#6D4AFF",
          light:   "#8E70FF",
          dark:    "#5739DD",
          50:      "#F0EBFF",
        },
        // §3 Teal accent — max 10% usage rule documented separately.
        // Use only for active states, selected tabs, render progress,
        // and the logo micro accent.
        accent: {
          DEFAULT: "#14C8A8",
          light:   "#33D9BD",
          dark:    "#0FA88B",
        },
        teal: {
          DEFAULT: "#14C8A8",
        },
        // §3 Background trio — primary / surface / elevated.
        // surface.DEFAULT remains bg-primary for back-compat with the
        // many `bg-surface` references; surface-1/2/3 map to the spec's
        // surface / elevated levels (intermediate steps for cards on
        // glass effects).
        surface: {
          DEFAULT: "#09090F", // bg-primary per spec
          1:       "#12121A", // bg-surface
          2:       "#181821", // bg-elevated
          3:       "#222230", // one step above for hover/popovers
        },
        // §3 Text and border tokens.
        ink: {
          primary:   "#F5F7FA",
          secondary: "#A0A3B1",
        },
      },
      // §4 Typography — Inter (already preloaded in index.css).
      fontFamily: {
        sans:    ['"Inter"', "system-ui", "sans-serif"],
        display: ['"Inter"', "system-ui", "sans-serif"],
      },
      // §4 Type scale — H1/H2/Body/Small UI per spec.
      fontSize: {
        // H1: clamp(52px, 7vw, 88px); weight 700; line-height .95;
        // letter-spacing -0.04em.
        h1:   ["clamp(52px, 7vw, 88px)",   { lineHeight: "0.95", letterSpacing: "-0.04em", fontWeight: "700" }],
        // H2: 40-56px (we pick the clamped midpoint).
        h2:   ["clamp(40px, 4.5vw, 56px)", { lineHeight: "1.05", letterSpacing: "-0.03em", fontWeight: "700" }],
        // Body: 18-20px (default 19 as midpoint).
        body: ["19px", { lineHeight: "1.6" }],
        // Small UI: 14-15px (default 14).
        ui:   ["14px", { lineHeight: "1.45" }],
      },
      // §8 Cards — border-radius 24px → rounded-card. We add a token
      // rather than redefining `rounded-2xl` (Tailwind default = 16px)
      // so tighter inner pills keep their 16px where appropriate.
      // §7 Primary button radius: 16px → rounded-button.
      borderRadius: {
        card:   "24px",
        button: "16px",
      },
      // §9 Motion — 200-300ms duration with a custom ease.
      transitionDuration: {
        brand: "240ms",
      },
      transitionTimingFunction: {
        brand: "cubic-bezier(.2,.8,.2,1)",
      },
      boxShadow: {
        // Glow tints retuned to the new violet-primary.
        glow:       "0 0 40px rgba(109,74,255,.18), 0 0 80px rgba(109,74,255,.06)",
        "glow-lg":  "0 0 80px rgba(109,74,255,.22), 0 0 120px rgba(109,74,255,.10)",
        depth:      "0 2px 4px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.2)",
        "depth-lg": "0 4px 8px rgba(0,0,0,.4), 0 16px 48px rgba(0,0,0,.3)",
      },
      animation: {
        "pulse-slow": "pulse 3s ease-in-out infinite",
        "gradient-x": "gradient-x 6s ease infinite",
        "slide-up":   "slide-up 0.5s ease-out",
        "fade-in":    "fade-in 0.6s ease-out",
      },
      keyframes: {
        "gradient-x": {
          "0%, 100%": { backgroundPosition: "0% 50%" },
          "50%":      { backgroundPosition: "100% 50%" },
        },
        "slide-up": {
          "0%":   { transform: "translateY(20px)", opacity: 0 },
          "100%": { transform: "translateY(0)",     opacity: 1 },
        },
        "fade-in": {
          "0%":   { opacity: 0 },
          "100%": { opacity: 1 },
        },
      },
    },
  },
  plugins: [],
};
