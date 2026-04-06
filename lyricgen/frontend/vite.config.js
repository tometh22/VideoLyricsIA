import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
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
    },
  },
});
