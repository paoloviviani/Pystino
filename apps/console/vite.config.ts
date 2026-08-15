import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],

  // Served from /console/ by the gateway, not from the domain root, so the
  // built asset URLs must carry that prefix. Getting this wrong produces a page
  // that loads and then 404s every script — with no error in the server log,
  // because the requests never reach a route that knows it should have matched.
  base: "/console/",

  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Every asset gets a content hash, which is what makes the immutable
    // cache-control header the gateway sets safe: a changed file is a changed
    // URL, so a cached copy can never be the stale one.
    assetsDir: "assets",
    // Vite normally inlines a small module-preload polyfill as a <script> tag.
    // The gateway serves a CSP without 'unsafe-inline', so an inline script is
    // silently blocked and the app never starts. Every browser the console
    // supports handles modulepreload natively.
    modulePreload: { polyfill: false },
    // Same reason, for CSS: an inlined <style> would violate style-src 'self'.
    cssCodeSplit: true,
    // 4kb of base64 in a stylesheet is cheaper than a request; larger assets
    // stay as files so they can be cached separately.
    assetsInlineLimit: 4096,
    sourcemap: true,
  },

  server: {
    port: 5173,
    // `pnpm dev` runs the console against a gateway on :8000. Proxying rather
    // than enabling CORS keeps development on the same origin as production, so
    // the session cookie behaves identically in both.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: false },
      "/auth": { target: "http://localhost:8000", changeOrigin: false },
      "/v1": { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});
