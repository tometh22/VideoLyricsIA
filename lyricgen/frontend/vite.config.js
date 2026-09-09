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
      // REGEX, no prefijo: el backend expone `/status/{job_id}` (polling de
      // un job) y el frontend tiene la ruta pública `/status` (página de
      // estado del servicio). Con la key `"/status"` el dev server proxeaba
      // las DOS al backend y la página era inalcanzable en local — un 500
      // de ECONNREFUSED en vez de la SPA. `^/status/.+` deja el `/status`
      // pelado para el router. En prod no aplica: Vercel sirve la SPA y la
      // API vive en otro origen (VITE_API_URL).
      "^/status/.+": "http://localhost:8000",
      // Endpoints públicos de la página de status (status_page.py).
      "/service-status": "http://localhost:8000",
      "/download": "http://localhost:8000",
      "/preview": "http://localhost:8000",
      // Token scoped por (job, file_type) que piden los <video>/<img> antes de
      // cargar media desde /download|/preview. Sin proxearlo el dev server
      // devuelve 404 → "Miniatura no disponible" / player negro en local.
      "/media-token": "http://localhost:8000",
      "/jobs": "http://localhost:8000",
      "/youtube": "http://localhost:8000",
      "/settings": "http://localhost:8000",
      "/usage": "http://localhost:8000",
      "/plans": "http://localhost:8000",
      "/billing": "http://localhost:8000",
      // Mismo caso que `/status` (ver arriba): el backend expone
      // `/admin/<recurso>` y el frontend tiene la ruta `/admin` del panel.
      // Con la key `"/admin"` el dev server proxeaba el `/admin` pelado al
      // backend y el panel devolvía "Not Found" en local — no se podía
      // desarrollar el admin con `vite dev`. Si algún día se agrega una
      // ruta de frontend bajo `/admin/algo`, hay que excluirla acá.
      "^/admin/.+": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
