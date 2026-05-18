import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Minimal Vitest config — pairs the Vite/React plugin we already use
// in the app build with jsdom for component tests. Tests live next to
// the components they cover (sufijo .test.jsx) so changes ship together.
//
// `globals: true` lets tests use `describe/it/expect/vi` without imports,
// matching the Jest convention the team is used to.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.js"],
    // Co-locate tests with their components. Anything outside src/ is
    // a CI/build script and shouldn't be picked up.
    include: ["src/**/*.{test,spec}.{js,jsx}"],
  },
});
