import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { visualizer } from "rollup-plugin-visualizer";

export default defineConfig({
  plugins: [
    react(),
    // 2026-05-31 perf: write dist/stats.html on every build with the
    // gzip+brotli size breakdown per chunk. Opt-in via ANALYZE=1 so
    // the default `vite build` stays light for Vercel CI; locally run
    //   ANALYZE=1 npm run build && open dist/stats.html
    // to inspect what's hogging the main bundle. The visualizer plugin
    // produces zero runtime output — it only emits the analysis file.
    process.env.ANALYZE === "1" && visualizer({
      filename: "dist/stats.html",
      gzipSize: true,
      brotliSize: true,
      template: "treemap",
      open: false,
    }),
  ].filter(Boolean),
  build: {
    rollupOptions: {
      output: {
        // Stable shared chunks keep the app shell small and let browsers cache
        // large, rarely-changing catalogs/SDKs independently of product code.
        manualChunks(id) {
          if (id.endsWith("/src/i18n.jsx")) return "i18n";
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@sentry")) return "sentry-vendor";
          if (id.includes("react-joyride") || id.includes("@floating-ui")) return "tour-vendor";
          if (/node_modules\/(react|react-dom|react-router|react-router-dom|scheduler)\//.test(id)) return "react-vendor";
          return undefined;
        },
      },
    },
  },
  server: {
    allowedHosts: true,
    proxy: {
      "/auth": "http://localhost:8000",
      "/upload": "http://localhost:8000",
      "/transcribe": "http://localhost:8000",
      "/generate": "http://localhost:8000",
      // SSE de progreso del render (pollJob). Sin esto el dev server sirve
      // index.html (text/html) en vez del stream → el EventSource falla y la
      // barra "Armando el video" queda trabada. En prod va directo a la API
      // (VITE_API_URL), así que este proxy es solo para dev local.
      "/events": { target: "http://localhost:8000", changeOrigin: true },
      "/status": "http://localhost:8000",
      "/download": "http://localhost:8000",
      "/preview": "http://localhost:8000",
      "/jobs": "http://localhost:8000",
      "/youtube": "http://localhost:8000",
      "/settings": "http://localhost:8000",
      "/usage": "http://localhost:8000",
      "/plans": "http://localhost:8000",
      "/billing": "http://localhost:8000",
      "/admin": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
