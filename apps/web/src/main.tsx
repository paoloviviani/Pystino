import "@llmp/ui/tokens.css";
/* assistant-ui's own stylesheet, then the bridge that points its variables at
   our tokens. Order matters only for readability — both are `:root` and the
   bridge defines names their CSS reads rather than overriding its rules.
   Their preflight is scoped to `.aui-thread-root`, so nothing here escapes
   into the rest of the app. */
import "@assistant-ui/styles/index.css";
/* KaTeX's own stylesheet. Self-hosted like everything else — Vite bundles the
   fonts it references as assets, so nothing is fetched from a CDN and the CSP
   stays as it is. */
import "katex/dist/katex.min.css";
import "./aui-theme.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";

const root = document.getElementById("root");
if (!root) throw new Error("no #root element");

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
