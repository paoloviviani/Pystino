import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { ToastProvider } from "@llmp/ui";
import { App } from "./App";
import { initTheme } from "./lib/theme";

// The whole stylesheet — Tailwind's layers and the tokens file, in the right
// order — is imported as one unit from index.css. Splitting the tokens import
// out into JS (as this file once did) bypasses the Tailwind compiler, which
// only sees CSS reached through the stylesheet import graph; see index.css.
import "./index.css";

// The `.dark` class goes on before the first render, so a reader who chose dark
// meets dark at the first frame React paints. CSP rules out doing it earlier;
// see lib/theme.ts for the trade.
initTheme();

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // A console is read in one sitting with the tab left open. Refetching on
      // every window focus turns "I alt-tabbed back" into a burst of requests
      // for figures that change on the timescale of an LLM call, not a click.
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

createRoot(container).render(
  <StrictMode>
    {/* Toasts are app-global plumbing: one provider, one viewport, and any
        screen can say "saved" without owning the corner of the screen it
        appears in. */}
    <ToastProvider>
      <QueryClientProvider client={client}>
        {/* Served under /console/, so the router must be told: without this every
            route match is off by the prefix and only the index page works. */}
        <BrowserRouter basename="/console">
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ToastProvider>
  </StrictMode>,
);
