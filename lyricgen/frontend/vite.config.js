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
  server: {
    allowedHosts: true,
    proxy: {
      "/auth": "http://localhost:8000",
      "/upload": "http://localhost:8000",
      "/transcribe": "http://localhost:8000",
      "/generate": "http://localhost:8000",
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
