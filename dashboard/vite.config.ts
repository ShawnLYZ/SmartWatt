import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

export default defineConfig({
  // Cast needed: vitest hard-depends on vite@^5 for its own module runner
  // (vite-node), so `defineConfig` here types `plugins` against that
  // nested vite@5 copy, distinct from the top-level vite@6 that
  // @vitejs/plugin-react and @tailwindcss/vite are built against. Same
  // shape, different type identity — harmless at runtime.
  plugins: [react(), tailwindcss()] as any[],
  build: {
    // Served by the API. One origin, no CORS, no dev server at demo time.
    outDir: resolve(__dirname, "../server/static"),
    emptyOutDir: true,
    // Inline nothing above 0 bytes so every asset is a real file we can
    // scan for external references in the build-output test.
    assetsInlineLimit: 0,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    globals: true,
  },
});
