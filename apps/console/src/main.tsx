import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App";

// Tokens first: components reference the custom properties this defines, so it
// must be in the document before anything renders.
import "@llmp/ui/tokens.css";

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
    <QueryClientProvider client={client}>
      {/* Served under /console/, so the router must be told: without this every
          route match is off by the prefix and only the index page works. */}
      <BrowserRouter basename="/console">
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
