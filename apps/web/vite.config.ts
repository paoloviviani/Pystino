import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],

  // Served from /chat/, in every deployment shape. The built asset URLs carry
  // the prefix, so getting this wrong produces a page that loads and then 404s
  // every script — with nothing in any server log, because the requests never
  // reach a route that knows it should have matched.
  base: "/chat/",

  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    // chat-api serves a CSP without 'unsafe-inline', so an inlined polyfill
    // script is silently blocked and the app never starts — no error, a blank
    // page. Same reasoning for cssCodeSplit.
    modulePreload: { polyfill: false },
    cssCodeSplit: true,
    assetsInlineLimit: 4096,
    sourcemap: true,
  },

  server: {
    port: 5174,
    // Proxied rather than CORS-enabled, so development is same-origin exactly
    // as production is and the session cookie behaves identically.
    proxy: {
      "/chat/api": { target: "http://localhost:8100", changeOrigin: false },
      "/chat/auth": { target: "http://localhost:8100", changeOrigin: false },
    },
  },
});
