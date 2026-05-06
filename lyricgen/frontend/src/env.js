// Single source of truth for "what environment is this build running in?".
// Order of precedence:
//   1. VITE_APP_ENV at build time (set explicitly in Vercel per branch).
//   2. Hostname heuristic (anything with "staging" or "preview" or
//      "vercel.app" → not production).
//   3. Default to production.
//
// Used by the staging pill in the sidebar, the page <title> stamp, and
// any "show this only outside prod" affordances. Production prod loads
// must NOT trip the heuristic — set VITE_APP_ENV=production in the prod
// Vercel project to be safe.

const explicit = (import.meta.env.VITE_APP_ENV || "").toLowerCase().trim();

function detectFromHostname() {
  if (typeof window === "undefined") return "production";
  const h = window.location.hostname.toLowerCase();
  if (h.includes("staging")) return "staging";
  if (h.includes("preview")) return "staging";
  if (h.endsWith(".vercel.app") && !h.startsWith("app.")) return "staging";
  if (h === "localhost" || h.startsWith("127.") || h.startsWith("192.168.")) return "development";
  return "production";
}

export const APP_ENV = explicit || detectFromHostname();
export const IS_PRODUCTION = APP_ENV === "production";
export const IS_STAGING = APP_ENV === "staging";
export const IS_DEV = APP_ENV === "development";
