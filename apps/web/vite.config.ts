import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),

    // The PWA. Installable, with an offline shell — and it is the mobile story
    // for this platform until a native app exists, not a polish item.
    //
    // Three things here are decisions rather than boilerplate:
    //
    // `scope` and `start_url` are /chat/, because that is where this app lives
    // on every deployment. A service worker's scope cannot exceed the directory
    // it is served from, and one registered at the origin root would try to
    // claim the gateway's console as well.
    //
    // `navigateFallbackDenylist` keeps the API and the login flow off the
    // shell: a cached index.html returned for /chat/auth/callback would break
    // sign-in in a way that survives a reload, which is the worst kind of
    // offline bug.
    //
    // And nothing to do with a conversation is precached. Transcripts are the
    // server's, and an offline copy of somebody's chat history sitting in a
    // browser cache is a data question nobody has asked for yet.
    VitePWA({
      registerType: "autoUpdate",
      scope: "/chat/",
      base: "/chat/",
      includeAssets: ["icon.svg"],
      manifest: {
        name: "LLM Platform Chat",
        short_name: "Chat",
        description: "Chat with the models this platform provides.",
        start_url: "/chat/",
        scope: "/chat/",
        display: "standalone",
        background_color: "#f4f4f2",
        theme_color: "#12142c",
        icons: [
          {
            src: "icon.svg",
            sizes: "any",
            type: "image/svg+xml",
            purpose: "any maskable",
          },
        ],
      },
      workbox: {
        globPatterns: ["**/*.{js,css,html,woff2,svg}"],
        navigateFallback: "/chat/index.html",
        navigateFallbackDenylist: [/^\/chat\/api\//, /^\/chat\/auth\//],
      },
      devOptions: { enabled: false },
    }),
  ],

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
